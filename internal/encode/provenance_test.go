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
