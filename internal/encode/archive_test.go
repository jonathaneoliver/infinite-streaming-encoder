package encode

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func outputWithContent(t *testing.T, out, name, body string) string {
	t.Helper()
	dir := filepath.Join(out, name)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "manifest.mpd"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

// A re-encode must preserve the copy it replaces, and preserve it OUT OF THE
// WAY: under .archive/, where the dot prefix means every existing "skip hidden
// entries" guard covers it.
func TestReEncodeArchivesThePreviousOutput(t *testing.T) {
	tmp, out := t.TempDir(), t.TempDir()
	old := outputWithContent(t, out, "clip_p200_h264", "old")
	when := time.Date(2026, 6, 1, 12, 0, 0, 0, time.UTC)
	if err := os.Chtimes(old, when, when); err != nil {
		t.Fatal(err)
	}
	outputWithContent(t, tmp, "clip_p200_h264", "new")

	m := &Manager{OutputDir: out}
	if _, err := m.moveTmpToOutput(tmp); err != nil {
		t.Fatalf("move: %v", err)
	}

	body, err := os.ReadFile(filepath.Join(out, "clip_p200_h264", "manifest.mpd"))
	if err != nil || string(body) != "new" {
		t.Fatalf("live output = %q, %v; want the new encode", body, err)
	}
	arch := filepath.Join(out, ArchiveDirName, "clip_p200_h264_20260601")
	body, err = os.ReadFile(filepath.Join(arch, "manifest.mpd"))
	if err != nil || string(body) != "old" {
		t.Fatalf("archived copy = %q, %v; want the previous encode", body, err)
	}
	if _, err := os.Stat(filepath.Join(out, "clip_p200_h264_20260601")); !os.IsNotExist(err) {
		t.Errorf("the backup is still a sibling of the live outputs")
	}
	// The name carries the encode date; the mtime carries when it was
	// superseded, which is the clock the sweeper reads. Without the stamp a
	// re-encode of a year-old output would be instantly older than any
	// retention window.
	fi, err := os.Stat(arch)
	if err != nil {
		t.Fatal(err)
	}
	if time.Since(fi.ModTime()) > time.Minute {
		t.Errorf("archived dir mtime is %s, not the moment it was superseded", fi.ModTime())
	}
}

// Two re-encodes on the same day must not collapse into one archived copy.
func TestSameDayReEncodesBothSurvive(t *testing.T) {
	out := t.TempDir()
	when := time.Date(2026, 6, 1, 12, 30, 45, 0, time.UTC)
	for i, body := range []string{"first", "second"} {
		dir := outputWithContent(t, out, "clip_p200_h264", body)
		stamp := when.Add(time.Duration(i) * time.Hour)
		if err := os.Chtimes(dir, stamp, stamp); err != nil {
			t.Fatal(err)
		}
		m := &Manager{OutputDir: out}
		m.archiveExisting("clip_p200_h264", stamp)
	}
	entries, err := os.ReadDir(filepath.Join(out, ArchiveDirName))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 {
		var got []string
		for _, e := range entries {
			got = append(got, e.Name())
		}
		t.Fatalf("archive holds %v; a same-day re-encode overwrote the earlier copy", got)
	}
}

// The one-time collection of what earlier versions left beside the live
// outputs. Idempotent, and it must not touch anything else in OUTPUT_DIR.
func TestMigrateCollectsDatedBackupsOnly(t *testing.T) {
	out := t.TempDir()
	outputWithContent(t, out, "clip_p200_h264", "live")
	outputWithContent(t, out, "clip_p200_h264_20260101", "backup")
	outputWithContent(t, out, "clip_p200_h264_20260101_143000", "same-day backup")
	outputWithContent(t, out, "other_p200_hevc", "live")

	m := &Manager{OutputDir: out}
	if n := m.MigrateDatedBackups(); n != 2 {
		t.Fatalf("collected %d, want 2", n)
	}
	if n := m.MigrateDatedBackups(); n != 0 {
		t.Errorf("second pass collected %d; it is not idempotent", n)
	}
	for _, name := range []string{"clip_p200_h264", "other_p200_hevc"} {
		if _, err := os.Stat(filepath.Join(out, name)); err != nil {
			t.Errorf("live output %s was collected: %v", name, err)
		}
	}
	for _, name := range []string{"clip_p200_h264_20260101", "clip_p200_h264_20260101_143000"} {
		if _, err := os.Stat(filepath.Join(out, name)); !os.IsNotExist(err) {
			t.Errorf("%s left at the old path", name)
		}
		fi, err := os.Stat(filepath.Join(out, ArchiveDirName, name))
		if err != nil {
			t.Fatalf("%s not in the archive: %v", name, err)
		}
		// Stamped as archived NOW: on the master this moves 168 directories at
		// once, and an unstamped migration would hand every one of them to the
		// sweeper's first pass.
		if time.Since(fi.ModTime()) > time.Minute {
			t.Errorf("%s kept its old mtime; the retention window does not start at the upgrade", name)
		}
	}
}

// Never overwrite: a name present on both sides stays where it is, the same
// rule the state migration follows.
func TestMigrateNeverOverwritesAnArchivedCopy(t *testing.T) {
	out := t.TempDir()
	outputWithContent(t, out, "clip_p200_h264_20260101", "at the old path")
	if err := os.MkdirAll(filepath.Join(out, ArchiveDirName), 0o755); err != nil {
		t.Fatal(err)
	}
	outputWithContent(t, filepath.Join(out, ArchiveDirName), "clip_p200_h264_20260101", "already archived")

	m := &Manager{OutputDir: out}
	if n := m.MigrateDatedBackups(); n != 0 {
		t.Fatalf("collected %d; it overwrote an archived copy", n)
	}
	body, _ := os.ReadFile(filepath.Join(out, ArchiveDirName, "clip_p200_h264_20260101", "manifest.mpd"))
	if string(body) != "already archived" {
		t.Errorf("archived copy = %q", body)
	}
	if _, err := os.Stat(filepath.Join(out, "clip_p200_h264_20260101")); err != nil {
		t.Errorf("the copy at the old path was deleted rather than left: %v", err)
	}
}

// The sweeper asks "does the base still exist" of this, so it has to agree with
// the rule that recognised the directory as a backup in the first place.
func TestDatedBackupBaseIsTheInverseOfIsDatedBackup(t *testing.T) {
	cases := map[string]string{
		"clip_p200_h264_20260101":        "clip_p200_h264",
		"clip_p200_h264_20260101_143000": "clip_p200_h264",
		"clip_p200_h264_xs_20260101":     "clip_p200_h264_xs",
		// A live output is never a backup, and its name comes back untouched.
		"clip_p200_h264": "clip_p200_h264",
	}
	for name, want := range cases {
		if got := DatedBackupBase(name); got != want {
			t.Errorf("DatedBackupBase(%q) = %q, want %q", name, got, want)
		}
		if IsDatedBackup(name) == (name == want) {
			t.Errorf("IsDatedBackup(%q) disagrees with DatedBackupBase", name)
		}
	}
}

// The keep-list the sweeper holds on: a job that is re-encoding a clip right
// now must keep the copy it is about to supersede.
func TestActiveOutputStemsCoversQueuedAndRunning(t *testing.T) {
	m := &Manager{jobs: []*Job{
		{Status: StatusRunning, Config: JobConfig{Files: []string{"clip.mp4"}}},
		{Status: StatusQueued, Config: JobConfig{Files: []string{"other.mov"}, Padding: "black"}},
		{Status: StatusDone, Config: JobConfig{Files: []string{"finished.mp4"}}},
	}}
	got := m.ActiveOutputStems()
	want := map[string]bool{"clip_p200": true, "other_p200_padblack": true}
	if len(got) != len(want) {
		t.Fatalf("stems = %v, want %v", got, want)
	}
	for _, s := range got {
		if !want[s] {
			t.Errorf("unexpected stem %q", s)
		}
	}
}
