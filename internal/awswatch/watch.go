// Package awswatch polls the AWS inventory every WatchdogInterval and
// enforces a MaxLifetime on running instances. Any instance whose
// LaunchedAt tag is older than MaxLifetime is either logged as a
// warning (when AutoTerminateStale is false) or force-terminated via
// the Python cleanup module (when true).
//
// This is the backstop for catastrophic failure modes: if the Python
// CLI crashed between launch and tagging, if the OS-level
// `shutdown -h` hung, or if Docker restarted while the instance was
// running — whatever the cause, an instance older than MaxLifetime
// is a leak and gets cleaned up.
//
// Scoped strictly to Application=infinite-streaming-encoder-app tagged resources via the
// same Python inventory module the HTTP handler uses.
package awswatch

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	// How often to poll the AWS inventory. Zero disables the watchdog.
	Interval time.Duration
	// Instances older than this get flagged / terminated.
	MaxLifetime time.Duration
	// When true, stale instances are force-terminated via
	// infinite_streaming_encoder.cloud.cleanup. When false, we only log warnings.
	AutoTerminateStale bool
	// S3 staging prefix retention for FAILED jobs — prefixes whose
	// _FAILED marker is older than this get garbage-collected. Gives
	// the Retry UI a runway to resume from prior work without letting
	// staging accumulate indefinitely. Zero disables.
	FailedStagingMaxAge time.Duration
	// How often to run that GC. It rides the inventory poll, but it enforces an
	// HOURS-scale retention (FailedStagingMaxAge), so running it once per
	// Interval was ~1,440 LIST calls plus a head_object per job prefix per day
	// to act on something that changes at most a few times a day. Zero falls
	// back to every tick (the old behaviour).
	FailedStagingInterval time.Duration
	// How often to sweep telemetry/state SQS queues and EventBridge rules
	// stranded by orchestrators killed mid-run (a server restart kills the
	// cli_batch subprocess; a cancel docker stops it). Zero disables.
	//
	// This exists because the sweep used to run ONLY at submit, so an orphan was
	// reclaimed by the next cloud encode — and never at all for someone who
	// stops encoding (#191). Submit still sweeps; this is what makes the reclaim
	// independent of whether anyone submits again.
	//
	// Nothing is gained by going faster than the queues' 1h message retention:
	// the sweep will not touch a queue younger than that, so a shorter period is
	// pure SQS request spend on a set that cannot have changed.
	TelemetryGCInterval time.Duration
	// Trigger fires an immediate inventory check + warm reconcile out of band
	// (e.g. a job just started or finalized), so the keep-warm floor reacts at
	// once instead of on the next Interval tick. Nil = poll-only.
	Trigger <-chan struct{}
}

type inventoryDoc struct {
	Instances []struct {
		ID         string  `json:"id"`
		State      string  `json:"state"`
		Type       string  `json:"type"`
		JobID      string  `json:"job_id"`
		AgeSeconds float64 `json:"age_seconds"`
		HourlyUSD  float64 `json:"estimated_hourly_usd"`
	} `json:"instances"`
	Summary struct {
		RunningInstances   int     `json:"running_instances"`
		OrphanVolumes      int     `json:"orphan_volumes"`
		EstimatedHourlyUSD float64 `json:"estimated_hourly_usd"`
		RunningExecutions  int     `json:"running_executions"`
		ActiveBatchJobs    int     `json:"active_batch_jobs"`
	} `json:"summary"`
}

// lastFailedStagingGC is when gcFailedStaging last ran, so it can keep its own
// (much slower) cadence while riding this loop's tick. Zero value = never, so
// the first tick always runs it. Same single-goroutine contract as above.
var lastFailedStagingGC time.Time

// maybeGCFailedStaging runs the failed-staging GC if it is enabled and due.
// Both the quiet and the busy path call it; the schedule lives here so they
// cannot drift apart.
func maybeGCFailedStaging(cfg Config) {
	if cfg.FailedStagingMaxAge <= 0 {
		return
	}
	if time.Since(lastFailedStagingGC) < cfg.FailedStagingInterval {
		return
	}
	lastFailedStagingGC = time.Now()
	if err := gcFailedStaging(cfg.FailedStagingMaxAge); err != nil {
		log.Printf("awswatch: gc_failed_staging failed: %v", err)
	}
}

// lastTelemetryGC is when the SQS/EventBridge sweep last ran. Same
// single-goroutine contract as lastFailedStagingGC above.
var lastTelemetryGC time.Time

