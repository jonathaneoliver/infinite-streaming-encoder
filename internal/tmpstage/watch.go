// Package tmpstage reclaims the $TMP_DIR/<job_id>/ staging that the happy path
// missed (#207) — the local-disk half of what internal/diststage does for MinIO.
//
// Every encode stages into $TMP_DIR/<job_id>/ so partial output never appears in
// OutputDir, and Manager.run's finalize path removes that directory on every
// terminal outcome (moved to OutputDir on success, to $TMP_DIR/failed/<id>/ on
// failure). A job whose process dies before reaching that path — the server
// container killed or crashed mid-encode — leaves the directory behind, and
// nothing ever collects it: Reconcile enumerates $TMP_DIR/jobs/*.json, and the
// state file is gone too, so the payload is orphaned from the only index that
// could find it. 6.8 GB had accumulated that way over four months.
//
// Scope is structural, not a list of exclusions: $TMP_DIR also holds the
// deliberate encode_<stem>/ mezzanine caches, the learned-state JSON that sizes
// every chunk plan (encode_speeds.json, ladders.json, quality-curves.json) and
// the permanent record (logs/, history.md, failed/). Only names matching
// ^[0-9]+$ — the epoch-ms job ID shape — are eligible, so everything else is
// out of scope by construction rather than by a rule someone can forget.
package tmpstage

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type Config struct {
	// $TMP_DIR itself — the directory job staging lives directly under.
	Dir string
	// How often to sweep. Zero disables the watchdog entirely.
	Interval time.Duration
	// A job directory untouched for longer than this is reclaimed. Doubles as
	// the debugging window for a crashed job's staging. Zero disables.
	MaxAge time.Duration
	// Job IDs of queued/running jobs — never reclaimed regardless of age, the
	// same guarantee ActiveDistPrefixes gives MinIO. Nil means "no job is
	// active", which is only correct if the caller genuinely has no way to know.
	ActiveIDs func() []string
}

func Run(ctx context.Context, cfg Config) {
	if cfg.Interval <= 0 || cfg.MaxAge <= 0 || cfg.Dir == "" {
		return
	}
	log.Printf("tmpstage: starting; dir=%s interval=%s max_age=%s",
		cfg.Dir, cfg.Interval, cfg.MaxAge)

	t := time.NewTicker(cfg.Interval)
	defer t.Stop()

	logSweep(cfg)
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			logSweep(cfg)
		}
	}
}

// reclaim is one directory the sweep removed (or tried to).
type reclaim struct {
	ID    string
	Bytes int64
	Err   error
}

func logSweep(cfg Config) {
	// A silent GC that eats 6.8 GB is indistinguishable from a disk that was
	// always that size, so every reclaim is logged; the quiet path stays quiet.
	for _, r := range sweep(cfg) {
		if r.Err != nil {
			log.Printf("tmpstage: reclaim failed for %s: %v", r.ID, r.Err)
			continue
		}
		log.Printf("tmpstage: reclaimed %s (%s)", r.ID, humanBytes(r.Bytes))
	}
}

func sweep(cfg Config) []reclaim {
	// A zero MaxAge means "disabled", never "everything is old enough" — the
	// cutoff would be now and the first sweep would take every job directory
	// with it. Run refuses to start without one; this refuses to act on one.
	if cfg.MaxAge <= 0 || cfg.Dir == "" {
		return nil
	}
	entries, err := os.ReadDir(cfg.Dir)
	if err != nil {
		log.Printf("tmpstage: cannot read %s: %v", cfg.Dir, err)
		return nil
	}
	keep := keepSet(cfg)
	cutoff := time.Now().Add(-cfg.MaxAge)

	var out []reclaim
	for _, e := range entries {
		if !e.IsDir() || !isJobID(e.Name()) || keep[e.Name()] {
			continue
		}
		path := filepath.Join(cfg.Dir, e.Name())
		size, busy, err := measure(path, cutoff)
		if err != nil {
			out = append(out, reclaim{ID: e.Name(), Err: err})
			continue
		}
		if busy {
			continue
		}
		r := reclaim{ID: e.Name(), Bytes: size}
		if err := os.RemoveAll(path); err != nil {
			r.Err = err
		}
		out = append(out, r)
	}
	return out
}

// keepSet is the union of the live Manager's active jobs and the persisted
// state files. Both name the same set — Reconcile turns the second into the
// first at startup — but the on-disk half also covers a job submitted between
// this sweep's keep-list snapshot and its ReadDir.
func keepSet(cfg Config) map[string]bool {
	keep := map[string]bool{}
	if cfg.ActiveIDs != nil {
		for _, id := range cfg.ActiveIDs() {
			keep[id] = true
		}
	}
	states, err := os.ReadDir(filepath.Join(cfg.Dir, "jobs"))
	if err != nil {
		return keep
	}
	for _, s := range states {
		if name := strings.TrimSuffix(s.Name(), ".json"); name != s.Name() {
			keep[name] = true
		}
	}
	return keep
}

// isJobID reports whether name has the epoch-ms shape Submit gives a job.
func isJobID(name string) bool {
	if name == "" {
		return false
	}
	for _, c := range name {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// errBusy stops the walk at the first sign of recent activity.
var errBusy = errors.New("recently modified")

// measure walks a job directory for its total size and whether anything in it
// was modified since cutoff. It stops at the first recent file: a directory
// that is still being written is the case we must not delete AND the expensive
// one to walk, so it costs a few stats; a genuinely abandoned one is walked in
// full, which is what produces the byte count for the reclaim log.
//
// The tree's mtimes are the signal rather than the top-level directory's own,
// which an encode writing deep inside it never touches.
func measure(root string, cutoff time.Time) (int64, bool, error) {
	var total int64
	err := filepath.WalkDir(root, func(_ string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		info, err := d.Info()
		if err != nil {
			// Raced with something removing it — treat as gone, not as busy.
			if errors.Is(err, fs.ErrNotExist) {
				return nil
			}
			return err
		}
		if info.ModTime().After(cutoff) {
			return errBusy
		}
		if !d.IsDir() {
			total += info.Size()
		}
		return nil
	})
	if errors.Is(err, errBusy) {
		return 0, true, nil
	}
	if err != nil {
		return 0, false, err
	}
	return total, false, nil
}

func humanBytes(n int64) string {
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%d B", n)
	}
	div, exp := int64(unit), 0
	for v := n / unit; v >= unit; v /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f %s", float64(n)/float64(div), "KMGTP"[exp:exp+1]+"B")
}
