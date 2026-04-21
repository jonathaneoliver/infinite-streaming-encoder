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
// Scoped strictly to Application=encoder-app tagged resources via the
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
	// encoder.cloud.cleanup. When false, we only log warnings.
	AutoTerminateStale bool
	// S3 staging prefix retention for FAILED jobs — prefixes whose
	// _FAILED marker is older than this get garbage-collected. Gives
	// the Retry UI a runway to resume from prior work without letting
	// staging accumulate indefinitely. Zero disables.
	FailedStagingMaxAge time.Duration
}

type inventoryDoc struct {
	Instances []struct {
		ID          string  `json:"id"`
		State       string  `json:"state"`
		Type        string  `json:"type"`
		JobID       string  `json:"job_id"`
		AgeSeconds  float64 `json:"age_seconds"`
		HourlyUSD   float64 `json:"estimated_hourly_usd"`
	} `json:"instances"`
	Summary struct {
		RunningInstances   int     `json:"running_instances"`
		OrphanVolumes      int     `json:"orphan_volumes"`
		EstimatedHourlyUSD float64 `json:"estimated_hourly_usd"`
	} `json:"summary"`
}

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
		}
	}
}

func runCheck(cfg Config) {
	inv, err := fetchInventory()
	if err != nil {
		log.Printf("awswatch: inventory fetch failed: %v", err)
		return
	}
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

func gcFailedStaging(maxAge time.Duration) error {
	args := []string{
		"-m", "encoder.cloud.cleanup", "--json",
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
	cmd := exec.Command("python3", "-m", "encoder.cloud.inventory", "--json")
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
		args = []string{"-m", "encoder.cloud.cleanup", "--json", "--job-id", jobID}
	} else {
		// An instance with the Application tag but no JobId is a
		// bug somewhere; sweep_all is the safe sledgehammer.
		args = []string{"-m", "encoder.cloud.cleanup", "--json", "--sweep-all"}
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
	return "python3 -m encoder.cloud." + e.module + " exited " +
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
