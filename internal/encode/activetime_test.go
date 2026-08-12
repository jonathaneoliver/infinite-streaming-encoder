package encode

import (
	"testing"
	"time"
)

var atBase = time.Date(2026, 8, 12, 10, 0, 0, 0, time.UTC)

func atSec(sec int) *time.Time {
	t := atBase.Add(time.Duration(sec) * time.Second)
	return &t
}

func ivStage(start, end int) StageProgress {
	s := StageProgress{Key: "encode:h264:1080p", Status: "done", StartedAt: atSec(start)}
	if end >= 0 {
		s.EndedAt = atSec(end)
	} else {
		s.Status = "running"
	}
	return s
}

// The whole point: parallel stages count ONCE. Summing them is a machine-hours
// measure that overshoots elapsed time by the width of the fan-out — ~8x on a
// 336-chunk run, which is what made "total measured" unusable for comparing runs.
func TestParallelStagesCountOnce(t *testing.T) {
	// Four stages, all running 0-100s on four workers.
	stages := []StageProgress{ivStage(0, 100), ivStage(0, 100), ivStage(0, 100), ivStage(0, 100)}
	got := jobActiveTime(stages, atBase, atBase.Add(100*time.Second))
	if got.Active != 100*time.Second {
		t.Errorf("active = %v, want 100s (the sum, 400s, is the wrong answer)", got.Active)
	}
	if got.Span != 100*time.Second {
		t.Errorf("span = %v, want 100s", got.Span)
	}
	if got.Idle() != 0 {
		t.Errorf("idle = %v, want 0", got.Idle())
	}
}

// Queue wait is what the displayed total counted as work. It must land in
// Queued, not Active.
func TestQueueWaitIsNotActive(t *testing.T) {
	// Submitted at atBase, first stage does not start until 600s later.
	stages := []StageProgress{ivStage(600, 700)}
	got := jobActiveTime(stages, atBase, atBase.Add(700*time.Second))
	if got.Queued != 600*time.Second {
		t.Errorf("queued = %v, want 600s", got.Queued)
	}
	if got.Active != 100*time.Second {
		t.Errorf("active = %v, want 100s", got.Active)
	}
}

// Starvation: the job is running but has nothing on a worker, because another
// job holds the pool. Distinct from queue wait — different cause, different fix
// — so they are reported separately.
func TestGapsInsideTheRunAreIdleNotActive(t *testing.T) {
	// 0-100 busy, 100-300 nothing, 300-400 busy.
	stages := []StageProgress{ivStage(0, 100), ivStage(300, 400)}
	got := jobActiveTime(stages, atBase, atBase.Add(400*time.Second))
	if got.Active != 200*time.Second {
		t.Errorf("active = %v, want 200s", got.Active)
	}
	if got.Span != 400*time.Second {
		t.Errorf("span = %v, want 400s", got.Span)
	}
	if got.Idle() != 200*time.Second {
		t.Errorf("idle = %v, want 200s", got.Idle())
	}
}

func TestOverlapMerging(t *testing.T) {
	cases := []struct {
		name   string
		stages []StageProgress
		active time.Duration
	}{
		{"disjoint", []StageProgress{ivStage(0, 10), ivStage(20, 30)}, 20 * time.Second},
		{"overlapping", []StageProgress{ivStage(0, 20), ivStage(10, 30)}, 30 * time.Second},
		{"abutting merges into one run", []StageProgress{ivStage(0, 10), ivStage(10, 20)}, 20 * time.Second},
		{"nested", []StageProgress{ivStage(0, 100), ivStage(20, 30)}, 100 * time.Second},
		{"chained overlaps", []StageProgress{ivStage(0, 20), ivStage(15, 40), ivStage(35, 60)}, 60 * time.Second},
		{"single", []StageProgress{ivStage(5, 25)}, 20 * time.Second},
		{"zero-length", []StageProgress{ivStage(5, 5)}, 0},
	}
	for _, tc := range cases {
		if got := jobActiveTime(tc.stages, atBase, atBase.Add(time.Hour)); got.Active != tc.active {
			t.Errorf("%s: active = %v, want %v", tc.name, got.Active, tc.active)
		}
	}
}

