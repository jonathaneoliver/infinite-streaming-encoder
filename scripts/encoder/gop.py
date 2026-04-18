"""Keyframe / GOP math.

LL-HLS needs closed GOPs with keyframes at fixed intervals. The
interval is derived from the source frame rate and the configured
GOP duration:

    KEYINT = round(fps_num / fps_den * gop_duration_s)

fps must be a `Fraction` — a decimal like 29.97 drifts enough over
long content to push keyframes off the intended second boundaries.
30000/1001 at a 1-second GOP gives KEYINT=30; a float of 29.97 gives
29.97, which rounds to 30 but compounds wrong over hundreds of GOPs.
"""
from __future__ import annotations

from fractions import Fraction


def keyint(fps: Fraction, gop_duration_s: float) -> int:
    """Frames per keyframe interval. Never returns less than 1."""
    if fps <= 0:
        raise ValueError(f"non-positive fps: {fps}")
    value = round(float(fps) * gop_duration_s)
    return max(1, int(value))
