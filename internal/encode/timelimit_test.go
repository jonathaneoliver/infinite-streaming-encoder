package encode

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"testing"
)

func TestTimeLimitSeconds(t *testing.T) {
	for _, tc := range []struct {
		in   string
		want float64
		ok   bool
	}{
		{"", 0, false},
		{"   ", 0, false},
		// Already a segment multiple — unchanged.
		{"30", 30, true},
		{" 30 ", 30, true},
		// Snapped to the nearest whole 6s segment: a partial final segment is
		// what every consumer downstream disagrees about.
		{"10", 12, true},
		{"8", 6, true},
		{"9", 12, true},
		{"2.5", 6, true}, // floor of one segment, never zero
		{"0.1", 6, true},
		// Garbage in a free-text field is "no limit", not a broken encode.
		{"full clip", 0, false},
		{"30s", 0, false},
		{"abc", 0, false},
		// Non-positive is meaningless as a limit and would plan zero chunks.
		{"0", 0, false},
		{"-5", 0, false},
	} {
		cfg := JobConfig{Time: tc.in}
		got, ok := cfg.TimeLimitSeconds()
		if got != tc.want || ok != tc.ok {
			t.Errorf("TimeLimitSeconds(%q) = (%v, %v), want (%v, %v)",
				tc.in, got, ok, tc.want, tc.ok)
		}
	}
}

// "If the clipped length ends up bigger than the whole content, just do the
// whole content" — a limit can never describe more media than exists.
func TestTimeLimitForClip(t *testing.T) {
	for _, tc := range []struct {
		in    string
		clip  float64
		want  float64
		isLim bool
	}{
		{"10", 60, 12, true},   // snaps to 12, well inside the clip
		{"19", 20, 18, true},   // snaps DOWN, still a real limit
		{"21", 20, 0, false},   // snaps up to 24, past the clip → whole content
		{"99", 20, 0, false},   // far past → whole content
		{"24", 24, 0, false},   // exactly the clip length is not a limit
		{"12", 12.5, 12, true}, // just inside stays a limit
		// Duration unknown (probe failed) keeps the limit: dropping it on a
		// number we never measured would silently encode the whole clip when a
		// short one was asked for.
		{"12", 0, 12, true},
		{"12", -1, 12, true},
		// No request is still no limit, whatever the clip.
		{"", 20, 0, false},
		{"junk", 20, 0, false},
	} {
		cfg := JobConfig{Time: tc.in}
		got, ok := cfg.TimeLimitFor(tc.clip)
		if got != tc.want || ok != tc.isLim {
			t.Errorf("Time=%q clip=%v → (%v, %v), want (%v, %v)",
				tc.in, tc.clip, got, ok, tc.want, tc.isLim)
		}
	}
}

// The snap follows the job's resolved segment duration, not a hardcoded 6 —
// resolveTimings fills it from the ladder before either target dispatches.
func TestTimeLimitSnapsToJobSegmentDuration(t *testing.T) {
	for _, tc := range []struct {
		seg, in string
		want    float64
	}{
		{"4", "10", 12},  // nearest multiple of 4
		{"4", "9", 8},    //
		{"10", "12", 10}, //
		{"", "10", 12},   // unset → the global 6s default
		{"0", "10", 12},  // nonsense → the global 6s default
	} {
		cfg := JobConfig{Time: tc.in, SegmentDuration: tc.seg}
		if got, _ := cfg.TimeLimitSeconds(); got != tc.want {
			t.Errorf("segment=%q Time=%q → %v, want %v", tc.seg, tc.in, got, tc.want)
		}
	}
}

// The limit must reach the orchestrator, and only when it is real — the whole
// bug in #184 was a value that stopped at the Go boundary.
func TestDistArgsCarryTimeLimit(t *testing.T) {
	base := JobConfig{Codec: "h264", Files: []string{"clip.mp4"}}
	withLimit := base
	// 10 snaps to 12; the orchestrator must receive the SNAPPED value, or it
	// would key its mezzanine cache on a limit the encode never used.
	withLimit.Time = "10"
	args := withLimit.distArgsForFile("/src", "/out", "clip.mp4", "job1", 0)
	i := slices.Index(args, "--time")
	if i < 0 || i+1 >= len(args) {
		t.Fatalf("--time absent from %v", args)
	}
	if args[i+1] != "12" {
		t.Errorf("--time %s, want 12", args[i+1])
	}

	for _, junk := range []string{"", "full clip", "0"} {
		cfg := base
		cfg.Time = junk
		if a := cfg.distArgsForFile("/src", "/out", "clip.mp4", "job1", 0); slices.Contains(a, "--time") {
			t.Errorf("Time=%q sent --time", junk)
		}
	}
}

// A truncated mezzanine filed under the full clip's key would silently shorten
// every later encode of that source — the trap #184 called out.
func TestSourceMezzKeyIncludesTimeLimit(t *testing.T) {
	src := filepath.Join(t.TempDir(), "clip.mp4")
	if err := os.WriteFile(src, []byte("data"), 0o644); err != nil {
		t.Fatal(err)
	}
	full, ok := sourceMezzKey(src, 0)
	if !ok {
		t.Fatal("no key for an existing file")
	}
	limited, _ := sourceMezzKey(src, 30)
	other, _ := sourceMezzKey(src, 60)
	if full == limited {
		t.Error("limited run shares the unlimited mezzanine key")
	}
	if limited == other {
		t.Error("two different limits share a mezzanine key")
	}
	// Unlimited must keep hashing exactly as before, or every cached mezzanine
	// in the bucket is orphaned by this change.
	if again, _ := sourceMezzKey(src, 0); again != full {
		t.Error("unlimited key is not stable")
	}
}

// The cloud plan has to describe the truncated clip: chunks are planned from
// the same duration the mezzanine will actually contain.
func TestSFNInputClampsPlanToTimeLimit(t *testing.T) {
	build := func(limit float64) map[string]any {
		in, _ := buildSFNInput(LoadLadderStore(""), LoadEncodeSpeedStore(""),
			"s3://in/x.mp4", "s3://p", "s3://m", "apple-uniq-live-full", "h264",
			"", "", false, false, true, false, 3840, 30, 334.4, limit,
			"12", "6", "0.2", "1.0", 9000, nil, nil)
		var doc map[string]any
		if err := json.Unmarshal([]byte(in), &doc); err != nil {
			t.Fatalf("unmarshal SFN input: %v", err)
		}
		return doc
	}
	countChunks := func(doc map[string]any) int {
		n := 0
		for _, v := range doc["variants"].([]any) {
			n += len(v.(map[string]any)["chunks"].([]any))
		}
		return n
	}

	full, limited := build(0), build(36)
	if got := full["time_limit"]; got != "0" {
		t.Errorf("unset time_limit = %v, want \"0\"", got)
	}
	// Always present: the ASL reads it with Value.$, and a missing key fails the
	// state at runtime rather than reading as absent.
	if got, ok := limited["time_limit"]; !ok || got != "36" {
		t.Errorf("time_limit = %v (present=%v), want \"36\"", got, ok)
	}
	if fc, lc := countChunks(full), countChunks(limited); lc >= fc {
		t.Errorf("limited plan has %d chunks, full has %d — expected fewer", lc, fc)
	}

	// A limit at or above the clip is not a limit, so it must not reach the
	// mezzanine (which would truncate to a length it never had) or the plan.
	over := build(1000)
	if got := over["time_limit"]; got != "0" {
		t.Errorf("over-length time_limit = %v, want \"0\"", got)
	}
	if countChunks(over) != countChunks(full) {
		t.Error("over-length limit changed the chunk plan")
	}
}
