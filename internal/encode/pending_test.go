package encode

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func writePending(t *testing.T, dir string, p PendingInfo) {
	t.Helper()
	b, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, PendingSidecar), b, 0o644); err != nil {
		t.Fatal(err)
	}
}

// Deferring means NOBODY packages — not the state machine, not the host. Miss
// either half and two packagers write the same output prefix, or one does when
// the sidecar says none did.
func TestDeferPackagingLeavesNothingPackaging(t *testing.T) {
	doc := sfnInputForDefer(t, "both", true, true)
	for _, k := range []string{"do_h264", "do_hevc", "do_av1"} {
		if doc[k] != false {
			t.Errorf("%s is %v with packaging deferred — the SFN would package it", k, doc[k])
		}
	}
	if got := hostPackageList(t, doc); len(got) != 0 {
		t.Errorf("host_package = %v with packaging deferred, want empty", got)
	}
	if doc["defer_packaging"] != true {
		t.Error("defer_packaging is not set in the SFN input")
	}
}

// The run plan is built from this, and deferring empties do_* and host_package
// both — so without it a perfectly good two-codec run reports encoding nothing.
func TestEncodedCodecsSurvivesDeferring(t *testing.T) {
	for _, tc := range []struct {
		name   string
		defer_ bool
	}{{"deferred", true}, {"packaged", false}} {
		t.Run(tc.name, func(t *testing.T) {
			doc := sfnInputForDefer(t, "both", true, tc.defer_)
			raw, ok := doc["encoded_codecs"].([]any)
			if !ok {
				t.Fatalf("encoded_codecs is %T, not a list", doc["encoded_codecs"])
			}
			if len(raw) != 2 || raw[0] != "h264" || raw[1] != "hevc" {
				t.Errorf("encoded_codecs = %v, want [h264 hevc]", raw)
			}
		})
	}
}

// Three states, and the worst one must not be reachable by accident. An output
// that is merely pending offers Package; one whose chunks are gone must not,
// because unlike a remote output there is no partial result to fall back on.
func TestPendingStatesAreDistinct(t *testing.T) {
	future := time.Now().Add(48 * time.Hour).UTC().Format(time.RFC3339)
	past := time.Now().Add(-1 * time.Hour).UTC().Format(time.RFC3339)

	live := &PendingInfo{ExpiresAt: future}
	if !live.Packageable() || live.Unrecoverable() {
		t.Error("a live pending output is not packageable")
	}

	expired := &PendingInfo{ExpiresAt: past}
	if expired.Packageable() || !expired.Unrecoverable() {
		t.Error("an expired pending output is still offered")
	}

	gone := &PendingInfo{ExpiresAt: future, Gone: true}
	if gone.Packageable() || !gone.Unrecoverable() {
		t.Error("a gone pending output is still offered — the chunks were observed absent")
	}

	// nil is "not pending", i.e. a normal packaged output: neither packageable
	// nor unrecoverable. Getting this wrong would put every ordinary output into
	// an error state.
	var none *PendingInfo
	if none.Packageable() || none.Unrecoverable() {
		t.Error("a packaged output reads as pending")
	}
}

// An absent or unparseable stamp must NOT read as expired. Both sidecars share
// this reading, and denying an action because a date could not be parsed would
// refuse the user something that is probably still there.
func TestUnparseableExpiryIsNotExpired(t *testing.T) {
	for _, s := range []string{"", "not a date", "7 days"} {
		if stagingExpired(s) {
			t.Errorf("%q read as expired", s)
		}
		if (&PendingInfo{ExpiresAt: s}).Expired() {
			t.Errorf("pending %q read as expired", s)
		}
		if (&RemoteInfo{ExpiresAt: s}).Expired() {
			t.Errorf("remote %q read as expired", s)
		}
	}
}

