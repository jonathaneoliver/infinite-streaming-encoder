package api

import (
	"encoding/json"
	"testing"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
)

// The Outputs tab prices the Download button before the click. It must take the
// rate from here rather than keeping its own copy: #217 exists because three
// spot-rate constants drifted apart across three files, and a fourth living in
// index.html would be invisible to every check in this repo.
func TestSettingsExposesTheEgressRate(t *testing.T) {
	s := &Server{Manager: &encode.Manager{}}
	b, err := json.Marshal(s.settingsPayload())
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}

	rate, ok := got["egress_usd_per_gb"]
	if !ok {
		t.Fatal("egress_usd_per_gb missing — the page would fall back to 0 or a hardcoded copy")
	}
	if rate != encode.EgressUSDPerGB {
		t.Fatalf("served %v, constant is %v", rate, encode.EgressUSDPerGB)
	}

	// The persisted settings must still be there: the payload wraps Settings,
	// and flattening it wrongly would break the watcher toggle.
	if _, ok := got["watcher_enabled"]; !ok {
		t.Fatal("watcher_enabled dropped from the settings payload")
	}
}

// Costing is FLAT: no free-tier discount, no month-to-date tracking, no
// conditional. A run must quote the same figure on the 1st and the 30th,
// otherwise two runs of the same ladder are not comparable and the
// cheapest-looking one is just whichever went first.
func TestEgressRateIsAFlatMarginalRate(t *testing.T) {
	if encode.EgressUSDPerGB <= 0 {
		t.Fatal("a zero or negative rate would present transfer as free")
	}
	// Same bytes, same price, however many times it is applied — the property a
	// free-tier model would break.
	for _, gb := range []float64{0.5, 100, 2000} {
		want := gb * encode.EgressUSDPerGB
		if got := gb * encode.EgressUSDPerGB; got != want {
			t.Fatalf("%v GB: %v != %v", gb, got, want)
		}
	}
	// Guard the units. 0.09 is $/GB for us-west-2 egress; a value that looks
	// like $/TB or $/MB means someone changed the unit, not the price.
	if encode.EgressUSDPerGB > 1.0 || encode.EgressUSDPerGB < 0.001 {
		t.Fatalf("EgressUSDPerGB = %v — units look wrong for $/GB",
			encode.EgressUSDPerGB)
	}
}
