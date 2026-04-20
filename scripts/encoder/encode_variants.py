"""Phase 3 — encode per-codec × per-resolution variants from the mezzanine.

Software encoders only (libx264 / libx265 / libsvtav1); hardware path
is not reproduced here. Each variant is written as a fragmented MP4
with closed GOPs at the configured keyframe interval — a strict
requirement for LL-HLS playback downstream.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable

from encoder.burnin import BurninContext, build_filter, rate_label
from encoder.gop import keyint
from encoder.ladder import (
    BUFSIZE_MULTIPLIER,
    DEFAULT_MAXRATE_PERCENT,
    Tier,
    resolve_bitrate,
)
from encoder.progress import run_ffmpeg_with_progress


def variant_stage_key(codec: str, tier_name: str) -> str:
    """Stable stage key the Go server uses to identify a single variant."""
    return f"encode:{codec}:{tier_name}"


# Tag values written in the MP4 so players pick the right parser.
_VIDEO_TAGS = {"h264": "avc1", "hevc": "hvc1", "av1": "av01"}

# Fragment duration inside each variant MP4, in microseconds. 1s keeps
# LL-HLS partials aligned to keyframe boundaries when GOP=1s (default).
_FRAG_DURATION_US = 1_000_000


@dataclass(frozen=True)
class EncodeContext:
    mezzanine_path: Path
    output_dir: Path       # where {codec}_{res}.mp4 files land
    fps: Fraction
    gop_duration_s: float
    content_duration_s: float   # pre-padding duration, for PADDING label enable
    padding_duration_s: float   # 0.0 = no padding applied
    maxrate_percent: int = DEFAULT_MAXRATE_PERCENT


class EncodeError(RuntimeError):
    pass


def _variant_path(output_dir: Path, codec: str, tier: Tier) -> Path:
    # Matches bash: $TEMP_DIR/${codec}_${res_name}.mp4
    return output_dir / f"{codec}_{tier.name}.mp4"


def _codec_specific_args(codec: str, target_kbps: int, k: int, preset: str) -> list[str]:
    maxrate_k = target_kbps  # placeholder — real value is set in build_ffmpeg_cmd
    if codec == "hevc":
        # `pools=*` = use every host CPU for the x265 thread pool. The bash
        # script had `pools=+`, which isn't a valid value — x265 silently
        # skipped the pool ("No thread pool allocated"), leaving encodes on
        # just the 3 default frame threads. Switch to `*` for actual
        # parallelism. Revisit if MAX_CONCURRENT > 1: multiple jobs each
        # grabbing all cores will thrash; at that point hard-code a count
        # like pools=4 per encode.
        return [
            "-c:v", "libx265",
            "-preset", preset,
            "-threads", "0",
            "-x265-params",
            f"keyint={k}:min-keyint={k}:scenecut=0:open-gop=0:pools=*:frame-threads=0",
            "-pix_fmt", "yuv420p",
        ]
    if codec == "h264":
        return [
            "-c:v", "libx264",
            "-preset", preset,
            "-threads", "0",
            "-x264-params",
            f"keyint={k}:min-keyint={k}:scenecut=0:open-gop=0",
            "-pix_fmt", "yuv420p",
        ]
    if codec == "av1":
        return [
            "-c:v", "libsvtav1",
            "-preset", "8",
            "-svtav1-params", f"keyint={k}:scd=0",
            "-g", str(k),
            "-force_key_frames", f"expr:gte(n,n_forced*{k})",
            "-pix_fmt", "yuv420p",
        ]
    raise EncodeError(f"unsupported codec: {codec}")


def build_ffmpeg_cmd(
    ctx: EncodeContext,
    codec: str,
    tier: Tier,
    target_kbps: int,
    bitrate_override: dict[str, int],
) -> list[str]:
    """Return the ffmpeg argv for a single variant encode.

    Kept separate from the subprocess call so callers/tests can
    inspect the command without spawning ffmpeg.
    """
    k = keyint(ctx.fps, ctx.gop_duration_s)
    maxrate_k = int(target_kbps * ctx.maxrate_percent / 100)
    bufsize_k = target_kbps * BUFSIZE_MULTIPLIER

    filter_str = build_filter(BurninContext(
        codec=codec,
        tier=tier,
        fps=ctx.fps,
        rate_label=rate_label(target_kbps, ctx.maxrate_percent),
        encoder_label="SW",
        content_duration_s=ctx.content_duration_s,
        padding_duration_s=ctx.padding_duration_s,
    ))

    out_path = _variant_path(ctx.output_dir, codec, tier)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(ctx.mezzanine_path),
        "-vf", filter_str,
    ]
    cmd += _codec_specific_args(codec, target_kbps, k, tier.preset)
    cmd += [
        "-b:v", f"{target_kbps}k",
        "-maxrate", f"{maxrate_k}k",
        "-bufsize", f"{bufsize_k}k",
        "-tag:v", _VIDEO_TAGS[codec],
        "-an",
        "-movflags", "empty_moov+default_base_moof",
        "-frag_duration", str(_FRAG_DURATION_US),
        str(out_path),
        "-loglevel", "warning",
        "-stats",
    ]
    return cmd


def encode_variant(
    ctx: EncodeContext,
    codec: str,
    tier: Tier,
    bitrate_override: dict[str, int] | None = None,
) -> Path:
    """Encode one (codec, tier) combination. Returns the output path."""
    bitrate_override = bitrate_override or {}
    target_kbps = resolve_bitrate(tier, codec, bitrate_override)
    cmd = build_ffmpeg_cmd(ctx, codec, tier, target_kbps, bitrate_override)

    out_path = _variant_path(ctx.output_dir, codec, tier)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        run_ffmpeg_with_progress(
            cmd,
            duration_s=ctx.content_duration_s + ctx.padding_duration_s,
            stage_key=variant_stage_key(codec, tier.name),
        )
    except subprocess.CalledProcessError as e:
        raise EncodeError(
            f"encode failed: {codec} {tier.name} @ {target_kbps}kbps "
            f"(ffmpeg exit {e.returncode})"
        ) from e

    if not out_path.is_file():
        raise EncodeError(f"encode produced no output: {out_path}")
    # Atomic completion marker: a sibling file named <out>.done whose
    # body is the MP4's final byte size. Resume logic (resume.discover)
    # only counts variants with a matching .done sidecar, so a file
    # rsynced mid-write (e.g. during spot interrupt) stays flagged as
    # partial and gets re-encoded instead of silently producing a
    # corrupted output on retry.
    _write_done_marker(out_path)
    return out_path


def _write_done_marker(path: Path) -> None:
    """Write `<path>.done` atomically with the source file's size."""
    size = path.stat().st_size
    marker = path.with_suffix(path.suffix + ".done")
    tmp = marker.with_suffix(marker.suffix + ".tmp")
    tmp.write_text(f"{size}\n")
    tmp.rename(marker)


