package encode

import (
	"math"
	"strconv"
)

// Chunk boundary planning, mirroring scripts/infinite_streaming_encoder/
// chunking.plan_chunks and encode_variants._coalesce_runt_tail.
//
// The orchestrator is the sole authority on chunk boundaries: it plans once and
// hands every worker its explicit (index, start, duration). Workers do not
// re-derive the plan.
//
// This replaced a split contract in which the control plane passed only a chunk
// COUNT and each worker independently re-derived the boundaries from its own
// probe of the mezzanine. That was self-consistent only by coincidence — every
// worker had to arrive at the same answer from the same inputs — and it made the
// count a cross-process convention that could break in two ways: the worker's
// derived length disagreeing with the dispatched count (a chunk index landing
// out of range), and local-dist needing a COALESCE_RUNT_TAIL env flag purely so
// three processes would fold a short tail chunk identically.
//
// The cost of moving the authority here is that the control plane plans from the
// SOURCE probe, before the mezzanine Batch job has run. The mezzanine is a pure
// stream copy so the two durations should be identical, but "should" is what the
// old worker-side derivation quietly absorbed. cli_phase therefore VALIDATES the
// passed plan against its own probe and fails loudly on a mismatch, which turns
// silent boundary drift into a visible error.
//
// _EPS in chunking.py.
const chunkEps = 1e-6

// minTailChunkSeconds mirrors encode_variants._MIN_TAIL_CHUNK_S. A final chunk
// shorter than this is folded into its predecessor: a sub-frame tail makes
// 2-pass x265 choke on an empty stats file, and even a tail of a second or two
// costs a whole container/instance/S3 round trip to encode nearly nothing.
const minTailChunkSeconds = 2.0

// chunkSpan is one planned chunk. Start/Duration are strings because they travel
// as Batch container Parameters, which must be strings; formatting them here
// (rather than via States.Format in the state machine) keeps the rounding in one
// place and identical to what local-dist sends.
type chunkSpan struct {
	Index     int    `json:"index"`
	StartS    string `json:"start_s"`
	DurationS string `json:"duration_s"`
}

// chunkGridSeconds is the CROSS-LADDER chunk grid: every chunk boundary lands on
// a multiple of this, whatever the ladder's segment duration.
//
// Chunk boundaries only ever had to be segment boundaries, which meant the same
// clip cut in different places on each delivery profile — 334s at a 132s target
// gives 112/111/111 on a 1s ladder, 112/112/110 on 2s and 114/114/106 on 6s.
// Every chunk boundary is an encoder-state reset, so four ladders that are meant
// to differ ONLY in delivery profile were also differing in where the encoder
// restarted. That is the confound docs/ladders-and-delivery.md warns about in
// the other direction: a comparison intended to isolate one variable quietly
// carrying a second.
//
// 6s because it is the default segment duration and every shipped ladder's
// segment (1s, 2s, 6s) divides it, so the grid costs those profiles nothing but
// agreement.
const chunkGridSeconds = 6.0

// chunkGridMaxUnits bounds the search in chunkGridFor. Reached only by a segment
// duration with no small common multiple with 6 (7s needs 42), which no ladder
// has.
const chunkGridMaxUnits = 240

// chunkGridFor returns the tiling unit for a ladder: the smallest duration that
// is both a whole number of SEGMENTS and a multiple of chunkGridSeconds — i.e.
// lcm(segmentS, 6). Segment alignment is a correctness requirement (the packager
// segments the concatenated variant), the 6s grid is a comparability one, and
// the LCM is the only value satisfying both.
//
// Falls back to the segment duration when no common multiple is found in range,
// which restores exactly the old behaviour rather than inventing a boundary that
// is not a segment edge.
func chunkGridFor(segmentS float64) float64 {
	if segmentS <= 0 {
		return chunkGridSeconds
	}
	for k := 1; k <= chunkGridMaxUnits; k++ {
		g := chunkGridSeconds * float64(k)
		r := g / segmentS
		// Round(r) >= 1 is load-bearing, not defensive. Without it a segment
		// LONGER than the grid passes on the first try: 6/1e9 is 6e-9, which
		// rounds to 0 and sits well inside chunkEps, so the search would return
		// a 6s grid that is not a whole number of segments — breaking the
		// correctness half of the contract to satisfy the comparability half.
		if math.Round(r) >= 1 && math.Abs(r-math.Round(r)) < chunkEps {
			return g
		}
	}
	return segmentS
}

