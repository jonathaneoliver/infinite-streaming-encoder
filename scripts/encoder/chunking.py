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
    """Tile [0, content_duration_s) into chunks.

    Every chunk is `chunk_duration_s` long except the last, which is the
    remainder. A clip shorter than one chunk yields a single chunk covering
    the whole content. Raises if the chunk duration isn't a whole multiple
    of the segment duration.
    """
    if content_duration_s <= 0:
        raise ValueError(f"content_duration_s must be positive, got {content_duration_s}")
    _validate(chunk_duration_s, segment_duration_s)

    chunks: list[Chunk] = []
    start = 0.0
    index = 0
    while start < content_duration_s - _EPS:
        duration = min(chunk_duration_s, content_duration_s - start)
        chunks.append(Chunk(index=index, start_s=start, duration_s=duration))
        start += duration
        index += 1
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
