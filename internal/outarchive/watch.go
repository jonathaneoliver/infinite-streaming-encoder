// Package outarchive reclaims superseded output copies (#332) — the third
// instance of a sweeper this repo already runs twice: internal/tmpstage for
// $TMP_DIR job staging and internal/diststage for MinIO prefixes. Same shape:
// an interval, a max age, an active keep-list, and a reclaimed-bytes line in
// the log for every directory it takes.
//
// What it sweeps is OUTPUT_DIR/.archive/, where a re-encode preserves the copy
// it replaced. Nothing ever removed those: 168 directories and ~158 GB had
// accumulated on the master, against 208 GB of OUTPUT_DIR in total.
//
// The rules are all about NOT deleting, because unlike the other two sweepers
// what sits here is finished work rather than scratch:
//
//   - A dated name is the eligibility test, not age (the same structural rule
//     tmpstage's ^[0-9]+$ job-ID shape gives it). Anything else a human put in
//     the archive is out of scope by construction.
//   - A directory carrying .remote.json or .pending.json is NEVER touched. The
//     first means the media is in S3 and these manifests are the only copy of
//     the metadata; the second means the CHUNKS in S3 are the only copy of the
//     encode. Held indefinitely, and reported, because an immortal directory
//     nobody can see is how this problem started.
//   - A directory carrying .keep is never touched. Evidence somebody chose to
//     keep is not a backup, and the alternative to a marker is remembering.
//   - An ORPHAN — a dated copy whose base output no longer exists — is not a
//     superseded copy, it is the LAST copy, so it is held unless explicitly
//     opted in. 10 of the master's 168 are orphans (the ladder tags changed
//     underneath them), which is also why no keep-N-per-base rule would work:
//     it cannot see them.
//   - A directory whose base name belongs to a queued or running job is held
//     regardless of age, so a re-encode cannot have its own predecessor
//     reclaimed while it is still running.
//
// Age is the archived directory's own mtime, which archive.go stamps when it
// moves the directory in. That is the point of the stamp: retention here means
// "how long a superseded copy is kept", and that clock starts when it is
// superseded, not when it was encoded.
package outarchive

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

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
)

type Config struct {
	// OUTPUT_DIR itself; the archive is the .archive directory under it.
	OutputDir string
	// How often to sweep. Zero disables the watchdog entirely.
	Interval time.Duration
	// A superseded copy older than this is reclaimed. Zero disables — read
	// literally it would put the cutoff at now and take the whole archive.
	MaxAge time.Duration
	// Reclaim archives whose base output no longer exists. Off by default:
	// those are last copies, not superseded ones.
	SweepOrphans bool
	// Output-name stems of queued/running jobs — never reclaimed regardless of
	// age. Nil means "no job is active", which is only correct if the caller
	// genuinely has no way to know.
	ActiveStems func() []string
}

