package encode

import (
	"fmt"
)

// The whole-job chunk budget (#316).
//
// The dynamic chunk selector is PER-VARIANT and stateless: dynamicChunkSeconds
// sizes one rung from its learned speed and knows nothing about the rest of the
// job. The ceiling it has to live under is a WHOLE-JOB property — one Step
// Functions execution's history, shared by every chunk of every rung of every
// codec in the run. Nothing connected the two, so a 4h HEVC encode planned
// ~5,100 chunks, passed submit, launched spot capacity, encoded for hours, and
// died when the history filled.
//
// This raises the wall-time target until the whole plan fits. It is rationing,
// not a fix: #313 (Mode: DISTRIBUTED) removes the ceiling, and until then the
// trade is real — for HEVC especially, where variantResourcesFor reserves a flat
// 2 vCPU because x265 tops out around two cores, chunk count IS the parallelism.
// Doubling the target roughly halves the peak concurrency the run can use and
// lifts its makespan floor. Better than failing mid-run; worse than not needing
// to choose.
const (
	// A Step Functions execution's history is capped at 25,000 events.
	sfnHistoryLimit = 25000

	// Events an INLINE Map spends per iteration: MapIterationStarted /
	// MapIterationSucceeded plus the TaskStateEntered / TaskScheduled /
	// TaskStarted / TaskSucceeded / TaskStateExited a batch:submitJob.sync Task
	// emits.
	//
	// This is an ESTIMATE and everything below scales by it. It has not been
	// checked against a real get_execution_history — one call against a finished
	// execution turns it into a measurement, and if the true figure is 6 the
	// budget is 4,166 rather than 3,125 and 4h HEVC nearly fits untouched. Being
	// wrong HIGH is the safe direction (fewer chunks than allowed), which is why
	// it is not tuned down on a guess.
	sfnEventsPerChunk = 8

	// The plan is held to 80% of the hard ceiling. The margin covers the events
	// the run spends outside the chunk Maps — mezzanine, audio, the per-codec
	// packaging branch, the Choice states — and the error in the constant above.
	sfnChunkBudget = sfnHistoryLimit / sfnEventsPerChunk * 4 / 5 // 2,500

	// Bounds on the search for a target that fits.
	//
	// sfnBudgetMaxDoublings caps the hunt for ANY fitting target. Each doubling
	// roughly halves the chunk count, and at the far end every variant collapses
	// to a single whole-clip chunk, so 12 is far past enough for any real ladder
	// — it is a backstop against a plan that cannot fit at all, which would need
	// more variants than the budget has chunks.
	sfnBudgetMaxDoublings = 12
	// sfnBudgetToleranceS is how close the bisection gets to the smallest
	// fitting target. Chunk lengths quantise to whole multiples of
	// dynamicMinChunkSeconds, so resolving the target finer than a second buys
	// nothing.
	sfnBudgetToleranceS = 1.0
)

// plannedVariant is one (codec, rung) pair the run will encode, resolved before
// any chunk sizing happens.
//
// buildSFNInput used to resolve a rung and size it in the same pass, which made
// the job's total chunk count unknowable until the loop was over — and the
// budget is a property of that total. Splitting "which variants" from "how big
// are their chunks" is what lets the second question be asked with the answer to
// the first already in hand.
type plannedVariant struct {
	codec   string
	rung    ladderRung
	twoPass bool
}

// plannedChunkCount is the number of chunks the whole job would dispatch at a
// given wall-time target.
//
// It calls the SAME dynamicChunkSecondsAt + planChunks the build loop calls,
// deliberately, rather than approximating with clip/chunkSeconds. The floor and
// the 12s quantum mean a 4K HEVC rung sits at 12s whatever the target, and
// planChunks tiles to segment boundaries and folds a runt tail — so an
// arithmetic estimate is wrong in both directions and a budget derived from one
// would not describe the plan that actually ships.
func plannedChunkCount(planned []plannedVariant, targetWallS, clipS, segS float64,
	speeds *EncodeSpeedStore, fps int) int {
	total := 0
	for _, p := range planned {
		cs := dynamicChunkSecondsAt(targetWallS, speeds, p.codec, p.rung.Height,
			p.twoPass, p.rung.Preset, fps, clipS)
		total += len(planChunks(clipS, cs, segS))
	}
	return total
}

