package encode

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

// The remote sidecar is the ONLY thing standing between "this output plays" and
// "hls.js 404s on every segment" (#214). A metadata-only run leaves a directory
// that looks complete — correct name, correct rung subdirs, manifests present —
// so nothing else in the system can tell the difference. These pin that.

func writeSidecar(t *testing.T, dir string, info RemoteInfo) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	b, err := json.Marshal(info)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, RemoteSidecar), b, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestReadRemoteAbsentMeansLocal(t *testing.T) {
	dir := t.TempDir()
	if got := ReadRemote(dir); got != nil {
		t.Fatalf("no sidecar should mean local, got %+v", got)
	}
	// A normal full download is the common case and must stay cheap: absence is
	// the signal, so nothing is parsed and Play stays enabled.
	if ReadRemote(dir).Expired() {
		t.Fatal("nil RemoteInfo must not report expired")
	}
}

func TestReadRemoteIgnoresUnusableSidecar(t *testing.T) {
	// A truncated or half-written sidecar must not strand an output as
	// permanently "remote" with no prefix to fetch from — treat it as local and
	// let the files on disk speak.
	for name, body := range map[string]string{
		"malformed":    `{"s3_prefix": "s3://b/k"`,
		"no_prefix":    `{"pending_files": 12}`,
		"empty_object": `{}`,
	} {
		dir := t.TempDir()
		if err := os.WriteFile(filepath.Join(dir, RemoteSidecar), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		if got := ReadRemote(dir); got != nil {
			t.Fatalf("%s: expected nil, got %+v", name, got)
		}
	}
}

func TestReadRemoteRoundTripsThePythonContract(t *testing.T) {
	dir := t.TempDir()
	writeSidecar(t, dir, RemoteInfo{
		S3Prefix:     "s3://bucket/jobs/j1-clip/output_h264",
		PendingFiles: 728,
		PendingBytes: 2_628_899_717,
		ExpiresAt:    "2099-01-01T00:00:00Z",
		ExpiryDays:   7,
	})
	got := ReadRemote(dir)
	if got == nil {
		t.Fatal("sidecar not read")
	}
	if got.S3Prefix != "s3://bucket/jobs/j1-clip/output_h264" ||
		got.PendingFiles != 728 || got.PendingBytes != 2_628_899_717 {
		t.Fatalf("field names must match _write_remote_sidecar: %+v", got)
	}
}

func TestExpiredGatesTheDownloadButton(t *testing.T) {
	past := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	future := time.Now().Add(time.Hour).UTC().Format(time.RFC3339)

	if !(&RemoteInfo{ExpiresAt: past}).Expired() {
		t.Fatal("past expiry must report expired — the media is gone from S3")
	}
	if (&RemoteInfo{ExpiresAt: future}).Expired() {
		t.Fatal("future expiry must be fetchable")
	}
	// Unparseable or missing: assume fetchable and let the fetch report the
	// truth. Refusing to try is worse than trying and failing.
	if (&RemoteInfo{ExpiresAt: "not a date"}).Expired() {
		t.Fatal("unparseable expiry must not block the fetch")
	}
	if (&RemoteInfo{}).Expired() {
		t.Fatal("absent expiry must not block the fetch")
	}
}

func TestFetchOutputRejectsPathEscape(t *testing.T) {
	m := &Manager{OutputDir: t.TempDir()}
	// The name arrives from a request path; a traversal must never resolve to a
	// directory outside OutputDir.
	for _, bad := range []string{"", ".", "..", "../etc", "a/b", `a\b`} {
		if err := m.FetchOutput(bad); err == nil {
			t.Fatalf("%q was accepted", bad)
		}
	}
}

func TestFetchOutputRefusesWhenNothingIsPending(t *testing.T) {
	out := t.TempDir()
	m := &Manager{OutputDir: out}
	if err := os.MkdirAll(filepath.Join(out, "clip_h264"), 0o755); err != nil {
		t.Fatal(err)
	}
	// No sidecar: the media is already local. Clicking Download here would
	// re-pay for bytes on disk, which is the whole thing this issue exists to
	// stop, so it must not start a transfer.
	if err := m.FetchOutput("clip_h264"); err == nil {
		t.Fatal("fetch started for an output with no pending media")
	}
}

func TestFetchOutputRefusesExpiredStaging(t *testing.T) {
	out := t.TempDir()
	m := &Manager{OutputDir: out}
	writeSidecar(t, filepath.Join(out, "clip_h264"), RemoteInfo{
		S3Prefix:  "s3://bucket/jobs/j1/output_h264",
		ExpiresAt: time.Now().Add(-time.Hour).UTC().Format(time.RFC3339),
	})
	err := m.FetchOutput("clip_h264")
	if err == nil {
		t.Fatal("fetch started against expired staging")
	}
	if err == ErrFetchInFlight {
		t.Fatal("wrong error")
	}
}

func TestSkipMediaDownloadPrecedence(t *testing.T) {
	m := &Manager{}
	yes, no := true, false

	// Explicit per-run choice beats the server default in BOTH directions —
	// otherwise a developer default silently makes a keeper run metadata-only.
	for _, tc := range []struct {
		name string
		def  bool
		cfg  *bool
		want bool
	}{
		{"default off, unset", false, nil, false},
		{"default on, unset", true, nil, true},
		{"default off, opt in", false, &yes, true},
		{"default on, opt out", true, &no, false},
	} {
		prev := SkipMediaDownloadDefault
		SkipMediaDownloadDefault = tc.def
		got := m.skipMediaDownload(JobConfig{SkipMediaDownload: tc.cfg})
		SkipMediaDownloadDefault = prev
		if got != tc.want {
			t.Errorf("%s: got %v want %v", tc.name, got, tc.want)
		}
	}
}

// The fetch runs a subprocess and scrapes its stdout. Nothing else exercises
// that plumbing, and its failure mode is the worst kind: an output stuck at
// "downloading" forever, with Play disabled and no error shown. This pins that
// the goroutine always reaches a terminal state.
func TestRunFetchAlwaysReachesATerminalState(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH")
	}
	out := t.TempDir()
	m := &Manager{OutputDir: out, fetches: map[string]*FetchState{}}
	dir := filepath.Join(out, "clip_h264")
	writeSidecar(t, dir, RemoteInfo{
		// Deliberately unreachable: no bucket, no creds needed to prove the
		// point. Whether python exits 0 (nothing pending) or non-zero (import
		// error, AWS error), the state must not stay "fetching".
		S3Prefix:  "s3://nonexistent-bucket-for-test/jobs/j/output_h264",
		ExpiresAt: time.Now().Add(time.Hour).UTC().Format(time.RFC3339),
	})
	m.fetches["clip_h264"] = &FetchState{Name: "clip_h264", State: "fetching"}

	done := make(chan struct{})
	go func() { m.runFetch("clip_h264", dir); close(done) }()
	select {
	case <-done:
	case <-time.After(60 * time.Second):
		t.Fatal("runFetch never returned — a stuck fetch disables Play forever")
	}
	if st := m.FetchStateFor("clip_h264"); st != nil && st.State == "fetching" {
		t.Fatal("terminal state not recorded; UI would spin indefinitely")
	}
}

func TestFetchStateForIsACopy(t *testing.T) {
	m := &Manager{fetches: map[string]*FetchState{
		"a": {Name: "a", State: "fetching", Percent: 40},
	}}
	got := m.FetchStateFor("a")
	if got == nil {
		t.Fatal("missing state")
	}
	got.Percent = 99
	// The handler serialises this while the fetch goroutine writes to the
	// original; handing out the pointer would be a data race.
	if m.fetches["a"].Percent != 40 {
		t.Fatal("FetchStateFor handed out the live pointer")
	}
	if m.FetchStateFor("nope") != nil {
		t.Fatal("unknown output should have no fetch state")
	}
}
