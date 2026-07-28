"""Phase 2 — create a (CFR-normalized) stream-copy mezzanine from the input.

The mezzanine is an intermediate MP4 container holding a copy of the source's
video + audio streams. Every later phase encodes against this single file, so
codec/container edge cases in the raw source (MKV, MOV, HLS master, TS) are
resolved here exactly once.

MP4 container is used (not MKV) so that AV1 stream copy works downstream
without container-specific hacks.

**CFR normalization (fps_num/fps_den set).** Many sources are effectively VFR:
e.g. an MKV stores timestamps on a 1ms timebase, where a 1001/30000s frame is
33.3667ms — not a whole number of ms — so the per-frame timestamps jitter
(33/33/34ms). That jitter is invisible in the source's declared frame rate but
surfaces the moment the encoder converts to CFR: the encoder *resamples*
(drops/dups frames), and the VMAF audit — comparing a CFR rung against the VFR
mezzanine via a continuous `fps` re-time — drifts a frame around mid-clip,
cratering min/harmonic on otherwise-perfect rungs. We fix it at the root here:
a **lossless** relabel (two `-c copy` passes, NO re-encode) that snaps every
frame onto an exact integer-tick CFR grid derived from the source's nominal
`r_frame_rate`. The encoder then passes frames through 1:1 and every VMAF
method (in-encode per-chunk, whole-variant, offline) agrees. Cost is up to a
~1-frame A/V shift inherent to any VFR->CFR relabel — correct for a CFR-output
streaming pipeline. Leaving fps unset keeps the legacy plain stream-copy.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from infinite_streaming_encoder.progress import run_ffmpeg_with_progress


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
    # Nominal source frame rate (r_frame_rate) as num/den. When both are set,
    # the mezzanine is losslessly relabeled to EXACT CFR on this grid (see the
    # module docstring). None keeps the legacy plain stream-copy that lets the
    # source's (possibly VFR) timing pass through unchanged.
    fps_num: int | None = None
    fps_den: int | None = None


class MezzanineError(RuntimeError):
    """ffmpeg stream-copy failed, or the output file is missing afterward."""


def _cfr_grid(fps_num: int, fps_den: int) -> tuple[int, int]:
    """(timescale, ticks_per_frame) that represents fps_num/fps_den frames
    EXACTLY as integer ticks. frame_dur = timescale*fps_den/fps_num must be a
    whole number, which holds for timescale = fps_num*mult (=> ticks per frame
    = fps_den*mult). We scale up until timescale >= 10000 so integer rates
    (24/25/30/50/60) don't land on a coarse container timescale — the NTSC
    fractional rates (24000/1001, 30000/1001, 60000/1001) already qualify with
    mult=1, giving the natural 24000/30000/60000 timescales.
    """
    mult = 1
    while fps_num * mult < 10000:
        mult *= 10
    return fps_num * mult, fps_den * mult


def _relabel_tmp(output_path: Path) -> Path:
    """Pass-1 output path (pass 2's setts relabel reads it, then it's removed).
    Distinct infix so it can't collide with the `<name>.done` sidecar callers
    use to gate cache reuse."""
    return output_path.with_name(output_path.stem + ".prelabel" + output_path.suffix)


def build_ffmpeg_cmd(spec: MezzanineSpec, output_path: Path | None = None) -> list[str]:
    """Return the ffmpeg argv for the copy pass that produces the mezzanine.

    Factored out so tests can verify the exact command shape without
    spawning a process.

    We explicitly map the FIRST video and FIRST audio stream. Without
    this, ffmpeg's default stream-selection rule ("most channels" for
    audio) picks a 5.1 AC-3 track over a stereo MP3 track when the
    source has both — that 6-channel audio then propagates through the
    pipeline as 6-channel AAC, which many HLS players (and the Safari
    AVFoundation stack) refuse to play, manifesting as NAL-unit decode
    errors on the VIDEO track when renditions are multiplexed.

    When `spec.fps_num`/`fps_den` are set this is PASS 1 of the CFR relabel:
    `-video_track_timescale <timescale>` forces the MP4 video track onto a
    `1/timescale` timebase so PASS 2 (`build_setts_cmd`) can land every frame
    on an exact integer-tick grid. `output_path` overrides the destination
    (the relabel temp); default is `spec.output_path`.
    """
    out = output_path or spec.output_path
    cmd: list[str] = ["ffmpeg", "-y", "-i", str(spec.input_path)]

    if spec.video_stream_index is not None:
        cmd += ["-map", f"0:v:{spec.video_stream_index}"]
    else:
        cmd += ["-map", "0:v:0?"]
    # `?` makes the audio map optional so video-only inputs still work.
    cmd += ["-map", "0:a:0?"]

    if spec.time_limit_s is not None:
        cmd += ["-t", str(spec.time_limit_s)]

    cmd += ["-c", "copy"]
    if spec.fps_num and spec.fps_den:
        timescale, _ = _cfr_grid(spec.fps_num, spec.fps_den)
        cmd += ["-video_track_timescale", str(timescale)]
    cmd += [str(out)]
    # -loglevel error + -stats keeps output quiet but preserves the
    # progress lines the Go server tails for the UI.
    cmd += ["-loglevel", "error", "-stats"]
    return cmd


def build_setts_cmd(spec: MezzanineSpec, input_path: Path) -> list[str]:
    """PASS 2 of the CFR relabel: rewrite every video packet's PTS/DTS onto the
    exact grid (frame N -> N*ticks) via the `setts` bitstream filter. It's a
    `-c copy`, so no decode/re-encode and no quality loss. `input_path` is pass
    1's output (already on the `1/timescale` timebase, so `N*ticks` lands
    cleanly). Requires `spec.fps_num`/`fps_den`.
    """
    if not (spec.fps_num and spec.fps_den):
        raise MezzanineError("build_setts_cmd requires fps_num/fps_den")
    _, ticks = _cfr_grid(spec.fps_num, spec.fps_den)
    return [
        "ffmpeg", "-y", "-i", str(input_path),
        "-map", "0", "-c", "copy",
        "-bsf:v", f"setts=pts=N*{ticks}:dts=N*{ticks}",
        str(spec.output_path),
        "-loglevel", "error", "-stats",
    ]


def create_mezzanine(spec: MezzanineSpec, stage_key: str = "mezzanine",
                     duration_s: float = 0.0,
                     pct_lo: float = 0.0, pct_hi: float = 100.0,
                     terminal: bool = True) -> Path:
    """Run the ffmpeg copy (+ optional CFR relabel) and return the mezzanine.

    When `duration_s` is provided, live progress is emitted as
    ENCODER-STAGE markers under `stage_key`. Passing 0 (the default)
    keeps the old behaviour where ffmpeg inherits stderr and stats
    stream directly into the log. `pct_lo`/`pct_hi`/`terminal` place the
    copy's progress in a sub-band of the stage (the phase brackets it with
    download before and upload after).

    When `spec.fps_num`/`fps_den` are set, the copy is followed by a lossless
    `setts` relabel that forces EXACT CFR (two `-c copy` passes; no re-encode).
    This resolves VFR / non-uniform-timestamp sources up front so the encoder
    passes frames through 1:1 and the VMAF audit never drifts (module docstring).
    """
    spec.output_path.parent.mkdir(parents=True, exist_ok=True)
    relabel = bool(spec.fps_num and spec.fps_den)

    # Pass 1 (copy) gets the bulk of the progress band; the pass-2 setts copy
    # is fast, so reserve a thin slice at the end for it.
    pass1_out = _relabel_tmp(spec.output_path) if relabel else spec.output_path
    split = pct_lo + (pct_hi - pct_lo) * 0.9
    pass1_hi = split if relabel else pct_hi

    cmd1 = build_ffmpeg_cmd(spec, output_path=pass1_out)
    try:
        if duration_s > 0:
            run_ffmpeg_with_progress(cmd1, duration_s, stage_key,
                                     pct_lo=pct_lo, pct_hi=pass1_hi,
                                     terminal=(terminal and not relabel))
        else:
            subprocess.run(cmd1, check=True)

        if relabel:
            cmd2 = build_setts_cmd(spec, pass1_out)
            if duration_s > 0:
                run_ffmpeg_with_progress(cmd2, duration_s, stage_key,
                                         pct_lo=pass1_hi, pct_hi=pct_hi,
                                         terminal=terminal)
            else:
                subprocess.run(cmd2, check=True)
    except subprocess.CalledProcessError as e:
        what = "CFR relabel" if relabel else "stream copy"
        raise MezzanineError(f"ffmpeg mezzanine {what} failed ({e.returncode})") from e
    finally:
        if relabel:
            _relabel_tmp(spec.output_path).unlink(missing_ok=True)

    if not spec.output_path.is_file():
        raise MezzanineError(f"mezzanine not produced: {spec.output_path}")
    return spec.output_path