// maybeGCTelemetryQueues sweeps orphaned telemetry/state channels if enabled
// and due. Called from the quiet path as well as the busy one — an orphan is
// left by a run that ALREADY ENDED, so the fleet being empty is the normal
// state to find one in, not a reason to skip.
//
// Silently skipped without STATE_MACHINE_ARN, and that is deliberate. The ARN
// is what scopes the keep-list; _active_execution_cores returns an empty set
// without it, which makes the sweep MORE aggressive rather than less, and a run
// outliving the 1h message retention sits at zero messages looking exactly like
// an orphan. Declining to sweep is the safe degrade — the same "degrades open
// is not automatically safe" lesson as #248's require-idle.
func maybeGCTelemetryQueues(cfg Config) {
	if cfg.TelemetryGCInterval <= 0 {
		return
	}
	if time.Since(lastTelemetryGC) < cfg.TelemetryGCInterval {
		return
	}
	arn := os.Getenv("STATE_MACHINE_ARN")
	if arn == "" {
		return
	}
	lastTelemetryGC = time.Now()
	if err := gcTelemetryQueues(arn); err != nil {
		log.Printf("awswatch: gc_telemetry_queues failed: %v", err)
	}
}

// gcTelemetryQueues is a package var so the scheduling and the ARN guard above
// can be tested without spawning python3 or reaching AWS. Same reason
// internal/api made runPythonCloud one.
var gcTelemetryQueues = runGCTelemetryQueues

// Run polls the AWS inventory on `cfg.Interval` until ctx is cancelled.
// Returns immediately if cfg.Interval == 0.
func Run(ctx context.Context, cfg Config) {
	if cfg.Interval <= 0 {
		return
	}
	log.Printf("awswatch: starting; interval=%s max_lifetime=%s auto_terminate=%v",
		cfg.Interval, cfg.MaxLifetime, cfg.AutoTerminateStale)

	// First tick fires immediately — we want a baseline inventory dump
	// at startup, especially for the "you started the server with N
	// instances already running" case.
	t := time.NewTicker(cfg.Interval)
	defer t.Stop()

	runCheck(cfg)
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			runCheck(cfg)
		case <-cfg.Trigger:
			// Out-of-band nudge (job start/finalize). A nil Trigger channel never
			// fires, so this is a no-op when the feature is unwired.
			runCheck(cfg)
		}
	}
}

func runCheck(cfg Config) {
	inv, err := fetchInventory()
	if err != nil {
		log.Printf("awswatch: inventory fetch failed: %v", err)
		return
	}

	// Self-heal a dangling worker-AMI pointer (#79): if the compute env is wired
	// to an AMI that no longer exists, clear it → pull-on-boot, so a deleted AMI
	// degrades to a slow cold start instead of breaking every cloud encode. No-op
	// for every safe state; cheap read + at most one UpdateComputeEnvironment.
	healDanglingAMI()

	if inv.Summary.RunningInstances == 0 && inv.Summary.OrphanVolumes == 0 {
		// Quiet — but still run the housekeeping sweeps. Neither is reflected
		// in the inventory summary, and an orphan is by definition left by a run
		// that already ended, so an empty fleet is the normal state to find one
		// in rather than a reason to skip.
		maybeGCFailedStaging(cfg)
		maybeGCTelemetryQueues(cfg)
		return
	}

	log.Printf("awswatch: %d running, %d orphan volumes, ~$%.2f/hr",
		inv.Summary.RunningInstances, inv.Summary.OrphanVolumes,
		inv.Summary.EstimatedHourlyUSD)

	max := cfg.MaxLifetime.Seconds()
	for _, i := range inv.Instances {
		if i.State != "running" && i.State != "pending" {
			continue
		}
		if max <= 0 || i.AgeSeconds < max {
			continue
		}
		label := i.ID
		if i.JobID != "" {
			label = i.ID + " (job " + i.JobID + ")"
		}
		log.Printf("awswatch: STALE %s %s age=%.0fmin > max=%.0fmin type=%s $%.2f/hr",
			label, i.State, i.AgeSeconds/60, max/60, i.Type, i.HourlyUSD)

		if !cfg.AutoTerminateStale {
			continue
		}
		log.Printf("awswatch: terminating %s (auto-terminate-stale=true)", label)
		if err := terminate(i.JobID, i.ID); err != nil {
			log.Printf("awswatch: terminate failed for %s: %v", label, err)
		}
	}

	// GC S3 staging for failed jobs past their retention window, on its own
	// slower cadence — it is idempotent, but "idempotent" is not "free": each
	// pass is a LIST plus a head_object per job prefix, billed as requests and
	// (from outside the region) as egress.
	maybeGCFailedStaging(cfg)
	maybeGCTelemetryQueues(cfg)
}

