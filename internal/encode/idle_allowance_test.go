package encode

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// #237: the pre-flight estimate priced ALLOCATED vCPU-time while the finished
// run was billed MACHINE time, so the same application quoted ~60% of what it
// then reported — systematically, on every cloud run.
//
// These pin the one term that reconciles them (machine = allocated / (1 - idle))
// and the rules the measurement has to follow to be worth trusting. The
// estimate-vs-report agreement is the point; everything else here protects it.

// managerWithSamples builds a Manager whose rolling run log contains exactly
// these samples, so the idle reader can be exercised without a cloud encode.
func managerWithSamples(t *testing.T, samples []RunSample) *Manager {
	t.Helper()
	dir := t.TempDir()
	m := &Manager{
		TmpDir:  dir,
		Ladders: LoadLadderStore(filepath.Join(dir, "ladders.json")),
		Speeds:  LoadEncodeSpeedStore(filepath.Join(dir, "speeds.json")),
	}
	if samples != nil {
		data, err := json.Marshal(samples)
		if err != nil {
			t.Fatalf("marshal samples: %v", err)
		}
		if err := os.WriteFile(m.runSamplesPath(), data, 0644); err != nil {
			t.Fatalf("write samples: %v", err)
		}
	}
	return m
}

// sample is a non-overlapping run at the given idle percent. Spans are laid out
// back to back off the index so no two samples of the same batch overlap.
func sample(i int, idlePct float64) RunSample {
	start := int64(1_000_000 + i*10_000)
	return RunSample{
		TS: start + 5_000, IdlePct: idlePct, MachineVCPUHours: 10,
		AllocatedVCPUHours: 10 * (1 - idlePct/100),
		StartedAt:          start, EndedAt: start + 5_000,
	}
}

func TestIdleAllowanceFallsBackToTheStatedAssumption(t *testing.T) {
	// No history at all. The allowance must be the documented constant AND
	// declare itself unmeasured — a UI that cannot tell an assumption from an
	// observation is the defect this fixes, not the fix.
	m := managerWithSamples(t, nil)
	got, runs, measured := m.fleetIdleFraction()
	if measured {
		t.Fatal("no samples, yet the allowance claims to be measured")
	}
	if runs != 0 {
		t.Fatalf("runs = %d, want 0", runs)
	}
	if got != assumedFleetIdleFraction {
		t.Fatalf("fraction = %v, want the stated assumption %v", got, assumedFleetIdleFraction)
	}
}

func TestIdleAllowanceIgnoresRunsWithNoRentalMeasurement(t *testing.T) {
	// A local run, or a cloud run whose hosts never resolved, carries no rental
	// figure. Treating a zero as "0% idle" would drag the median towards a
	// number nobody measured and quietly restore the undercount.
	m := managerWithSamples(t, []RunSample{
		{TS: 1, SpotUSD: 1}, // no IdlePct, no MachineVCPUHours
		{TS: 2, IdlePct: 41, MachineVCPUHours: 0},
	})
	got, runs, measured := m.fleetIdleFraction()
	if measured || runs != 0 || got != assumedFleetIdleFraction {
		t.Fatalf("unusable samples were counted: %v / %d runs / measured=%v", got, runs, measured)
	}
}

func TestIdleAllowanceIsTheMedianNotTheMean(t *testing.T) {
	// One pathological run must not set the price of every future estimate.
	// Mean of these is ~50%; median is 40%.
	m := managerWithSamples(t, []RunSample{
		sample(0, 38), sample(1, 40), sample(2, 42), sample(3, 80),
	})
	got, runs, measured := m.fleetIdleFraction()
	if !measured {
		t.Fatal("four usable samples, yet not reported as measured")
	}
	if runs != 4 {
		t.Fatalf("runs = %d, want 4", runs)
	}
	if got < 0.40-1e-9 || got > 0.41+1e-9 {
		t.Fatalf("fraction = %v, want the median ~0.41 (mean would be ~0.50)", got)
	}
}

