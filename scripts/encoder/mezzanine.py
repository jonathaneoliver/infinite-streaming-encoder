"""Phase 2 — create a stream-copy mezzanine from the input file.

The mezzanine is an intermediate MP4 container holding an unmodified
copy of the source's video + audio streams. Every later phase encodes
against this single file, so codec/container edge cases in the raw
source (MKV, MOV, HLS master, TS) are resolved here exactly once.

MP4 container is used (not MKV) so that AV1 stream copy works
downstream without container-specific hacks.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from encoder.progress import run_ffmpeg_with_progress


@dataclass(frozen=True)
class MezzanineSpec:
    # Path to the source (container-agnostic).
    input_path: Path
    # Target path for the mezzanine MP4 (caller owns the directory).
    output_path: Path
    # When the input is an HLS master playlist and the caller has
    # already picked the highest-quality video variant, `video_stream_index`
    # is the stream index to map. `None` means "use the only/default
    # video stream", which ffmpeg handles implicitly with `-c copy`.
    video_stream_index: int | None = None
    # Optional `-t <seconds>` limit on the mezzanine length; truncates
    # the source for faster test encodes.
    time_limit_s: float | None = None


class MezzanineError(RuntimeError):
    """ffmpeg stream-copy failed, or the output file is missing afterward."""


def build_ffmpeg_cmd(spec: MezzanineSpec) -> list[str]:
    """Return the ffmpeg argv that produces the mezzanine.

    Factored out so tests can verify the exact command shape without
    spawning a process.

    We explicitly map the FIRST video and FIRST audio stream. Without
    this, ffmpeg's default stream-selection rule ("most channels" for
    audio) picks a 5.1 AC-3 track over a stereo MP3 track when the
    source has both — that 6-channel audio then propagates through the
    pipeline as 6-channel AAC, which many HLS players (and the Safari
    AVFoundation stack) refuse to play, manifesting as NAL-unit decode
    errors on the VIDEO track when renditions are multiplexed.
    """
    cmd: list[str] = ["ffmpeg", "-y", "-i", str(spec.input_path)]

    if spec.video_stream_index is not None:
        cmd += ["-map", f"0:v:{spec.video_stream_index}"]
    else:
        cmd += ["-map", "0:v:0?"]
    # `?` makes the audio map optional so video-only inputs still work.
    cmd += ["-map", "0:a:0?"]

    if spec.time_limit_s is not None:
        cmd += ["-t", str(spec.time_limit_s)]

    cmd += ["-c", "copy", str(spec.output_path)]
    # -loglevel error + -stats keeps output quiet but preserves the
    # progress lines the Go server tails for the UI.
    cmd += ["-loglevel", "error", "-stats"]
    return cmd


def create_mezzanine(spec: MezzanineSpec, stage_key: str = "mezzanine",
                     duration_s: float = 0.0) -> Path:
    """Run the ffmpeg stream copy and return the resulting mezzanine path.

    When `duration_s` is provided, live progress is emitted as
    ENCODER-STAGE markers under `stage_key`. Passing 0 (the default)
    keeps the old behaviour where ffmpeg inherits stderr and stats
    stream directly into the log.
    """
    spec.output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_ffmpeg_cmd(spec)
    try:
        if duration_s > 0:
            run_ffmpeg_with_progress(cmd, duration_s, stage_key)
        else:
            subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise MezzanineError(f"ffmpeg stream copy failed ({e.returncode})") from e

    if not spec.output_path.is_file():
        raise MezzanineError(f"mezzanine not produced: {spec.output_path}")
    return spec.output_path