// planChunks tiles [0, durationS) into nc near-equal chunks on the ladder's chunk
// grid (chunkGridFor), mirroring chunking.plan_chunks. Every interior boundary
// lands on a whole grid unit — so on a segment edge, an IDR, and a multiple of
// 6s — and only the final chunk carries the sub-grid tail.
//
// Deliberately NOT a fixed chunkS tiling with a trailing remainder: a dynamic
// chunk target of e.g. 330s on a 334s clip would yield one ~full-length chunk
// plus a ~4s remainder — no parallelism, but still paying the per-chunk
// container/S3 overhead. Even division turns that into two ~167s chunks.
func planChunks(durationS, chunkS, segmentS float64) []chunkSpan {
	if durationS <= 0 || chunkS <= 0 || segmentS <= 0 {
		return []chunkSpan{{Index: 0, StartS: "0", DurationS: formatSeconds(math.Max(durationS, 0))}}
	}
	grid := chunkGridFor(segmentS)
	n := chunkCountForDuration(durationS, chunkS)
	totalUnits := int(math.Ceil(durationS/grid - chunkEps))
	if totalUnits < 1 {
		totalUnits = 1
	}
	// A clip cannot be cut into more pieces than it has grid units. Asking for
	// more used to hand the surplus chunks ZERO duration — 334s at chunkS=3 on a
	// 6s ladder planned 111 chunks of which 55 encoded nothing, each still a real
	// Batch job with a queue wait and a container start. Python's _validate
	// refused chunkS < segmentS and so never reached it; Go has no such guard and
	// the cloud path did. Clamping gives the caller fewer chunks than asked for,
	// which is the honest answer and what the count in chunkPlanLine reports.
	if n > totalUnits {
		n = totalUnits
	}
	base, extra := totalUnits/n, totalUnits%n

	spans := make([]chunkSpan, 0, n)
	start := 0.0
	for i := 0; i < n; i++ {
		units := base
		if i < extra {
			units++
		}
		duration := float64(units) * grid
		// The last chunk (or any that would overrun) is clipped to the remainder.
		if i == n-1 || start+duration > durationS-chunkEps {
			duration = durationS - start
		}
		spans = append(spans, chunkSpan{
			Index:     i,
			StartS:    formatSeconds(start),
			DurationS: formatSeconds(duration),
		})
		start += duration
	}
	return coalesceRuntTail(spans)
}

// coalesceRuntTail merges a too-short final chunk into the one before it,
// mirroring encode_variants._coalesce_runt_tail. Only the last chunk is ever
// short (planChunks clips only the tail), so a single merge suffices, and
// indices stay contiguous 0..n-1 because the final index is dropped and its
// predecessor extended — which the concat step relies on.
func coalesceRuntTail(spans []chunkSpan) []chunkSpan {
	if len(spans) < 2 {
		return spans
	}
	tail := spans[len(spans)-1]
	prev := spans[len(spans)-2]
	tailDur, err1 := strconv.ParseFloat(tail.DurationS, 64)
	prevDur, err2 := strconv.ParseFloat(prev.DurationS, 64)
	if err1 != nil || err2 != nil || tailDur >= minTailChunkSeconds {
		return spans
	}
	merged := chunkSpan{Index: prev.Index, StartS: prev.StartS,
		DurationS: formatSeconds(prevDur + tailDur)}
	return append(spans[:len(spans)-2:len(spans)-2], merged)
}

// formatSeconds renders a duration for the wire. 6 decimals is finer than one
// frame at any real frame rate (a 1000fps frame is 1ms) while staying short
// enough to read in a log line; 'f' rather than 'g' so a large offset never
// arrives as scientific notation, which ffmpeg's -ss would reject.
func formatSeconds(v float64) string {
	return strconv.FormatFloat(v, 'f', 6, 64)
}