// Stages are appended as they are ANNOUNCED, and on the cloud path three
// sources announce at three different latencies — so insertion order is not
// start order, and a merge that assumed it would over-count.
func TestOutOfOrderStagesStillMerge(t *testing.T) {
	forward := []StageProgress{ivStage(0, 20), ivStage(10, 30), ivStage(25, 50)}
	shuffled := []StageProgress{ivStage(25, 50), ivStage(0, 20), ivStage(10, 30)}
	a := jobActiveTime(forward, atBase, atBase.Add(time.Hour))
	b := jobActiveTime(shuffled, atBase, atBase.Add(time.Hour))
	if a.Active != b.Active || a.Span != b.Span {
		t.Errorf("order changed the answer: %v/%v vs %v/%v", a.Active, a.Span, b.Active, b.Span)
	}
	if a.Active != 50*time.Second {
		t.Errorf("active = %v, want 50s", a.Active)
	}
}

// A live job: running stages are bounded by `now`, so active grows as it works
// rather than treating an unfinished stage as instantaneous.
func TestRunningStagesAreBoundedByNow(t *testing.T) {
	stages := []StageProgress{ivStage(0, -1)} // still running
	got := jobActiveTime(stages, atBase, atBase.Add(90*time.Second))
	if got.Active != 90*time.Second {
		t.Errorf("active = %v, want 90s", got.Active)
	}
}

// A queued job has no stages at all, and "how long have I been waiting" is
// exactly the question worth answering in that state.
func TestQueuedJobWithNoStages(t *testing.T) {
	got := jobActiveTime(nil, atBase, atBase.Add(300*time.Second))
	if got.Queued != 300*time.Second {
		t.Errorf("queued = %v, want 300s", got.Queued)
	}
	if got.Active != 0 || got.Span != 0 {
		t.Errorf("active/span = %v/%v, want 0/0", got.Active, got.Span)
	}
}

// Pending rows never ran and must contribute nothing — otherwise a job that
// failed early would report the declared-but-never-started stages as work.
func TestPendingStagesAreSkipped(t *testing.T) {
	stages := []StageProgress{
		ivStage(0, 100),
		{Key: "encode:h264:720p", Status: "pending"}, // no StartedAt
	}
	got := jobActiveTime(stages, atBase, atBase.Add(100*time.Second))
	if got.Active != 100*time.Second {
		t.Errorf("active = %v, want 100s", got.Active)
	}
}

// Clocks move. A negative interval must not subtract from the union.
func TestNegativeIntervalsDoNotSubtract(t *testing.T) {
	stages := []StageProgress{ivStage(0, 100), ivStage(200, 150)} // ends before it starts
	got := jobActiveTime(stages, atBase, atBase.Add(300*time.Second))
	if got.Active != 100*time.Second {
		t.Errorf("active = %v, want 100s", got.Active)
	}
	if got.Queued < 0 || got.Span < 0 {
		t.Errorf("negative duration: queued=%v span=%v", got.Queued, got.Span)
	}
}

// The measured case this was built for: three ladders whose displayed totals
// spread 1.37h -> 3.88h while the work was identical, because MAX_CONCURRENT=2
// made each later submission wait. Active must collapse that spread.
func TestQueuedLaddersReportTheSameActiveTime(t *testing.T) {
	const work = 4800 // 1.33h of work apiece
	var actives []time.Duration
	for i, queued := range []int{0, 4300, 9100} { // 1st, 2nd, 3rd in the queue
		stages := []StageProgress{ivStage(queued, queued+work/2), ivStage(queued+work/2, queued+work)}
		got := jobActiveTime(stages, atBase, atBase.Add(time.Duration(queued+work)*time.Second))
		actives = append(actives, got.Active)
		if got.Queued != time.Duration(queued)*time.Second {
			t.Errorf("ladder %d: queued = %v, want %ds", i, got.Queued, queued)
		}
	}
	for i, a := range actives {
		if a != actives[0] {
			t.Errorf("ladder %d active = %v, ladder 0 = %v — identical work must report identically",
				i, a, actives[0])
		}
	}
}