func TestIdleAllowanceExcludesOverlappingRuns(t *testing.T) {
	// _emit_machine_rental attributes a concurrent run's time on a shared
	// instance to whichever run is reporting, so an overlapping sample reads
	// high for a reason that says nothing about how the fleet packs one run.
	// Counting those turns a systematic undercount into a systematic overcount.
	overlapA := RunSample{TS: 30, IdlePct: 90, MachineVCPUHours: 10, StartedAt: 100, EndedAt: 200}
	overlapB := RunSample{TS: 31, IdlePct: 88, MachineVCPUHours: 10, StartedAt: 150, EndedAt: 250}
	solo := RunSample{TS: 32, IdlePct: 40, MachineVCPUHours: 10, StartedAt: 400, EndedAt: 500}

	m := managerWithSamples(t, []RunSample{overlapA, overlapB, solo})
	got, runs, measured := m.fleetIdleFraction()
	if !measured {
		t.Fatal("one clean sample survives, so the allowance is measured")
	}
	if runs != 1 {
		t.Fatalf("runs = %d, want 1 — both overlapping runs must be dropped", runs)
	}
	if got < 0.399 || got > 0.401 {
		t.Fatalf("fraction = %v, want the solo run's 0.40", got)
	}
}

func TestIdleAllowanceKeepsSamplesWithNoRecordedSpan(t *testing.T) {
	// Records written before the span was persisted have no start/end. Dropping
	// them as "might have overlapped" would strand the allowance on its
	// assumption for another ten runs, so they count.
	m := managerWithSamples(t, []RunSample{
		{TS: 1, IdlePct: 44, MachineVCPUHours: 10},
		{TS: 2, IdlePct: 44, MachineVCPUHours: 10},
	})
	got, runs, measured := m.fleetIdleFraction()
	if !measured || runs != 2 {
		t.Fatalf("spanless samples were dropped: %d runs, measured=%v", runs, measured)
	}
	if got < 0.439 || got > 0.441 {
		t.Fatalf("fraction = %v, want 0.44", got)
	}
}

func TestIdleAllowanceIsClampedAndBounded(t *testing.T) {
	// A degenerate sample — a run whose instances were almost entirely someone
	// else's — divides the estimate towards infinity. Clamp keeps a bad
	// measurement merely wrong rather than catastrophic.
	m := managerWithSamples(t, []RunSample{sample(0, 99.9), sample(1, 99.9)})
	got, _, _ := m.fleetIdleFraction()
	if got > maxFleetIdleFraction+1e-9 {
		t.Fatalf("fraction = %v, want clamped to %v", got, maxFleetIdleFraction)
	}
}

func TestIdleAllowanceUsesOnlyRecentRuns(t *testing.T) {
	// A fleet-packing improvement has to show up without anyone editing a
	// constant, so old runs must age out of the window.
	var samples []RunSample
	for i := 0; i < fleetIdleRuns*2; i++ {
		idle := 80.0 // ancient, badly-packed history
		if i >= fleetIdleRuns {
			idle = 20.0 // the recent, better-packed runs
		}
		samples = append(samples, sample(i, idle))
	}
	m := managerWithSamples(t, samples)
	got, runs, _ := m.fleetIdleFraction()
	if runs != fleetIdleRuns {
		t.Fatalf("runs = %d, want the %d-run window", runs, fleetIdleRuns)
	}
	if got < 0.199 || got > 0.201 {
		t.Fatalf("fraction = %v, want the recent 0.20 — old runs did not age out", got)
	}
}