def codec_list(selection: str) -> list[str]:
    """Expand 'both' / 'all' / single-codec selections into a list."""
    if selection == "both":
        return ["hevc", "h264"]
    if selection == "all":
        return ["hevc", "h264", "av1"]
    if selection in ("hevc", "h264", "av1"):
        return [selection]
    raise EncodeError(f"unknown codec selection: {selection}")


def encode_all(
    ctx: EncodeContext,
    tiers: Iterable[Tier],
    codec_selection: str,
    bitrate_override_h264: dict[str, int] | None = None,
    bitrate_override_hevc: dict[str, int] | None = None,
) -> list[Path]:
    """Encode every (tier, codec) pair and return the output paths.

    Runs sequentially — parallelism happens at the Go-server level (one
    worker container per file; multiple jobs can run concurrently via
    MAX_CONCURRENT), not inside a single variant loop.
    """
    codecs = codec_list(codec_selection)
    overrides = {"h264": bitrate_override_h264 or {},
                 "hevc": bitrate_override_hevc or {},
                 "av1": {}}
    outputs: list[Path] = []
    for tier in tiers:
        for codec in codecs:
            out = _variant_path(ctx.output_dir, codec, tier)
            # Skip variants that are already fully encoded — the .done
            # sidecar's size must match the MP4's current size, so a
            # file rsynced mid-write (common after spot interrupt)
            # doesn't trigger a false skip.
            if _is_complete(out):
                print(f"[resume] skipping {codec} {tier.name} "
                      f"— already complete ({out.name})", flush=True)
                outputs.append(out)
                continue
            outputs.append(encode_variant(ctx, codec, tier, overrides[codec]))
    return outputs


def _is_complete(mp4: Path) -> bool:
    """Matches resume._is_complete — duplicated to avoid a circular import
    (resume imports ladder; encode_variants imports ladder via ffprobe)."""
    if not mp4.is_file() or mp4.stat().st_size == 0:
        return False
    marker = mp4.with_suffix(mp4.suffix + ".done")
    if not marker.is_file():
        return False
    try:
        return int(marker.read_text().strip()) == mp4.stat().st_size
    except (OSError, ValueError):
        return False
