package encode

import (
	"log"
	"os"
	"path/filepath"
	"time"
)

// Superseded outputs (#332).
//
// A re-encode never destroys the previous output: it preserves it under a dated
// name. That part is old. What changed is WHERE — these used to be renamed in
// place, as siblings of the live outputs, and nothing ever removed them. On the
// master they reached 168 directories and ~158 GB against a 208 GB OUTPUT_DIR:
// most of the volume was superseded copies of a handful of clips.
//
// Four call sites already agreed to ignore them (`/api/outputs`, output
// metadata, promote, the watcher's already-encoded check), which is a good sign
// they did not belong in that directory. Ignoring them was not free either —
// `/api/outputs` still walked and classified every one, which is exactly the
// cost #228 was about.
//
// So they live under OUTPUT_DIR/.archive/ now. Two consequences worth stating:
// the dot prefix means the `strings.HasPrefix(name, ".")` guards that already
// exist cover them, so the four `IsDatedBackup` checks become belt-and-braces
// rather than load-bearing; and one directory to skip beats N entries to stat.
//
// CLAUDE.md has described this layout since long before it was true. The doc was
// not wrong about the intent, only about the tense.

// ArchiveDirName is the single definition of where superseded outputs go,
// relative to OutputDir. Dot-prefixed deliberately: every existing "skip
// hidden entries" guard in the tree then covers the whole archive for free.
const ArchiveDirName = ".archive"

// ArchiveKeepFile makes an archived directory permanent when present in it. It
// exists because the alternative is remembering: an A/B pair kept as evidence
// (…_1440grp and …_1440solo) is not a backup, and an age sweeper would
// eventually eat it. Nothing writes this — it is for a human.
const ArchiveKeepFile = ".keep"

// ArchiveDir is OUTPUT_DIR/.archive.
func (m *Manager) ArchiveDir() string { return filepath.Join(m.OutputDir, ArchiveDirName) }

// DatedBackupBase is the live output directory an archived copy superseded —
// its name with the dated suffix removed. It shares datedBackupRe with
// IsDatedBackup deliberately: a second rule for taking the suffix off could
// disagree with the rule that recognised it, and the consequence would be the
// sweeper asking whether the wrong base still exists.
func DatedBackupBase(name string) string {
	return datedBackupRe.ReplaceAllString(name, "")
}

// archiveExisting moves an output directory that a re-encode is about to
// replace into the archive, under a dated name.
//
// The timestamp is the REPLACED copy's own mtime, so the name still says when
// that output was encoded — but the archived directory's mtime is stamped to
// NOW, because retention here means "how long a superseded copy is kept", and
// that clock starts when it is superseded, not when it was made. Without the
// stamp a re-encode of a year-old output would produce an archive that is
// instantly older than any retention window, and the sweeper would take it
// before anyone could compare old against new.
//
// Preserving beats tidying: if the move fails for any reason, it falls back to
// the old in-place rename rather than leaving the copy where the incoming
// encode is about to land on it.
func (m *Manager) archiveExisting(name string, modTime time.Time) {
	dst := filepath.Join(m.OutputDir, name)
	stamped := name + "_" + modTime.Format("20060102")
	if err := os.MkdirAll(m.ArchiveDir(), 0o755); err == nil {
		target := filepath.Join(m.ArchiveDir(), stamped)
		// A same-day re-encode disambiguates with a time suffix.
		if _, err := os.Stat(target); err == nil {
			target = filepath.Join(m.ArchiveDir(), stamped+"_"+modTime.Format("150405"))
		}
		err := os.Rename(dst, target)
		if err == nil {
			stampArchived(target)
			return
		}
		log.Printf("archive: could not move %s into %s (%v) — preserving it in place",
			name, ArchiveDirName, err)
	}
	// Legacy path: a sibling of the live outputs. Still preserved, still
	// recognised by IsDatedBackup, and the next startup migration collects it.
	legacy := filepath.Join(m.OutputDir, stamped)
	if _, err := os.Stat(legacy); err == nil {
		legacy = filepath.Join(m.OutputDir, stamped+"_"+modTime.Format("150405"))
	}
	os.Rename(dst, legacy)
}

// stampArchived records WHEN a directory was superseded, in the only place a
// sweeper can cheaply read it. Best-effort: a filesystem that refuses says so
// in the log, because the consequence is an archive that reads older than it is.
func stampArchived(dir string) {
	now := time.Now()
	if err := os.Chtimes(dir, now, now); err != nil {
		log.Printf("archive: could not stamp %s (%v) — its retention clock will "+
			"read from the encode date instead of from now", filepath.Base(dir), err)
	}
}

// MigrateDatedBackups collects the dated backups that earlier versions left as
// siblings of the live outputs into OUTPUT_DIR/.archive/. Idempotent, and cheap
// — a rename within one directory tree — so it runs at every startup.
//
// Each one is stamped as archived NOW rather than keeping the mtime it has
// carried since it was encoded. That is the conservative choice and it is
// deliberate: on the master this moves 168 directories at once, and an
// unstamped migration would hand every one of them to the sweeper's first pass.
// Stamping starts the retention window at the upgrade instead, which is the
// operator's chance to mark anything worth keeping (see ArchiveKeepFile). The
// date each output was encoded is still in its name.
func (m *Manager) MigrateDatedBackups() (moved int) {
	entries, err := os.ReadDir(m.OutputDir)
	if err != nil {
		return 0
	}
	for _, e := range entries {
		name := e.Name()
		if !e.IsDir() || name == ArchiveDirName || !IsDatedBackup(name) {
			continue
		}
		if err := os.MkdirAll(m.ArchiveDir(), 0o755); err != nil {
			log.Printf("archive: cannot create %s: %v", m.ArchiveDir(), err)
			return moved
		}
		target := filepath.Join(m.ArchiveDir(), name)
		if _, err := os.Stat(target); err == nil {
			// Same name on both sides. Never overwrite — leave it where it is
			// and say so, the same rule the state migration follows.
			log.Printf("archive: %s exists in %s already; leaving the copy at the old path",
				name, ArchiveDirName)
			continue
		}
		if err := os.Rename(filepath.Join(m.OutputDir, name), target); err != nil {
			log.Printf("archive: could not collect %s: %v", name, err)
			continue
		}
		stampArchived(target)
		moved++
	}
	return moved
}

// ActiveOutputStems lists the output-name stems of every queued or running job
// — `<stem>_p200[_padblack]`, the prefix every directory those jobs produce
// begins with. The archive sweeper holds anything matching, so a re-encode can
// never have its own predecessor reclaimed while it is still running: the same
// guarantee ActiveJobIDs gives TMP_DIR and ActiveDistPrefixes gives MinIO.
func (m *Manager) ActiveOutputStems() []string {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []string
	for _, j := range m.jobs {
		if j.Status != StatusQueued && j.Status != StatusRunning {
			continue
		}
		for _, f := range j.Config.Files {
			out = append(out, j.Config.OutputStem(f))
		}
	}
	return out
}
