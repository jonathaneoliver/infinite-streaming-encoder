package encode

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// #225: "not yet expired" was standing in for "still there". Everything that
// removes objects other than the clock — a staging clear, a console delete, the
// lifecycle firing early — produced an output that looked perfectly healthy
// right up until the Download click, then threw.
//
// Each property below is INVISIBLE in a working system: they only differ once
// the media is already gone, which is exactly when nobody is watching.

// writeSidecar lives in remote_test.go — same package, same fixture.

func TestFetchableSeparatesTheThreeStates(t *testing.T) {
	future := time.Now().Add(24 * time.Hour).UTC().Format(time.RFC3339)
	past := time.Now().Add(-24 * time.Hour).UTC().Format(time.RFC3339)

	for _, tc := range []struct {
		name string
		info *RemoteInfo
		want bool
	}{
		{"available", &RemoteInfo{S3Prefix: "s3://b/jobs/1/o", ExpiresAt: future}, true},
		{"expired", &RemoteInfo{S3Prefix: "s3://b/jobs/1/o", ExpiresAt: past}, false},
		// The state that used to be missing: in date, but the objects are gone.
		// This is the one that rendered as "available" and failed on click.
		{"deleted", &RemoteInfo{S3Prefix: "s3://b/jobs/1/o", ExpiresAt: future, Gone: true}, false},
		{"deleted and expired", &RemoteInfo{S3Prefix: "s3://b/jobs/1/o", ExpiresAt: past, Gone: true}, false},
		{"not remote at all", nil, false},
	} {
		if got := tc.info.Fetchable(); got != tc.want {
			t.Errorf("%s: Fetchable() = %v, want %v", tc.name, got, tc.want)
		}
	}
}

func TestMarkRemoteGonePreservesTheRecord(t *testing.T) {
	dir := t.TempDir()
	writeSidecar(t, dir, RemoteInfo{
		S3Prefix: "s3://b/jobs/1786-clip/output_h264", PendingFiles: 44,
		PendingBytes: 4522825, ExpiresAt: "2026-08-13T21:01:42Z", ExpiryDays: 7,
	})

	ok, err := MarkRemoteGone(dir, "staging cleared")
	if err != nil || !ok {
		t.Fatalf("MarkRemoteGone = %v, %v", ok, err)
	}

	// The sidecar must SURVIVE. Deleting it is the one wrong answer available:
	// the output would be reclassified as complete, and every other signal
	// agrees with that lie — right name, right rung subdirs, manifests present.
	got := ReadRemote(dir)
	if got == nil {
		t.Fatal("sidecar deleted — the output now reads as complete")
	}
	if !got.Gone || got.Fetchable() {
		t.Fatalf("not marked unfetchable: %+v", got)
	}
	if got.GoneDetectedAt == "" || got.GoneReason != "staging cleared" {
		t.Fatalf("provenance lost: %+v", got)
	}
	// The original record has to come through, or the UI can no longer say how
	// much was lost or where it was.
	if got.PendingFiles != 44 || got.PendingBytes != 4522825 ||
		got.S3Prefix != "s3://b/jobs/1786-clip/output_h264" ||
		got.ExpiresAt != "2026-08-13T21:01:42Z" {
		t.Fatalf("original record not preserved: %+v", got)
	}

	// Idempotent: a second clear must not report a change it did not make.
	if ok, _ := MarkRemoteGone(dir, "again"); ok {
		t.Error("re-marking reported a change")
	}
	if ReadRemote(dir).GoneReason != "staging cleared" {
		t.Error("re-marking overwrote the original reason")
	}
}

func TestMarkRemoteGoneIgnoresNonRemoteOutputs(t *testing.T) {
	// A fully-downloaded output has no sidecar. A clear must not invent one —
	// that would make a complete output look like a loss.
	dir := t.TempDir()
	if ok, err := MarkRemoteGone(dir, "cleared"); ok || err != nil {
		t.Fatalf("MarkRemoteGone on a local output = %v, %v", ok, err)
	}
	if _, err := os.Stat(filepath.Join(dir, RemoteSidecar)); !os.IsNotExist(err) {
		t.Fatal("a sidecar was created for an output that had none")
	}
}

