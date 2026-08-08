package encode

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The run record is the ONLY durable copy of a run's config and timings — the
// Job it is built from is gone at the next server restart, and nothing else in
// the output dir can be re-derived into it. So the tests here are about the two
// ways it can be quietly wrong rather than absent: attributing one codec's (or
// one file's) work to another output, and recording a config that doesn't
// round-trip back into POST /api/encode.

func stage(key string, startMin, endMin int, cpu, work float64) StageProgress {
	base := time.Date(2026, 8, 7, 10, 0, 0, 0, time.UTC)
	s := base.Add(time.Duration(startMin) * time.Minute)
	e := base.Add(time.Duration(endMin) * time.Minute)
	return StageProgress{Key: key, Status: "done", StartedAt: &s, EndedAt: &e,
		CPUSeconds: cpu, WorkerSeconds: work}
}

func phaseByName(t *testing.T, phases []PhaseStat, name string) PhaseStat {
	t.Helper()
	for _, p := range phases {
		if p.Phase == name {
			return p
		}
	}
	t.Fatalf("phase %q missing from %+v", name, phases)
	return PhaseStat{}
}

func hasPhase(phases []PhaseStat, name string) bool {
	for _, p := range phases {
		if p.Phase == name {
			return true
		}
	}
	return false
}

// A job encoding two codecs writes two dirs. Each must account for its own
// encode time only — an hevc output that claims the h264 encode reads as a
// plausible number and is the exact comparison people make between runs.
func TestPhaseRollupSeparatesCodecs(t *testing.T) {
	j := &Job{Stages: []StageProgress{
		stage("mezzanine", 0, 2, 100, 120),
		stage("encode:h264:1080p:chunk0", 2, 4, 200, 120),
		stage("encode:h264:1080p:chunk1", 2, 5, 300, 180),
		stage("encode:hevc:1080p:chunk0", 2, 9, 900, 420),
		stage("package:hevc", 9, 10, 10, 60),
		stage("package:h264", 5, 6, 10, 60),
	}}

	h264 := j.PhaseRollupFor("", "h264")
	if hasPhase(h264, "encode:hevc:1080p") || hasPhase(h264, "package:hevc") {
		t.Fatalf("h264 rollup leaked hevc phases: %+v", h264)
	}
	// Chunks of one rung collapse to a single row; a 336-chunk run is ~12 rows.
	if got := phaseByName(t, h264, "encode:h264:1080p"); got.N != 2 || got.CPUS != 500 {
		t.Fatalf("h264 rung rollup: %+v", got)
	}
	// Span is first-start to last-end, so parallel chunks count once (2 min to
	// 5 min = 3 min), while Σ job wall sums them (2 + 3 = 5 min).
	if got := phaseByName(t, h264, "encode:h264:1080p"); got.SpanS != 180 || got.JobWallS != 300 {
		t.Fatalf("h264 span/wall: span=%v wall=%v", got.SpanS, got.JobWallS)
	}

	hevc := j.PhaseRollupFor("", "hevc")
	if hasPhase(hevc, "encode:h264:1080p") || hasPhase(hevc, "package:h264") {
		t.Fatalf("hevc rollup leaked h264 phases: %+v", hevc)
	}

	// The mezzanine served both, so it appears in both — flagged, not dropped
	// and not silently attributed. Dropping loses time that was really spent;
	// unflagged, the two records sum to more than the run took.
	for _, r := range [][]PhaseStat{h264, hevc} {
		if got := phaseByName(t, r, "mezzanine"); !got.Shared {
			t.Fatalf("mezzanine should be marked shared: %+v", got)
		}
	}
	if got := phaseByName(t, h264, "encode:h264:1080p"); got.Shared {
		t.Fatalf("a codec's own encode phase is not shared: %+v", got)
	}

	// Unfiltered is still the whole job, for history.md's summary.
	if all := j.PhaseRollup(); len(all) != 5 {
		t.Fatalf("unfiltered rollup should hold every phase, got %d: %+v", len(all), all)
	}
}

