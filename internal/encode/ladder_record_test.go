package encode

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The ladder a run used has to be recoverable from history.md (#202). A rerun
// built from history silently used the default ladder instead of
// apple-uniq-live-full — 9 rungs capped at 1080p rather than 12 up to 2160p,
// 252 chunks instead of 336 — and nothing in the permanent record said so.
//
// These pin the two halves of the fix: the fallback name is resolved the same
// way the encoder resolves it, and the recorded rungs are the ones that ran.

func TestEffectiveLadderResolvesTheDefault(t *testing.T) {
	if got := EffectiveLadder(JobConfig{Ladder: "apple-uniq-live-full"}); got != "apple-uniq-live-full" {
		t.Errorf("named ladder = %q, want apple-uniq-live-full", got)
	}
	// An unnamed ladder must record what actually ran, not "". Recording the
	// empty string is what made "ran with the default" and "ran with X"
	// indistinguishable in the first place.
	if got := EffectiveLadder(JobConfig{}); got != DefaultLadderName {
		t.Errorf("unnamed ladder = %q, want %q", got, DefaultLadderName)
	}
}

func jobWithStages(keys ...string) *Job {
	j := &Job{}
	for _, k := range keys {
		j.Stages = append(j.Stages, StageProgress{Key: k})
	}
	return j
}

func TestLadderRungSummaryReportsWhatActuallyRan(t *testing.T) {
	// The run that exposed #202: 9 rungs capped at 1080p.
	nine := jobWithStages(
		"encode:h264:234p:chunk0", "encode:h264:360p:chunk0", "encode:h264:396p:chunk0",
		"encode:h264:432p:chunk0", "encode:h264:540p:chunk0", "encode:h264:594p:chunk0",
		"encode:h264:720p:chunk0", "encode:h264:954p:chunk0", "encode:h264:1080p:chunk0",
	)
	if got, want := ladderRungSummary(nine), " (9 rungs, 234p–1080p)"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}

	// Repeated chunks of the same rung must not inflate the count — the whole
	// point is that 336 stages and 252 stages are 12 rungs and 9 rungs.
	dup := jobWithStages(
		"encode:h264:1080p:chunk0", "encode:h264:1080p:chunk1", "encode:h264:1080p:chunk2")
	if got, want := ladderRungSummary(dup), " (1 rung, 1080p)"; got != want {
		t.Errorf("duplicate chunks: got %q, want %q", got, want)
	}
}

