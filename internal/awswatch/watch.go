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
	// WarmMinVCPUs keeps the Batch compute environment's minvCpus at this
	// value while a cloud-batch run is active, then resets it to 0 when idle —
	// so the packaging tail lands on a hot instance instead of cold-starting.
	// Zero disables the feature (min_vcpus is left at whatever Terraform set).
	WarmMinVCPUs int
	// ActiveJobs returns the count of the app's own queued/running cloud jobs.
	// The warm floor is held while this is > 0 (not just while AWS resources are
	// live), so it stays warm across the gap between sequential jobs and only
	// drops when the whole app job queue is empty. Nil disables this signal.
	ActiveJobs func() int
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

// lastSetMinVCPUs tracks the min_vcpus we last pushed to Batch so we only call
// UpdateComputeEnvironment on a change. -1 = unknown (force a set on first tick,
// which also resets any value leaked by a crash mid-run). Accessed only from
// the single awswatch goroutine, so no lock needed.
var lastSetMinVCPUs = -1

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

	// Warm-fleet control: keep min_vcpus > 0 while a run is active so the
	// packaging tail lands on a hot box; drop to 0 when idle. Runs before the
	// quiet-path early return so the reset-to-0 still happens at zero fleet.
	reconcileWarmCapacity(cfg, inv)

	// Self-heal a dangling worker-AMI pointer (#79): if the compute env is wired
	// to an AMI that no longer exists, clear it → pull-on-boot, so a deleted AMI
	// degrades to a slow cold start instead of breaking every cloud encode. No-op
	// for every safe state; cheap read + at most one UpdateComputeEnvironment.
	healDanglingAMI()

	if inv.Summary.RunningInstances == 0 && inv.Summary.OrphanVolumes == 0 {
		// Quiet — but still run the staging GC since failed prefixes
		// aren't reflected in the inventory summary.
		if cfg.FailedStagingMaxAge > 0 {
			if err := gcFailedStaging(cfg.FailedStagingMaxAge); err != nil {
				log.Printf("awswatch: gc_failed_staging failed: %v", err)
			}
		}
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

	// GC S3 staging for failed jobs past their retention window.
	// Runs every tick; the Python side cheaply skips prefixes without
	// a _FAILED marker, so this is idempotent and low-cost.
	if cfg.FailedStagingMaxAge > 0 {
		if err := gcFailedStaging(cfg.FailedStagingMaxAge); err != nil {
			log.Printf("awswatch: gc_failed_staging failed: %v", err)
		}
	}
}

// reconcileWarmCapacity sets the compute environment's min_vcpus to WarmMinVCPUs
// while a run is active and to 0 when idle, calling Batch only on a change.
func reconcileWarmCapacity(cfg Config, inv *inventoryDoc) {
	if cfg.WarmMinVCPUs <= 0 {
		return // feature disabled
	}
	// Hold the warm floor while the app's own job queue has work (covers the gap
	// between sequential jobs) OR AWS still shows a live run. Drops to 0 only when
	// the app queue is empty AND no execution/Batch job is active.
	appQueued := cfg.ActiveJobs != nil && cfg.ActiveJobs() > 0
	active := appQueued || inv.Summary.RunningExecutions > 0 || inv.Summary.ActiveBatchJobs > 0
	desired := 0
	if active {
		desired = cfg.WarmMinVCPUs
	}
	if desired == lastSetMinVCPUs {
		return
	}
	if err := setMinVCPUs(desired); err != nil {
		log.Printf("awswatch: set min_vcpus=%d failed: %v", desired, err)
		return
	}
	reason := "idle, draining to zero"
	if active {
		reason = "run active, keeping a box warm"
	}
	log.Printf("awswatch: min_vcpus -> %d (%s)", desired, reason)
	lastSetMinVCPUs = desired
}

func setMinVCPUs(n int) error {
	cmd := exec.Command("python3", "-m", "infinite_streaming_encoder.cloud.compute_env",
		"--set-min-vcpus", strconv.Itoa(n))
	cmd.Env = os.Environ()
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return &pyError{module: "compute_env", code: ee.ExitCode(),
				stderr: strings.TrimSpace(string(ee.Stderr))}
		}
		return err
	}
	var doc struct {
		Error string `json:"error"`
	}
	if json.Unmarshal(out, &doc) == nil && doc.Error != "" {
		return &pyError{module: "compute_env", stderr: doc.Error}
	}
	return nil
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
	cmd := exec.Command("python3", "-m", "infinite_streaming_encoder.cloud.inventory", "--json")
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
