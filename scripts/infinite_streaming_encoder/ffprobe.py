"""Thin structured wrapper around ffprobe.

`probe(path)` returns a `ProbeResult` with the fields later phases need:
width, height, fps (as a `Fraction`), duration_s, plus booleans for
video/audio stream presence. FPS is a Fraction because keyframe
interval math depends on the exact ratio (e.g. 30000/1001), and
rounding to a decimal early is the kind of thing that drifts from
the bash script's `awk -F/ '{printf "%.0f", num/den * gop}'` by
one frame per hour.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


@dataclass(frozen=True)
class ProbeResult:
    width: int
    height: int
    fps: Fraction
    duration_s: float
    has_video: bool
    has_audio: bool
    video_codec: str | None


class ProbeError(RuntimeError):
    """ffprobe couldn't read the file, or the file lacks a video stream."""


def probe(path: Path, stream_index: int = 0) -> ProbeResult:
    """Run ffprobe against `path` and return the structured result.

    `stream_index` selects which video stream to read resolution/fps
    from — useful for multi-variant HLS inputs where the caller has
    already picked the highest-quality track.

    Raises `ProbeError` if the file is unreadable, has no video, or
    ffprobe fails for any other reason.
    """
    path = Path(path)
    if not path.is_file():
        raise ProbeError(f"not a file: {path}")

    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True, check=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise ProbeError(f"ffprobe failed: {e.stderr.strip() or e}") from e

    data = json.loads(proc.stdout)
    streams = data.get("streams", [])

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not video_streams:
        raise ProbeError(f"no video stream in {path}")
    if stream_index >= len(video_streams):
        raise ProbeError(
            f"video stream index {stream_index} out of range "
            f"(file has {len(video_streams)})"
        )

    v = video_streams[stream_index]
    try:
        width = int(v["width"])
        height = int(v["height"])
    except (KeyError, ValueError) as e:
        raise ProbeError(f"video stream missing width/height: {e}") from e

    fps_str = v.get("r_frame_rate") or v.get("avg_frame_rate") or "0/1"
    try:
        fps = Fraction(fps_str)
    except (ValueError, ZeroDivisionError) as e:
        raise ProbeError(f"invalid frame rate {fps_str!r}: {e}") from e
    if fps == 0:
        raise ProbeError(f"zero frame rate in {path}")

    duration_s = 0.0
    fmt = data.get("format", {})
    for candidate in (fmt.get("duration"), v.get("duration")):
        if candidate:
            try:
                duration_s = float(candidate)
                break
            except ValueError:
                continue

    return ProbeResult(
        width=width,
        height=height,
        fps=fps,
        duration_s=duration_s,
        has_video=True,
        has_audio=bool(audio_streams),
        video_codec=v.get("codec_name"),
    )
