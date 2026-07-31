package encode

import (
	"bufio"
	"os"
	"strconv"
	"strings"
	"testing"
)

// The Go chunk planner must agree with the Python one EXACTLY — not just on how
// many chunks, but on every boundary. Go plans the cloud path; Python plans
// local-dist and is what the packager's segment alignment was designed around.
// A divergence does not fail loudly: both sides keep working and the two paths
// simply cut the clip in different places, so a local and a cloud encode of the
// same source stop being comparable. That is the exact class of drift #167
// chased by hand.
//
// testdata_chunkplan.txt is generated FROM the Python (chunking.plan_chunks +
// encode_variants._coalesce_runt_tail), so Python stays the authority and this
// test pins Go to it. Regenerate with the snippet in TestChunkPlanMatchesPython's
// doc comment if the Python planner intentionally changes.
func TestChunkPlanMatchesPython(t *testing.T) {
	f, err := os.Open("testdata_chunkplan.txt")
	if err != nil {
		t.Fatalf("golden vectors: %v", err)
	}
	defer f.Close()

	n := 0
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		parts := strings.Split(line, "|")
		if len(parts) != 4 {
			t.Fatalf("malformed golden line: %q", line)
		}
		dur, chunk, seg := mustFloat(t, parts[0]), mustFloat(t, parts[1]), mustFloat(t, parts[2])

		got := planChunks(dur, chunk, seg)
		want := strings.Split(parts[3], ";")
		if len(got) != len(want) {
			t.Errorf("dur=%g chunk=%g: got %d chunks, python planned %d",
				dur, chunk, len(got), len(want))
			continue
		}
		for i, w := range want {
			wf := strings.Split(w, ",")
			wi, ws, wd := wf[0], mustFloat(t, wf[1]), mustFloat(t, wf[2])
			gs, gd := mustFloat(t, got[i].StartS), mustFloat(t, got[i].DurationS)
			if strconv.Itoa(got[i].Index) != wi || !near(gs, ws) || !near(gd, wd) {
				t.Errorf("dur=%g chunk=%g idx=%d: go=(%d,%g,%g) python=(%s,%g,%g)",
					dur, chunk, i, got[i].Index, gs, gd, wi, ws, wd)
			}
		}
		n++
	}
	if n == 0 {
		t.Fatal("no golden vectors read — this test would pass vacuously")
	}
	t.Logf("%d cases matched the Python planner exactly", n)
}

// Chunks must tile the clip with no gap and no overlap: the packager
// concatenates them in index order and any seam error shifts every later
// segment.
func TestChunkPlanTilesWithoutGaps(t *testing.T) {
	for _, dur := range []float64{334.4, 330, 12, 6.5, 3600, 1799.9, 0.5} {
		for _, chunk := range []float64{12, 30, 60, 300} {
			spans := planChunks(dur, chunk, 6)
			var at float64
			for i, s := range spans {
				start, d := mustFloat(t, s.StartS), mustFloat(t, s.DurationS)
				if !near(start, at) {
					t.Errorf("dur=%g chunk=%g: chunk %d starts at %g, previous ended at %g",
						dur, chunk, i, start, at)
				}
				if d <= 0 {
					t.Errorf("dur=%g chunk=%g: chunk %d has non-positive duration %g",
						dur, chunk, i, d)
				}
				at = start + d
			}
			if !near(at, dur) && dur > 0 {
				t.Errorf("dur=%g chunk=%g: chunks cover %g, not the whole clip", dur, chunk, at)
			}
		}
	}
}

// A runt tail is folded away, never dispatched. A sub-frame final chunk makes
// 2-pass x265 choke on an empty stats file, and even a 1-2s tail costs a whole
// container/instance round trip to encode almost nothing.
func TestChunkPlanHasNoRuntTail(t *testing.T) {
	for _, dur := range []float64{330.02, 300.01, 60.5, 180.1, 1200.05} {
		for _, chunk := range []float64{12, 30, 60} {
			spans := planChunks(dur, chunk, 6)
			if len(spans) < 2 {
				continue
			}
			last := mustFloat(t, spans[len(spans)-1].DurationS)
			if last < minTailChunkSeconds {
				t.Errorf("dur=%g chunk=%g: final chunk is %gs, under the %gs floor",
					dur, chunk, last, minTailChunkSeconds)
			}
		}
	}
}

// Boundaries travel to the worker as 6-decimal strings. Parsing one back must
// land on the same value, or the worker's frame math (ceil(t*fps)) can pick a
// different frame than the plan intended.
func TestChunkPlanBoundariesSurviveTheWire(t *testing.T) {
	for _, dur := range []float64{334.4, 1000.333, 77.77, 3600} {
		for _, s := range planChunks(dur, 12, 6) {
			for _, v := range []string{s.StartS, s.DurationS} {
				f, err := strconv.ParseFloat(v, 64)
				if err != nil {
					t.Fatalf("dur=%g: %q does not parse: %v", dur, v, err)
				}
				if strconv.FormatFloat(f, 'f', 6, 64) != v {
					t.Errorf("dur=%g: %q does not round-trip", dur, v)
				}
				if strings.ContainsAny(v, "eE") {
					t.Errorf("dur=%g: %q is scientific notation — ffmpeg -ss rejects it", dur, v)
				}
			}
		}
	}
}

func mustFloat(t *testing.T, s string) float64 {
	t.Helper()
	f, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil {
		t.Fatalf("parse %q: %v", s, err)
	}
	return f
}

func near(a, b float64) bool {
	d := a - b
	return d < 1e-6 && d > -1e-6
}
