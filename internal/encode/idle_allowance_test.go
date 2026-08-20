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
// #380: the term that reconciled them was a single idle FRACTION, and a
// fraction is the wrong shape. Overhead is roughly constant per instance, so it
// is 90%+ of a three-minute smoke run and 19% of a twenty-two-minute encode —
// idle% describes RUN SIZE, not the fleet. Averaging across sizes described
// smoke tests, because smoke tests are the numerous kind, and the app quoted
// $1.59 for a run that cost $0.56 while claiming MediaConvert would charge
// $1.00.
//
// These pin the replacement: machine = allocated/packing + fleetVCPU*edge/3600.
// The estimate-vs-report agreement is still the point; what changed is that it
// now has to hold at more than one size.

// managerWithSamples builds a Manager whose rolling run log contains exactly
// these samples, so the model can be exercised without a cloud encode.
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

// shaped is a non-overlapping run that recorded its fleet shape. Spans are laid
// out back to back off the index so no two samples of a batch overlap.
//
// The caller gives the two things being learned — the fleet's width and the
// per-instance edge — and the machine figure follows from them, so a sample can
// never describe a rental its own terms do not add up to.
func shaped(i int, fleetVCPU, allocated, edgeSeconds, packing float64) RunSample {
	start := int64(1_000_000 + i*10_000)
	edgeHours := fleetVCPU * edgeSeconds / 3600.0
	machine := allocated/packing + edgeHours
	return RunSample{
		TS: start + 5_000, StartedAt: start, EndedAt: start + 5_000,
		MachineVCPUHours: machine, AllocatedVCPUHours: allocated,
		IdlePct:   100 * (1 - allocated/machine),
		FleetVCPU: fleetVCPU, EdgeVCPUHours: edgeHours,
	}
}

// legacy is a sample written before #380 — rental measured, fleet size not.
func legacy(i int, idlePct float64) RunSample {
	s := shaped(i, 16, 1, 100, 0.9)
	s.FleetVCPU, s.EdgeVCPUHours, s.IdlePct = 0, 0, idlePct
	return s
}

func TestOverheadModelFallsBackToStatedAssumptions(t *testing.T) {
	// No history at all. Both terms must be the documented constants AND the
	// model must declare itself unmeasured — a UI that cannot tell an
	// assumption from an observation is the defect this fixes, not the fix.
	f := managerWithSamples(t, nil).fleetOverheadModel()
	if f.Measured || f.Runs != 0 {
		t.Fatalf("no samples, yet the model claims measurement: %+v", f)
	}
	if f.EdgeSeconds != assumedEdgeSeconds || f.Packing != assumedPackingEfficiency {
		t.Fatalf("fallback terms = %v/%v, want %v/%v",
			f.EdgeSeconds, f.Packing, assumedEdgeSeconds, assumedPackingEfficiency)
	}
}

func TestOverheadModelSkipsSamplesWithNoFleetSize(t *testing.T) {
	// Samples written before #380 recorded rental but not the fleet width, and
	// the edge term divides by exactly that. There is no way to recover it
	// afterwards, so they must be SKIPPED rather than guessed at — a guessed
	// divisor teaches the model an overhead nobody measured.
	m := managerWithSamples(t, []RunSample{legacy(0, 91), legacy(1, 94), legacy(2, 88)})
	f := m.fleetOverheadModel()
	if f.Measured {
		t.Fatalf("pre-#380 samples were treated as measurements: %+v", f)
	}
	if f.EdgeSeconds != assumedEdgeSeconds {
		t.Fatalf("edge = %v, want the assumption %v", f.EdgeSeconds, assumedEdgeSeconds)
	}
}

func TestOverheadModelLearnsBothTerms(t *testing.T) {
	m := managerWithSamples(t, []RunSample{
		shaped(0, 48, 14, 120, 0.90),
		shaped(1, 48, 12, 120, 0.90),
		shaped(2, 32, 6, 120, 0.90),
	})
	f := m.fleetOverheadModel()
	if !f.Measured || f.Runs != 3 {
		t.Fatalf("expected 3 measured runs, got %+v", f)
	}
	if f.EdgeSeconds < 119.9 || f.EdgeSeconds > 120.1 {
		t.Fatalf("edge = %v, want 120", f.EdgeSeconds)
	}
	if f.Packing < 0.899 || f.Packing > 0.901 {
		t.Fatalf("packing = %v, want 0.90", f.Packing)
	}
}