// A multi-file job writes a dir per file. Same argument as codecs, one level up.
func TestPhaseRollupSeparatesFiles(t *testing.T) {
	j := &Job{
		CurrentFile: "second.mov",
		Stages:      []StageProgress{stage("encode:h264:720p:chunk0", 10, 12, 50, 120)},
		StagesHistory: []FileStages{{
			File: "first.mov", FileIndex: 1, TotalFiles: 2,
			Stages: []StageProgress{stage("encode:h264:720p:chunk0", 0, 4, 400, 240)},
		}},
	}
	first := j.PhaseRollupFor("first.mov", "h264")
	if got := phaseByName(t, first, "encode:h264:720p"); got.CPUS != 400 {
		t.Fatalf("first file rollup picked up the second's work: %+v", got)
	}
	second := j.PhaseRollupFor("second.mov", "h264")
	if got := phaseByName(t, second, "encode:h264:720p"); got.CPUS != 50 {
		t.Fatalf("second file rollup: %+v", got)
	}

	// A stage set whose file is unknown must not match a filter: an empty
	// rollup is visibly missing, where a guess silently credits one file's
	// encode to another file's permanent record.
	j.CurrentFile = ""
	if got := j.PhaseRollupFor("second.mov", "h264"); len(got) != 0 {
		t.Fatalf("unattributed stages must not be claimed by a filtered file: %+v", got)
	}
}

// The record's whole purpose is to be re-postable. #202 was a rerun rebuilt
// from a config record that was a SUBSET of the config: it silently used the
// default ladder, then silently used dynamic chunking instead of the fixed 12s,
// cutting 41 chunks where the run being reproduced cut 336.
func TestEffectiveConfigResolvesDefaultsThatArentAbsences(t *testing.T) {
	got := EffectiveConfig(JobConfig{Files: []string{"clip.mov"}, Codec: "h264"})
	if got.Ladder != DefaultLadderName {
		t.Fatalf("ladder must resolve to the default that would have applied, got %q", got.Ladder)
	}
	if got.ChunkDuration != "dynamic" {
		t.Fatalf("chunk duration: %q", got.ChunkDuration)
	}
	if got.Burnin == nil || !*got.Burnin {
		t.Fatalf("burn-in defaults ON and must be recorded explicitly, got %v", got.Burnin)
	}

	// A fixed chunk size must survive VERBATIM. chunkModeLabel renders it as
	// "12s", which fails ParseFloat on the way back in and silently becomes the
	// default — the second half of #202.
	fixed := EffectiveConfig(JobConfig{ChunkDuration: "12"})
	if fixed.ChunkDuration != "12" {
		t.Fatalf("fixed chunk duration must round-trip unformatted, got %q", fixed.ChunkDuration)
	}
	// And an explicit false is never overwritten by the default.
	off := false
	if c := EffectiveConfig(JobConfig{Burnin: &off}); *c.Burnin {
		t.Fatalf("explicit burnin=false must survive")
	}
}

func TestBuildRunRecordScopesToItsOwnOutput(t *testing.T) {
	ended := time.Date(2026, 8, 7, 10, 30, 0, 0, time.UTC)
	j := &Job{
		ID: "job123", Status: StatusDone,
		StartedAt: time.Date(2026, 8, 7, 10, 0, 0, 0, time.UTC), EndedAt: &ended,
		Stages: []StageProgress{
			stage("mezzanine", 0, 2, 100, 120),
			stage("encode:h264:1080p:chunk0", 2, 4, 200, 120),
			stage("encode:h264:720p:chunk0", 2, 3, 100, 60),
			stage("encode:hevc:1080p:chunk0", 2, 9, 900, 420),
		},
		Vmaf: map[string]*VmafScore{
			"h264/1080p": {Mean: 94.2},
			"hevc/1080p": {Mean: 96.1},
		},
		SpotUSD: 1.25, TotalUSD: 3.5,
	}
	cfg := JobConfig{Files: []string{"clip.mov"}, Codec: "both", Target: TargetLocalDist}

	rec := buildRunRecord(j, cfg, "clip_h264")
	if rec.Codec != "h264" || rec.Source != "clip.mov" {
		t.Fatalf("codec/source: %q %q", rec.Codec, rec.Source)
	}
	if rec.WallS != 1800 {
		t.Fatalf("wall: %v", rec.WallS)
	}
	if rec.LogFile != "job123.log" {
		t.Fatalf("the record indexes the raw log, got %q", rec.LogFile)
	}
	// Rungs are read back from what ACTUALLY ran, tallest first. The ladder name
	// cannot answer this: ladders are editable through the API after the fact,
	// and MaxRes/MinRes narrow them per job.
	if len(rec.Rungs) != 2 || rec.Rungs[0] != "1080p" || rec.Rungs[1] != "720p" {
		t.Fatalf("rungs: %v", rec.Rungs)
	}
	if _, ok := rec.Vmaf["hevc/1080p"]; ok {
		t.Fatalf("the h264 output must not carry hevc scores: %v", rec.Vmaf)
	}
	if rec.Cost == nil || rec.Cost.TotalUSD != 3.5 {
		t.Fatalf("cost: %+v", rec.Cost)
	}

	// A local run has no cost at all, and must say nothing rather than $0.00 —
	// "this was free" is a different claim from "not applicable".
	bare := buildRunRecord(&Job{ID: "j", Status: StatusDone}, cfg, "clip_h264")
	if bare.Cost != nil || bare.Efficiency != nil {
		t.Fatalf("absent cost must stay absent: %+v %+v", bare.Cost, bare.Efficiency)
	}
}

