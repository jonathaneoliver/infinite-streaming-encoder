package encode

import (
	"math"
	"path/filepath"
	"strings"
	"testing"
)

// #316: the dynamic chunk selector is per-variant, the Step Functions history
// ceiling is per-job, and nothing connected them.
//
// The speeds below are the REAL measured graviton figures from a live
// encode_speeds.json (content-seconds per wall-second, 2-pass, medium, 30fps),
// not invented ones. The whole question this feature answers — does a given
// clip's plan fit — is a function of those numbers, so a test using tidy fake
// speeds would exercise the arithmetic and prove nothing about the outcome.
//
// HEVC's 954p and 1800p are OMITTED, because they are absent from the real store
// too — no cloud HEVC run has learned them yet. Speed() has no neighbour
// interpolation, so in production those two fall through to seedSpeed, and the
// seed is markedly more pessimistic than the measurements either side of it
// (954p seeds at 0.063 against a measured 0.090 at 1080p). Filling them in here
// would make every count below optimistic in a way production is not — and the
// gap is most of why 2h HEVC does not have the margin earlier estimates gave it.
var gravitonHevc2Pass = map[int]float64{
	360: 1.9589, 432: 1.4613, 468: 1.0829, 504: 0.7513,
	540: 0.6677, 594: 0.6655, 720: 0.5289,
	1080: 0.0904, 1440: 0.0498, 2160: 0.0200,
}

// The shipped apple-uniq-live rung heights. h264 and hevc differ — hevc has
// 468p/504p where h264 has 234p/396p — which is worth stating in a test because
// getting it wrong is how #312's and #313's original numbers came to be wrong.
var (
	h264Heights = []int{234, 360, 396, 432, 540, 594, 720, 954, 1080, 1440, 1800, 2160}
	hevcHeights = []int{360, 432, 468, 504, 540, 594, 720, 954, 1080, 1440, 1800, 2160}
)

func seededSpeeds(t *testing.T) *EncodeSpeedStore {
	t.Helper()
	s := LoadEncodeSpeedStore(filepath.Join(t.TempDir(), "speeds.json"))
	// The first sample for a key is stored verbatim (no EWMA blend), so
	// content=speed with wall=1 pins the key to exactly that speed.
	for h, sp := range gravitonHevc2Pass {
		s.Update("graviton", "hevc", h, true, "medium", 30, sp, 1)
	}
	return s
}

func plannedFor(codec string, heights []int, twoPass bool) []plannedVariant {
	out := make([]plannedVariant, 0, len(heights))
	for _, h := range heights {
		out = append(out, plannedVariant{
			codec:   codec,
			rung:    ladderRung{Label: "x", Height: h, Width: h * 16 / 9, Preset: "medium"},
			twoPass: twoPass,
		})
	}
	return out
}

const segS = 6.0

// The property that matters most: nothing that works today moves. If the plan
// already fits, the target must come back as the untouched constant — not as
// "close to" it, because any other value re-sizes every variant and the plans
// stop being comparable with runs from before this landed.
func TestBudgetLeavesFittingJobsExactlyAsTheyWere(t *testing.T) {
	s := seededSpeeds(t)
	cases := []struct {
		name  string
		clipS float64
		p     []plannedVariant
	}{
		{"5-minute clip, hevc", 300, plannedFor("hevc", hevcHeights, true)},
		{"the 334s reference clip, hevc", 334, plannedFor("hevc", hevcHeights, true)},
		{"1h hevc", 3600, plannedFor("hevc", hevcHeights, true)},
		{"2h h264", 2 * 3600, plannedFor("h264", h264Heights, true)},
		{"4h h264", 4 * 3600, plannedFor("h264", h264Heights, true)},
	}
	for _, tc := range cases {
		target, before, after := budgetedChunkTarget(tc.p, tc.clipS, segS, s, 30)
		if target != dynamicTargetWallSeconds {
			t.Errorf("%s: target moved %.0f → %.0f but the plan already fits (%d chunks)",
				tc.name, dynamicTargetWallSeconds, target, before)
		}
		if before != after {
			t.Errorf("%s: chunk count changed %d → %d without the target moving", tc.name, before, after)
		}
		if before > sfnChunkBudget {
			t.Errorf("%s: %d chunks is over the %d budget — this case belongs in the over-budget test",
				tc.name, before, sfnChunkBudget)
		}
		// And the sizing call is literally the same one, so the emitted plan is
		// byte-identical rather than merely equivalent.
		for _, p := range tc.p {
			was := dynamicChunkSeconds(s, p.codec, p.rung.Height, p.twoPass, p.rung.Preset, 30, tc.clipS)
			now := dynamicChunkSecondsAt(target, s, p.codec, p.rung.Height, p.twoPass, p.rung.Preset, 30, tc.clipS)
			if was != now {
				t.Errorf("%s: %s %dp chunk size %.0f → %.0f", tc.name, p.codec, p.rung.Height, was, now)
			}
		}
	}
}

