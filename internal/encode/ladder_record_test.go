package encode

import "testing"

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
