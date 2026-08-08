package awswatch

import (
	"errors"
	"testing"
	"time"
)

// #191: the telemetry-queue sweep ran only at submit, so an orphan was
// reclaimed by the next cloud encode — and never at all once you stopped
// encoding. These pin the second trigger and, more importantly, the guard that
// keeps it from being worse than the problem.

// stubTelemetryGC swaps the sweep for a recorder and resets the schedule, so
// each test starts from "never run". Restores both on cleanup.
func stubTelemetryGC(t *testing.T, err error) *[]string {
	t.Helper()
	var calls []string
	prev, prevAt := gcTelemetryQueues, lastTelemetryGC
	gcTelemetryQueues = func(arn string) error {
		calls = append(calls, arn)
		return err
	}
	lastTelemetryGC = time.Time{}
	t.Cleanup(func() { gcTelemetryQueues, lastTelemetryGC = prev, prevAt })
	return &calls
}

func TestTelemetryGCRefusesToSweepUnscoped(t *testing.T) {
	// THE important one. STATE_MACHINE_ARN is what scopes the keep-list, and
	// _active_execution_cores returns an EMPTY set without it — which makes the
	// sweep more aggressive, not less. A run outliving the 1h message retention
	// sits at zero messages looking exactly like an orphan, so an unscoped sweep
	// could delete a live run's queue out from under it. Not sweeping is the
	// safe degrade.
	calls := stubTelemetryGC(t, nil)
	t.Setenv("STATE_MACHINE_ARN", "")

	maybeGCTelemetryQueues(Config{TelemetryGCInterval: time.Hour})

	if len(*calls) != 0 {
		t.Fatalf("swept with no state machine ARN: %v", *calls)
	}
}

func TestTelemetryGCSweepsWhenScoped(t *testing.T) {
	calls := stubTelemetryGC(t, nil)
	t.Setenv("STATE_MACHINE_ARN", "arn:aws:states:us-west-2:1:stateMachine:enc")

	maybeGCTelemetryQueues(Config{TelemetryGCInterval: time.Hour})

	if len(*calls) != 1 {
		t.Fatalf("got %d sweeps, want 1", len(*calls))
	}
	if (*calls)[0] != "arn:aws:states:us-west-2:1:stateMachine:enc" {
		t.Fatalf("swept with the wrong ARN: %q", (*calls)[0])
	}
}

func TestTelemetryGCIsDisabledByAZeroInterval(t *testing.T) {
	calls := stubTelemetryGC(t, nil)
	t.Setenv("STATE_MACHINE_ARN", "arn:aws:states:us-west-2:1:stateMachine:enc")

	maybeGCTelemetryQueues(Config{TelemetryGCInterval: 0})

	if len(*calls) != 0 {
		t.Fatalf("swept while disabled: %v", *calls)
	}
}

func TestTelemetryGCKeepsItsOwnCadence(t *testing.T) {
	// It rides the 60s inventory tick but must not run on every one: each pass
	// is two list_queues, a get_queue_attributes per queue and a list_rules,
	// against a set that cannot change faster than the 1h retention it enforces.
	calls := stubTelemetryGC(t, nil)
	t.Setenv("STATE_MACHINE_ARN", "arn:aws:states:us-west-2:1:stateMachine:enc")
	cfg := Config{TelemetryGCInterval: time.Hour}

	for i := 0; i < 60; i++ { // an hour of 60s ticks
		maybeGCTelemetryQueues(cfg)
	}

	if len(*calls) != 1 {
		t.Fatalf("got %d sweeps across 60 ticks, want 1", len(*calls))
	}
}

func TestTelemetryGCRetriesAfterTheIntervalElapses(t *testing.T) {
	calls := stubTelemetryGC(t, nil)
	t.Setenv("STATE_MACHINE_ARN", "arn:aws:states:us-west-2:1:stateMachine:enc")
	cfg := Config{TelemetryGCInterval: time.Hour}

	maybeGCTelemetryQueues(cfg)
	lastTelemetryGC = time.Now().Add(-2 * time.Hour) // the interval has passed
	maybeGCTelemetryQueues(cfg)

	if len(*calls) != 2 {
		t.Fatalf("got %d sweeps, want 2 — the schedule never came round again", len(*calls))
	}
}

func TestTelemetryGCFailureDoesNotWedgeTheSchedule(t *testing.T) {
	// A failing sweep still stamps the clock, so a persistently broken AWS
	// credential cannot turn an hourly housekeeping call into a per-tick one.
	calls := stubTelemetryGC(t, errors.New("sqs unavailable"))
	t.Setenv("STATE_MACHINE_ARN", "arn:aws:states:us-west-2:1:stateMachine:enc")
	cfg := Config{TelemetryGCInterval: time.Hour}

	maybeGCTelemetryQueues(cfg)
	maybeGCTelemetryQueues(cfg)

	if len(*calls) != 1 {
		t.Fatalf("got %d sweeps after a failure, want 1", len(*calls))
	}
}
