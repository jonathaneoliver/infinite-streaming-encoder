package outarchive

import (
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
)

// archived creates an archived output directory aged `age` with some content,
// plus (unless empty) the live base output it superseded.
func archived(t *testing.T, out, name string, age time.Duration, files ...string) string {
	t.Helper()
	dir := filepath.Join(out, encode.ArchiveDirName, name)
	if err := os.MkdirAll(filepath.Join(dir, "1080p"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "1080p", "seg.m4s"), make([]byte, 2048), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, f := range files {
		if err := os.WriteFile(filepath.Join(dir, f), []byte("{}"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	when := time.Now().Add(-age)
	if err := os.Chtimes(dir, when, when); err != nil {
		t.Fatal(err)
	}
	return dir
}

func liveBase(t *testing.T, out, name string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(out, name), 0o755); err != nil {
		t.Fatal(err)
	}
}

func names(rs []reclaim) []string {
	var out []string
	for _, r := range rs {
		out = append(out, r.Name)
	}
	sort.Strings(out)
	return out
}

func heldNames(hs []held) map[string]string {
	out := map[string]string{}
	for _, h := range hs {
		out[h.Name] = h.Reason
	}
	return out
}

// The ordinary case, and the only one that deletes anything: a superseded copy,
// past its retention, whose live replacement is right there.
func TestReclaimsASupersededCopy(t *testing.T) {
	out := t.TempDir()
	liveBase(t, out, "clip_p200_h264")
	archived(t, out, "clip_p200_h264_20260101", 40*24*time.Hour)
	archived(t, out, "clip_p200_h264_20260601", 2*24*time.Hour) // still young

	got, _ := sweep(Config{OutputDir: out, MaxAge: 30 * 24 * time.Hour})
	if want := []string{"clip_p200_h264_20260101"}; len(got) != 1 || got[0].Name != want[0] {
		t.Fatalf("reclaimed %v, want %v", names(got), want)
	}
	if got[0].Bytes < 2048 {
		t.Errorf("reclaimed bytes = %d; the log would understate what was freed", got[0].Bytes)
	}
	if _, err := os.Stat(filepath.Join(out, encode.ArchiveDirName, "clip_p200_h264_20260101")); !os.IsNotExist(err) {
		t.Errorf("aged copy still on disk")
	}
	if _, err := os.Stat(filepath.Join(out, encode.ArchiveDirName, "clip_p200_h264_20260601")); err != nil {
		t.Errorf("young copy was taken: %v", err)
	}
	if _, err := os.Stat(filepath.Join(out, "clip_p200_h264")); err != nil {
		t.Errorf("the LIVE output was taken: %v", err)
	}
}

// Every rule that holds a directory, checked one at a time — each is a
// different way of saying "this is not a superseded copy", and each failure
// mode is the loss of something that has no other copy.
func TestHoldsWhatMustNotBeDeleted(t *testing.T) {
	out := t.TempDir()
	old := 40 * 24 * time.Hour
	for _, base := range []string{"remote_p200_h264", "pending_p200_h264",
		"keep_p200_h264", "busy_p200_h264", "plain_p200_h264"} {
		liveBase(t, out, base)
	}
	archived(t, out, "remote_p200_h264_20260101", old, encode.RemoteSidecar)
	archived(t, out, "pending_p200_h264_20260101", old, encode.PendingSidecar)
	archived(t, out, "keep_p200_h264_20260101", old, encode.ArchiveKeepFile)
	archived(t, out, "busy_p200_h264_20260101", old)
	archived(t, out, "orphan_p200_h264_20260101", old) // no live base
	archived(t, out, "plain_p200_h264_20260101", old)
	// Not a dated backup at all — someone's own directory inside the archive.
	archived(t, out, "notes", old)

	got, holds := sweep(Config{
		OutputDir:   out,
		MaxAge:      30 * 24 * time.Hour,
		ActiveStems: func() []string { return []string{"busy_p200"} },
	})

	if want := []string{"plain_p200_h264_20260101"}; len(got) != 1 || got[0].Name != want[0] {
		t.Fatalf("reclaimed %v, want exactly %v", names(got), want)
	}
	reasons := heldNames(holds)
	for _, name := range []string{"remote_p200_h264_20260101", "pending_p200_h264_20260101",
		"keep_p200_h264_20260101", "busy_p200_h264_20260101", "orphan_p200_h264_20260101"} {
		if _, err := os.Stat(filepath.Join(out, encode.ArchiveDirName, name)); err != nil {
			t.Errorf("%s was deleted: %v", name, err)
		}
		if reasons[name] == "" {
			t.Errorf("%s survived but the sweep did not say why — an immortal "+
				"directory nobody can see is how this started", name)
		}
	}
	if _, err := os.Stat(filepath.Join(out, encode.ArchiveDirName, "notes")); err != nil {
		t.Errorf("a non-dated directory in the archive was deleted: %v", err)
	}
}

// The orphan opt-in, and the shape of the rule: it is off by default, and
// turning it on is the ONLY thing that changes.
func TestOrphansOnlyGoWhenAskedFor(t *testing.T) {
	out := t.TempDir()
	archived(t, out, "gone_p200_h264_20260101", 40*24*time.Hour)

	if got, holds := sweep(Config{OutputDir: out, MaxAge: 30 * 24 * time.Hour}); len(got) != 0 {
		t.Fatalf("orphan reclaimed by default: %v", names(got))
	} else if len(holds) != 1 {
		t.Fatalf("orphan held without a reason: %v", holds)
	}
	got, _ := sweep(Config{OutputDir: out, MaxAge: 30 * 24 * time.Hour, SweepOrphans: true})
	if len(got) != 1 || got[0].Name != "gone_p200_h264_20260101" {
		t.Fatalf("orphan survived the opt-in: %v", names(got))
	}
}

// Zero max-age means DISABLED, never "everything is old enough" — read
// literally it puts the cutoff at now and takes the whole archive. tmpstage
// learned this at both the loop and the sweep; so does this.
func TestZeroMaxAgeIsDisabledNotEverything(t *testing.T) {
	out := t.TempDir()
	liveBase(t, out, "clip_p200_h264")
	archived(t, out, "clip_p200_h264_20260101", 400*24*time.Hour)

	if got, _ := sweep(Config{OutputDir: out, MaxAge: 0}); len(got) != 0 {
		t.Fatalf("a zero max age swept %v", names(got))
	}
	if _, err := os.Stat(filepath.Join(out, encode.ArchiveDirName, "clip_p200_h264_20260101")); err != nil {
		t.Errorf("archive deleted with sweeping disabled: %v", err)
	}
}

// No archive directory is the normal state of a fresh install, not an error to
// log every interval.
func TestMissingArchiveIsQuiet(t *testing.T) {
	got, holds := sweep(Config{OutputDir: t.TempDir(), MaxAge: time.Hour})
	if got != nil || holds != nil {
		t.Fatalf("sweep of a missing archive returned %v / %v", got, holds)
	}
}
