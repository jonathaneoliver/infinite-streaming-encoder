package encode

import "testing"

// A mezzanine cache hit runs NOTHING: MezzCheck routes straight past the task,
// so no container starts and no marker is ever emitted for that row. The server
// is the only thing that knows, and it says so up front — before the
// orchestrator container exists (#189).
//
// That ordering only works because ENCODER-PLAN merges. The plan declares
// `mezzanine` unconditionally and arrives AFTER these rows are set, so a plan
// that overwrote existing rows would put the row back to `pending` and leave it
// there for the whole run — which is the bug, restored by the fix's own timing.

func TestPlanDoesNotResurrectRowsTheServerAlreadySettled(t *testing.T) {
	j := &Job{}
	j.upsertStage("upload:inputs", "upload inputs", "skipped", 100)
	j.upsertStage("mezzanine", "mezzanine", "skipped", 100)

	plan := `[[ENCODER-PLAN [{"key":"mezzanine","label":"mezzanine"},` +
		`{"key":"audio","label":"audio"},` +
		`{"key":"package:h264","label":"package h264"}]]]`
	if !j.parseMarker(plan) {
		t.Fatal("plan marker not recognised")
	}

	for _, key := range []string{"mezzanine", "upload:inputs"} {
		st := findStage(j, key)
		if st == nil {
			t.Fatalf("%s row vanished after the plan", key)
		}
		if st.Status != "skipped" {
			t.Errorf("%s = %q after the plan, want \"skipped\" — the row reads as "+
				"a phase that hung for the whole run", key, st.Status)
		}
	}

	// The plan still contributes the rows nothing had settled yet.
	if findStage(j, "audio") == nil {
		t.Error("plan did not add audio")
	}
}

// `skipped`, not `done`: nothing ran. Worth pinning because the two are easy to
// swap and only one of them is true — and `done` would also claim a duration for
// a phase that never started.
func TestASkippedStageClaimsNoDuration(t *testing.T) {
	j := &Job{}
	j.upsertStage("mezzanine", "mezzanine", "skipped", 100)

	st := findStage(j, "mezzanine")
	if st.StartedAt != nil || st.EndedAt != nil {
		t.Errorf("skipped stage carries timestamps (%v -> %v); it would be drawn "+
			"as work that took time", st.StartedAt, st.EndedAt)
	}
}

func findStage(j *Job, key string) *StageProgress {
	for i := range j.Stages {
		if j.Stages[i].Key == key {
			return &j.Stages[i]
		}
	}
	return nil
}