// THE REGRESSION. A model learned entirely from smoke runs must still price a
// real encode correctly. Under the old fraction this was a 13x overcharge; the
// whole reason the shape changed is that these two sizes share one fleet and
// must share one model.
func TestSmokeRunsDoNotPoisonTheEstimateForRealOnes(t *testing.T) {
	var smoke []RunSample
	for i := 0; i < 9; i++ {
		// ~0.08 vCPU-hr of work on one 16-vCPU box: 91% idle, and correctly so.
		smoke = append(smoke, shaped(i, 16, 0.08, 116, 0.91))
	}
	m := managerWithSamples(t, smoke)
	f := m.fleetOverheadModel()

	// Sanity: those samples really are the pathological ones.
	if idle := f.idleFraction(0.08, 16); idle < 0.80 {
		t.Fatalf("a smoke run should still be mostly overhead, got idle=%v", idle)
	}

	// The real run: 14.56 allocated vCPU-hours across a 48-vCPU fleet, which
	// actually rented 17.88 (#380's measurement).
	got := f.machineVCPUHours(14.56, 48)
	if got < 17.0 || got > 18.5 {
		t.Fatalf("real run priced at %v vCPU-hr, want ~17.9 (the measured rental)", got)
	}
	if idle := f.idleFraction(14.56, 48); idle < 0.10 || idle > 0.25 {
		t.Fatalf("derived idle for the real run = %v, want ~0.19", idle)
	}
	// The old model's answer, kept as a number rather than a memory: a median
	// of these samples' idle_pct is ~0.91, which divides to 11x the truth.
	if bad := 14.56 / (1 - 0.91); bad < 100 {
		t.Fatalf("the old model is supposed to be catastrophic here, got %v", bad)
	}
}

func TestOverheadModelIsTheMedianNotTheMean(t *testing.T) {
	// One pathological run must not set the price of every estimate — the
	// reason the median was chosen in the first place, and still true.
	m := managerWithSamples(t, []RunSample{
		shaped(0, 48, 10, 100, 0.9),
		shaped(1, 48, 10, 100, 0.9),
		shaped(2, 48, 10, 4000, 0.9),
	})
	if f := m.fleetOverheadModel(); f.EdgeSeconds < 99.9 || f.EdgeSeconds > 100.1 {
		t.Fatalf("edge = %v, want the median 100 rather than the mean", f.EdgeSeconds)
	}
}

func TestOverheadModelExcludesOverlappingRuns(t *testing.T) {
	// _emit_machine_rental counts a concurrent run's time on a shared instance
	// as this run's overhead, so an overlapping sample reads high for a reason
	// that says nothing about how the fleet packs one run.
	a := shaped(0, 48, 10, 100, 0.9)
	b := shaped(1, 48, 10, 4000, 0.9)
	b.StartedAt, b.EndedAt = a.StartedAt+1, a.EndedAt+1 // overlaps a
	m := managerWithSamples(t, []RunSample{a, b})
	f := m.fleetOverheadModel()
	if f.Runs != 0 {
		t.Fatalf("overlapping pair should leave nothing usable, got %d runs", f.Runs)
	}
}

func TestOverheadModelClampsPacking(t *testing.T) {
	// Packing divides. A degenerate sample near zero would send every estimate
	// towards infinity — the hazard maxFleetIdleFraction used to guard, in the
	// term that replaced it.
	m := managerWithSamples(t, []RunSample{shaped(0, 48, 0.001, 100, 0.01)})
	if f := m.fleetOverheadModel(); f.Packing < minPackingEfficiency {
		t.Fatalf("packing %v below the floor %v", f.Packing, minPackingEfficiency)
	}
	// And the floor has to hold at the point of use, not only at the source.
	f := fleetOverhead{EdgeSeconds: 100, Packing: 0}
	if got := f.machineVCPUHours(10, 48); got > 10/minPackingEfficiency+2 {
		t.Fatalf("a zero packing term was not floored: %v", got)
	}
}

func TestOverheadModelUsesOnlyRecentRuns(t *testing.T) {
	var samples []RunSample
	for i := 0; i < fleetIdleRuns+5; i++ {
		samples = append(samples, shaped(i, 48, 10, 4000, 0.9)) // stale, absurd
	}
	for i := 0; i < fleetIdleRuns; i++ {
		samples = append(samples, shaped(100+i, 48, 10, 100, 0.9)) // recent
	}
	f := managerWithSamples(t, samples).fleetOverheadModel()
	if f.Runs != fleetIdleRuns {
		t.Fatalf("window = %d runs, want %d", f.Runs, fleetIdleRuns)
	}
	if f.EdgeSeconds > 101 {
		t.Fatalf("edge = %v — stale runs are still in the window", f.EdgeSeconds)
	}
}

