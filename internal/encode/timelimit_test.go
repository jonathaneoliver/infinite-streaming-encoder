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
		{"30", 30, true},
		{" 30 ", 30, true},
		{"2.5", 2.5, true},
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

// The limit must reach the orchestrator, and only when it is real — the whole
// bug in #184 was a value that stopped at the Go boundary.
func TestDistArgsCarryTimeLimit(t *testing.T) {
	base := JobConfig{Codec: "h264", Files: []string{"clip.mp4"}}
	withLimit := base
	withLimit.Time = "30"
	args := withLimit.distArgsForFile("/src", "/out", "clip.mp4", "job1", 0)
	i := slices.Index(args, "--time")
	if i < 0 || i+1 >= len(args) {
		t.Fatalf("--time absent from %v", args)
	}
	if args[i+1] != "30" {
		t.Errorf("--time %s, want 30", args[i+1])
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
			"", "", false, false, true, 3840, 30, 334.4, limit,
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
