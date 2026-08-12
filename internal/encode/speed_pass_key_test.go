package encode

import (
	"path/filepath"
	"strings"
	"testing"
)

// The Go half of #314.
//
// The learned-speed store is a cross-LANGUAGE contract with no schema: a Python
// worker prints an ENCODER-SPEED marker naming a pass count, and Go reads a key
// built from its own idea of the pass count. Speed() is an exact map lookup with
// no cross-pass fallback, so the two agreeing is the whole mechanism — and when
// they stopped agreeing, both sides went on succeeding. h264 wrote to `:1:`
// forever and Go read `:2:` forever, so no h264 encode could move the model no
// matter how many completed, and 43,021 samples piled up on a curve nothing
// reads.
//
// Neither side can catch that alone, which is why this pins the JOIN: the key a
// marker lands in must be the key the planner asks for, for the same variant.

// twoPassLadder is the shipped default — no `passes` pinned, so passesFor
// returns 2 for every codec.
func twoPassLadder() LadderDef { return LadderDef{} }

func TestMarkerKeyMatchesPlannerKeyForEveryCodec(t *testing.T) {
	def := twoPassLadder()
	m := &Manager{Speeds: LoadEncodeSpeedStore(filepath.Join(t.TempDir(), "speeds.json"))}

	for _, codec := range []string{"h264", "hevc", "av1"} {
		// What the PLANNER will ask for (buildSFNInput / projectCloudCost /
		// computeProgress all resolve the pass count this way now).
		twoPass := def.twoPassFor(codec, false)
		want := speedKey("graviton", codec, 1080, twoPass, "medium", 30)

		// What the WORKER emits. two_pass=1 because encode_variants.two_pass_for
		// returns True for a profile pass count of 2 — pinned on the Python side
		// by scripts/test_speed_marker_pass.py.
		marker := "[[ENCODER-SPEED machine=graviton codec=" + codec +
			" height=1080 two_pass=1 preset=medium fps=30 " +
			"content_s=60.0 encode_s=30.0]]"
		if !m.learnSpeed(marker) {
			t.Fatalf("%s: marker did not parse: %s", codec, marker)
		}

		// The sample must have landed in the key the planner reads. Asking the
		// store is the real test — comparing two speedKey() calls would only
		// prove speedKey is deterministic.
		if got := m.Speeds.samples[want]; got != 1 {
			t.Errorf("%s: planner reads %q, which has %d samples — the marker landed elsewhere: %v",
				codec, want, got, speedKeysOf(m.Speeds))
		}
		if !strings.Contains(want, ":2:") {
			t.Errorf("%s: default ladder should be 2-pass, key is %q", codec, want)
		}
	}
}

// The regression in its original form: h264 running two passes but labelling the
// measurement 1-pass. If the emitter ever reverts, this is what fails.
func TestOnePassMarkerFromATwoPassLadderMissesThePlannerKey(t *testing.T) {
	def := twoPassLadder()
	m := &Manager{Speeds: LoadEncodeSpeedStore(filepath.Join(t.TempDir(), "speeds.json"))}

	// The pre-fix marker: `args.codec == "hevc" and ctx.hevc_two_pass` gave 0
	// for h264 whatever the profile said.
	if !m.learnSpeed("[[ENCODER-SPEED machine=graviton codec=h264 height=1080 " +
		"two_pass=0 preset=medium fps=30 content_s=60.0 encode_s=30.0]]") {
		t.Fatal("marker did not parse")
	}

	planner := speedKey("graviton", "h264", 1080, def.twoPassFor("h264", false), "medium", 30)
	if m.Speeds.samples[planner] != 0 {
		t.Fatal("a 1-pass marker reached the 2-pass key — speedKey stopped separating passes, " +
			"which would make the two curves silently share a bucket")
	}
	// And the read falls through to the seed and stays there — the "h264 never
	// learns" half of the bug, stated as a test.
	before := m.Speeds.Speed("graviton", "h264", 1080, true, "medium", 30)
	for i := 0; i < 50; i++ {
		m.learnSpeed("[[ENCODER-SPEED machine=graviton codec=h264 height=1080 " +
			"two_pass=0 preset=medium fps=30 content_s=60.0 encode_s=30.0]]")
	}
	if after := m.Speeds.Speed("graviton", "h264", 1080, true, "medium", 30); after != before {
		t.Errorf("50 mislabelled samples moved the planner's value %v -> %v; they should not reach it at all",
			before, after)
	}
}

// twoPassFor is the one Go definition, so pin what it answers — including the
// override, which is the only reason it is not just passesFor.
func TestTwoPassForHonoursProfileAndOverride(t *testing.T) {
	cases := []struct {
		name           string
		def            LadderDef
		codec          string
		hevcSinglePass bool
		want           bool
	}{
		{"default ladder, h264 → 2-pass", LadderDef{}, "h264", false, true},
		{"default ladder, hevc → 2-pass", LadderDef{}, "hevc", false, true},
		{"default ladder, av1 → 2-pass", LadderDef{}, "av1", false, true},
		// The profile is the authority, so a ladder can buy h264's encode time
		// back — this is the case the old `== "hevc"` literal got right by
		// accident and would now get wrong.
		{"passes{h264:1} → 1-pass", LadderDef{Passes: map[string]int{"h264": 1}}, "h264", false, false},
		{"passes{h264:1} leaves hevc alone", LadderDef{Passes: map[string]int{"h264": 1}}, "hevc", false, true},
		// The per-encode override exists for a single-pass HEVC comparison run
		// without cloning the ladder, and it must not touch anything else.
		{"override forces hevc to 1-pass", LadderDef{}, "hevc", true, false},
		{"override does not touch h264", LadderDef{}, "h264", true, true},
		{"override does not touch av1", LadderDef{}, "av1", true, true},
		// A zero/absent count is "unset", not "zero passes".
		{"passes{h264:0} falls back to the default", LadderDef{Passes: map[string]int{"h264": 0}}, "h264", false, true},
	}
	for _, tc := range cases {
		if got := tc.def.twoPassFor(tc.codec, tc.hevcSinglePass); got != tc.want {
			t.Errorf("%s: got %v, want %v", tc.name, got, tc.want)
		}
	}
}

func speedKeysOf(s *EncodeSpeedStore) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, 0, len(s.samples))
	for k := range s.samples {
		out = append(out, k)
	}
	return out
}