func TestMarkGoneUnderPrefixMatchesTheJobPrefixNotTheExactKey(t *testing.T) {
	out := t.TempDir()
	m := &Manager{OutputDir: out}

	// The sidecar points at a codec subdirectory INSIDE the job prefix, but
	// cleanup deletes the job prefix. An equality test would match neither of
	// these, which is the whole reason this is a prefix test.
	writeSidecar(t, filepath.Join(out, "clip_h264"), RemoteInfo{
		S3Prefix: "s3://b/jobs/1786-clip/output_h264", PendingFiles: 40})
	writeSidecar(t, filepath.Join(out, "clip_hevc"), RemoteInfo{
		S3Prefix: "s3://b/jobs/1786-clip/output_hevc", PendingFiles: 40})
	// Same prefix as a STRING PREFIX but a different job — jobs/1786-clip2 must
	// not be swept up by a delete of jobs/1786-clip.
	writeSidecar(t, filepath.Join(out, "clip2_h264"), RemoteInfo{
		S3Prefix: "s3://b/jobs/1786-clip2/output_h264", PendingFiles: 40})
	// A different bucket entirely.
	writeSidecar(t, filepath.Join(out, "other_h264"), RemoteInfo{
		S3Prefix: "s3://other/jobs/1786-clip/output_h264", PendingFiles: 40})
	// Not remote at all.
	if err := os.MkdirAll(filepath.Join(out, "local_h264"), 0o755); err != nil {
		t.Fatal(err)
	}

	// Trailing slash, as cleanup.py reports it.
	n := m.MarkGoneUnderPrefix("s3://b/jobs/1786-clip/", "prefix deleted")
	if n != 2 {
		t.Fatalf("marked %d outputs, want 2", n)
	}
	for _, name := range []string{"clip_h264", "clip_hevc"} {
		if r := ReadRemote(filepath.Join(out, name)); r == nil || !r.Gone {
			t.Errorf("%s not marked gone", name)
		}
	}
	for _, name := range []string{"clip2_h264", "other_h264"} {
		if r := ReadRemote(filepath.Join(out, name)); r == nil || r.Gone {
			t.Errorf("%s wrongly marked gone — a sibling prefix was swept up", name)
		}
	}
	if r := ReadRemote(filepath.Join(out, "local_h264")); r != nil {
		t.Error("a sidecar appeared in an output that had none")
	}

	// Idempotent across repeated clears.
	if again := m.MarkGoneUnderPrefix("s3://b/jobs/1786-clip/", "again"); again != 0 {
		t.Errorf("second sweep reported %d changes, want 0", again)
	}
}

func TestMarkGoneUnderPrefixRefusesAnEmptyOrRelativePrefix(t *testing.T) {
	// A bug upstream that produced "" or "/" must not mark EVERY output gone.
	out := t.TempDir()
	m := &Manager{OutputDir: out}
	writeSidecar(t, filepath.Join(out, "clip_h264"), RemoteInfo{
		S3Prefix: "s3://b/jobs/1786-clip/output_h264"})
	for _, bad := range []string{"", "/", "jobs/", "s3:/"} {
		if n := m.MarkGoneUnderPrefix(bad, "oops"); n != 0 {
			t.Errorf("prefix %q marked %d outputs", bad, n)
		}
	}
	if ReadRemote(filepath.Join(out, "clip_h264")).Gone {
		t.Fatal("a malformed prefix marked a live output gone")
	}
}

func TestFetchOutputRefusesWhenTheMediaIsGone(t *testing.T) {
	// The acceptance criterion, at the API layer: the click reports the reason
	// rather than shelling out to a fetch that cannot succeed.
	out := t.TempDir()
	m := &Manager{OutputDir: out}
	dir := filepath.Join(out, "clip_h264")
	writeSidecar(t, dir, RemoteInfo{
		S3Prefix:  "s3://b/jobs/1786-clip/output_h264",
		Gone:      true,
		ExpiresAt: time.Now().Add(24 * time.Hour).UTC().Format(time.RFC3339),
	})
	err := m.FetchOutput("clip_h264")
	if err == nil {
		t.Fatal("fetch of deleted media was accepted")
	}
	if m.FetchStateFor("clip_h264") != nil {
		t.Error("a fetch was registered for media that cannot be fetched")
	}
}
