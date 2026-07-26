// Package diststage reclaims the local-dist (MinIO) encode staging that the
// happy path missed (#93).
//
// A local-dist job stages its source, mezzanine, every per-chunk encode, the
// variants and the packaged output under s3://<bucket>/jobs/<id>-<base>/ —
// ~2.3 GB for a typical clip. The orchestrator deletes that prefix itself once
// the outputs have been downloaded, so a job that completes normally leaves
// nothing behind. This watchdog is for everything else: failed jobs, cancelled
// jobs (the control plane `docker stop`s the orchestrator, so its own cleanup
// never runs) and crashes. Without it MinIO grew unbounded on the master.
//
// Two layers, both delegated to infinite_streaming_encoder.dist_staging:
//   - an idle-age GC on every tick, told which prefixes belong to in-flight
//     jobs so it can never reclaim a running encode;
//   - a bucket lifecycle expiry, re-asserted every tick (idempotent, quiet
//     when already correct), which is the backstop if this server never runs
//     again — and the only thing that reclaims aborted multipart uploads.
package diststage

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	// How often to sweep. Zero disables the watchdog entirely.
	Interval time.Duration
	// A job prefix untouched for longer than this is reclaimed. Doubles as
	// the debugging window for a failed job's staging. Zero disables the GC
	// (the lifecycle rule is still asserted).
	MaxAge time.Duration
	// Bucket lifecycle expiry for jobs/, in days. Zero disables.
	LifecycleDays int
	// Staging prefixes of queued/running jobs — never reclaimed regardless
	// of age. Nil means "no job is active", which is only correct if the
	// caller genuinely has no way to know.
	ActivePrefixes func() []string
}

// exitNoEndpoint is dist_staging's exit code for "no MinIO endpoint
// configured". That's a permanent condition (the server would have to be
// restarted with different env to fix it), so we stop the loop instead of
// logging the same failure forever.
const exitNoEndpoint = 2

func Run(ctx context.Context, cfg Config) {
	if cfg.Interval <= 0 {
		return
	}
	log.Printf("diststage: starting; interval=%s max_age=%s lifecycle=%dd",
		cfg.Interval, cfg.MaxAge, cfg.LifecycleDays)

	t := time.NewTicker(cfg.Interval)
	defer t.Stop()

	if !sweep(cfg) {
		return
	}
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if !sweep(cfg) {
				return
			}
		}
	}
}

// sweep runs one lifecycle assert + one GC pass. Returns false when the loop
// should stop (MinIO isn't addressable from this server).
func sweep(cfg Config) bool {
	if cfg.LifecycleDays > 0 {
		if err := ensureLifecycle(cfg.LifecycleDays); err != nil {
			if isNoEndpoint(err) {
				log.Printf("diststage: %v — disabling staging reclaim", err)
				return false
			}
			log.Printf("diststage: lifecycle assert failed: %v", err)
		}
	}
	if cfg.MaxAge <= 0 {
		return true
	}
	var keep []string
	if cfg.ActivePrefixes != nil {
		keep = cfg.ActivePrefixes()
	}
	if err := gc(cfg.MaxAge, keep); err != nil {
		if isNoEndpoint(err) {
			log.Printf("diststage: %v — disabling staging reclaim", err)
			return false
		}
		log.Printf("diststage: gc failed: %v", err)
	}
	return true
}

func ensureLifecycle(days int) error {
	_, err := runStaging("--ensure-lifecycle", "--days", strconv.Itoa(days))
	return err
}

func gc(maxAge time.Duration, keep []string) error {
	args := []string{"--gc", "--max-age-s",
		strconv.FormatFloat(maxAge.Seconds(), 'f', 0, 64)}
	for _, p := range keep {
		args = append(args, "--keep", p)
	}
	report, err := runStaging(args...)
	// Log actual reclaims (and only those) so the quiet path stays quiet.
	for _, a := range report.Actions {
		switch a.Action {
		case "deleted":
			log.Printf("diststage: reclaimed %s (%s)", a.ID, a.Detail)
		case "failed":
			log.Printf("diststage: reclaim failed for %s: %s", a.ID, a.Detail)
		}
	}
	return err
}

type stagingAction struct {
	Kind   string `json:"kind"`
	ID     string `json:"id"`
	JobID  string `json:"job_id"`
	Action string `json:"action"`
	Detail string `json:"detail"`
}

type stagingReport struct {
	Scope   string          `json:"scope"`
	Actions []stagingAction `json:"actions"`
}

type noEndpointError struct{ stderr string }

func (e *noEndpointError) Error() string {
	if e.stderr != "" {
		return e.stderr
	}
	return "MinIO staging not addressable"
}

func isNoEndpoint(err error) bool {
	var ne *noEndpointError
	return errors.As(err, &ne)
}

// runStaging invokes the Python staging module and parses its report. The
// report is returned even on a non-zero exit — dist_staging exits 1 when SOME
// prefix failed to delete, having already printed the (partial) report, and
// those per-prefix failures are what we want to log.
func runStaging(args ...string) (stagingReport, error) {
	full := append([]string{"-m", "infinite_streaming_encoder.dist_staging", "--json"}, args...)
	cmd := exec.Command("python3", full...)
	// MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY / DIST_S3_BUCKET
	// come from the server's own environment — the same values it forwards
	// into orchestrator containers.
	cmd.Env = os.Environ()
	out, err := cmd.Output()

	var report stagingReport
	_ = json.Unmarshal(out, &report) // best-effort: empty on a hard failure

	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			stderr := strings.TrimSpace(string(ee.Stderr))
			if ee.ExitCode() == exitNoEndpoint {
				return report, &noEndpointError{stderr: stderr}
			}
			// Exit 1 = at least one action failed; the report carries the
			// detail, so don't duplicate it in the error text.
			if ee.ExitCode() == 1 && len(report.Actions) > 0 {
				return report, nil
			}
			return report, fmt.Errorf("dist_staging exited %d: %s", ee.ExitCode(), stderr)
		}
		return report, fmt.Errorf("dist_staging: %w", err)
	}
	return report, nil
}