// healDanglingAMI clears the compute env's image_id_override when it points at a
// deleted AMI (the one worker-AMI state that breaks encodes). Same live
// UpdateComputeEnvironment path as setMinVCPUs; a no-op for warm / pull-on-boot /
// wrong-tag states. Best-effort — logs only when it actually heals or errors.
func healDanglingAMI() {
	cmd := exec.Command("python3", "-m", "infinite_streaming_encoder.cloud.image_state", "--heal", "--json")
	cmd.Env = os.Environ()
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			log.Printf("awswatch: image_state --heal failed (exit %d): %s",
				ee.ExitCode(), strings.TrimSpace(string(ee.Stderr)))
		}
		return
	}
	var doc struct {
		Healed     bool   `json:"healed"`
		ClearedAMI string `json:"cleared_ami"`
	}
	if json.Unmarshal(out, &doc) == nil && doc.Healed {
		log.Printf("awswatch: self-healed dangling worker AMI %s — compute env reset to pull-on-boot",
			doc.ClearedAMI)
	}
}

// gcTelemetryQueues shells out to the orchestrator's own bounded sweep rather
// than reimplementing it in Go. The bounds it enforces (keep-list of RUNNING
// executions, empty, no messages in flight, older than the retention window)
// are the reason it is safe to call from anywhere, and duplicating them here
// would be a second place for them to drift — the #217 shape of mistake.
//
// Nothing on stdout to parse: the sweep logs what it deletes to its own stderr
// and is best-effort by design, so only a non-zero exit is worth reporting.
func runGCTelemetryQueues(stateMachineARN string) error {
	cmd := exec.Command("python3",
		"-m", "infinite_streaming_encoder.cli_batch", "gc",
		"--state-machine-arn", stateMachineARN)
	cmd.Env = os.Environ()
	if _, err := cmd.Output(); err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return &pyError{module: "cli_batch gc", code: ee.ExitCode(),
				stderr: strings.TrimSpace(string(ee.Stderr))}
		}
		return err
	}
	return nil
}

func gcFailedStaging(maxAge time.Duration) error {
	args := []string{
		"-m", "infinite_streaming_encoder.cloud.cleanup", "--json",
		"--gc-failed-staging",
		"--max-age-s", strconv.Itoa(int(maxAge.Seconds())),
	}
	cmd := exec.Command("python3", args...)
	cmd.Env = os.Environ()
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return &pyError{module: "cleanup", code: ee.ExitCode(),
				stderr: strings.TrimSpace(string(ee.Stderr))}
		}
		return err
	}
	// Only log when something was actually deleted — keeps the
	// happy-path quiet.
	var doc struct {
		Actions []struct {
			Action string `json:"action"`
			ID     string `json:"id"`
		} `json:"actions"`
	}
	if json.Unmarshal(out, &doc) == nil {
		for _, a := range doc.Actions {
			if a.Action == "deleted" {
				log.Printf("awswatch: gc staged %s", a.ID)
			}
		}
	}
	return nil
}

func fetchInventory() (*inventoryDoc, error) {
	// --no-s3-prefixes: the watchdog reads instances, executions and Batch jobs;
	// nothing here touches the per-prefix staging sizes, which are a display on
	// the AWS tab. Enumerating every staged object once a minute cost ~$2.90/month
	// of LIST requests on an idle account and grew with whatever was staged (#227).
	// The HTTP handler computes them on demand instead, cached.
	cmd := exec.Command("python3", "-m", "infinite_streaming_encoder.cloud.inventory",
		"--json", "--no-s3-prefixes")
	cmd.Env = os.Environ()
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return nil, &pyError{module: "inventory", code: ee.ExitCode(),
				stderr: strings.TrimSpace(string(ee.Stderr))}
		}
		return nil, err
	}
	var doc inventoryDoc
	if err := json.Unmarshal(out, &doc); err != nil {
		return nil, err
	}
	return &doc, nil
}

// terminate uses the JobId tag when present (scoped, clean), else
// falls back to sweep_all (rare: something leaked before tagging).
func terminate(jobID, instanceID string) error {
	var args []string
	if jobID != "" {
		args = []string{"-m", "infinite_streaming_encoder.cloud.cleanup", "--json", "--job-id", jobID}
	} else {
		// An instance with the Application tag but no JobId is a
		// bug somewhere; sweep_all is the safe sledgehammer.
		args = []string{"-m", "infinite_streaming_encoder.cloud.cleanup", "--json", "--sweep-all"}
	}
	cmd := exec.Command("python3", args...)
	cmd.Env = os.Environ()
	_, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return &pyError{module: "cleanup", code: ee.ExitCode(),
				stderr: strings.TrimSpace(string(ee.Stderr))}
		}
		return err
	}
	return nil
}

type pyError struct {
	module string
	code   int
	stderr string
}

func (e *pyError) Error() string {
	return "python3 -m infinite_streaming_encoder.cloud." + e.module + " exited " +
		itoa(e.code) + ": " + e.stderr
}

func itoa(i int) string {
	// avoid pulling strconv for this one call
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var buf [12]byte
	pos := len(buf)
	for i > 0 {
		pos--
		buf[pos] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		pos--
		buf[pos] = '-'
	}
	return string(buf[pos:])
}
