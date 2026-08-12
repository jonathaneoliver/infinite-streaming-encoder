package encode

import "time"

// How long did this job actually take?
//
// There were two answers and both were misleading, in opposite directions.
//
// `EndedAt - StartedAt` is what the UI showed. StartedAt is set in Submit, at
// the same moment Status becomes queued, so it counts every second the job spent
// waiting for a MAX_CONCURRENT slot. Submit four ladders at once with a limit of
// two and the fourth reports ~3x the first, having done identical work — which
// is exactly how a four-way ladder comparison came to look like the 6s profile
// was slow. It was last in the queue.
//
// `total measured` in history.md is the SUM of stage durations, and the comment
// beside it said it "may double-count parallel stages if any — unlikely but
// noted". It is not unlikely: a run fans 336 chunks across a worker pool, so the
// sum overshoots wall clock by ~8x. That number is a machine-hours measure — the
// thing you would bill for — and it answers a different question.
//
// So: jobActiveTime, the UNION of the intervals in which at least one stage was
// running. Measured on the same three HEVC ladders, active was 1.34h / 1.36h /
// 1.35h where wall said 1.37h / 2.55h / 3.88h and the sum said ~10h.
//
// What union does NOT remove is CONTENTION. Two jobs sharing the worker pool
// both encode slower, and that inflates active for both. This fixes the
// accounting; only running them one at a time fixes the measurement.

// interval is one stage's running window.
type interval struct{ start, end time.Time }

// jobTiming is the three-way split of a job's elapsed time.
type jobTiming struct {
	// Queued is time from submission to the first stage starting: waiting for a
	// MAX_CONCURRENT slot, plus the pre-fan-out work (probe, mezzanine) that
	// runs before any stage is announced.
	Queued time.Duration
	// Active is the union of running-stage intervals — wall clock during which
	// this job had work in flight.
	Active time.Duration
	// Span is first stage start to last stage end. Span - Active is STARVATION:
	// the job was running but had nothing on a worker, which is what a
	// concurrent job competing for the same pool looks like from here. Kept
	// separate from Queued because they have different causes and different
	// fixes, and one number cannot distinguish them.
	Span time.Duration
}

// Idle returns the starvation inside the job's own run.
func (t jobTiming) Idle() time.Duration {
	if t.Span <= t.Active {
		return 0
	}
	return t.Span - t.Active
}

// jobActiveTime computes the split from a job's stages.
//
// `now` bounds stages still running (pass the job's end time for a finished job,
// time.Now() for a live one) so a live job's active time grows as it works
// rather than counting an unfinished stage as instantaneous.
//
// Stages with no StartedAt never ran and are skipped — a pending row contributes
// nothing, which is what makes this safe to call on a job that failed early.
func jobActiveTime(stages []StageProgress, submitted, now time.Time) jobTiming {
	ivs := make([]interval, 0, len(stages))
	for _, s := range stages {
		if s.StartedAt == nil {
			continue
		}
		end := now
		if s.EndedAt != nil {
			end = *s.EndedAt
		}
		if end.Before(*s.StartedAt) {
			// A clock adjustment, or a stage that ended before the bound we
			// were given. Treat it as instantaneous rather than negative, which
			// would silently subtract from the union.
			end = *s.StartedAt
		}
		ivs = append(ivs, interval{*s.StartedAt, end})
	}
	if len(ivs) == 0 {
		return jobTiming{Queued: nonNegative(now.Sub(submitted))}
	}
	// Insertion order is not start order — stages are appended as they are
	// announced, and on the cloud path they are announced by three different
	// sources at three different latencies.
	sortIntervals(ivs)

	first, last := ivs[0].start, ivs[0].end
	var active time.Duration
	curStart, curEnd := ivs[0].start, ivs[0].end
	for _, v := range ivs[1:] {
		if v.end.After(last) {
			last = v.end
		}
		if !v.start.After(curEnd) { // overlaps or abuts the open run
			if v.end.After(curEnd) {
				curEnd = v.end
			}
			continue
		}
		active += curEnd.Sub(curStart)
		curStart, curEnd = v.start, v.end
	}
	active += curEnd.Sub(curStart)

	return jobTiming{
		Queued: nonNegative(first.Sub(submitted)),
		Active: active,
		Span:   nonNegative(last.Sub(first)),
	}
}

func nonNegative(d time.Duration) time.Duration {
	if d < 0 {
		return 0
	}
	return d
}

// sortIntervals orders by start time. Insertion sort: the slice is nearly sorted
// already (stages are announced roughly in start order) and this avoids pulling
// in a comparator closure per call on a path that runs on every SSE update.
func sortIntervals(ivs []interval) {
	for i := 1; i < len(ivs); i++ {
		for j := i; j > 0 && ivs[j].start.Before(ivs[j-1].start); j-- {
			ivs[j], ivs[j-1] = ivs[j-1], ivs[j]
		}
	}
}

// Timing returns the job's queued/active/span split. Safe to call on a live job.
func (j *Job) Timing() jobTiming {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.timingLocked()
}

func (j *Job) timingLocked() jobTiming {
	now := time.Now()
	if j.EndedAt != nil {
		now = *j.EndedAt
	}
	return jobActiveTime(j.Stages, j.StartedAt, now)
}
