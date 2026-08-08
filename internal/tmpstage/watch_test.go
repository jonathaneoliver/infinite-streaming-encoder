package tmpstage

import (
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"
)

// mkfile writes a file (creating parents) and backdates the whole chain to age.
func mkfile(t *testing.T, path string, size int, age time.Duration) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, make([]byte, size), 0o644); err != nil {
		t.Fatal(err)
	}
	backdate(t, path, age)
}

func backdate(t *testing.T, path string, age time.Duration) {
	t.Helper()
	when := time.Now().Add(-age)
	if err := os.Chtimes(path, when, when); err != nil {
		t.Fatal(err)
	}
}

// backdateTree ages a directory and everything under it.
func backdateTree(t *testing.T, root string, age time.Duration) {
	t.Helper()
	var paths []string
	if err := filepath.Walk(root, func(p string, _ os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		paths = append(paths, p)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	// Deepest first: touching a file bumps its parent directory's mtime.
	sort.Sort(sort.Reverse(sort.StringSlice(paths)))
	for _, p := range paths {
		backdate(t, p, age)
	}
}

func ids(rs []reclaim) []string {
	var out []string
	for _, r := range rs {
		out = append(out, r.ID)
	}
	sort.Strings(out)
	return out
}

func exists(t *testing.T, path string) bool {
	t.Helper()
	_, err := os.Stat(path)
	return err == nil
}

// The whole point of the sweep: an abandoned job directory goes, and every
// other kind of thing TMP_DIR holds stays — in particular the learned-state
// JSON that sizes every chunk plan, which an age-only sweep would eat.
func TestSweepReclaimsOnlyAbandonedJobDirs(t *testing.T) {
	dir := t.TempDir()

	mkfile(t, filepath.Join(dir, "1776524596533", "h264_1080p.mp4"), 4096, 90*24*time.Hour)
	mkfile(t, filepath.Join(dir, "encode_bucks_bunny_p200", "chunk0.mp4"), 4096, 90*24*time.Hour)
	mkfile(t, filepath.Join(dir, "encode_speeds.json"), 128, 90*24*time.Hour)
	mkfile(t, filepath.Join(dir, "ladders.json"), 128, 90*24*time.Hour)
	mkfile(t, filepath.Join(dir, "quality-curves.json"), 128, 90*24*time.Hour)
	mkfile(t, filepath.Join(dir, "history.md"), 128, 90*24*time.Hour)
	mkfile(t, filepath.Join(dir, "logs", "1776524596533.log"), 128, 90*24*time.Hour)
	mkfile(t, filepath.Join(dir, "failed", "1776524596533", "user-data.log"), 128, 90*24*time.Hour)
	backdateTree(t, dir, 90*24*time.Hour)

	got := sweep(Config{Dir: dir, MaxAge: 24 * time.Hour})
	if want := []string{"1776524596533"}; len(got) != 1 || ids(got)[0] != want[0] {
		t.Fatalf("reclaimed %v, want %v", ids(got), want)
	}
	if got[0].Err != nil {
		t.Fatalf("reclaim error: %v", got[0].Err)
	}
	if got[0].Bytes != 4096 {
		t.Errorf("reclaimed %d bytes, want 4096", got[0].Bytes)
	}

	if exists(t, filepath.Join(dir, "1776524596533")) {
		t.Error("abandoned job dir survived the sweep")
	}
	for _, keep := range []string{
		"encode_bucks_bunny_p200", "encode_speeds.json", "ladders.json",
		"quality-curves.json", "history.md", "logs", "failed",
	} {
		if !exists(t, filepath.Join(dir, keep)) {
			t.Errorf("%s was swept; only ^[0-9]+$ dirs are in scope", keep)
		}
	}
	// The nested failed/<job_id>/ is job-ID-shaped but not at the top level.
	if !exists(t, filepath.Join(dir, "failed", "1776524596533", "user-data.log")) {
		t.Error("failed/<job_id>/ was swept")
	}
}

func TestSweepKeepsActiveAndRecentJobs(t *testing.T) {
	dir := t.TempDir()

	// Old on disk but still queued/running per the Manager.
	mkfile(t, filepath.Join(dir, "1776000000001", "h264_1080p.mp4"), 10, 90*24*time.Hour)
	// Old top-level dir, but an encode is writing deep inside it right now —
	// the case a top-level-mtime check would get wrong.
	mkfile(t, filepath.Join(dir, "1776000000002", "deep", "h264_1080p.mp4"), 10, 0)
	backdate(t, filepath.Join(dir, "1776000000002"), 90*24*time.Hour)
	// Wholly recent.
	mkfile(t, filepath.Join(dir, "1776000000003", "h264_1080p.mp4"), 10, time.Minute)
	// Old and abandoned, but a state file still names it: Reconcile will resume
	// it on the next restart.
	mkfile(t, filepath.Join(dir, "1776000000004", "h264_1080p.mp4"), 10, 90*24*time.Hour)
	backdateTree(t, filepath.Join(dir, "1776000000004"), 90*24*time.Hour)
	mkfile(t, filepath.Join(dir, "jobs", "1776000000004.json"), 10, 90*24*time.Hour)
	// Old, abandoned, unreferenced — the only one that should go.
	mkfile(t, filepath.Join(dir, "1776000000005", "h264_1080p.mp4"), 10, 90*24*time.Hour)
	backdateTree(t, filepath.Join(dir, "1776000000005"), 90*24*time.Hour)

	got := sweep(Config{
		Dir:       dir,
		MaxAge:    24 * time.Hour,
		ActiveIDs: func() []string { return []string{"1776000000001"} },
	})
	if want := "1776000000005"; len(got) != 1 || got[0].ID != want {
		t.Fatalf("reclaimed %v, want [%s]", ids(got), want)
	}
	for _, keep := range []string{"1776000000001", "1776000000002", "1776000000003", "1776000000004"} {
		if !exists(t, filepath.Join(dir, keep)) {
			t.Errorf("%s was reclaimed", keep)
		}
	}
}

func TestSweepNoopWithoutMaxAgeOrDir(t *testing.T) {
	dir := t.TempDir()
	mkfile(t, filepath.Join(dir, "1776000000001", "h264_1080p.mp4"), 10, 90*24*time.Hour)
	backdateTree(t, filepath.Join(dir, "1776000000001"), 90*24*time.Hour)

	// MaxAge 0 is "disabled". Taken literally it would put the cutoff at now
	// and reclaim every job directory on the first sweep, so it must be refused
	// rather than applied.
	if got := sweep(Config{Dir: dir, MaxAge: 0}); len(got) != 0 {
		t.Fatalf("swept %v with MaxAge=0", ids(got))
	}
	if !exists(t, filepath.Join(dir, "1776000000001")) {
		t.Error("job dir reclaimed with MaxAge=0")
	}
	if got := sweep(Config{Dir: filepath.Join(dir, "nope"), MaxAge: time.Hour}); got != nil {
		t.Fatalf("swept %v in a missing dir", ids(got))
	}
}

func TestIsJobID(t *testing.T) {
	for _, tc := range []struct {
		name string
		want bool
	}{
		{"1776524596533", true},
		{"0", true},
		{"", false},
		{"encode_bucks_bunny_p200", false},
		{"logs", false},
		{"failed", false},
		{"encode_speeds.json", false},
		{"1776524596533.json", false},
		{"1776524596533-tmp", false},
		{" 1776524596533", false},
	} {
		if got := isJobID(tc.name); got != tc.want {
			t.Errorf("isJobID(%q) = %v, want %v", tc.name, got, tc.want)
		}
	}
}