func Run(ctx context.Context, cfg Config) {
	if cfg.Interval <= 0 || cfg.MaxAge <= 0 || cfg.OutputDir == "" {
		return
	}
	log.Printf("outarchive: starting; dir=%s interval=%s max_age=%s sweep_orphans=%v",
		filepath.Join(cfg.OutputDir, encode.ArchiveDirName), cfg.Interval, cfg.MaxAge, cfg.SweepOrphans)

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

// reclaim is one archived directory the sweep removed, or tried to.
type reclaim struct {
	Name  string
	Bytes int64
	Err   error
}

// held is one directory the sweep would have taken on age but did not, with the
// rule that saved it. Only aged-out entries are reported: a young archive being
// kept is not news, an old one being kept forever is.
type held struct {
	Name   string
	Reason string
	Bytes  int64
}

func logSweep(cfg Config) {
	reclaims, holds := sweep(cfg)
	var freed int64
	for _, r := range reclaims {
		if r.Err != nil {
			log.Printf("outarchive: reclaim failed for %s: %v", r.Name, r.Err)
			continue
		}
		freed += r.Bytes
		log.Printf("outarchive: reclaimed %s (%s)", r.Name, humanBytes(r.Bytes))
	}
	if freed > 0 {
		log.Printf("outarchive: freed %s", humanBytes(freed))
	}
	// One line per reason, not per directory: on a first sweep after the
	// orphans accumulate this would otherwise be ten near-identical lines every
	// interval, which is the shape of log nobody reads.
	byReason := map[string]held{}
	for _, h := range holds {
		agg := byReason[h.Reason]
		agg.Reason, agg.Name = h.Reason, h.Name
		agg.Bytes += h.Bytes
		byReason[h.Reason] = agg
	}
	for reason, agg := range byReason {
		log.Printf("outarchive: holding past its retention: %s (%s, e.g. %s)",
			reason, humanBytes(agg.Bytes), agg.Name)
	}
}

func sweep(cfg Config) ([]reclaim, []held) {
	// Zero means disabled, never "everything is old enough". Run refuses to
	// start without a MaxAge; this refuses to act on one.
	if cfg.MaxAge <= 0 || cfg.OutputDir == "" {
		return nil, nil
	}
	archive := filepath.Join(cfg.OutputDir, encode.ArchiveDirName)
	entries, err := os.ReadDir(archive)
	if err != nil {
		if !errors.Is(err, fs.ErrNotExist) { // no archive yet is the normal state
			log.Printf("outarchive: cannot read %s: %v", archive, err)
		}
		return nil, nil
	}
	stems := activeStems(cfg)
	cutoff := time.Now().Add(-cfg.MaxAge)

	var out []reclaim
	var holds []held
	for _, e := range entries {
		name := e.Name()
		if !e.IsDir() || !encode.IsDatedBackup(name) {
			continue
		}
		info, err := e.Info()
		if err != nil || info.ModTime().After(cutoff) {
			continue
		}
		path := filepath.Join(archive, name)
		if reason := holdReason(cfg, path, name, stems); reason != "" {
			holds = append(holds, held{Name: name, Reason: reason, Bytes: dirSize(path)})
			continue
		}
		r := reclaim{Name: name, Bytes: dirSize(path)}
		if err := os.RemoveAll(path); err != nil {
			r.Err = err
		}
		out = append(out, r)
	}
	return out, holds
}

// holdReason returns why this archived directory must survive, or "" when it is
// an ordinary superseded copy. Every branch here is a rule from the package
// doc; adding one means adding it there too.
func holdReason(cfg Config, path, name string, stems []string) string {
	for _, f := range []string{encode.RemoteSidecar, encode.PendingSidecar} {
		if _, err := os.Stat(filepath.Join(path, f)); err == nil {
			return "carries " + f + ", so S3 holds the only copy of part of it"
		}
	}
	if _, err := os.Stat(filepath.Join(path, encode.ArchiveKeepFile)); err == nil {
		return "marked " + encode.ArchiveKeepFile
	}
	base := encode.DatedBackupBase(name)
	for _, s := range stems {
		if base == s || strings.HasPrefix(base, s+"_") {
			return "a queued or running job is re-encoding it"
		}
	}
	if !cfg.SweepOrphans {
		if _, err := os.Stat(filepath.Join(cfg.OutputDir, base)); err != nil {
			return "its base output no longer exists, so this is the last copy"
		}
	}
	return ""
}

func activeStems(cfg Config) []string {
	if cfg.ActiveStems == nil {
		return nil
	}
	return cfg.ActiveStems()
}

// dirSize is the reclaimed byte count for the log. Unlike tmpstage's measure it
// does not double as a busy check — nothing writes into an archived directory,
// so the top-level mtime is the whole story and the walk is only arithmetic.
func dirSize(root string) int64 {
	var total int64
	_ = filepath.WalkDir(root, func(_ string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if !d.IsDir() {
			if info, err := d.Info(); err == nil {
				total += info.Size()
			}
		}
		return nil
	})
	return total
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
