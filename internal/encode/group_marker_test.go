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

// A codec has SEVERAL bands — the low rungs pack into one, the mid rungs into
// another — so these are keyed by (codec, lead). Keying on codec alone made the
// second band overwrite the first: the UI then filtered one band's members out
// of the lanes and drew the other band's as separate jobs.
//
// A re-announced BAND still replaces, since a reconnect (or the next file of a
// multi-file job) says it again and appending would leave the UI filtering
// against a stale membership as well as the current one.
func TestEachBandIsKeptAndReAnnouncementReplaces(t *testing.T) {
	j := &Job{}
	j.parseMarker(`[[ENCODER-GROUP codec=h264 lead=720p members=594p|540p]]`)
	j.parseMarker(`[[ENCODER-GROUP codec=h264 lead=1080p members=954p]]`)
	j.parseMarker(`[[ENCODER-GROUP codec=hevc lead=540p members=432p]]`)

	if len(j.Groups) != 3 {
		t.Fatalf("got %d groups, want 3 (two h264 bands + one hevc): %+v",
			len(j.Groups), j.Groups)
	}

	j.parseMarker(`[[ENCODER-GROUP codec=h264 lead=720p members=594p|540p|432p]]`)
	if len(j.Groups) != 3 {
		t.Fatalf("re-announcing a band appended instead of replacing: %+v", j.Groups)
	}
	for _, g := range j.Groups {
		if g.Codec == "h264" && g.Lead == "720p" && len(g.Members) != 3 {
			t.Errorf("band kept a stale membership: %v", g.Members)
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