// budgetedChunkTarget returns the wall-time target to plan this job at, plus the
// chunk count at the default target and at the returned one.
//
// Returns dynamicTargetWallSeconds unchanged whenever the job already fits,
// which is every job that works today: a 5-minute clip, a 1h HEVC run, and h264
// out to ~7h all come in under budget, so their plans are byte-identical to
// before this existed.
//
// When it does have to move, it finds the SMALLEST target that fits — double
// until something fits, then bisect. The obvious cheaper approach is to scale
// the target by the ratio of chunks to budget and repeat, which converges in two
// or three rounds; it also overshoots badly, because quantising every rung to a
// 12s grid drops the count faster than the ratio predicts. On a 4h HEVC ladder
// that lands at 1,881 chunks against a 2,500 budget — a quarter of the run's
// parallelism handed back for nothing, and parallelism is the one thing this
// feature spends. Bisecting costs ~20 more passes over a 24-element slice.
func budgetedChunkTarget(planned []plannedVariant, clipS, segS float64,
	speeds *EncodeSpeedStore, fps int) (target float64, before, after int) {
	target = dynamicTargetWallSeconds
	if len(planned) == 0 || clipS <= 0 {
		return target, 0, 0
	}
	count := func(t float64) int { return plannedChunkCount(planned, t, clipS, segS, speeds, fps) }

	before = count(target)
	if before <= sfnChunkBudget {
		return target, before, before
	}

	// lo never fits, hi does (once found). Doubling rather than stepping because
	// the relationship between target and count is neither linear nor smooth.
	lo, hi := target, target
	fits := false
	for i := 0; i < sfnBudgetMaxDoublings; i++ {
		hi *= 2
		if count(hi) <= sfnChunkBudget {
			fits = true
			break
		}
		lo = hi
	}
	if !fits {
		// Nothing fits. Return the largest target tried with its real count, so
		// the caller can say so rather than submitting a plan that looks adjusted.
		return hi, before, count(hi)
	}
	for hi-lo > sfnBudgetToleranceS {
		mid := (lo + hi) / 2
		if count(mid) <= sfnChunkBudget {
			hi = mid
		} else {
			lo = mid
		}
	}
	return hi, before, count(hi)
}

// chunkBudgetLine explains a raised target for the job log, or returns "" when
// the default was kept.
//
// A run that grows 13-minute chunks must say why, or the next person to look at
// it sees the dynamic selector behaving unlike every other run and no reason
// given. The parallelism cost is named because it is the part that is not
// obvious: this buys history headroom with concurrency, and for HEVC that is the
// one lever x265 leaves you.
func chunkBudgetLine(target float64, before, after int, planned []plannedVariant,
	clipS, segS float64, speeds *EncodeSpeedStore, fps int) string {
	if target <= dynamicTargetWallSeconds {
		return ""
	}
	peakBefore := peakUsefulVCPU(planned, dynamicTargetWallSeconds, clipS, segS, speeds, fps)
	peakAfter := peakUsefulVCPU(planned, target, clipS, segS, speeds, fps)
	fit := "fits"
	if after > sfnChunkBudget {
		// Could not get under budget at any target tried. Say so rather than
		// letting a still-doomed plan submit looking adjusted.
		fit = "STILL OVER — the run may exhaust its Step Functions history"
	}
	return fmt.Sprintf(
		"[cloud-batch] chunk budget: %d chunks at the %.0fs default exceeds the "+
			"%d-chunk budget (%d events per execution, limit %d) — raising the "+
			"target to %.0fs gives %d chunks, %s. Cost: peak useful concurrency "+
			"%d → %d vCPU. See #316; #313 removes the ceiling.",
		before, dynamicTargetWallSeconds, sfnChunkBudget, sfnEventsPerChunk,
		sfnHistoryLimit, target, after, fit, peakBefore, peakAfter)
}

