package encode

import (
	"math"
	"os"
	"regexp"
	"strconv"
	"testing"
)

// Money is summed in float64, so sums like 0.75+0.18 are not exactly
// representable. Compare to a tolerance well below a cent — asserting exact
// equality here tests IEEE-754, not the cost logic.
func closeTo(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

// #217: cost reporting quoted 21% of a real bill ($5.47 modelled against $26.48
// billed) because it summed compute and nothing else. These pin the two ways
// that silently regresses: a marker that stops parsing, and rate constants that
// drift apart across files and languages.

func TestCostMarkerParsesEgressFields(t *testing.T) {
	j := &Job{}
	line := "[[ENCODER-COST exec=e1 spot_usd=0.3000 ondemand_usd=0.7800 " +
		"saved_usd=0.4800 vcpu_hours=21.43 egress_gb=2.635 egress_usd=0.2371 " +
		"egress_avoided_gb=0.000 total_usd=0.5371]]"
	if !j.parseMarker(line) {
		t.Fatal("marker not consumed")
	}
	if j.SpotUSD != 0.30 {
		t.Fatalf("spot = %v", j.SpotUSD)
	}
	if j.EgressGB != 2.635 || j.EgressUSD != 0.2371 {
		t.Fatalf("egress = %v GB / $%v", j.EgressGB, j.EgressUSD)
	}
	// The headline number is the whole point of the issue.
	if !closeTo(j.TotalUSD, 0.30+0.2371) {
		t.Fatalf("total = %v, want %v", j.TotalUSD, 0.30+0.2371)
	}
}

// The old regex listed every field in order, so appending one would have made it
// match NOTHING — and a non-matching cost marker fails silently, reporting $0
// rather than erroring. Key=value parsing must tolerate both directions.
func TestCostMarkerToleratesFieldSetChanges(t *testing.T) {
	t.Run("older worker, no egress fields", func(t *testing.T) {
		j := &Job{}
		if !j.parseMarker("[[ENCODER-COST exec=e1 spot_usd=0.5 ondemand_usd=1.3 " +
			"saved_usd=0.8 vcpu_hours=10]]") {
			t.Fatal("legacy marker not consumed")
		}
		if j.SpotUSD != 0.5 {
			t.Fatalf("legacy spot lost: %v", j.SpotUSD)
		}
		if j.EgressUSD != 0 || !closeTo(j.TotalUSD, 0.5) {
			t.Fatalf("absent egress should be zero, got %v / total %v",
				j.EgressUSD, j.TotalUSD)
		}
	})
	t.Run("future worker, unknown extra field", func(t *testing.T) {
		j := &Job{}
		if !j.parseMarker("[[ENCODER-COST exec=e1 spot_usd=0.5 requests_usd=0.01 " +
			"ondemand_usd=1.3 saved_usd=0.8 vcpu_hours=10 egress_usd=0.2]]") {
			t.Fatal("marker with an unknown field not consumed")
		}
		if j.SpotUSD != 0.5 || j.EgressUSD != 0.2 {
			t.Fatalf("known fields lost when an unknown one is present: %v / %v",
				j.SpotUSD, j.EgressUSD)
		}
	})
}

func TestCostIsKeyedByExecNotAccumulated(t *testing.T) {
	// A re-emitted marker (reattach, drain) must REPLACE that execution's
	// contribution. Accumulating would inflate cost every time a run was polled
	// twice — and the number would still look plausible.
	j := &Job{}
	line := "[[ENCODER-COST exec=e1 spot_usd=0.5 ondemand_usd=1.0 saved_usd=0.5 " +
		"vcpu_hours=10 egress_gb=1.0 egress_usd=0.09]]"
	j.parseMarker(line)
	j.parseMarker(line)
	if !closeTo(j.SpotUSD, 0.5) || !closeTo(j.EgressUSD, 0.09) {
		t.Fatalf("re-emitted marker double-counted: spot=%v egress=%v",
			j.SpotUSD, j.EgressUSD)
	}
	// A DIFFERENT execution does add.
	j.parseMarker("[[ENCODER-COST exec=e2 spot_usd=0.25 ondemand_usd=0.5 " +
		"saved_usd=0.25 vcpu_hours=5 egress_gb=1.0 egress_usd=0.09]]")
	if !closeTo(j.SpotUSD, 0.75) {
		t.Fatalf("second execution not summed: %v", j.SpotUSD)
	}
	if !closeTo(j.TotalUSD, 0.75+0.18) {
		t.Fatalf("total = %v, want %v", j.TotalUSD, 0.75+0.18)
	}
}

// Full-rate lines must reach the total. Several of these bill $0.00 on the real
// invoice today purely because a free allowance has not run out — Step Functions
// is already at 4,000/4,000, SQS at 29% — so a total that only summed what AWS
// currently charges would drift the moment an allowance was crossed.
func TestFullRateLinesAreIncludedInTheTotal(t *testing.T) {
	j := &Job{}
	if !j.parseMarker("[[ENCODER-COST exec=e1 spot_usd=0.30 ondemand_usd=0.78 " +
		"saved_usd=0.48 vcpu_hours=21 egress_gb=2.6 egress_usd=0.2340 " +
		"sfn_transitions=812 sfn_usd=0.0203 s3_get=1486 s3_request_usd=0.0006 " +
		"storage_usd=0.0004 total_usd=0.5553 " +
		"unmodelled=s3-put,cloudwatch-logs,sqs]]") {
		t.Fatal("marker not consumed")
	}
	if !closeTo(j.SfnUSD, 0.0203) || !closeTo(j.RequestUSD, 0.0006) ||
		!closeTo(j.StorageUSD, 0.0004) {
		t.Fatalf("full-rate lines lost: sfn=%v req=%v store=%v",
			j.SfnUSD, j.RequestUSD, j.StorageUSD)
	}
	want := 0.30 + 0.2340 + 0.0203 + 0.0006 + 0.0004
	if !closeTo(j.TotalUSD, want) {
		t.Fatalf("total = %v, want %v — a line was dropped from the sum",
			j.TotalUSD, want)
	}
	// The gap must be carried, not swallowed: a partial total that looks
	// complete is exactly what #217 was filed about.
	if j.CostUnmodelled == "" {
		t.Fatal("unmodelled list dropped — the UI could not disclose the gap")
	}
}

// Go and Python cannot share a constant, so the only defence against them
// drifting is a test that reads the other language's file. #217 exists because
// three copies of the spot rate disagreed (0.011 / 0.013 / 0.0155).
func TestRatesMatchThePythonDefinitions(t *testing.T) {
	src, err := os.ReadFile("../../scripts/infinite_streaming_encoder/pricing.py")
	if err != nil {
		t.Skipf("pricing.py unreadable: %v", err)
	}
	pyVal := func(name string) float64 {
		m := regexp.MustCompile(name + `\s*=\s*([0-9.]+)`).FindSubmatch(src)
		if m == nil {
			t.Fatalf("%s not found in pricing.py", name)
		}
		v, err := strconv.ParseFloat(string(m[1]), 64)
		if err != nil {
			t.Fatalf("%s unparseable: %v", name, err)
		}
		return v
	}
	for _, tc := range []struct {
		py  string
		go_ float64
	}{
		{"AWS_SPOT_VCPU_HR", awsSpotVCPUHourUSD},
		{"AWS_ONDEMAND_VCPU_HR", awsOndemandVCPUHourUSD},
		{"EGRESS_USD_PER_GB", EgressUSDPerGB},
	} {
		if got := pyVal(tc.py); got != tc.go_ {
			t.Errorf("%s: python=%v go=%v — change both together", tc.py, got, tc.go_)
		}
	}
}