// The case the issue is about: 4h HEVC plans past the ceiling at the default and
// must come back under it on its own.
func TestBudgetBringsFourHourHevcUnderTheCeiling(t *testing.T) {
	s := seededSpeeds(t)
	p := plannedFor("hevc", hevcHeights, true)
	const clipS = 4 * 3600

	target, before, after := budgetedChunkTarget(p, clipS, segS, s, 30)

	if before <= sfnChunkBudget {
		t.Fatalf("4h hevc planned %d chunks at the default — under the %d budget, so this "+
			"test no longer covers what it was written for (speeds or rungs changed)",
			before, sfnChunkBudget)
	}
	if after > sfnChunkBudget {
		t.Errorf("after budgeting: %d chunks, still over the %d budget (target %.0fs)",
			after, sfnChunkBudget, target)
	}
	if after*sfnEventsPerChunk > sfnHistoryLimit {
		t.Errorf("projected %d events exceeds the %d hard limit", after*sfnEventsPerChunk, sfnHistoryLimit)
	}
	if target <= dynamicTargetWallSeconds {
		t.Errorf("target did not rise: %.0fs", target)
	}
	t.Logf("4h hevc: %d chunks at %.0fs → %d chunks at %.0fs (%d events)",
		before, dynamicTargetWallSeconds, after, target, after*sfnEventsPerChunk)
}

// Scale the TARGET, never the floor.
//
// The obvious alternative is to raise dynamicMinChunkSeconds alongside it, and
// the reason not to is not that the target is free — fitting a 4h HEVC plan
// lengthens the longest chunk either way, which is the trade #316 is making.
// It is that scaling the floor too buys the SAME fit for a LONGER worst chunk:
// the floor binds on the slowest rungs, which are already the longest pieces in
// the run, so lifting it grows the atomic long pole to save chunks that are not
// where the count is. Raising only the target lengthens the rungs with room to
// spare first.
//
// So the claim is comparative, and this test compares. Pinned because the change
// that would regress it is one line in dynamicChunkSecondsAt, and the damage —
// a longer makespan floor — is invisible in the chunk COUNT everything else here
// looks at.
func TestScalingTheFloorTooIsWorseAtTheSameFit(t *testing.T) {
	s := seededSpeeds(t)
	p := plannedFor("hevc", hevcHeights, true)
	const clipS = 4 * 3600

	// Strategy B: the floor (and quantum) scale with the target, snapped to a
	// whole segment so chunk boundaries still land on segment edges.
	sizeWithFloor := func(target, floor float64, v plannedVariant) float64 {
		c := target * s.Speed("graviton", v.codec, v.rung.Height, v.twoPass, v.rung.Preset, 30)
		c = math.Round(c/floor) * floor
		if c < floor {
			c = floor
		}
		if c > clipS {
			c = clipS
		}
		return c
	}
	measure := func(target float64, scaleFloor bool) (chunks int, longestWall float64) {
		floor := dynamicMinChunkSeconds
		if scaleFloor {
			floor = math.Ceil(dynamicMinChunkSeconds*(target/dynamicTargetWallSeconds)/segS) * segS
		}
		for _, v := range p {
			var cs float64
			if scaleFloor {
				cs = sizeWithFloor(target, floor, v)
			} else {
				cs = dynamicChunkSecondsAt(target, s, v.codec, v.rung.Height, v.twoPass, v.rung.Preset, 30, clipS)
			}
			chunks += len(planChunks(clipS, cs, segS))
			if w := cs / s.Speed("graviton", v.codec, v.rung.Height, v.twoPass, v.rung.Preset, 30); w > longestWall {
				longestWall = w
			}
		}
		return chunks, longestWall
	}
	// Smallest target on a 12s grid that fits, for each strategy — comparing at
	// equal fit is the only comparison that means anything.
	fit := func(scaleFloor bool) (target float64, chunks int, longest float64) {
		for target = dynamicTargetWallSeconds; target <= 200*dynamicTargetWallSeconds; target += 12 {
			if chunks, longest = measure(target, scaleFloor); chunks <= sfnChunkBudget {
				return target, chunks, longest
			}
		}
		t.Fatalf("scaleFloor=%v never fit", scaleFloor)
		return
	}

	tA, chunksA, longestA := fit(false) // target only — what we ship
	tB, chunksB, longestB := fit(true)  // target + floor

	t.Logf("target only : target %.0fs  %d chunks  longest %.0fs", tA, chunksA, longestA)
	t.Logf("target+floor: target %.0fs  %d chunks  longest %.0fs", tB, chunksB, longestB)

	if longestA > longestB {
		t.Errorf("scaling the floor too gave a SHORTER longest chunk (%.0fs vs %.0fs) — "+
			"the reason for not scaling it no longer holds", longestB, longestA)
	}
	if chunksA > sfnChunkBudget || chunksB > sfnChunkBudget {
		t.Errorf("a strategy did not actually fit: %d / %d against budget %d", chunksA, chunksB, sfnChunkBudget)
	}
}