func TestWriteAndReadRunRecord(t *testing.T) {
	out := t.TempDir()
	dir := filepath.Join(out, "clip_h264")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	m := &Manager{OutputDir: out}
	cfg := JobConfig{Files: []string{"clip.mov"}, Codec: "h264", ChunkDuration: "12"}
	j := &Job{ID: "abc", Status: StatusDone, StartedAt: time.Now(),
		Stages: []StageProgress{stage("encode:h264:1080p:chunk0", 0, 2, 10, 60)}}
	m.writeRunRecord("clip_h264", cfg, j)

	rec := ReadRunRecord(dir)
	if rec == nil {
		t.Fatal("record not written")
	}
	if rec.SchemaVersion != runRecordSchema || rec.JobID != "abc" {
		t.Fatalf("record: %+v", rec)
	}
	if rec.Config.ChunkDuration != "12" || rec.Config.Ladder != DefaultLadderName {
		t.Fatalf("config not recorded effectively: %+v", rec.Config)
	}

	// Absent and unreadable both mean "this output predates the record" to
	// every caller — never an error, or old outputs stop rendering at all.
	if ReadRunRecord(out) != nil {
		t.Fatal("a dir with no record must read as nil")
	}
	if err := os.WriteFile(filepath.Join(dir, RunRecordFile), []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if ReadRunRecord(dir) != nil {
		t.Fatal("an unreadable record must read as nil")
	}
}

// A record reconstructed from a source that never captured the config must omit
// it entirely. An empty JobConfig marshals as a perfectly valid-looking config,
// so a reader would take a wall of defaults for facts — #202 with extra steps.
func TestAbsentConfigIsOmittedNotEmptied(t *testing.T) {
	b, err := json.Marshal(RunRecord{SchemaVersion: runRecordSchema, JobID: "x",
		Recovered: &RunRecovery{From: "history.md", Missing: []string{"config"}}})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), `"config":`) {
		t.Fatalf("a record with no config must not emit one: %s", b)
	}
	// And the reconstruction must be self-declaring — same filename, same
	// fields, same shape as a first-hand record otherwise.
	if !strings.Contains(string(b), `"recovered"`) {
		t.Fatalf("reconstructed records must say so: %s", b)
	}
}

// The record has to survive a round trip through JSON: it is read back by a
// different process than wrote it, months later.
func TestRunRecordRoundTripsThroughJSON(t *testing.T) {
	j := &Job{ID: "x", Status: StatusDone,
		Stages: []StageProgress{stage("encode:hevc:2160p:chunk0", 0, 5, 400, 300)}}
	rec := buildRunRecord(j, JobConfig{Files: []string{"a.mov"}, Codec: "hevc"}, "a_hevc")
	b, err := json.Marshal(rec)
	if err != nil {
		t.Fatal(err)
	}
	var back RunRecord
	if err := json.Unmarshal(b, &back); err != nil {
		t.Fatal(err)
	}
	if len(back.Phases) != 1 || back.Phases[0].Phase != "encode:hevc:2160p" {
		t.Fatalf("phases lost in transit: %+v", back.Phases)
	}
	if back.Config.Codec != "hevc" {
		t.Fatalf("config lost in transit: %+v", back.Config)
	}
}
