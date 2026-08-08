package encode

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// The work-vs-paid figures are written by Go and read by name in static/index.html.
// That is a cross-language contract with no compiler on either side: rename the
// tag and the panel silently shows nothing, because a missing key in JS is
// undefined, and `undefined > 0` is false. No error, just a stat that quietly
// stops appearing — the same shape as the other silent-omission bugs this
// codebase keeps finding. I wrote `machine_vcpu_h` in the JS against a
// `machine_vcpu_hours` tag while making this very change, so it is not
// hypothetical.
func TestRentalFieldsMatchThePageThatReadsThem(t *testing.T) {
	b, err := json.Marshal(RunEfficiency{
		MachineVCPUHours: 1.176, AllocatedVCPUHours: 0.045, IdlePct: 96.2,
		MaxVcpus: 96, EfficiencyPct: 0.89,
	})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	page, err := os.ReadFile("../../static/index.html")
	if err != nil {
		t.Skipf("page not readable from here: %v", err)
	}
	src := string(page)

	// Every key the panel needs must (a) be emitted and (b) be referenced by
	// that exact name in the page.
	for _, key := range []string{
		"machine_vcpu_hours", "allocated_vcpu_hours", "max_vcpus", "efficiency_pct",
	} {
		if _, ok := got[key]; !ok {
			t.Errorf("RunEfficiency does not emit %q — the panel reads it", key)
		}
		if !strings.Contains(src, "e."+key) {
			t.Errorf("static/index.html does not read e.%s — renamed on one side only?", key)
		}
	}

	// "the name appears somewhere" is too weak on its own: a PARTIAL rename
	// leaves some references right and some wrong, and the check above passes
	// on the strength of the ones that are right. It happened while writing
	// this test — two of three references were fixed and the third, followed by
	// `;` rather than `.` or a space, was not, and the suite stayed green.
	// So also reject the truncated spellings outright.
	for _, wrong := range []string{"e.machine_vcpu_h.", "e.machine_vcpu_h ", "e.machine_vcpu_h;",
		"e.allocated_vcpu_h.", "e.allocated_vcpu_h ", "e.allocated_vcpu_h;"} {
		if strings.Contains(src, wrong) {
			t.Errorf("static/index.html still references %q — the Go tag is the _hours form", wrong)
		}
	}

	// And the ratio the whole change exists to show must be computable.
	m, _ := got["machine_vcpu_hours"].(float64)
	a, _ := got["allocated_vcpu_hours"].(float64)
	if m <= 0 || a <= 0 {
		t.Fatalf("machine=%v allocated=%v — cannot form work-vs-paid", m, a)
	}
	if pct := 100 * a / m; pct < 3.5 || pct > 4.0 {
		t.Errorf("work-vs-paid = %.2f%%, want ~3.8%% for the pinned figures", pct)
	}
}

// idle_pct is the complement of the ratio, and they come from the same emission.
// If they ever disagree, one of them is being computed somewhere else.
func TestIdlePctIsTheComplementOfWorkDone(t *testing.T) {
	e := RunEfficiency{MachineVCPUHours: 1.176, AllocatedVCPUHours: 0.045, IdlePct: 96.2}
	work := 100 * e.AllocatedVCPUHours / e.MachineVCPUHours
	if diff := (work + e.IdlePct) - 100; diff > 0.5 || diff < -0.5 {
		t.Errorf("work %.2f%% + idle %.2f%% = %.2f%%, want ~100%%",
			work, e.IdlePct, work+e.IdlePct)
	}
}
