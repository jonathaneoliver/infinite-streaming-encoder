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
