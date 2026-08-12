package encode

import (
	"strconv"
	"strings"
	"testing"
)

// #312: a fixed --chunk-duration has no budget scaler behind it (dynamic does,
// via #316), so it scales linearly with content length until it blows one of two
// AWS limits. The guard refuses at submit rather than letting AWS return a
// payload-size error that never says the word "chunk", or — worse — letting the
// history fill hours in, after spot capacity has been paid for.

func TestChunkPlanFitsNamesTheLimitItBroke(t *testing.T) {
	underEvents := sfnChunkBudget // comfortably inside both
	cases := []struct {
		name   string
		chunks int
		bytes  int
		ok     bool
		says   string
	}{
		{"small plan fits", 300, 20 * 1024, true, ""},
		{"at the budget", underEvents, 100 * 1024, true, ""},
		// History binds first in practice — ~52 min of content at a fixed 12s,
		// against ~70 min for the input — so this is the common failure.
		{"history only", 5000, 100 * 1024, false, "history events"},
		{"input only", 100, 250 * 1024, false, "StartExecution"},
		{"both", 8000, 400 * 1024, false, "and"},
	}
	for _, tc := range cases {
		ok, why := chunkPlanFits(tc.chunks, tc.bytes)
		if ok != tc.ok {
			t.Errorf("%s: ok=%v want %v (%s)", tc.name, ok, tc.ok, why)
			continue
		}
		if !ok && !strings.Contains(why, tc.says) {
			t.Errorf("%s: message %q does not mention %q", tc.name, why, tc.says)
		}
		if !ok && !strings.Contains(why, "chunks") {
			t.Errorf("%s: message does not say how many chunks: %q", tc.name, why)
		}
	}
}

// The history message must say the run dies PART WAY THROUGH. That is the whole
// difference between the two limits: one costs a rejected submit, the other
// costs hours of spot capacity. An operator reading "limit exceeded" cannot tell
// which they are about to pay for.
func TestHistoryFailureSaysItDiesMidRun(t *testing.T) {
	_, why := chunkPlanFits(5000, 1024)
	if !strings.Contains(why, "PART WAY THROUGH") {
		t.Errorf("history message does not warn it fails mid-run: %q", why)
	}
	_, why = chunkPlanFits(100, 250*1024)
	if strings.Contains(why, "PART WAY THROUGH") {
		t.Errorf("input message wrongly claims a mid-run failure: %q", why)
	}
}

// The advice has to be a duration that actually fits, not a guess — otherwise
// the operator retries, waits, and is refused again.
func TestFixedChunkAdviceActuallyFits(t *testing.T) {
	const segS = 6.0
	for _, clipS := range []float64{2 * 3600, 4 * 3600, 8 * 3600} {
		p := plannedFor("h264", h264Heights, true)
		alt := fixedChunkAdvice(p, clipS, segS)
		if alt == "" {
			continue // nothing fixed fits; the caller says "use dynamic"
		}
		v, err := strconv.ParseFloat(alt, 64)
		if err != nil {
			t.Fatalf("advice %q is not a number", alt)
		}
		n := 0
		for range p {
			n += len(planChunks(clipS, v, segS))
		}
		if n*sfnEventsPerChunk > sfnHistoryLimit {
			t.Errorf("clip %.0fh: advised %ss but that is %d chunks / %d events — still over",
				clipS/3600, alt, n, n*sfnEventsPerChunk)
		}
	}
}

// The measured cases from #312, against the real learned speeds. These are the
// numbers the guard exists to catch, so pin them rather than trusting the shape.
func TestFixedDurationsThatMustBeRefused(t *testing.T) {
	const segS = 6.0
	cases := []struct {
		clipHours float64
		chunkS    float64
		refuse    bool
	}{
		{0.1, 12, false}, // the reference clip — must keep working
		{1, 12, true},    // history goes first, at ~52 min
		{2, 12, true},
		{2, 30, false},
		{4, 30, true},
		{4, 60, false},
	}
	for _, tc := range cases {
		p := plannedFor("h264", h264Heights, true)
		clipS := tc.clipHours * 3600
		n := 0
		for range p {
			n += len(planChunks(clipS, tc.chunkS, segS))
		}
		// Bytes are dominated by the chunk descriptors; ~59 each is the measured
		// serialized size, and the guard is checked on the real marshal in
		// buildSFNInput. Here the event limit is what these cases turn on.
		ok, _ := chunkPlanFits(n, n*59)
		if ok == tc.refuse {
			t.Errorf("%.1fh at %.0fs: %d chunks, refuse=%v want refuse=%v",
				tc.clipHours, tc.chunkS, n, !ok, tc.refuse)
		}
	}
}

// Dynamic and whole must never reach the guard — dynamic is scaled to fit by
// budgetedChunkTarget, and whole is one chunk per variant. Pinned because the
// guard would otherwise be free to reject a plan the planner just fixed.
func TestGuardIsFixedDurationsOnly(t *testing.T) {
	for _, cfg := range []string{"dynamic", "", "whole"} {
		if isFixedChunkDuration(cfg) {
			t.Errorf("%q reaches the guard, but the planner already sized it", cfg)
		}
	}
	for _, cfg := range []string{"12", "30", "0.5", "90"} {
		if !isFixedChunkDuration(cfg) {
			t.Errorf("%q skips the guard, so nothing checks its limits", cfg)
		}
	}
}
