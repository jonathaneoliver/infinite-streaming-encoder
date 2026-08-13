package encode

import (
	"os"
	"path/filepath"
	"testing"
)

func write(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", path, err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(data)
}

// The default install must be untouched: every path still resolves inside
// TMP_DIR and the migration does nothing at all. This is the case that ships,
// so it is the one that must not move a byte.
func TestUnconfiguredStateStaysInTmpDir(t *testing.T) {
	tmp := t.TempDir()
	write(t, filepath.Join(tmp, "ladders.json"), "{}")

	if got := MigrateDurableState(tmp, tmp, tmp); len(got) != 0 {
		t.Fatalf("migration acted with everything pointing at TMP_DIR: %v", got)
	}
	m := &Manager{TmpDir: tmp}
	if got, want := m.StatePath("ladders.json"), filepath.Join(tmp, "ladders.json"); got != want {
		t.Errorf("StatePath = %s, want %s", got, want)
	}
	if got, want := m.RecordPath("logs"), filepath.Join(tmp, "logs"); got != want {
		t.Errorf("RecordPath = %s, want %s", got, want)
	}
	if got := m.runSamplesPath(); got != filepath.Join(tmp, "spot_samples.json") {
		t.Errorf("runSamplesPath = %s, want it beside TMP_DIR", got)
	}
}

// The whole point of the feature: after a migration, nothing irreplaceable is
// left in TMP_DIR. Checked as "the directory is empty of these names" rather
// than file by file, because a file that fails to move is silent — the encoder
// just replans from a cold model.
func TestMigrationEmptiesTmpDirOfDurableState(t *testing.T) {
	tmp, state, record := t.TempDir(), t.TempDir(), t.TempDir()
	for _, n := range StateFiles {
		write(t, filepath.Join(tmp, n), n+" body")
	}
	write(t, filepath.Join(tmp, "logs", "42.log"), "job log")
	write(t, filepath.Join(tmp, "history.md"), "# history")
	write(t, filepath.Join(tmp, "failed", "42", "job.log"), "failure")
	// Genuinely temporary: must stay exactly where it is.
	write(t, filepath.Join(tmp, "1700000000000", "chunk.mp4"), "scratch")
	write(t, filepath.Join(tmp, "encode_clip", "mezz.mp4"), "mezzanine")

	for _, mg := range MigrateDurableState(tmp, state, record) {
		if mg.Err != nil || mg.Skipped != "" {
			t.Fatalf("migration did not move %s: %v", mg.Name, mg)
		}
	}

	for _, n := range StateFiles {
		if _, err := os.Stat(filepath.Join(tmp, n)); !os.IsNotExist(err) {
			t.Errorf("%s still in TMP_DIR", n)
		}
		if got := read(t, filepath.Join(state, n)); got != n+" body" {
			t.Errorf("%s = %q after migration", n, got)
		}
	}
	for _, n := range RecordEntries {
		if _, err := os.Stat(filepath.Join(tmp, n)); !os.IsNotExist(err) {
			t.Errorf("%s still in TMP_DIR", n)
		}
		if _, err := os.Stat(filepath.Join(record, n)); err != nil {
			t.Errorf("%s not in RECORD_DIR: %v", n, err)
		}
	}
	if got := read(t, filepath.Join(record, "logs", "42.log")); got != "job log" {
		t.Errorf("log content = %q", got)
	}
	if got := read(t, filepath.Join(record, "failed", "42", "job.log")); got != "failure" {
		t.Errorf("nested failed/ artifact = %q", got)
	}
	if got := read(t, filepath.Join(tmp, "1700000000000", "chunk.mp4")); got != "scratch" {
		t.Errorf("job staging was migrated; it is the one thing that should not move")
	}
	if got := read(t, filepath.Join(tmp, "encode_clip", "mezz.mp4")); got != "mezzanine" {
		t.Errorf("mezzanine cache was migrated")
	}
}

