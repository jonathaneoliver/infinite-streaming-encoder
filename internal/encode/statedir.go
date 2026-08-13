package encode

import (
	"fmt"
	"os"
	"path/filepath"
)

// Durable state, and why it does not belong in $TMP_DIR (#331).
//
// $TMP_DIR held four kinds of thing and only one of them was temporary: ~250 KB
// of JSON that CANNOT BE RECOVERED FROM ANYWHERE (the user-authored ladders, and
// a learned model with 43k+ samples behind it), ~31 MB of permanent record, and
// then the genuinely disposable job staging and mezzanine caches that account
// for all of the gigabytes.
//
// internal/tmpstage already had to know this — its eligibility rule is the
// ^[0-9]+$ job-ID shape rather than age, precisely so a sweep cannot take the
// learned state with it. That guard is real but it protects against exactly one
// actor. It does nothing about `rm -rf $TMP_DIR/*` by a human reclaiming space,
// or about `rm -rf $TMP_DIR/encode_*` to clear the mezzanine caches — a glob
// that also matches encode_speeds.json — or about the volume being unplugged.
//
// So the location is now a choice: StateDir for the irreplaceable files,
// RecordDir for the permanent record, BOTH DEFAULTING TO TmpDir so an existing
// install is byte-identical until someone points them somewhere durable. The
// point is not tidiness; it is that $TMP_DIR becomes genuinely disposable,
// which is what every cleanup path already assumes.
//
// The two are separate knobs on purpose. StateDir is a quarter of a megabyte
// that wants backing up; RecordDir holds failed/, which is GIGABYTES of
// half-encoded MP4s. Folding the second into the first would quietly move the
// heavy thing into the directory someone chose because it was small.

// StateFiles are the irreplaceable JSONs, relative to StateDir. Every one of
// them is either user-authored or learned over months, and nothing else in the
// system holds a copy.
//
// cost_samples.json is written by inventory.py rather than by Go — it is listed
// here because the migration has to move it, and because Python resolves the
// same directory (STATE_DIR, then TMP_DIR) rather than deriving its own.
var StateFiles = []string{
	"ladders.json",        // user-authored ladder configuration
	"quality-curves.json", // learned; the design-time VMAF estimate
	"encode_speeds.json",  // learned; sizes every chunk plan
	"spot_samples.json",   // learned; also read by inventory.py
	"cost_samples.json",   // learned; written by inventory.py
	"settings.json",       // user-toggled (the watcher switch)
}

// RecordEntries are the permanent record, relative to RecordDir. Two
// directories and a file; the sizes are unbounded, which is why this is not
// merged into StateFiles.
var RecordEntries = []string{
	"logs",       // per-job logs, also served at /logs/
	"history.md", // the append-only run history
	"failed",     // preserved artifacts of failed jobs
}

// Migration is one entry the startup migration moved, skipped or failed on.
type Migration struct {
	Name string
	From string
	To   string
	// Skipped, when non-empty, says why nothing was moved. A destination that
	// already exists is the normal repeat-boot case AND the dangerous one (both
	// copies present), so it is reported rather than silently resolved.
	Skipped string
	Err     error
}

func (mg Migration) String() string {
	switch {
	case mg.Err != nil:
		return fmt.Sprintf("%s: FAILED to move to %s: %v", mg.Name, mg.To, mg.Err)
	case mg.Skipped != "":
		return fmt.Sprintf("%s: left at %s (%s)", mg.Name, mg.From, mg.Skipped)
	default:
		return fmt.Sprintf("%s: moved %s -> %s", mg.Name, mg.From, mg.To)
	}
}

// MigrateDurableState moves the state files and the record out of tmpDir on
// first boot after StateDir/RecordDir are pointed somewhere else. Call it
// BEFORE anything reads them — NewManager loads three of the stores in its
// constructor, so "before NewManager" is the actual requirement.
//
// It is idempotent, and the way it achieves that matters: a file present at the
// destination is never overwritten and the source is never deleted, so the
// worst case is both copies existing and being told about it. The failure this
// guards against is the quiet one — the old file left where nothing reads it,
// the encoder replanning from a cold model and re-learning, with no error
// anywhere.
func MigrateDurableState(tmpDir, stateDir, recordDir string) []Migration {
	var out []Migration
	out = append(out, migrateInto(tmpDir, stateDir, StateFiles)...)
	out = append(out, migrateInto(tmpDir, recordDir, RecordEntries)...)
	return out
}

func migrateInto(from, to string, names []string) []Migration {
	if from == "" || to == "" || filepath.Clean(from) == filepath.Clean(to) {
		return nil // the default: state lives where it always did
	}
	if err := os.MkdirAll(to, 0o755); err != nil {
		return []Migration{{Name: filepath.Base(to), To: to, Err: err}}
	}
	var out []Migration
	for _, n := range names {
		src, dst := filepath.Join(from, n), filepath.Join(to, n)
		if _, err := os.Stat(src); err != nil {
			continue // nothing to migrate — the normal case after the first boot
		}
		if _, err := os.Stat(dst); err == nil {
			out = append(out, Migration{Name: n, From: src, To: dst,
				Skipped: "destination already exists; delete one of the two"})
			continue
		}
		mg := Migration{Name: n, From: src, To: dst}
		if err := os.Rename(src, dst); err != nil {
			// Cross-device — the whole point of the feature is pointing these at
			// a different disk, so this is the EXPECTED branch, not the odd one.
			// Copy first and only then drop the source: a half-copy that fails
			// leaves the original intact and retries on the next boot.
			if cpErr := copyDir(src, dst); cpErr != nil {
				os.RemoveAll(dst)
				mg.Err = fmt.Errorf("copy: %w (rename: %v)", cpErr, err)
			} else if rmErr := os.RemoveAll(src); rmErr != nil {
				mg.Err = fmt.Errorf("copied to %s but could not remove %s: %w", dst, src, rmErr)
			}
		}
		out = append(out, mg)
	}
	return out
}

// stateDir is where the irreplaceable JSONs live. Empty means "not configured",
// which resolves to TmpDir — the pre-#331 location, and what every Manager
// built without a StateDir (tests, and any caller that predates this) gets.
func (m *Manager) stateDir() string {
	if m.StateDir != "" {
		return m.StateDir
	}
	return m.TmpDir
}

// recordDir is where logs/, history.md and failed/ live; same fallback rule.
func (m *Manager) recordDir() string {
	if m.RecordDir != "" {
		return m.RecordDir
	}
	return m.TmpDir
}

// StatePath / RecordPath are the single definitions of those two layouts.
// Exported because internal/api serves /logs/ out of the record dir.
func (m *Manager) StatePath(name string) string  { return filepath.Join(m.stateDir(), name) }
func (m *Manager) RecordPath(name string) string { return filepath.Join(m.recordDir(), name) }

// hostStateDir is the HOST-side path of StateDir, for bind-mounting into
// spawned worker containers (docker run -v resolves against the host daemon).
// Empty when the state dir is not separate from TmpDir, which is already
// mounted — see buildRunArgs.
func (m *Manager) hostStateDir() string {
	if m.stateDir() == m.TmpDir {
		return ""
	}
	return m.HostStateDir
}
