package encode

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// Encoder provenance: which ffmpeg and which encoder LIBRARY made this output.
//
// This exists because the image tracks BtbN's rolling `latest` — a dated pin
// gets pruned upstream and breaks every build — so the Dockerfile no longer
// names a version and the artifact is the only record. Silent failure here
// looks like success: encodes keep working and encode.json just quietly omits
// the field, which is discovered months later when two VMAF numbers disagree
// and nothing says whether the encoder moved underneath them.

func TestFfmpegAndCodecLibMarkersAreCaptured(t *testing.T) {
	j := &Job{}
	for _, line := range []string{
		"[[ENCODER-FFMPEG version=N-125978-g95c43d7df7-20260806]]",
		"[[ENCODER-CODECLIB codec=hevc lib=libx265 version=4.2+37-b81f650e]]",
		"[[ENCODER-CODECLIB codec=av1 lib=libsvtav1 version=4.1.0-279-gd3c4cb394]]",
	} {
		if !j.parseMarker(line) {
			t.Fatalf("marker not consumed: %s", line)
		}
	}
	if j.FfmpegVersion != "N-125978-g95c43d7df7-20260806" {
		t.Fatalf("ffmpeg version: %q", j.FfmpegVersion)
	}
	if got := j.CodecLibs["hevc"]; got != "libx265 4.2+37-b81f650e" {
		t.Fatalf("hevc lib: %q", got)
	}
	if got := j.CodecLibs["av1"]; got != "libsvtav1 4.1.0-279-gd3c4cb394" {
		t.Fatalf("av1 lib: %q", got)
	}
	// h264 must NOT appear: libx264 prints no version at init, so nothing emits
	// it. Asserted so a future "helpful" fabricated value fails loudly rather
	// than silently making up provenance.
	if v, ok := j.CodecLibs["h264"]; ok {
		t.Fatalf("h264 should have no library entry, got %q", v)
	}
}

func TestEncodeMetaRecordsOnlyItsOwnCodecsLibrary(t *testing.T) {
	// A job encoding h264 AND hevc writes two output dirs. Copying the whole
	// CodecLibs map into both would claim the h264 output was produced by
	// libx265 — provenance that is worse than none, because it reads as fact.
	out := t.TempDir()
	m := &Manager{OutputDir: out, Ladders: nil}
	job := &Job{
		FfmpegVersion: "N-125978-g95c43d7df7",
		CodecLibs: map[string]string{
			"hevc": "libx265 4.2+37-b81f650e",
			"av1":  "libsvtav1 4.1.0-279",
		},
	}
	for _, dir := range []string{"clip_p200_h264", "clip_p200_hevc"} {
		if err := os.MkdirAll(filepath.Join(out, dir), 0o755); err != nil {
			t.Fatal(err)
		}
		m.writeEncodeMeta(dir, JobConfig{}, nil, job)
	}

	read := func(dir string) encodeMeta {
		b, err := os.ReadFile(filepath.Join(out, dir, "encode.json"))
		if err != nil {
			t.Fatalf("%s: %v", dir, err)
		}
		var em encodeMeta
		if err := json.Unmarshal(b, &em); err != nil {
			t.Fatal(err)
		}
		return em
	}

	hevc := read("clip_p200_hevc")
	if hevc.CodecLibs["hevc"] != "libx265 4.2+37-b81f650e" {
		t.Fatalf("hevc output lost its library: %+v", hevc.CodecLibs)
	}
	if len(hevc.CodecLibs) != 1 {
		t.Fatalf("hevc output claims other codecs' libraries: %+v", hevc.CodecLibs)
	}

	h264 := read("clip_p200_h264")
	if len(h264.CodecLibs) != 0 {
		t.Fatalf("h264 output must claim NO library, got %+v", h264.CodecLibs)
	}
	// ...but it still records the ffmpeg build, which is the proxy for x264.
	if h264.FfmpegVersion != "N-125978-g95c43d7df7" {
		t.Fatalf("h264 output lost the ffmpeg build: %q", h264.FfmpegVersion)
	}
}

