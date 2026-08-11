package encode

import (
	"strings"
	"testing"
)

// A stage's machine reaches the UI only via ENCODER-HOST, and the machine
// timeline draws nothing for a stage without one (`_laneStages` filters on
// `s.instance`). Since the mezzanine and the packaging moved onto the master
// (#298), those phases never touch a worker — so the only thing that can say
// where they ran is the orchestrator itself, and it can only say it if it was
// told which box it is on (#293).

func TestLocalDistOrchestratorIsToldWhichBoxItIsOn(t *testing.T) {
	t.Setenv("LOCAL_WORKER_LABEL", "macmini")
	m := &Manager{}
	job := &Job{ID: "1", Config: JobConfig{Target: TargetLocalDist}}

	args := m.buildRunArgs(job, "encoder_job_1_f0", "script.py", nil)
	if !hasRunEnv(args, "WORKER_LABEL=macmini") {
		t.Errorf("orchestrator not given WORKER_LABEL; the host mezzanine and "+
			"host packaging would leave the master's lane blank.\nargs: %v", args)
	}
}

// Unset falls back to the same default the worker and internal/api/dist.go use.
// A different fallback would open a SECOND lane for one machine — the phases
// this process runs under one name, the chunks its worker runs under another.
func TestOrchestratorLabelFallsBackToTheWorkersDefault(t *testing.T) {
	t.Setenv("LOCAL_WORKER_LABEL", "")
	m := &Manager{}
	job := &Job{ID: "1", Config: JobConfig{Target: TargetLocalDist}}

	if !hasRunEnv(m.buildRunArgs(job, "n", "s.py", nil), "WORKER_LABEL=mac") {
		t.Error("fallback label is not `mac`, which is what LOCAL_WORKER_LABEL " +
			"defaults to in docker-compose.yml and internal/api/dist.go")
	}
}

// Cloud runs the same host phases, but its lanes are about rented instances and
// what they cost. A Mac in that chart is a box nobody is billed for drawn beside
// the ones they are.
func TestCloudOrchestratorGetsNoWorkerLabel(t *testing.T) {
	t.Setenv("LOCAL_WORKER_LABEL", "macmini")
	m := &Manager{}
	job := &Job{ID: "1", Config: JobConfig{Target: TargetCloudBatch}}

	for _, a := range m.buildRunArgs(job, "n", "s.py", nil) {
		if strings.HasPrefix(a, "WORKER_LABEL=") {
			t.Errorf("cloud orchestrator given %q — its host phases would draw "+
				"a lane for an unrented machine", a)
		}
	}
}

// The Python side emits its host markers AFTER the plan, which is load-bearing
// rather than incidental: unlike ENCODER-REUSED, this handler updates a row in
// place and never seeds one. A marker that arrives first is dropped in silence,
// so the phase stays uncoloured exactly as if nothing had been emitted.
func TestHostMarkerColoursAPlannedRowAndDropsAnUnplannedOne(t *testing.T) {
	j := &Job{}
	j.upsertStage("package:h264", "package h264", "pending", 0)

	if !j.parseMarker(`[[ENCODER-HOST key=package:h264 instance=mac]]`) {
		t.Fatal("marker not recognised")
	}
	if !j.parseMarker(`[[ENCODER-HOST key=hls:av1 instance=mac]]`) {
		t.Fatal("marker not recognised")
	}

	if len(j.Stages) != 1 {
		t.Fatalf("a HOST marker seeded a row for an unplanned key: %v", j.Stages)
	}
	if j.Stages[0].Instance != "mac" {
		t.Errorf("instance = %q, want %q — the stage would be dropped from the "+
			"machine timeline", j.Stages[0].Instance, "mac")
	}
}

// The env is passed as `-e KEY=VALUE` pairs here, not as a plain env slice like
// host_mezzanine_test.go's hasEnv takes.
func hasRunEnv(args []string, want string) bool {
	for i, a := range args {
		if a == "-e" && i+1 < len(args) && args[i+1] == want {
			return true
		}
	}
	return false
}
