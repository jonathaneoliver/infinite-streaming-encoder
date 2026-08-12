"""Time-chunk planning for spot-resumable variant encoding.

A variant is split into fixed-duration time chunks, each encoded as an
independent Batch job and concatenated back together before packaging.
The chunk is the resumable unit: a spot reclaim loses one chunk, not the
whole variant. See docs/chunked-encode-design.md.

Alignment contract (must hold): GOP duration | segment duration | chunk
GRID | chunk duration, e.g. 1s | 2s | 6s | 30s. Every interior chunk
boundary lands on a whole grid unit, hence on a delivery-segment boundary
(and, with closed GOPs, on an IDR), so the packager segments the
concatenated variant cleanly.

The grid — lcm(segment, 6s), see chunk_grid_for — is what makes ladders
that differ only in delivery profile cut the clip in the SAME places. It
sits between segment and chunk in that chain rather than replacing the
segment link, because segment alignment is a correctness property and the
grid is a comparability one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# 30s chunks: comfortably under AWS's "keep spot jobs <= 30 min" guidance
# while large enough to amortize per-chunk container/S3 overhead. A 30s
# chunk is 5x 6s segments.
DEFAULT_CHUNK_DURATION_S = 30.0
DEFAULT_SEGMENT_DURATION_S = 6.0

# The CROSS-LADDER chunk grid: every chunk boundary lands on a multiple of this,
# whatever the ladder's segment duration. See chunk_grid_for.
CHUNK_GRID_S = 6.0
# Bounds the search in chunk_grid_for. Reached only by a segment duration with no
# small common multiple with 6 (7s needs 42), which no ladder has.
_CHUNK_GRID_MAX_UNITS = 240

# Float tolerance for duration comparisons (ffprobe durations are floats).
_EPS = 1e-6


def chunk_grid_for(segment_duration_s: float) -> float:
    """The tiling unit for a ladder: lcm(segment_duration_s, CHUNK_GRID_S).

    Chunk boundaries only ever had to be SEGMENT boundaries, which meant the same
    clip cut in different places on each delivery profile — 334s at a 132s target
    gives 112/111/111 on a 1s ladder, 112/112/110 on 2s and 114/114/106 on 6s.
    Every chunk boundary is an encoder-state reset, so four ladders meant to
    differ only in delivery profile were also differing in where the encoder
    restarted, and a comparison intended to isolate one variable quietly carried
    a second.

    Segment alignment is a correctness requirement (the packager segments the
    concatenated variant); the 6s grid is a comparability one. The LCM is the
    only value satisfying both. Falls back to the segment duration when no common
    multiple is found in range, restoring the old behaviour rather than inventing
    a boundary that is not a segment edge.
    """
    if segment_duration_s <= 0:
        return CHUNK_GRID_S
    for k in range(1, _CHUNK_GRID_MAX_UNITS + 1):
        g = CHUNK_GRID_S * k
        r = g / segment_duration_s
        # round(r) >= 1 is load-bearing, not defensive. Without it a segment
        # LONGER than the grid passes on the first try: 6/1e9 is 6e-9, which
        # rounds to 0 and sits well inside _EPS, so the search would return a 6s
        # grid that is not a whole number of segments — breaking the correctness
        # half of the contract to satisfy the comparability half.
        if round(r) >= 1 and abs(r - round(r)) < _EPS:
            return g
    return segment_duration_s


@dataclass(frozen=True)
class Chunk:
    index: int          # 0-based; doubles as the Batch array index
    start_s: float      # absolute offset into the (padded) content
    duration_s: float   # nominal chunk duration; the last chunk is the remainder

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


def _validate(chunk_duration_s: float, segment_duration_s: float) -> None:
    if chunk_duration_s <= 0 or segment_duration_s <= 0:
        raise ValueError("chunk and segment durations must be positive")
    ratio = chunk_duration_s / segment_duration_s
    if abs(ratio - round(ratio)) > _EPS:
        raise ValueError(
            f"chunk duration ({chunk_duration_s}s) must be a whole multiple of "
            f"the segment duration ({segment_duration_s}s) so chunk boundaries "
            f"land on segment boundaries"
        )


def plan_chunks(
    content_duration_s: float,
    chunk_duration_s: float = DEFAULT_CHUNK_DURATION_S,
    segment_duration_s: float = DEFAULT_SEGMENT_DURATION_S,
) -> list[Chunk]:
    """Tile [0, content_duration_s) into near-equal, grid-aligned chunks.

    The chunk COUNT is `ceil(content / chunk_duration_s)` — the same value the
    Go control plane computes for the SFN chunk_indices — but the clip is then
    divided as evenly as possible across that many chunks, rather than laid out
    as fixed `chunk_duration_s` chunks with a small trailing remainder.

    This avoids a pathological split: a dynamic target of e.g. 330s on a 334s
    clip would otherwise yield one ~full-length chunk + a ~4s remainder — no
    parallelism, but still paying per-chunk container/S3 overhead. Even division
    turns that into two ~167s chunks. Every interior boundary lands on a whole
    grid unit — hence on a segment edge and an IDR, and on a multiple of 6s
    whatever the ladder — and only the final chunk carries the sub-grid tail.
    Raises if `chunk_duration_s` isn't a whole multiple of the segment duration.
    """
    if content_duration_s <= 0:
        raise ValueError(f"content_duration_s must be positive, got {content_duration_s}")
    _validate(chunk_duration_s, segment_duration_s)

    grid = chunk_grid_for(segment_duration_s)
    n = chunk_count(content_duration_s, chunk_duration_s)
    # Whole grid units spanning the clip (the last one may be partial).
    # Distribute them as evenly as possible, handing the leftover units to the
    # earlier chunks so the partial tail stays in the final chunk.
    total_units = max(1, math.ceil(content_duration_s / grid - _EPS))
    # A clip cannot be cut into more pieces than it has grid units. Asking for
    # more used to hand the surplus chunks ZERO duration — each still a real
    # Batch job with a queue wait and a container start. _validate refused
    # chunk < segment and so never reached it here, but Go has no such guard and
    # the cloud path did; both now clamp, so the two planners agree on inputs
    # neither rejects.
    n = min(n, total_units)
    base, extra = divmod(total_units, n)

    chunks: list[Chunk] = []
    start = 0.0
    for index in range(n):
        units = base + (1 if index < extra else 0)
        duration = units * grid
        # The last chunk (or any that would overrun) is clipped to the remainder.
        if index == n - 1 or start + duration > content_duration_s - _EPS:
            duration = content_duration_s - start
        chunks.append(Chunk(index=index, start_s=start, duration_s=duration))
        start += duration
    return chunks


def chunk_count(
    content_duration_s: float,
    chunk_duration_s: float = DEFAULT_CHUNK_DURATION_S,
) -> int:
    """Number of chunks a clip of this duration splits into (>= 1).

    Cheap enough for the Go control plane / SFN input to mirror without
    building the full Chunk list.
    """
    if content_duration_s <= 0:
        raise ValueError(f"content_duration_s must be positive, got {content_duration_s}")
    return max(1, math.ceil(content_duration_s / chunk_duration_s - _EPS))