// The estimate and the finished run must be computed on one basis. That was
// #237's finding and it survives the change of model: what the estimator
// predicts as rental is what the run reports as rental.
func TestEstimateAndReportShareOneBasis(t *testing.T) {
	cfg := JobConfig{Codec: "h264", Ladder: DefaultLadderName}
	const w, fps, dur = 3840, 30, 600.0

	tight := managerWithSamples(t, []RunSample{
		shaped(0, 48, 14, 60, 0.95), shaped(1, 48, 14, 60, 0.95)})
	loose := managerWithSamples(t, []RunSample{
		shaped(0, 48, 14, 600, 0.70), shaped(1, 48, 14, 600, 0.70)})

	spotTight, ondTight, idleTight, runs, measured := tight.projectCloudCostDetail(cfg, w, fps, dur)
	spotLoose, _, idleLoose, _, _ := loose.projectCloudCostDetail(cfg, w, fps, dur)
	if spotTight <= 0 || spotLoose <= 0 {
		t.Skip("seed ladder or speed model unavailable")
	}
	if !measured || runs != 2 {
		t.Fatalf("expected a measured model over 2 runs, got measured=%v runs=%d", measured, runs)
	}

	// A fleet that boots slowly and packs badly must be quoted MORE. The
	// direction is the assertion; the exact ratio depends on the seed ladder.
	if spotLoose <= spotTight {
		t.Fatalf("a wasteful fleet was quoted no more than a tight one: %v vs %v", spotLoose, spotTight)
	}
	if idleLoose <= idleTight {
		t.Fatalf("derived idle did not follow the model: %v vs %v", idleLoose, idleTight)
	}

	// Both rates must stay on one basis — the #217 failure this repo already
	// paid for, where three spot constants drifted apart in three files.
	if r := ondTight / spotTight; r < awsOndemandVCPUHourUSD/awsSpotVCPUHourUSD*0.999 ||
		r > awsOndemandVCPUHourUSD/awsSpotVCPUHourUSD*1.001 {
		t.Fatalf("on-demand/spot = %v, want the rate ratio %v", r,
			awsOndemandVCPUHourUSD/awsSpotVCPUHourUSD)
	}

	// The displayed allowance must be the one that priced the run. #237's rule
	// is that it is never folded in silently, which is worth nothing if the
	// number shown is not the number applied.
	if idleTight <= 0 || idleTight >= 1 {
		t.Fatalf("derived idle %v is not a fraction", idleTight)
	}
}

// TestPersistedSampleCarriesTheFleetShape closes the loop: the model is
// worthless if the writer drops the fields it learns from.
func TestPersistedSampleCarriesTheFleetShape(t *testing.T) {
	m := managerWithSamples(t, nil)
	end := time.Unix(5_000, 0)
	job := &Job{
		ID: "1", StartedAt: time.Unix(1_000, 0), EndedAt: &end,
		IdlePct: 41.5, MachineVCPUHours: 11.85, AllocatedVCPUHours: 7.29,
		EncodeTotalS: 120,
		Rentals: []MachineRental{
			{ID: "i-a", VCPUs: 16, LaunchedAt: 1_000, FirstJobAt: 1_040,
				LastJobAt: 4_900, EndedAt: 5_000},
			{ID: "i-b", VCPUs: 8, LaunchedAt: 1_000, FirstJobAt: 1_060,
				LastJobAt: 4_800, EndedAt: 5_000},
		},
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
		t.Fatalf("run span not recorded (%d..%d) — overlap exclusion depends on it",
			s.StartedAt, s.EndedAt)
	}
	if s.FleetVCPU != 24 {
		t.Fatalf("fleet width = %v, want 24 (16+8)", s.FleetVCPU)
	}
	// i-a: 40s before, 100s after, x16. i-b: 60s before, 200s after, x8.
	if want := (16*140.0 + 8*260.0) / 3600.0; s.EdgeVCPUHours < want*0.999 ||
		s.EdgeVCPUHours > want*1.001 {
		t.Fatalf("edge = %v vCPU-hr, want %v", s.EdgeVCPUHours, want)
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

// A box still alive at the end has no measurable tail, and a box that never
// reported its jobs has no measurable edge at all. Both must contribute their
// WIDTH — they were rented — without teaching the model an overhead nobody saw.
func TestFleetShapeCountsWidthButNotUnmeasuredEdges(t *testing.T) {
	fleet, edge := fleetShape([]MachineRental{
		{VCPUs: 16, LaunchedAt: 1_000, FirstJobAt: 1_100, LastJobAt: 4_000,
			EndedAt: 4_500, Alive: true}, // tail unknown: still running
		{VCPUs: 8, LaunchedAt: 1_000, EndedAt: 4_500}, // never reported a job
	})
	if fleet != 24 {
		t.Fatalf("fleet width = %v, want 24 — a rented box counts either way", fleet)
	}
	if want := 16 * 100.0 / 3600.0; edge < want*0.999 || edge > want*1.001 {
		t.Fatalf("edge = %v, want only the measured 100s pre-job slice (%v)", edge, want)
	}
}

// A job carrying only rental data — no spot cost, no reclaim — must still be
// recorded, or the very first cloud runs never seed the model.
func TestPersistKeepsARentalOnlyRun(t *testing.T) {
	m := managerWithSamples(t, nil)
	m.persistSpotSample(&Job{ID: "1", IdlePct: 38})
	if got := m.readRunSamples(); len(got) != 1 {
		t.Fatalf("rental-only run was dropped: %d samples", len(got))
	}
}