// TestEstimateAndReportShareOneBasis is the acceptance criterion from #237.
//
// The finished run prices MACHINE vCPU-hours (_emit_cost_summary, on the
// rental); the estimator predicts ALLOCATED vCPU-hours. They reconcile through
// exactly one term, machine = allocated / (1 - idle). This pins that the
// estimator applies it — and applies it as a real function of the measured
// idle, not as a fixed fudge that happens to look right at one value.
//
// Deliberately NOT written as `allocated := got * (1-idle)` and then comparing
// back: that recovers the input from the output and passes no matter what the
// code does. Two DIFFERENT measured fleets are compared instead, so the scaling
// has to actually track the measurement.
func TestEstimateAndReportShareOneBasis(t *testing.T) {
	cfg := JobConfig{Codec: "h264", Ladder: DefaultLadderName}
	const w, fps, dur = 3840, 30, 600.0

	idle40 := managerWithSamples(t, []RunSample{sample(0, 40), sample(1, 40)})
	idle20 := managerWithSamples(t, []RunSample{sample(0, 20), sample(1, 20)})
	noHistory := managerWithSamples(t, nil)

	got40, ond40, f40, runs, measured := idle40.projectCloudCostDetail(cfg, w, fps, dur)
	got20, _, f20, _, _ := idle20.projectCloudCostDetail(cfg, w, fps, dur)
	assumed, _, fAssumed, _, wasMeasured := noHistory.projectCloudCostDetail(cfg, w, fps, dur)
	if got40 <= 0 || got20 <= 0 {
		t.Skip("seed ladder or speed model unavailable")
	}
	if !measured || runs != 2 {
		t.Fatalf("expected a measured allowance over 2 runs, got measured=%v runs=%d", measured, runs)
	}
	if f40 < 0.399 || f40 > 0.401 || f20 < 0.199 || f20 > 0.201 {
		t.Fatalf("idle fractions not read back: %v / %v", f40, f20)
	}

	// A fleet that wastes 40% must be quoted more than one that wastes 20%, in
	// exactly the ratio the two measurements imply: (1-0.20)/(1-0.40) = 1.3333.
	// A hardcoded multiplier — or no multiplier — fails here.
	wantRatio := (1 - f20) / (1 - f40)
	if ratio := got40 / got20; ratio < wantRatio*0.999 || ratio > wantRatio*1.001 {
		t.Fatalf("cost ratio between a 40%%-idle and a 20%%-idle fleet = %v, want %v", ratio, wantRatio)
	}

	// Scaling is applied to on-demand identically — the two rates must not drift
	// onto different bases, which is the #217 failure this repo already paid for.
	if r := ond40 / got40; r < awsOndemandVCPUHourUSD/awsSpotVCPUHourUSD*0.999 ||
		r > awsOndemandVCPUHourUSD/awsSpotVCPUHourUSD*1.001 {
		t.Fatalf("on-demand/spot = %v, want the rate ratio %v", r, awsOndemandVCPUHourUSD/awsSpotVCPUHourUSD)
	}

	// With no history the quote stands on the stated assumption, and says so.
	// Equal to the 40% fleet only because the assumption IS 40% — derived from
	// the constant, so editing it moves both together.
	if wasMeasured {
		t.Fatal("no samples, yet the estimate claims a measured allowance")
	}
	if fAssumed != assumedFleetIdleFraction {
		t.Fatalf("assumed fraction = %v, want %v", fAssumed, assumedFleetIdleFraction)
	}
	if want := got40 * (1 - f40) / (1 - fAssumed); assumed < want*0.999 || assumed > want*1.001 {
		t.Fatalf("assumption-based quote %v does not follow the same formula (want %v)", assumed, want)
	}
}

// TestPersistedSampleCarriesTheIdleFields closes the loop: the reader above is
// worthless if the writer drops the fields. This is the seam a rename would
// break silently, since inventory.py reads the same file by name.
func TestPersistedSampleCarriesTheIdleFields(t *testing.T) {
	m := managerWithSamples(t, nil)
	end := time.Unix(5_000, 0)
	job := &Job{
		ID: "1", StartedAt: time.Unix(1_000, 0), EndedAt: &end,
		IdlePct: 41.5, MachineVCPUHours: 11.85, AllocatedVCPUHours: 7.29,
		EncodeTotalS: 120,
	}
	m.persistSpotSample(job)

	got := m.readRunSamples()
	if len(got) != 1 {
		t.Fatalf("got %d samples, want 1", len(got))
	}
	s := got[0]
	if s.IdlePct != 41.5 || s.MachineVCPUHours != 11.85 || s.AllocatedVCPUHours != 7.29 {
		t.Fatalf("rental fields not round-tripped: %+v", s)
	}
	if s.StartedAt != 1_000 || s.EndedAt != 5_000 {
		t.Fatalf("run span not recorded (%d..%d) — overlap exclusion depends on it", s.StartedAt, s.EndedAt)
	}

	// The on-disk names are a cross-language contract with inventory.py's
	// _spot_and_reclaim_stats, which reads by name and silently ignores what it
	// does not recognise. A rename zeroes that panel with no error anywhere.
	raw, err := os.ReadFile(m.runSamplesPath())
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	var docs []map[string]any
	if err := json.Unmarshal(raw, &docs); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, k := range []string{"ts", "spot_usd", "ondemand_usd", "saved_usd", "idle_pct"} {
		if _, ok := docs[0][k]; !ok {
			t.Fatalf("persisted sample is missing %q: %v", k, docs[0])
		}
	}
}

// A job carrying only rental data — no spot cost, no reclaim — must still be
// recorded, or the very first cloud runs never seed the allowance.
func TestPersistKeepsARentalOnlyRun(t *testing.T) {
	m := managerWithSamples(t, nil)
	m.persistSpotSample(&Job{ID: "1", IdlePct: 38})
	if got := m.readRunSamples(); len(got) != 1 {
		t.Fatalf("rental-only run was dropped: %d samples", len(got))
	}
}