// The budget must be computed from the SAME plan that ships, or it guarantees
// nothing. This is the join that #312's guard also has to use.
func TestPlannedCountMatchesTheEmittedPlan(t *testing.T) {
	s := seededSpeeds(t)
	p := plannedFor("hevc", hevcHeights, true)
	const clipS = 4 * 3600

	target, _, after := budgetedChunkTarget(p, clipS, segS, s, 30)

	emitted := 0
	for _, v := range p {
		cs := variantChunkSeconds("dynamic", target, clipS, s, v.codec, v.rung.Height, v.twoPass, v.rung.Preset, 30)
		emitted += len(planChunks(clipS, cs, segS))
	}
	if emitted != after {
		t.Errorf("budget counted %d chunks, the plan emits %d — the two derive the size differently",
			after, emitted)
	}
}

// An explicit --chunk-duration must survive untouched. The budget is only ever
// consulted for dynamic chunking; silently growing a size the caller asked for
// would be a worse answer than #312's refusal.
func TestFixedAndWholeIgnoreTheTarget(t *testing.T) {
	s := seededSpeeds(t)
	for _, cfg := range []string{"12", "30", "whole"} {
		def := variantChunkSeconds(cfg, dynamicTargetWallSeconds, 4*3600, s, "hevc", 2160, true, "medium", 30)
		raised := variantChunkSeconds(cfg, 4*dynamicTargetWallSeconds, 4*3600, s, "hevc", 2160, true, "medium", 30)
		if def != raised {
			t.Errorf("chunk_duration %q moved %.0f → %.0f with the target", cfg, def, raised)
		}
	}
}

// A raised target has to explain itself, and a plan that could NOT be brought
// under budget has to say that rather than submitting while looking adjusted.
func TestBudgetLineStatesTheDecision(t *testing.T) {
	s := seededSpeeds(t)
	p := plannedFor("hevc", hevcHeights, true)
	const clipS = 4 * 3600
	target, before, after := budgetedChunkTarget(p, clipS, segS, s, 30)

	line := chunkBudgetLine(target, before, after, p, clipS, segS, s, 30)
	if line == "" {
		t.Fatal("target was raised with no log line")
	}
	for _, want := range []string{"chunk budget", "raising the target", "peak useful concurrency"} {
		if !strings.Contains(line, want) {
			t.Errorf("log line does not mention %q: %s", want, line)
		}
	}
	if strings.Contains(line, "STILL OVER") {
		t.Errorf("plan fits but the line claims otherwise: %s", line)
	}

	// Silent when nothing changed — a line on every run would train people to
	// ignore the one that matters.
	if l := chunkBudgetLine(dynamicTargetWallSeconds, 40, 40, p, 334, segS, s, 30); l != "" {
		t.Errorf("unchanged target still logged: %s", l)
	}
}

// The budget is 80% of hard, so the arithmetic behind the constants should hold
// even if someone retunes them.
func TestBudgetConstantsAreConsistent(t *testing.T) {
	if sfnChunkBudget*sfnEventsPerChunk > sfnHistoryLimit {
		t.Errorf("budget %d × %d events = %d exceeds the %d limit",
			sfnChunkBudget, sfnEventsPerChunk, sfnChunkBudget*sfnEventsPerChunk, sfnHistoryLimit)
	}
	if sfnBudgetToleranceS <= 0 {
		t.Error("a tolerance of zero never terminates the bisection")
	}
	if sfnBudgetMaxDoublings < 4 {
		t.Error("too few doublings to reach a fitting target for a realistic ladder")
	}
}

// The bisection has to return the SMALLEST fitting target, not merely a fitting
// one. Overshooting hands back parallelism, which is the only currency this
// feature spends — the proportional-scaling version it replaced left a 4h HEVC
// run at 1,881 chunks against a 2,500 budget.
func TestBudgetDoesNotOvershoot(t *testing.T) {
	s := seededSpeeds(t)
	p := plannedFor("hevc", hevcHeights, true)
	const clipS = 4 * 3600

	target, _, after := budgetedChunkTarget(p, clipS, segS, s, 30)

	// One tolerance step below the chosen target must NOT fit, or a smaller
	// target was available and went unused.
	lower := target - 2*sfnBudgetToleranceS
	if n := plannedChunkCount(p, lower, clipS, segS, s, 30); n <= sfnChunkBudget {
		t.Errorf("target %.0fs gives %d chunks, but %.0fs also fits at %d — overshot by at least %d chunks",
			target, after, lower, n, n-after)
	}
}
