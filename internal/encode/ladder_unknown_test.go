package encode

import (
	"strings"
	"testing"
)

// A ladder name that does not exist used to resolve to zero rungs rather than
// an error, and zero rungs is also what a real ladder with no column for the
// chosen codec produces (#289). The two need different messages, and neither
// may end in a submitted execution that encodes nothing.
//
// The route in is a RENAME: JobConfig.Ladder is persisted per job and replayed
// by Reconcile, so retiring a seed name (#286 retired apple-uniq-live and
// apple-uniq-live-full) dangles every stored reference to it. A test caught
// that; nothing in the product would have.

func TestUnknownLadderNamesItselfAndTheAlternatives(t *testing.T) {
	m := &Manager{Ladders: testStore(t)}
	err := m.ValidateResBand(JobConfig{Codec: "h264", Ladder: "apple-uniq-live-full"})
	if err == nil {
		t.Fatal("unknown ladder accepted; want an error")
	}
	if !strings.Contains(err.Error(), "unknown ladder") ||
		!strings.Contains(err.Error(), "apple-uniq-live-full") {
		t.Errorf("message must name the missing ladder, got: %v", err)
	}
	// The alternatives matter as much as the rejection — this is the message
	// someone reads after a rename, and it is the only place the new name
	// appears. Mirrors ladder.py's `unknown ladder 'x' (have: a, b, c)`.
	if !strings.Contains(err.Error(), DefaultLadderName) {
		t.Errorf("message must list the ladders that DO exist, got: %v", err)
	}
	// And it must not be phrased as the other failure, which sends someone
	// looking for a missing codec column instead of a missing ladder.
	if strings.Contains(err.Error(), "defines no") {
		t.Errorf("unknown ladder described as a missing codec column: %v", err)
	}
}

// The band check used to return early when neither Min nor Max Res was set —
// above the point the ladder name was even looked at. Since that is the common
// submission, an unknown ladder passed the only validation on the path.
func TestUnknownLadderIsRejectedWithNoResBandSet(t *testing.T) {
	m := &Manager{Ladders: testStore(t)}
	cfg := JobConfig{Codec: "h264", Ladder: "no-such-ladder"} // MinRes/MaxRes unset
	if err := m.ValidateResBand(cfg); err == nil {
		t.Fatal("unknown ladder accepted when no res band is set; want an error")
	}
}

// A real ladder that simply has no rungs for the chosen codec keeps its own
// message. Pinned alongside the above so the two cannot collapse back together.
func TestNoRungsForCodecStaysADifferentMessage(t *testing.T) {
	store := testStore(t)
	store.ladders["h264-only"] = LadderDef{
		Codecs: map[string][][]int{"h264": {{1920, 1080, 6000}}},
	}
	m := &Manager{Ladders: store}
	err := m.ValidateResBand(JobConfig{Codec: "hevc", Ladder: "h264-only", MaxRes: "1080p"})
	if err == nil {
		t.Fatal("codec with no rungs accepted; want an error")
	}
	if strings.Contains(err.Error(), "unknown ladder") {
		t.Errorf("existing ladder reported as unknown: %v", err)
	}
	if !strings.Contains(err.Error(), "hevc") {
		t.Errorf("message must name the codec that has no rungs, got: %v", err)
	}
}

// buildSFNInput is the replay path's last line of defence: a job that passed
// submit validation under a name that has since been renamed away reaches it
// without re-validating. It must refuse rather than emit an execution with an
// empty variants array — that run costs money, takes time, and reports SUCCESS
// having encoded nothing.
func TestBuildSFNInputRefusesAnUnknownLadder(t *testing.T) {
	_, _, err := buildSFNInput(testStore(t), LoadEncodeSpeedStore(""),
		"s3://in/x.mp4", "s3://p", "s3://m", "apple-uniq-live-full", "h264",
		"", "", false, false, true, false, false, 3840, 30, 334.4, 0,
		"12", "6", "0.2", "1.0", 9000, nil, nil)
	if err == nil {
		t.Fatal("unknown ladder produced an execution; want an error")
	}
	if !strings.Contains(err.Error(), "apple-uniq-live-full") {
		t.Errorf("message must name the missing ladder, got: %v", err)
	}
}

// The same refusal for a ladder that EXISTS but whose filters empty it. Reached
// by a band that no rung satisfies, which submit validation catches — but only
// for a job submitted under the current config, not one replayed after an edit.
func TestBuildSFNInputRefusesZeroVariants(t *testing.T) {
	in, n, err := buildSFNInput(testStore(t), LoadEncodeSpeedStore(""),
		"s3://in/x.mp4", "s3://p", "s3://m", DefaultLadderName, "h264",
		"144p", "", false, false, true, false, false, 3840, 30, 334.4, 0,
		"12", "6", "0.2", "1.0", 9000, nil, nil)
	if err == nil {
		t.Fatalf("zero variants produced an execution (%d encodes): %s", n, in)
	}
	if in != "" || n != 0 {
		t.Errorf("refusal must return no input doc, got %d encodes / %q", n, in)
	}
	// Name the ladder and the filter that emptied it — the caller cannot see
	// the rungs, so "no rungs" alone gives them nothing to act on.
	if !strings.Contains(err.Error(), DefaultLadderName) || !strings.Contains(err.Error(), "144p") {
		t.Errorf("message must name the ladder and the band, got: %v", err)
	}
}