// Gone is SET, never signalled by deleting the file. Deleting it would
// reclassify the output as complete — and a pending dir holds one JSON file, so
// the UI would offer Play on a directory with no media whatsoever.
func TestMarkPendingGoneSetsRatherThanDeletes(t *testing.T) {
	dir := t.TempDir()
	writePending(t, dir, PendingInfo{S3Prefix: "s3://b/jobs/1-x", Codec: "h264"})

	changed, err := MarkPendingGone(dir, "staging cleared")
	if err != nil || !changed {
		t.Fatalf("MarkPendingGone: changed=%v err=%v", changed, err)
	}
	if _, err := os.Stat(filepath.Join(dir, PendingSidecar)); err != nil {
		t.Fatal("the sidecar was deleted — the output now reads as complete")
	}
	got := ReadPending(dir)
	if got == nil || !got.Gone || got.GoneReason != "staging cleared" {
		t.Fatalf("gone not recorded: %+v", got)
	}
	if got.S3Prefix != "s3://b/jobs/1-x" || got.Codec != "h264" {
		t.Error("marking gone dropped fields the packager needs")
	}
	// Idempotent: a second sweep reports nothing changed.
	if changed, _ := MarkPendingGone(dir, "again"); changed {
		t.Error("re-marking an already-gone sidecar reported a change")
	}
}

// PackageOutput must refuse before spawning anything when the chunks are known
// to be gone, and say which of the two reasons it is.
func TestPackageOutputRefusesUnrecoverable(t *testing.T) {
	out := t.TempDir()
	m := &Manager{OutputDir: out}

	mk := func(name string, p PendingInfo) string {
		d := filepath.Join(out, name)
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		writePending(t, d, p)
		return name
	}

	goneName := mk("gone_h264", PendingInfo{Gone: true, GoneReason: "cleared"})
	if err := m.PackageOutput(goneName); err == nil {
		t.Error("packaged an output whose chunks were observed absent")
	}

	expName := mk("exp_h264", PendingInfo{
		ExpiresAt: time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)})
	if err := m.PackageOutput(expName); err == nil {
		t.Error("packaged an output whose chunk staging expired")
	}

	// No sidecar at all: already packaged, so this is a mistake worth naming
	// rather than a silent no-op that leaves a spinner running.
	plain := filepath.Join(out, "done_h264")
	if err := os.MkdirAll(plain, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := m.PackageOutput("done_h264"); err == nil {
		t.Error("packaging a finished output was accepted")
	}
}

// Deferring supersedes skip-media-download; they must not both be honoured.
// Together they would ask for a .remote.json describing media that was never
// created — two sidecars in one directory, disagreeing.
func TestDeferPackagingSupersedesSkipMediaDownload(t *testing.T) {
	m := &Manager{}
	yes := true

	if !m.deferPackaging(JobConfig{DeferPackaging: &yes}) {
		t.Fatal("the per-job override is ignored")
	}
	// With packaging deferred there is nothing for host packaging to do, and the
	// SFN input must reflect that regardless of what skip-media says.
	doc := sfnInputForDefer(t, "h264", true, true)
	if doc["do_h264"] != false || len(hostPackageList(t, doc)) != 0 {
		t.Error("something still packages a deferred run")
	}
}

func sfnInputForDefer(t *testing.T, codecSel string, packageOnHost, deferPkg bool) map[string]any {
	t.Helper()
	in, _, err := buildSFNInput(LoadLadderStore(""), LoadEncodeSpeedStore(""),
		"s3://in/x.mp4", "s3://p", "s3://m", "apple-uniq-live-xs", codecSel,
		"", "", false, false, true, packageOnHost, deferPkg, 3840, 30, 334.4, 0,
		"12", "6", "0.2", "1.0", 9000, nil, nil)
	if err != nil {
		t.Fatalf("buildSFNInput: %v", err)
	}
	var doc map[string]any
	if err := json.Unmarshal([]byte(in), &doc); err != nil {
		t.Fatalf("unmarshal SFN input: %v", err)
	}
	return doc
}
