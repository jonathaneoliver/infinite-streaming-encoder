package encode

import (
	"math"
	"strconv"
	"testing"
)

// The chunk grid: every chunk boundary lands on a multiple of 6s, whatever the
// ladder's segment duration.
//
// Chunk boundaries only ever had to be SEGMENT boundaries, so the same clip was
// cut in different places on each delivery profile. Every boundary is an encoder
// state reset, so four ladders meant to differ only in delivery profile were
// also differing in where the encoder restarted — a comparison carrying a second
// variable it was not supposed to have.

// The shipped ladders' segment durations. Their whole point is that they differ.
var ladderSegments = []float64{1, 2, 6}

func spanDurations(t *testing.T, spans []chunkSpan) []float64 {
	t.Helper()
	out := make([]float64, len(spans))
	for i, s := range spans {
		v, err := strconv.ParseFloat(s.DurationS, 64)
		if err != nil {
			t.Fatalf("unparseable duration %q", s.DurationS)
		}
		out[i] = v
	}
	return out
}

// The property the whole change exists for: identical boundaries across every
// delivery profile, so a four-ladder comparison isolates the profile.
func TestEveryLadderCutsTheClipInTheSamePlaces(t *testing.T) {
	// Chunk sizes the dynamic selector actually emits (multiples of the 12s
	// quantum), plus the reference clip and a couple of others.
	for _, clipS := range []float64{334, 300, 3600, 7200} {
		for _, chunkS := range []float64{12, 24, 96, 132, 2004} {
			var want []float64
			for _, segS := range ladderSegments {
				got := spanDurations(t, planChunks(clipS, chunkS, segS))
				if want == nil {
					want = got
					continue
				}
				if len(got) != len(want) {
					t.Errorf("clip=%g chunk=%g: seg=%g gives %d chunks, seg=%g gives %d",
						clipS, chunkS, ladderSegments[0], len(want), segS, len(got))
					continue
				}
				for i := range got {
					if got[i] != want[i] {
						t.Errorf("clip=%g chunk=%g seg=%g: chunk %d is %gs, but seg=%g cut it at %gs",
							clipS, chunkS, segS, i, got[i], ladderSegments[0], want[i])
						break
					}
				}
			}
		}
	}
}

// Every chunk but the last is a whole multiple of 6s. The last carries the
// sub-grid tail and cannot be, unless the clip happens to divide evenly — 334s
// does not, and pretending otherwise would mean encoding media that is not there.
func TestInteriorChunksAreMultiplesOfSixSeconds(t *testing.T) {
	for _, clipS := range []float64{334, 300, 62, 3600, 14400} {
		for _, chunkS := range []float64{12, 24, 96, 132} {
			for _, segS := range ladderSegments {
				d := spanDurations(t, planChunks(clipS, chunkS, segS))
				for i := 0; i < len(d)-1; i++ {
					if r := d[i] / chunkGridSeconds; math.Abs(r-math.Round(r)) > chunkEps {
						t.Errorf("clip=%g chunk=%g seg=%g: interior chunk %d is %gs, not a multiple of %gs",
							clipS, chunkS, segS, i, d[i], chunkGridSeconds)
					}
				}
				// And the tail still lands exactly on the end of the clip.
				var total float64
				for _, v := range d {
					total += v
				}
				if math.Abs(total-clipS) > chunkEps {
					t.Errorf("clip=%g chunk=%g seg=%g: chunks total %g", clipS, chunkS, segS, total)
				}
			}
		}
	}
}

// Segment alignment is the CORRECTNESS half and must survive: the packager
// segments the concatenated variant, so an interior boundary off a segment edge
// shifts every later segment.
func TestGridBoundariesAreStillSegmentBoundaries(t *testing.T) {
	for _, segS := range []float64{1, 2, 3, 5, 6, 10} {
		g := chunkGridFor(segS)
		if r := g / segS; math.Abs(r-math.Round(r)) > chunkEps {
			t.Errorf("seg=%g: grid %g is not a whole number of segments", segS, g)
		}
		if r := g / chunkGridSeconds; math.Abs(r-math.Round(r)) > chunkEps {
			t.Errorf("seg=%g: grid %g is not a multiple of %g", segS, g, chunkGridSeconds)
		}
	}
	// The LCM, not merely a common multiple — an unnecessarily coarse grid
	// costs parallelism on every rung.
	for _, tc := range []struct{ seg, want float64 }{
		{1, 6}, {2, 6}, {3, 6}, {6, 6}, // shipped ladders and near neighbours
		{4, 12}, {5, 30}, {8, 24}, {10, 30}, {12, 12}, {1.5, 6},
	} {
		if got := chunkGridFor(tc.seg); got != tc.want {
			t.Errorf("chunkGridFor(%g) = %g, want %g", tc.seg, got, tc.want)
		}
	}
	// A segment with no small common multiple falls back rather than inventing a
	// boundary that is not a segment edge.
	if got := chunkGridFor(1e9); got != 1e9 {
		t.Errorf("chunkGridFor(1e9) = %g, want the segment duration back", got)
	}
}

// A clip cannot be cut into more pieces than it has grid units. This used to
// hand the surplus chunks ZERO duration — 334s at chunkS=3 on a 6s ladder
// planned 111 chunks of which 55 encoded nothing, each a real Batch job with a
// queue wait and a container start.
func TestNoZeroDurationChunks(t *testing.T) {
	for _, clipS := range []float64{334, 300, 62, 6, 0.5} {
		for _, chunkS := range []float64{0.5, 1, 2, 3, 6, 12, 30, 1e6} {
			for _, segS := range []float64{1, 2, 6} {
				spans := planChunks(clipS, chunkS, segS)
				if len(spans) == 0 {
					t.Errorf("clip=%g chunk=%g seg=%g: no chunks at all", clipS, chunkS, segS)
					continue
				}
				for _, d := range spanDurations(t, spans) {
					if d <= 0 {
						t.Errorf("clip=%g chunk=%g seg=%g: zero-duration chunk in a plan of %d",
							clipS, chunkS, segS, len(spans))
						break
					}
				}
			}
		}
	}
}

// Indices stay contiguous 0..n-1 after the clamp, which the concat step relies
// on — it joins by index and a hole would silently drop media.
func TestClampedPlanKeepsContiguousIndices(t *testing.T) {
	spans := planChunks(334, 1, 1) // asks for 334, the grid allows 56
	if len(spans) >= 334 {
		t.Fatalf("expected the clamp to bite, got %d chunks", len(spans))
	}
	for i, s := range spans {
		if s.Index != i {
			t.Errorf("chunk %d carries index %d", i, s.Index)
		}
	}
}
