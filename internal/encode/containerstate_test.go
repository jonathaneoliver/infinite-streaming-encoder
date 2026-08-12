package encode

import (
	"os/exec"
	"strings"
	"testing"
)

// #323: a container that EXISTS is not necessarily a container that RAN.
//
// `docker run` creates the container and then starts it, so a start failure —
// a bad bind mount, a missing image, exhausted resources — leaves one with the
// right name stuck in `created`. `docker inspect` succeeds on it. The old
// containerState asked `{{.State.Running}}` and reported any successful inspect
// as reattachable, so runFileContainer skipped `docker run`, `docker logs -f`
// returned nothing, `.State.ExitCode` read 0, and the job was marked done.
//
// Then moveTmpToOutput moved whatever was in $TMP_DIR/<job>/ into OUTPUT_DIR —
// and because the run that hits this is usually a retry carrying
// force_reencode, the previous good output was archived to make room. Observed
// live during a drive recovery: a directory holding only a .prefetch scratch dir
// was one button press from replacing a complete ladder.

func TestContainerHasRunByStatus(t *testing.T) {
	cases := []struct {
		status string
		hasRun bool
		why    string
	}{
		{"running", true, "live — logs -f and follow"},
		{"exited", true, "finished — logs has history, exit code is real"},
		// The regression. Everything below never executed, so logs are empty and
		// ExitCode is 0 — which reads as success.
		{"created", false, "created but never started — #323"},
		{"dead", false, "removal failed; never usable"},
		{"restarting", false, "no stable logs or exit code to read"},
		{"paused", false, "started but not progressing; logs -f would hang"},
		{"removing", false, "on its way out"},
		// Unknown must be false: a duplicate docker run fails loudly on the name
		// conflict, while a wrong reattach fails silently and destructively.
		{"", false, "empty — inspect gave us nothing"},
		{"some-future-docker-state", false, "unknown states are not reattachable"},
	}
	for _, tc := range cases {
		if got := containerHasRun(tc.status); got != tc.hasRun {
			t.Errorf("containerHasRun(%q) = %v, want %v (%s)", tc.status, got, tc.hasRun, tc.why)
		}
	}
}

// The states Docker actually reports, against a real daemon. The fix depends on
// `{{.State.Status}}` returning these exact strings, which is not something a
// table test can establish.
func TestContainerStateAgainstRealDocker(t *testing.T) {
	if _, err := exec.LookPath("docker"); err != nil {
		t.Skip("no docker")
	}
	if err := exec.Command("docker", "info").Run(); err != nil {
		t.Skip("docker daemon not available")
	}
	// A tiny image that is certainly present: the encoder's own, else alpine.
	img := "alpine"
	if err := exec.Command("docker", "image", "inspect", img).Run(); err != nil {
		t.Skip("no alpine image available offline")
	}

	rm := func(name string) {
		_ = exec.Command("docker", "rm", "-f", name).Run()
	}

	t.Run("absent", func(t *testing.T) {
		exists, reattach, err := containerState("encoder_test_definitely_absent_323")
		if err != nil {
			t.Fatal(err)
		}
		if exists || reattach {
			t.Errorf("absent container reported exists=%v reattachable=%v", exists, reattach)
		}
	})

	// THE BUG: created but never started. docker create is exactly what a failed
	// `docker run` leaves behind.
	t.Run("created but never started", func(t *testing.T) {
		name := "encoder_test_created_323"
		rm(name)
		if out, err := exec.Command("docker", "create", "--name", name, img, "true").CombinedOutput(); err != nil {
			t.Skipf("docker create failed: %v: %s", err, out)
		}
		t.Cleanup(func() { rm(name) })

		exists, reattach, err := containerState(name)
		if err != nil {
			t.Fatal(err)
		}
		if !exists {
			t.Fatal("a created container should report as existing")
		}
		if reattach {
			t.Error("a created container reported REATTACHABLE — this is #323: " +
				"its logs are empty and its exit code reads 0, so the job would be marked done")
		}
		// Pin the two facts that make it dangerous, so the reasoning above is
		// evidence rather than assertion.
		out, _ := exec.Command("docker", "inspect", "-f", "{{.State.ExitCode}}", name).Output()
		if strings.TrimSpace(string(out)) != "0" {
			t.Logf("note: created container exit code is %q, not 0", strings.TrimSpace(string(out)))
		}
		logs, _ := exec.Command("docker", "logs", name).CombinedOutput()
		if len(strings.TrimSpace(string(logs))) != 0 {
			t.Logf("note: created container had logs: %q", logs)
		}
	})

	t.Run("exited is reattachable", func(t *testing.T) {
		name := "encoder_test_exited_323"
		rm(name)
		if out, err := exec.Command("docker", "run", "--name", name, img, "true").CombinedOutput(); err != nil {
			t.Skipf("docker run failed: %v: %s", err, out)
		}
		t.Cleanup(func() { rm(name) })

		exists, reattach, err := containerState(name)
		if err != nil {
			t.Fatal(err)
		}
		if !exists || !reattach {
			t.Errorf("exited container: exists=%v reattachable=%v, want true/true — "+
				"this is the restart-resilience path and must not regress", exists, reattach)
		}
		if containerIsRunning(name) {
			t.Error("an exited container reported as running; attachAndWait would use logs -f")
		}
	})

	t.Run("running is reattachable and live", func(t *testing.T) {
		name := "encoder_test_running_323"
		rm(name)
		if out, err := exec.Command("docker", "run", "-d", "--name", name, img, "sleep", "30").CombinedOutput(); err != nil {
			t.Skipf("docker run -d failed: %v: %s", err, out)
		}
		t.Cleanup(func() { rm(name) })

		exists, reattach, err := containerState(name)
		if err != nil {
			t.Fatal(err)
		}
		if !exists || !reattach {
			t.Errorf("running container: exists=%v reattachable=%v, want true/true", exists, reattach)
		}
		if !containerIsRunning(name) {
			t.Error("a running container did not report as running; attachAndWait would drain instead of follow")
		}
	})
}
