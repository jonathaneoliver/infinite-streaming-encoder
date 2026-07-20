"""Time-chunk planning for spot-resumable variant encoding.

A variant is split into fixed-duration time chunks, each encoded as an
independent Batch job and concatenated back together before packaging.
The chunk is the resumable unit: a spot reclaim loses one chunk, not the
whole variant. See docs/chunked-encode-design.md.

Alignment contract (must hold): GOP duration | segment duration | chunk
duration, e.g. 1s | 6s | 30s. Because the chunk duration is a whole
multiple of the segment duration, every interior chunk boundary lands
exactly on a delivery-segment boundary (and, with closed 1s GOPs, on an
IDR), so the packager segments the concatenated variant cleanly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# 30s chunks: comfortably under AWS's "keep spot jobs <= 30 min" guidance
# while large enough to amortize per-chunk container/S3 overhead. A 30s
# chunk is 5x 6s segments.
DEFAULT_CHUNK_DURATION_S = 30.0
DEFAULT_SEGMENT_DURATION_S = 6.0

# Float tolerance for duration comparisons (ffprobe durations are floats).
_EPS = 1e-6


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
    """Tile [0, content_duration_s) into near-equal, segment-aligned chunks.

    The chunk COUNT is `ceil(content / chunk_duration_s)` — the same value the
    Go control plane computes for the SFN chunk_indices — but the clip is then
    divided as evenly as possible across that many chunks, rather than laid out
    as fixed `chunk_duration_s` chunks with a small trailing remainder.

    This avoids a pathological split: a dynamic target of e.g. 330s on a 334s
    clip would otherwise yield one ~full-length chunk + a ~4s remainder — no
    parallelism, but still paying per-chunk container/S3 overhead. Even division
    turns that into two ~167s chunks. Every interior boundary still lands on a
    whole segment (so IDRs/segment edges align); only the final chunk carries the
    sub-segment tail. Raises if `chunk_duration_s` isn't a whole multiple of the
    segment duration.
    """
    if content_duration_s <= 0:
        raise ValueError(f"content_duration_s must be positive, got {content_duration_s}")
    _validate(chunk_duration_s, segment_duration_s)

    n = chunk_count(content_duration_s, chunk_duration_s)
    # Whole segments spanning the clip (the last one may be partial). Distribute
    # them as evenly as possible, handing the leftover segments to the earlier
    # chunks so the partial tail segment stays in the final chunk.
    total_segments = math.ceil(content_duration_s / segment_duration_s - _EPS)
    base, extra = divmod(total_segments, n)

    chunks: list[Chunk] = []
    start = 0.0
    for index in range(n):
        segs = base + (1 if index < extra else 0)
        duration = segs * segment_duration_s
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
