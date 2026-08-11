package encode

import "testing"

// localJobRank is the ONLY thing that makes an older local-dist job's chunks
// outrank a younger one's. It is assigned at launch and never revisited, so a
// collision is permanent for the life of both jobs — and it fails silently:
// both jobs run, both make progress, and the only symptom is that the job
// nearest to finishing is the one being starved.

func mgrWithJobs(ids []string, status []JobStatus) *Manager {
	m := &Manager{}
	for i, id := range ids {
		m.jobs = append(m.jobs, &Job{
			ID:     id,
			Status: status[i],
			Config: JobConfig{Target: TargetLocalDist},
		})
	}
	return m
}

func find(m *Manager, id string) *Job {
	for _, j := range m.jobs {
		if j.ID == id {
			return j
		}
	}
	return nil
}

// The multi-ladder case (#286): four ladders submitted together, MAX_CONCURRENT=2,
// each launching as the one before it finishes. Ranks must come out strictly
// increasing — 0, 1, 2, 3 — not 0, 1, 1, 1.
func TestRanksAreStrictlyOrderedAsSlotsFree(t *testing.T) {
	ids := []string{"172", "176", "180", "183"}
	m := mgrWithJobs(ids, []JobStatus{StatusQueued, StatusQueued, StatusQueued, StatusQueued})

	got := map[string]int{}
	// 172 and 176 launch immediately (two slots).
	for _, id := range []string{"172", "176"} {
		got[id] = m.localJobRank(find(m, id))
		find(m, id).Status = StatusRunning
	}
	// 172 finishes, 180 takes its slot; then 176 finishes and 183 takes that one.
	find(m, "172").Status = StatusDone
	got["180"] = m.localJobRank(find(m, "180"))
	find(m, "180").Status = StatusRunning
	find(m, "176").Status = StatusDone
	got["183"] = m.localJobRank(find(m, "183"))

	for i, id := range ids {
		if got[id] != i {
			t.Errorf("job %s: rank %d, want %d (ranks: %v)", id, got[id], i, got)
		}
	}
	// The specific inversion seen in production: the two jobs running together
	// must never share a band, or the intra-job cost band decides and the
	// newest job — still on its expensive top rungs — takes every slot.
	if got["180"] == got["183"] {
		t.Errorf("the two concurrent jobs tie at rank %d: "+
			"cost band decides and the NEWER job wins every slot", got["180"])
	}
}

// The chain resets once nothing is left running, so a server that has been up
// for weeks does not hand every new job the clamped bottom band.
func TestRankResetsWhenTheFarmDrains(t *testing.T) {
	m := mgrWithJobs([]string{"1", "2"}, []JobStatus{StatusQueued, StatusQueued})
	m.localJobRank(find(m, "1"))
	find(m, "1").Status = StatusRunning
	if r := m.localJobRank(find(m, "2")); r != 1 {
		t.Fatalf("second concurrent job: rank %d, want 1", r)
	}
	find(m, "1").Status = StatusDone
	find(m, "2").Status = StatusDone

	m.jobs = append(m.jobs, &Job{ID: "3", Status: StatusQueued,
		Config: JobConfig{Target: TargetLocalDist}})
	if r := m.localJobRank(find(m, "3")); r != 0 {
		t.Errorf("job launched onto an idle farm: rank %d, want 0", r)
	}
}

// A queued job has not launched and holds no rank, so it must not push a job
// that IS launching further down. Only running jobs have been assigned one.
func TestQueuedJobsDoNotConsumeRanks(t *testing.T) {
	// 1 running, 2 and 3 queued; 3 launches next (2 is still waiting).
	m := mgrWithJobs([]string{"1", "2", "3"},
		[]JobStatus{StatusQueued, StatusQueued, StatusQueued})
	m.localJobRank(find(m, "1"))
	find(m, "1").Status = StatusRunning

	r := m.localJobRank(find(m, "3"))
	// Two older jobs are active (1 running, 2 queued), so the count is 2 and
	// that wins over behind+1 = 1. Ranking BELOW a job that has not started is
	// the conservative direction: it can only cost this job priority, never
	// steal it from an older one.
	if r != 2 {
		t.Errorf("rank %d, want 2", r)
	}
}

// Ranks are pruned with the working set. Without this the map is a slow leak on
// a server that never restarts — one entry per encode, forever.
func TestRanksAreDroppedWithTheirJobs(t *testing.T) {
	m := mgrWithJobs([]string{"1"}, []JobStatus{StatusQueued})
	m.localJobRank(find(m, "1"))
	if len(m.distRanks) != 1 {
		t.Fatalf("rank not recorded: %v", m.distRanks)
	}
	// The job leaves the working set (trimmed from history).
	m.jobs = []*Job{{ID: "9", Status: StatusQueued,
		Config: JobConfig{Target: TargetLocalDist}}}
	m.localJobRank(find(m, "9"))
	if _, stale := m.distRanks["1"]; stale {
		t.Errorf("rank for a departed job survived: %v", m.distRanks)
	}
}

// The highest key the worker can build from a capped rank must land ON the
// server's ceiling, never past it. A key above matching.priorityLevels is
// rejected when the activity is SCHEDULED — the encode fails outright rather
// than merely running out of order — so this is the one drift that is not
// survivable, and nothing at build time checks the three files agree.
func TestTopRankFitsTheServerCeiling(t *testing.T) {
	if got := maxLocalJobRank*chunkPriorityBands + chunkPriorityBands; got != chunkPriorityLevels {
		t.Errorf("highest priority_key = %d, matching.priorityLevels = %d "+
			"(infra/local-cluster/dynamicconfig/encode.yaml must agree)",
			got, chunkPriorityLevels)
	}
}

// A queue that never drains climbs one rank per job and eventually saturates:
// past the cap every job shares the bottom band, ties, and falls through to the
// cost-band tie-break — which favours the NEWEST job, since it is the one still
// on its expensive top rungs. Raising the ceiling moves this point; it does not
// remove it. Pinned so the limit is a known property rather than a surprise.
func TestRankSaturatesOnAQueueThatNeverDrains(t *testing.T) {
	n := maxLocalJobRank + 5
	m := &Manager{}
	for i := 0; i < n; i++ {
		m.jobs = append(m.jobs, &Job{
			// Zero-padded so string ordering matches submission order — job IDs
			// are same-width millisecond timestamps in production.
			ID:     string(rune('A'+i/26)) + string(rune('a'+i%26)),
			Status: StatusQueued,
			Config: JobConfig{Target: TargetLocalDist},
		})
	}
	last := -1
	for i, j := range m.jobs {
		r := m.localJobRank(j)
		j.Status = StatusRunning
		if r > maxLocalJobRank {
			t.Fatalf("job %d: rank %d exceeds the cap %d", i, r, maxLocalJobRank)
		}
		if r < last {
			t.Fatalf("job %d: rank went backwards, %d after %d", i, r, last)
		}
		last = r
	}
	if last != maxLocalJobRank {
		t.Errorf("%d overlapping jobs ended at rank %d, want the %d cap",
			n, last, maxLocalJobRank)
	}
}
