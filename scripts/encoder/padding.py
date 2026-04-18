"""Segment-boundary padding math.

Bash pads both video and audio streams so the total duration lands on
a clean multiple of `LCM(segment_duration, partial_duration, gop_duration)`.
For the default 6/0.2/1s values this works out to a 6s LCM for video
and 4s for audio, but the script also supports larger multi-duration
LCMs (12s covers 2s/4s/6s segment durations for multi-ABR reuse).

If the remainder past the previous boundary is already small enough
(<0.5s by default), no padding is applied — too tiny to be worth a
re-encode's worth of tpad frames.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import gcd


# Matches bash: if duration is already within 0.5s of a boundary,
# skip padding entirely.
PADDING_THRESHOLD_S = 0.5


@dataclass(frozen=True)
class PaddingPlan:
    # Seconds of tpad to append to the video stream. 0 = no padding.
    video_pad_s: float
    # Seconds to pad the audio stream (usually equals video_pad_s, but
    # may differ if source video/audio durations diverge).
    audio_pad_s: float
    # The LCM boundary we padded to (for logging / PADDING label enable).
    boundary_s: float


def lcm(a: int, b: int) -> int:
    return a * b // gcd(a, b)


def multi_duration_lcm(segment_s: float, partial_s: float, gop_s: float) -> float:
    """Smallest duration divisible by all three component durations.

    The three inputs are in seconds but may be fractional (partial can
    be 0.2s). We scale to milliseconds for integer LCM and scale back.
    """
    ms = (int(round(segment_s * 1000)),
          int(round(partial_s * 1000)),
          int(round(gop_s * 1000)))
    result = ms[0]
    for v in ms[1:]:
        result = lcm(result, v)
    return result / 1000.0


def plan_padding(
    video_duration_s: float,
    audio_duration_s: float | None,
    boundary_s: float,
    threshold_s: float = PADDING_THRESHOLD_S,
) -> PaddingPlan:
    """Return the pad durations needed to land each stream on a boundary.

    `audio_duration_s=None` means "no audio stream" — audio pad is 0.
    """
    def pad_to(d: float) -> float:
        if d <= 0:
            return 0.0
        remainder = d % boundary_s
        if remainder == 0 or remainder <= threshold_s:
            return 0.0
        return boundary_s - remainder

    return PaddingPlan(
        video_pad_s=pad_to(video_duration_s),
        audio_pad_s=pad_to(audio_duration_s) if audio_duration_s is not None else 0.0,
        boundary_s=boundary_s,
    )