func TestLadderRungSummaryIgnoresNonEncodeStages(t *testing.T) {
	j := jobWithStages(
		"upload:inputs", "mezzanine", "audio",
		"encode:h264:1080p:chunk0", "encode:h264:2160p:chunk0",
		"package:h264", "download:outputs")
	if got, want := ladderRungSummary(j), " (2 rungs, 1080p–2160p)"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestLadderRungSummaryEmptyWhenNothingEncoded(t *testing.T) {
	// A job that died before any encode stage has no rungs to report. It must
	// say nothing rather than "0 rungs", which would read as a real finding.
	if got := ladderRungSummary(jobWithStages("upload:inputs", "mezzanine")); got != "" {
		t.Errorf("got %q, want empty", got)
	}
	if got := ladderRungSummary(&Job{}); got != "" {
		t.Errorf("no stages: got %q, want empty", got)
	}
}

func TestLadderRungSummaryCoversFinishedFiles(t *testing.T) {
	// Multi-file jobs move finished files into StagesHistory, so a summary read
	// only from Stages would describe the last file rather than the run.
	j := jobWithStages("encode:h264:1080p:chunk0")
	j.StagesHistory = []FileStages{{Stages: []StageProgress{
		{Key: "encode:h264:2160p:chunk0"}}}}
	if got, want := ladderRungSummary(j), " (2 rungs, 1080p–2160p)"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

// A rerun is only reproducible if history.md carries every option that shaped
// the encode. Recording a hand-picked subset failed twice — first the ladder
// (#202), then chunk_duration, where a run reconstructed from history used
// dynamic chunking and cut 41 chunks against the original's fixed-12s 336.

func writeConfigToTemp(t *testing.T, cfg JobConfig) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "history.md")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	writeConfigBlock(f, cfg)
	f.Close()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// configJSON pulls the fenced block back out, which also proves the block is
// shaped so a reader can copy it straight into POST /api/encode.
func configJSON(t *testing.T, out string) map[string]any {
	t.Helper()
	_, rest, ok := strings.Cut(out, "```json\n")
	if !ok {
		t.Fatalf("no json fence in:\n%s", out)
	}
	body, _, ok := strings.Cut(rest, "\n```")
	if !ok {
		t.Fatalf("unterminated json fence in:\n%s", out)
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(body), &m); err != nil {
		t.Fatalf("block is not valid JSON: %v\n%s", err, body)
	}
	return m
}

func TestConfigBlockResolvesDefaultsThatWouldOtherwiseBeInvisible(t *testing.T) {
	// The empty config is the dangerous one: every field that matters is absent,
	// and absent read as "nothing to reproduce" is what caused both reruns to
	// diverge. Ladder, chunking and burn-in must be spelled out.
	m := configJSON(t, writeConfigToTemp(t, JobConfig{}))

	if got := m["ladder"]; got != DefaultLadderName {
		t.Errorf("ladder = %v, want %q", got, DefaultLadderName)
	}
	if got := m["chunk_duration"]; got != "dynamic" {
		t.Errorf("chunk_duration = %v, want \"dynamic\"", got)
	}
	if got := m["burnin"]; got != true {
		t.Errorf("burnin = %v, want true (nil means on)", got)
	}
}

func TestConfigBlockPreservesExplicitValues(t *testing.T) {
	off := false
	cfg := JobConfig{
		Files:         []string{"clip.mp4"},
		Codec:         "h264",
		Ladder:        "apple-uniq-live-full",
		ChunkDuration: "12", // the field whose absence produced 41 chunks not 336
		Target:        TargetCloudBatch,
		MaxRes:        "2160p",
		Burnin:        &off,
		MeasureVmaf:   true,
		CpuArch:       "graviton",
		ForceReencode: true,
	}
	m := configJSON(t, writeConfigToTemp(t, cfg))

	for field, want := range map[string]any{
		"chunk_duration": "12",
		"ladder":         "apple-uniq-live-full",
		"codec":          "h264",
		"max_res":        "2160p",
		"burnin":         false,
		"measure_vmaf":   true,
		"cpu_arch":       "graviton",
		"force_reencode": true,
	} {
		if got := m[field]; got != want {
			t.Errorf("%s = %v, want %v", field, got, want)
		}
	}
}

// The point of marshalling JobConfig rather than listing fields by hand is that
// a newly added option is recorded without anyone remembering to. Guard that by
// checking the block carries the full field set, not a curated few.
func TestConfigBlockCoversEveryConfiguredField(t *testing.T) {
	spot := true
	cfg := JobConfig{
		Files: []string{"clip.mp4"}, Codec: "hevc", Ladder: "apple",
		MaxRes: "1080p", MinRes: "360p", Target: TargetCloudBatch, Time: "60",
		SegmentDuration: "6", PartialDuration: "0.2", ChunkDuration: "whole",
		GopDuration: "1.0", OutputTag: "6s", HlsFormat: "ts", Padding: "black",
		KeepMezzanine: true, ForceReencode: true, PromoteAfter: true,
		HevcSinglePass: true, MeasureVmaf: true, VmafPrescale: true,
		CpuArch: "graviton", UseSpot: &spot,
	}
	m := configJSON(t, writeConfigToTemp(t, cfg))

	for _, field := range []string{
		"files", "codec", "ladder", "max_res", "min_res", "target", "time",
		"segment_duration", "partial_duration", "chunk_duration", "gop_duration",
		"output_tag", "hls_format", "padding", "keep_mezzanine", "force_reencode",
		"burnin", "promote", "hevc_single_pass", "measure_vmaf", "vmaf_prescale",
		"cpu_arch", "use_spot",
	} {
		if _, ok := m[field]; !ok {
			t.Errorf("%s missing from the recorded config", field)
		}
	}
}

// Resolving the effective values must not mutate the caller's config — the job
// is still live when its history entry is written.
func TestConfigBlockDoesNotMutateTheJobConfig(t *testing.T) {
	cfg := JobConfig{Codec: "h264"}
	writeConfigToTemp(t, cfg)
	if cfg.Ladder != "" || cfg.ChunkDuration != "" || cfg.Burnin != nil {
		t.Errorf("config was mutated: ladder=%q chunk=%q burnin=%v",
			cfg.Ladder, cfg.ChunkDuration, cfg.Burnin)
	}
}

// The block is only a record if it round-trips. A fixed chunk size rendered for
// display ("12s") parses back as garbage and silently becomes the 30s default —
// so assert the recorded value is one the encoder itself accepts, at the same
// function that consumes it.
func TestConfigBlockChunkDurationRoundTrips(t *testing.T) {
	for _, in := range []string{"12", "24", "whole"} {
		m := configJSON(t, writeConfigToTemp(t, JobConfig{ChunkDuration: in}))
		got, _ := m["chunk_duration"].(string)
		if got != in {
			t.Errorf("chunk_duration %q recorded as %q", in, got)
			continue
		}
		// clipS 334s: the reference clip. "whole" collapses to it; "12"/"24"
		// must survive as themselves rather than falling back to 30.
		if want, have := chunkWant(in), variantChunkSeconds(got, 334, nil, "h264", 1080, false, "medium", 30); have != want {
			t.Errorf("chunk_duration %q resolved to %.0fs, want %.0fs", got, have, want)
		}
	}
}

func chunkWant(cfg string) float64 {
	switch cfg {
	case "whole":
		return 334
	case "12":
		return 12
	default:
		return 24
	}
}