// peakUsefulVCPU is the most vCPU this plan could ever keep busy at once: every
// chunk running concurrently, each at its job definition's reservation. Beyond
// it, more fleet buys nothing — which is the actual cost of raising the target,
// and the reason the log line states it.
func peakUsefulVCPU(planned []plannedVariant, targetWallS, clipS, segS float64,
	speeds *EncodeSpeedStore, fps int) int {
	total := 0
	for _, p := range planned {
		cs := dynamicChunkSecondsAt(targetWallS, speeds, p.codec, p.rung.Height,
			p.twoPass, p.rung.Preset, fps, clipS)
		vcpuStr, _ := variantResourcesFor(p.codec, p.rung.Height)
		vcpu := 0
		if _, err := fmt.Sscanf(vcpuStr, "%d", &vcpu); err != nil {
			continue
		}
		total += len(planChunks(clipS, cs, segS)) * vcpu
	}
	return total
}

// The fixed-chunk-duration guard (#312).
//
// budgetedChunkTarget above only sizes DYNAMIC chunking. An explicit
// --chunk-duration is deliberately left alone — silently growing a size the
// caller asked for is a worse answer than refusing — so a fixed value has no
// protection at all, and it scales linearly with content length:
//
//	         chunks   events    input
//	1h @12s   3,600   28,800   219 KB   history over
//	2h @12s   7,200   57,600   441 KB   history AND input over
//	4h @30s   5,760   46,080   354 KB   history AND input over
//
// Both AWS limits are real and they fail differently, which is why both are
// checked:
//
//   - INPUT (256 KB) fails at StartExecution, immediately, with a payload-size
//     error that never mentions chunks.
//   - HISTORY (25,000 events) fails MID-RUN, hours in, after spot capacity has
//     been launched and paid for.
//
// History binds first — ~52 minutes of content at a fixed 12s against ~70 for
// the input — so the common failure is the expensive one. #312 was filed
// believing the reverse.
const (
	// A Step Functions execution's input is capped at 262,144 bytes. The plan is
	// held to 80% of it: the chunk descriptors are the part that scales, but the
	// variants, rungs, flags and S3 URIs around them are not free, and they are
	// not modelled here.
	sfnInputLimitBytes = 262144
	sfnInputBudget     = sfnInputLimitBytes * 4 / 5
)

// isFixedChunkDuration reports whether the job asked for a specific chunk size,
// as opposed to "dynamic" (sized by budgetedChunkTarget, which already fits) or
// "whole" (one chunk per variant). Only a fixed value reaches the guard, and
// extracting the predicate is what lets a test check the real condition rather
// than restate the three strings.
func isFixedChunkDuration(cfg string) bool {
	return cfg != "" && cfg != "dynamic" && cfg != "whole"
}

// chunkPlanFits reports whether a whole-job plan clears both AWS limits, and
// says which one it fails.
//
// chunks is counted from the SAME planChunks the build loop calls, and bytes
// from the SAME marshalled chunkSpan that ships — an arithmetic estimate is
// wrong in both directions here, because the grid, the runt-tail fold and the
// 12s quantum all move the count.
func chunkPlanFits(chunks, bytes int) (ok bool, why string) {
	events := chunks * sfnEventsPerChunk
	switch {
	case events > sfnHistoryLimit && bytes > sfnInputBudget:
		return false, fmt.Sprintf(
			"%d chunks: ~%d history events (limit %d) and ~%d KB of execution input (limit %d KB)",
			chunks, events, sfnHistoryLimit, bytes/1024, sfnInputLimitBytes/1024)
	case events > sfnHistoryLimit:
		return false, fmt.Sprintf(
			"%d chunks: ~%d history events against a %d limit — the run would die PART WAY THROUGH, "+
				"after spot capacity has been launched",
			chunks, events, sfnHistoryLimit)
	case bytes > sfnInputBudget:
		return false, fmt.Sprintf(
			"%d chunks: ~%d KB of execution input against a %d KB limit — StartExecution would reject it",
			chunks, bytes/1024, sfnInputLimitBytes/1024)
	}
	return true, ""
}

// fixedChunkAdvice suggests the smallest whole-second fixed duration that would
// fit, so the error says what to do rather than only what is wrong. Returns ""
// when nothing sensible fits, in which case dynamic is the only answer.
func fixedChunkAdvice(planned []plannedVariant, clipS, segS float64) string {
	for _, try := range []float64{30, 60, 90, 120, 180, 240} {
		n := 0
		for range planned {
			n += len(planChunks(clipS, try, segS))
		}
		if n*sfnEventsPerChunk <= sfnHistoryLimit {
			return fmt.Sprintf("%.0f", try)
		}
	}
	return ""
}
