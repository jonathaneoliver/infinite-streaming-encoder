package encode

import "testing"

// The model, checked against the run that exposed the bug (#380).
//
// Job 1787181402465, 19 Aug 2026 — h264, apple-uniq-live-xs, 12 rungs, a 5.6
// minute clip on four c7g boxes. Every figure here is from that run's own log
// and its spot_samples.json entry, not from a fixture invented to pass:
//
//	machine 17.88 vCPU-hr   allocated 14.56   fleet 48 vCPU (16+16+8+8)
//	idle pre 33-38s, idle post 76-87s per instance   busy 91-92%
//
// It cost $0.25 of spot compute. The shipped estimate said $1.27, next to a
// line claiming MediaConvert would charge $1.00 — so the app advertised itself
// as more expensive than three commercial services while being half the price
// of the cheapest.
//
// This is a REFERENCE test: it fails if the arithmetic drifts away from a real
// measurement, which is the only thing that can tell us the shape is still
// right. Replace the numbers only with numbers from another real run.
func TestModelReproducesTheReferenceRun(t *testing.T) {
	const (
		machine   = 17.88
		allocated = 14.56
		fleetVCPU = 48.0
	)
	// Learned from that run alone: the edge is the fleet-weighted mean of
	// (pre + post), and packing is what is left once the edge is removed.
	edgeHours := fleetVCPU * 116.0 / 3600.0
	f := fleetOverhead{
		EdgeSeconds: 116.0,
		Packing:     allocated / (machine - edgeHours),
		Measured:    true, Runs: 1,
	}

	got := f.machineVCPUHours(allocated, fleetVCPU)
	if diff := got - machine; diff < -0.05 || diff > 0.05 {
		t.Fatalf("predicted %v vCPU-hr against a measured %v", got, machine)
	}

	// The derived allowance must match what the run reported (18.6%), because
	// that number is shown next to the Encode button and #237's rule is that it
	// is never folded in silently.
	if idle := f.idleFraction(allocated, fleetVCPU); idle < 0.17 || idle > 0.20 {
		t.Fatalf("derived idle %v, want ~0.186 as the run reported", idle)
	}

	// And the shape has to hold at the other end of the range, on the same
	// terms, or it is just the old bug with different constants. A smoke run —
	// one 16-vCPU box, ~0.08 allocated vCPU-hr — really is mostly overhead.
	if idle := f.idleFraction(0.08, 16); idle < 0.80 {
		t.Fatalf("a smoke run should price as mostly overhead, got idle=%v", idle)
	}
}

// The ceiling is what the environment will launch, not what the ladder asks
// for. One wave of a 12-rung h264 ladder reserves 96 vCPU against a 48 vCPU
// environment; predicting 96 would price twice the boot overhead the run can
// possibly incur.
func TestPredictedFleetIsCappedByTheComputeEnvironment(t *testing.T) {
	if got := predictedFleetVCPU(96, 48); got != 48 {
		t.Fatalf("wave of 96 against a 48 cap = %v, want 48", got)
	}
	if got := predictedFleetVCPU(16, 48); got != 16 {
		t.Fatalf("a run smaller than the cap must not be inflated to it: %v", got)
	}
	// No cap known: the wave stands. Inventing one would silently under-price
	// an environment someone has scaled up.
	if got := predictedFleetVCPU(96, 0); got != 96 {
		t.Fatalf("uncapped wave = %v, want 96", got)
	}
}