// Idempotent, and specifically: the second boot must not overwrite the file the
// first boot moved with an older copy, and must not delete either one. A
// half-migrated state is worse than either end state, so the answer is to keep
// both and say so.
func TestMigrationNeverOverwritesOrDeletesOnRepeat(t *testing.T) {
	tmp, state, record := t.TempDir(), t.TempDir(), t.TempDir()
	write(t, filepath.Join(tmp, "encode_speeds.json"), "learned")
	MigrateDurableState(tmp, state, record)

	// Something puts a file back at the old path — a rolled-back binary, a
	// restored backup, a stray copy.
	write(t, filepath.Join(tmp, "encode_speeds.json"), "stale")
	got := MigrateDurableState(tmp, state, record)

	var reported bool
	for _, mg := range got {
		if mg.Name == "encode_speeds.json" {
			reported = true
			if mg.Skipped == "" {
				t.Errorf("collision not reported: %v", mg)
			}
		}
	}
	if !reported {
		t.Fatalf("second migration said nothing about the collision: %v", got)
	}
	if body := read(t, filepath.Join(state, "encode_speeds.json")); body != "learned" {
		t.Errorf("migrated store overwritten with %q", body)
	}
	if body := read(t, filepath.Join(tmp, "encode_speeds.json")); body != "stale" {
		t.Errorf("source deleted rather than left for the operator: %q", body)
	}
}

// A Manager built with a state dir must read AND write there — the store loads
// happen in NewManager, so a path that only takes effect later would load the
// old file and then quietly write the new one.
func TestManagerUsesTheConfiguredDirs(t *testing.T) {
	tmp, state, record := t.TempDir(), t.TempDir(), t.TempDir()
	write(t, filepath.Join(state, "settings.json"), `{"watcher_enabled":false}`)

	m := NewManager(ManagerConfig{TmpDir: tmp, StateDir: state, RecordDir: record})
	m.InitSettings(true) // the flag default says on; the persisted value says off
	if m.WatcherEnabled() {
		t.Errorf("settings.json in STATE_DIR was not read")
	}
	if got := m.runSamplesPath(); got != filepath.Join(state, "spot_samples.json") {
		t.Errorf("runSamplesPath = %s, want it in STATE_DIR", got)
	}
	if got := m.RecordPath("history.md"); got != filepath.Join(record, "history.md") {
		t.Errorf("history = %s, want it in RECORD_DIR", got)
	}
	m.SetWatcherEnabled(true)
	if _, err := os.Stat(filepath.Join(state, "settings.json")); err != nil {
		t.Errorf("settings written outside STATE_DIR: %v", err)
	}
	if _, err := os.Stat(filepath.Join(tmp, "settings.json")); !os.IsNotExist(err) {
		t.Errorf("settings also written into TMP_DIR")
	}
}

// The spawned worker containers read ladders.json out of a path the CONTAINER
// must be able to open. Without a mount for a state dir that sits outside
// TmpDir, LADDER_STORE points at nothing and the encoder falls back to the
// built-in ladders — a custom ladder then encodes as something else, silently.
func TestWorkerContainerCanReachTheLadderStore(t *testing.T) {
	m := &Manager{
		TmpDir: "/media/tmp", HostTmpDir: "/host/tmp",
		StateDir: "/media/state", HostStateDir: "/host/state",
	}
	args := m.buildRunArgs(&Job{ID: "1"}, "n", "s.py", nil)
	var store, mount string
	for i, a := range args {
		if a == "-e" && len(a) > 0 && i+1 < len(args) && hasPrefix(args[i+1], "LADDER_STORE=") {
			store = args[i+1]
		}
		if a == "-v" && i+1 < len(args) && hasPrefix(args[i+1], "/host/state:") {
			mount = args[i+1]
		}
	}
	if store != "LADDER_STORE=/media/state/ladders.json" {
		t.Errorf("LADDER_STORE = %q, want it in the state dir", store)
	}
	if mount != "/host/state:/media/state:ro" {
		t.Errorf("state mount = %q; the container cannot open %q without it", mount, store)
	}

	// Same dir as TmpDir (the default): already mounted, so mounting it again
	// under a second host path would be wrong, not merely redundant.
	m2 := &Manager{TmpDir: "/media/tmp", HostTmpDir: "/host/tmp", HostStateDir: "/stale"}
	for _, a := range m2.buildRunArgs(&Job{ID: "1"}, "n", "s.py", nil) {
		if hasPrefix(a, "/stale:") {
			t.Errorf("mounted a state dir that is not separate from TMP_DIR: %q", a)
		}
	}
}

func hasPrefix(s, p string) bool { return len(s) >= len(p) && s[:len(p)] == p }
