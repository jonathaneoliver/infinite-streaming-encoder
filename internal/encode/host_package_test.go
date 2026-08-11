package encode

import (
	"encoding/json"
	"testing"
)

// Packaging on the host (#197) works by turning OFF the per-codec do_* flags,
// because those gate nothing but the state machine's packaging Choice states.
// That is the whole switch — no ASL change, no deploy — and it is entirely
// invisible unless something pins it.

func sfnInputFor(t *testing.T, codecSel string, packageOnHost bool) map[string]any {
	t.Helper()
	in, _, err := buildSFNInput(LoadLadderStore(""), LoadEncodeSpeedStore(""),
		"s3://in/x.mp4", "s3://p", "s3://m", "apple-uniq-live-xs", codecSel,
		"", "", false, false, true, packageOnHost, false, 3840, 30, 334.4, 0,
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

func hostPackageList(t *testing.T, doc map[string]any) []string {
	t.Helper()
	raw, ok := doc["host_package"]
	if !ok {
		t.Fatal("host_package absent — cmd_poll reads this key to decide what to package")
	}
	items, ok := raw.([]any)
	if !ok {
		t.Fatalf("host_package is %T, not a list; cmd_poll iterates it", raw)
	}
	out := make([]string, 0, len(items))
	for _, i := range items {
		out = append(out, i.(string))
	}
	return out
}

// The two sets must be exact complements. If do_h264 stayed true the state
// machine would ALSO submit a pkgall job, and both packagers would write the
// same output prefix — a race that produces a plausible-looking result.
func TestHostPackagingDisablesTheStateMachineBranch(t *testing.T) {
	on := sfnInputFor(t, "both", true)
	for _, k := range []string{"do_h264", "do_hevc"} {
		if on[k] != false {
			t.Errorf("%s is %v with host packaging on — the SFN would submit a pkgall job too", k, on[k])
		}
	}
	if got := hostPackageList(t, on); len(got) != 2 || got[0] != "h264" || got[1] != "hevc" {
		t.Errorf("host_package = %v, want [h264 hevc]", got)
	}

	off := sfnInputFor(t, "both", false)
	for _, k := range []string{"do_h264", "do_hevc"} {
		if off[k] != true {
			t.Errorf("%s is %v with host packaging off — nothing would package %s at all", k, off[k], k)
		}
	}
	if got := hostPackageList(t, off); len(got) != 0 {
		t.Errorf("host_package = %v with host packaging off, want empty", got)
	}
}

// A codec that was never encoded must not be packaged either way. Before the
// do_* flags existed a single-codec run failed in the packaging job with
// "no hevc variants found"; routing that through a second list is a second
// chance to reintroduce it.
func TestHostPackagingCoversOnlyEncodedCodecs(t *testing.T) {
	doc := sfnInputFor(t, "h264", true)
	got := hostPackageList(t, doc)
	if len(got) != 1 || got[0] != "h264" {
		t.Fatalf("host_package = %v for an h264-only run, want [h264]", got)
	}
	if doc["do_hevc"] != false || doc["do_av1"] != false {
		t.Error("a codec that is not encoded is marked for packaging")
	}
}

// host_package must be [] and not null when packaging stays in Batch. cmd_poll
// reads it as `inp.get("host_package") or []`, so null happens to work today —
// but "no codecs" and "key missing" should not be the same value by luck, and a
// JSON null is what a nil Go slice marshals to.
func TestHostPackageIsAlwaysAList(t *testing.T) {
	in, _, err := buildSFNInput(LoadLadderStore(""), LoadEncodeSpeedStore(""),
		"s3://in/x.mp4", "s3://p", "s3://m", "apple-uniq-live-xs", "both",
		"", "", false, false, true, false, false, 3840, 30, 334.4, 0,
		"12", "6", "0.2", "1.0", 9000, nil, nil)
	if err != nil {
		t.Fatalf("buildSFNInput: %v", err)
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal([]byte(in), &raw); err != nil {
		t.Fatal(err)
	}
	if string(raw["host_package"]) != "[]" {
		t.Errorf("host_package marshalled as %s, want []", raw["host_package"])
	}
}

// Host packaging and skip-media-download want opposite things: one pulls every
// chunk home to package it, the other exists so segments never come home. If
// both were honoured a run would fetch the whole ladder AND upload the packaged
// result back for a later fetch — strictly more transfer than either alone.
func TestSkipMediaDownloadForcesPackagingBackIntoBatch(t *testing.T) {
	m := &Manager{}
	yes, no := true, false

	if m.packageOnHost(JobConfig{SkipMediaDownload: &yes}) {
		t.Error("a run leaving its media in S3 still packaged on the host")
	}
	if !m.packageOnHost(JobConfig{SkipMediaDownload: &no}) {
		t.Error("host packaging is off by default for an ordinary run")
	}
	// The default follows the server-wide setting, so this pair is exactly the
	// interaction that breaks if SKIP_OUTPUT_MEDIA is not passed through compose.
	if m.packageOnHost(JobConfig{}) != (PackageOnHostDefault && !SkipMediaDownloadDefault) {
		t.Error("the unset case does not resolve against the server defaults")
	}
}