func TestVmafMarkerProvenanceIsCapturedAndBackwardCompatible(t *testing.T) {
	// The score's scale (model + comparison height) rides the ENCODER-VMAF marker
	// (#117). A marker from an OLDER worker image omits them — the optional regex
	// group must still match so the score aggregates, just without provenance.
	j := &Job{}
	newM := "[[ENCODER-VMAF codec=hevc label=1080p height=1080 chunk=0 mean=95.0000 harmonic=94.0000 min=80.0000 frames=300 inv_sum=3.150000 model=vmaf_v0.6.1 common_h=1080]]"
	oldM := "[[ENCODER-VMAF codec=av1 label=2160p height=2160 chunk=0 mean=90.0000 harmonic=89.0000 min=70.0000 frames=300 inv_sum=3.300000]]"
	if !j.parseMarker(newM) {
		t.Fatalf("new marker not consumed: %s", newM)
	}
	if !j.parseMarker(oldM) {
		t.Fatalf("old (no-provenance) marker not consumed: %s", oldM)
	}
	if v := j.Vmaf["hevc/1080p"]; v == nil || v.Model != "vmaf_v0.6.1" || v.CommonHeight != 1080 {
		t.Fatalf("hevc provenance not captured: %+v", j.Vmaf["hevc/1080p"])
	}
	if v := j.Vmaf["av1/2160p"]; v == nil || v.Mean == 0 {
		t.Fatalf("old marker must still aggregate: %+v", j.Vmaf["av1/2160p"])
	}
	if v := j.Vmaf["av1/2160p"]; v.Model != "" || v.CommonHeight != 0 {
		t.Fatalf("old marker must record NO provenance, got model=%q h=%d", v.Model, v.CommonHeight)
	}
}

func TestEncodeMetaRecordsVmafReproducibility(t *testing.T) {
	// #117: encode.json must carry what a post-hoc audit needs to reproduce the
	// reference (time_limit_s) and interpret a score (model, comparison height,
	// state). Absent must read as unknown for the 42 pre-existing outputs, so the
	// no-audit case records state=pending/ineligible but no fabricated numbers.
	out := t.TempDir()
	m := &Manager{OutputDir: out}
	read := func(dir string) encodeMeta {
		b, err := os.ReadFile(filepath.Join(out, dir, "encode.json"))
		if err != nil {
			t.Fatalf("%s: %v", dir, err)
		}
		var em encodeMeta
		if err := json.Unmarshal(b, &em); err != nil {
			t.Fatal(err)
		}
		return em
	}

	// Measured: state=done, provenance recorded, time limit carried through.
	mkdir(t, out, "clip_p200_hevc")
	vmaf := map[string]*VmafScore{
		"hevc/1080p": {Mean: 95, Model: "vmaf_v0.6.1", CommonHeight: 1080},
	}
	m.writeEncodeMeta("clip_p200_hevc", JobConfig{Time: "30", MeasureVmaf: true}, vmaf, nil)
	done := read("clip_p200_hevc")
	if done.VmafState != "done" {
		t.Fatalf("measured output state = %q, want done", done.VmafState)
	}
	if done.VmafModel != "vmaf_v0.6.1" || done.VmafComparisonHeight != 1080 {
		t.Fatalf("provenance not recorded: model=%q h=%d", done.VmafModel, done.VmafComparisonHeight)
	}
	if done.TimeLimitS != "30" {
		t.Fatalf("time_limit_s not recorded: %q", done.TimeLimitS)
	}

	// Audit requested but no score for this codec → failed.
	mkdir(t, out, "clip_p200_h264")
	m.writeEncodeMeta("clip_p200_h264", JobConfig{MeasureVmaf: true}, vmaf, nil)
	if s := read("clip_p200_h264").VmafState; s != "failed" {
		t.Fatalf("requested-but-unscored state = %q, want failed", s)
	}

	// Not requested, burn-in ON (default) → ineligible, and NO provenance numbers.
	mkdir(t, out, "clip_p200_av1")
	m.writeEncodeMeta("clip_p200_av1", JobConfig{}, nil, nil)
	inel := read("clip_p200_av1")
	if inel.VmafState != "ineligible" {
		t.Fatalf("burn-in output state = %q, want ineligible", inel.VmafState)
	}
	if inel.VmafModel != "" || inel.VmafComparisonHeight != 0 {
		t.Fatalf("unaudited output must fabricate no provenance: %+v", inel)
	}

	// Not requested, burn-in OFF → pending (eligible for a future audit).
	mkdir(t, out, "clip_p200_h264_nb")
	off := false
	m.writeEncodeMeta("clip_p200_h264_nb", JobConfig{Burnin: &off}, nil, nil)
	if s := read("clip_p200_h264_nb").VmafState; s != "pending" {
		t.Fatalf("eligible-unaudited state = %q, want pending", s)
	}
}

func mkdir(t *testing.T, base, dir string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(base, dir), 0o755); err != nil {
		t.Fatal(err)
	}
}

func TestEncodeMetaSurvivesAJoblessCall(t *testing.T) {
	// writeEncodeMeta is best-effort and must never panic the job; a nil job
	// (reconciled state, older persisted job) simply records no provenance.
	out := t.TempDir()
	m := &Manager{OutputDir: out}
	if err := os.MkdirAll(filepath.Join(out, "clip_h264"), 0o755); err != nil {
		t.Fatal(err)
	}
	m.writeEncodeMeta("clip_h264", JobConfig{}, nil, nil)
	if _, err := os.Stat(filepath.Join(out, "clip_h264", "encode.json")); err != nil {
		t.Fatalf("encode.json not written without a job: %v", err)
	}
}
