package encode

import "testing"

// ENCODER-GROUP is how the orchestrator tells the server that several rungs are
// produced by ONE ffmpeg per chunk (#317). It exists for the machine timeline
// and for nothing else: a lane's unit is the unit of DISPATCH, so a grouped job
// is one block, while the chunk grid keeps a row per rung. Without it six
// member rows with identical spans stack into six sub-rows — false concurrency
// depth for a single process — and the shared `nrows` deepens every other lane
// to match.

func TestGroupMarkerRecordsMembership(t *testing.T) {
	j := &Job{}
	if !j.parseMarker(`[[ENCODER-GROUP codec=h264 lead=594p members=540p|432p|234p]]`) {
		t.Fatal("marker not recognised")
	}
	if len(j.Groups) != 1 {
		t.Fatalf("got %d groups, want 1", len(j.Groups))
	}
	g := j.Groups[0]
	if g.Codec != "h264" || g.Lead != "594p" {
		t.Errorf("codec/lead = %q/%q, want h264/594p", g.Codec, g.Lead)
	}
	if len(g.Members) != 3 || g.Members[0] != "540p" || g.Members[2] != "234p" {
		t.Errorf("members = %v, want [540p 432p 234p]", g.Members)
	}
}

// One group per codec, and a re-announcement REPLACES it. The orchestrator
// emits this once per codec, but a job that reconnects (or a second file in a
// multi-file job) can say it again — appending would leave the UI filtering
// against a stale membership as well as the current one.
func TestGroupMarkerReplacesRatherThanAccumulates(t *testing.T) {
	j := &Job{}
	j.parseMarker(`[[ENCODER-GROUP codec=h264 lead=594p members=540p]]`)
	j.parseMarker(`[[ENCODER-GROUP codec=h264 lead=594p members=540p|432p]]`)
	j.parseMarker(`[[ENCODER-GROUP codec=hevc lead=540p members=432p]]`)

	if len(j.Groups) != 2 {
		t.Fatalf("got %d groups, want 2 (one per codec): %+v", len(j.Groups), j.Groups)
	}
	for _, g := range j.Groups {
		if g.Codec == "h264" && len(g.Members) != 2 {
			t.Errorf("h264 kept a stale membership: %v", g.Members)
		}
	}
}

// A group of one is expressible and must not crash the split: `members=` is
// empty, which is what an orchestrator would emit if a codec had a single
// groupable rung. Nothing should be filtered out of the lanes for it.
func TestGroupMarkerWithNoMembers(t *testing.T) {
	j := &Job{}
	if !j.parseMarker(`[[ENCODER-GROUP codec=av1 lead=540p members=]]`) {
		t.Fatal("marker with empty members not recognised")
	}
	if len(j.Groups) != 1 || len(j.Groups[0].Members) != 0 {
		t.Errorf("got %+v, want one group with no members", j.Groups)
	}
}
