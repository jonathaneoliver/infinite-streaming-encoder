package encode

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Promote publishes a finished, staged output directory to one or more live
// locations by rsync. Destinations come from the PROMOTE_DESTS env var
// (semicolon-separated): a local path (mounted into the container) or a remote
// user@host:path (over ssh). This is the staging → live step: encodes land in
// OUTPUT_DIR (the staging area) and are copied out to the promoted libraries on
// demand (a button) or automatically after a job (the "promote after encode"
// option).

// PromoteResult is the outcome of one destination in a Promote call.
type PromoteResult struct {
	Dest  string `json:"dest"`
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

// PromoteDests returns the configured rsync destinations (PROMOTE_DESTS,
// semicolon-separated, whitespace trimmed). Empty when promote isn't configured.
func PromoteDests() []string {
	var out []string
	for _, d := range strings.Split(os.Getenv("PROMOTE_DESTS"), ";") {
		if d = strings.TrimSpace(d); d != "" {
			out = append(out, d)
		}
	}
	return out
}

// isRemoteDest reports whether a destination is a remote ssh target
// (user@host:path or host:path) rather than a local path. A local absolute path
// (starts with "/") is never remote; otherwise a colon that precedes any slash
// marks the host:path split.
func isRemoteDest(d string) bool {
	if strings.HasPrefix(d, "/") {
		return false
	}
	c := strings.Index(d, ":")
	slash := strings.Index(d, "/")
	return c > 0 && (slash == -1 || c < slash)
}

// autoPromote rsyncs each freshly-moved output to the configured destinations
// after a job's "promote after encode" run, logging the per-destination result
// into the job. Dated backups are skipped (only live output dirs promote).
// Best-effort — a promote failure doesn't fail the job; the encode succeeded.
func (m *Manager) autoPromote(job *Job, moved []string) {
	if len(PromoteDests()) == 0 {
		job.AppendLog("[promote] skipped — PROMOTE_DESTS not configured")
		return
	}
	for _, name := range moved {
		if IsDatedBackup(name) {
			continue
		}
		job.AppendLog(fmt.Sprintf("[promote] %s → %d destination(s)", name, len(PromoteDests())))
		results, err := m.Promote(name)
		if err != nil {
			job.AppendLog(fmt.Sprintf("[promote] %s: %v", name, err))
			continue
		}
		for _, r := range results {
			if r.OK {
				job.AppendLog(fmt.Sprintf("[promote] ✓ %s → %s", name, r.Dest))
			} else {
				job.AppendLog(fmt.Sprintf("[promote] ✗ %s → %s: %s", name, r.Dest, r.Error))
			}
		}
	}
}

// Promote rsyncs OUTPUT_DIR/<name>/ to <dest>/<name>/ for every configured
// destination. Best-effort per destination: one failure is recorded but does not
// abort the others. rsync flags mirror the manual copy that has worked here
// (-a --partial); remote targets go over ssh with host-key auto-accept and
// BatchMode so a missing key fails fast instead of hanging on a prompt.
func (m *Manager) Promote(name string) ([]PromoteResult, error) {
	if err := ValidPathSegment("output name", name); err != nil {
		return nil, err
	}
	src := filepath.Join(m.OutputDir, name)
	if fi, err := os.Stat(src); err != nil || !fi.IsDir() {
		return nil, fmt.Errorf("no such output %q", name)
	}
	dests := PromoteDests()
	if len(dests) == 0 {
		return nil, fmt.Errorf("PROMOTE_DESTS is not configured")
	}
	var results []PromoteResult
	for _, dest := range dests {
		target := strings.TrimRight(dest, "/") + "/" + name + "/"
		args := []string{"-a", "--partial"}
		if isRemoteDest(dest) {
			// IgnoreUnknown=UseKeychain so the mounted macOS ~/.ssh/config parses
			// under Linux ssh; auth uses the forwarded host ssh-agent (the key's
			// passphrase lives in the macOS keychain, unreachable in-container).
			args = append(args, "-e", "ssh -o IgnoreUnknown=UseKeychain -o StrictHostKeyChecking=accept-new -o BatchMode=yes")
		}
		args = append(args, src+"/", target)
		cmd := exec.Command("rsync", args...)
		cmd.Env = os.Environ()
		outB, err := cmd.CombinedOutput()
		r := PromoteResult{Dest: dest, OK: err == nil}
		if err != nil {
			r.Error = strings.TrimSpace(string(outB))
			if r.Error == "" {
				r.Error = err.Error()
			}
		}
		results = append(results, r)
	}
	return results, nil
}
