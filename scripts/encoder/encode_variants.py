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
from encoder.chunking import Chunk
from encoder.gop import keyint
from encoder.ladder import (
    BUFSIZE_MULTIPLIER,
    DEFAULT_MAXRATE_PERCENT,
    Tier,
    resolve_bitrate,
)
from encoder.progress import emit_stage, run_ffmpeg_with_progress


def variant_stage_key(codec: str, tier_name: str, chunk_index: int | None = None) -> str:
    """Stable stage key the Go server uses to identify a single variant.

    With `chunk_index` set (chunked encode), the key gains a `:chunkN` suffix
    so each chunk's progress is tracked independently.
    """
    base = f"encode:{codec}:{tier_name}"
    return base if chunk_index is None else f"{base}:chunk{chunk_index}"


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
    # Two-pass software encode for libx264/libx265 (ports smashing #965).
    # Single-pass capped VBR distributes bits with only a lookahead
    # window, so on complex content the achieved average can fall short
    # of the -b:v target — which drags the real bitrate below the
    # advertised AVERAGE-BANDWIDTH and packs the ladder rungs together.
    # Two-pass fixes it: pass 1 profiles scene complexity into a stats
    # file (output discarded via the null muxer), pass 2 distributes bits
    # to hit the average accurately while the SAME maxrate/bufsize VBV
    # keeps peaks flat. Roughly doubles encode time. Ignored for av1
    # (libsvtav1) — matches bash, which only two-passes the x26x encoders.
    #
    # Note vs smashing: the undershoot there was ~17% and acute because
    # its VBV bufsize was 0.25x target. This repo runs a looser VBV
    # (BUFSIZE_MULTIPLIER=2, maxrate 124%), so single-pass drift is
    # milder here; two-pass is still the accurate-average path and keeps
    # behaviour aligned with the bash pipeline.
    two_pass: bool = False


class EncodeError(RuntimeError):
    pass


def _variant_path(output_dir: Path, codec: str, tier: Tier) -> Path:
    # Matches bash: $TEMP_DIR/${codec}_${res_name}.mp4
    return output_dir / f"{codec}_{tier.name}.mp4"


def _chunk_path(output_dir: Path, codec: str, tier: Tier, chunk_index: int) -> Path:
    """Per-chunk output, e.g. hevc_1080p_chunk003.mp4. The concat phase joins
    these into the whole-variant _variant_path before packaging."""
    return output_dir / f"{codec}_{tier.name}_chunk{chunk_index:03d}.mp4"


def _pass_suffix(pass_num: int | None, stats_path: Path | None) -> str:
    """`:pass=N:stats=PATH` appended to an x26x param string for two-pass,
    or empty for single-pass. Shared by the h264/hevc branches so both
    passes reuse the SAME base params (a divergence between passes would
    invalidate the pass-1 stats)."""
    if pass_num is None:
        return ""
    if stats_path is None:
        raise EncodeError("two-pass encode requires a stats_path")
    return f":pass={pass_num}:stats={stats_path}"


def _codec_specific_args(
    codec: str,
    target_kbps: int,
    k: int,
    preset: str,
    pass_num: int | None = None,
    stats_path: Path | None = None,
) -> list[str]:
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
            f"keyint={k}:min-keyint={k}:scenecut=0:open-gop=0:pools=*:frame-threads=0"
            + _pass_suffix(pass_num, stats_path),
            "-pix_fmt", "yuv420p",
        ]
    if codec == "h264":
        return [
            "-c:v", "libx264",
            "-preset", preset,
            "-threads", "0",
            "-x264-params",
            f"keyint={k}:min-keyint={k}:scenecut=0:open-gop=0"
            + _pass_suffix(pass_num, stats_path),
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


def _stats_path(output_dir: Path, codec: str, tier: Tier,
                chunk: Chunk | None = None) -> Path:
    """ffmpeg two-pass stats log for a variant (or one of its chunks). Lives
    beside the output in the ephemeral work dir; ffmpeg also writes a
    `<log>.mbtree` sibling. Chunk-specific so parallel chunk encodes never
    share a stats file."""
    suffix = "" if chunk is None else f"_chunk{chunk.index:03d}"
    return output_dir / f"{codec}_{tier.name}{suffix}_2pass.log"


def build_ffmpeg_cmd(
    ctx: EncodeContext,
    codec: str,
    tier: Tier,
    target_kbps: int,
    bitrate_override: dict[str, int],
    pass_num: int | None = None,
    chunk: Chunk | None = None,
) -> list[str]:
    """Return the ffmpeg argv for a single variant encode.

    Kept separate from the subprocess call so callers/tests can
    inspect the command without spawning ffmpeg.

    `pass_num` selects the encoding mode:
      - None  → single-pass (default).
      - 1     → two-pass analysis: same filter + rate control, but the
                encode is muxed to the null sink (`-f null -`) so only the
                stats file is produced. No -tag:v / fragmentation.
      - 2     → two-pass final: identical to single-pass output but reads
                the pass-1 stats to hit the target average accurately.
    Only valid for h264/hevc; av1 always single-passes.

    `chunk` selects chunked encoding: the encode is restricted to the
    chunk's `[start_s, start_s+duration_s)` window (fast input seek), the
    output goes to `_chunk_path`, and the burn-in timecode is offset by the
    chunk's start so it stays continuous across concatenated chunks. The
    encoder emits an IDR at the window start (frame 0 of a fresh encode) and
    closed 1s GOPs thereafter, so chunk boundaries land on IDRs.
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
        timecode_start_s=chunk.start_s if chunk else 0.0,
    ))

    if chunk is None:
        out_path = _variant_path(ctx.output_dir, codec, tier)
    else:
        out_path = _chunk_path(ctx.output_dir, codec, tier, chunk.index)
    stats_path = _stats_path(ctx.output_dir, codec, tier, chunk) if pass_num else None

    cmd = ["ffmpeg", "-y"]
    if chunk is not None:
        # Input seek: fast (keyframe) seek + re-encode = frame-accurate window.
        cmd += ["-ss", f"{chunk.start_s:.6f}", "-t", f"{chunk.duration_s:.6f}"]
    cmd += [
        "-i", str(ctx.mezzanine_path),
        "-vf", filter_str,
    ]
    cmd += _codec_specific_args(codec, target_kbps, k, tier.preset,
                                pass_num=pass_num, stats_path=stats_path)
    cmd += [
        "-b:v", f"{target_kbps}k",
        "-maxrate", f"{maxrate_k}k",
        "-bufsize", f"{bufsize_k}k",
    ]
    if pass_num == 1:
        # Analysis pass: discard the muxed output, keep only the stats.
        cmd += ["-an", "-f", "null", "-"]
    else:
        cmd += [
            "-tag:v", _VIDEO_TAGS[codec],
            "-an",
            "-movflags", "empty_moov+default_base_moof",
            "-frag_duration", str(_FRAG_DURATION_US),
            str(out_path),
        ]
    cmd += ["-loglevel", "warning", "-stats"]
    return cmd


def encode_variant(
    ctx: EncodeContext,
    codec: str,
    tier: Tier,
    bitrate_override: dict[str, int] | None = None,
    chunk: Chunk | None = None,
) -> Path:
    """Encode one (codec, tier) combination. Returns the output path.

    With `chunk` set, encodes only that time window into `_chunk_path`
    (the resumable unit for spot encoding); the concat phase later joins
    the chunks into the whole `_variant_path`. Without it, encodes the
    whole clip as before.
    """
    bitrate_override = bitrate_override or {}
    target_kbps = resolve_bitrate(tier, codec, bitrate_override)

    if chunk is None:
        out_path = _variant_path(ctx.output_dir, codec, tier)
        stage_key = variant_stage_key(codec, tier.name)
        # Whole-clip progress spans content + any padding.
        total_duration_s = ctx.content_duration_s + ctx.padding_duration_s
    else:
        out_path = _chunk_path(ctx.output_dir, codec, tier, chunk.index)
        stage_key = variant_stage_key(codec, tier.name, chunk.index)
        # Chunk progress spans just this window.
        total_duration_s = chunk.duration_s
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # av1 (libsvtav1) has no two-pass path here — bash never two-passed it.
    two_pass = ctx.two_pass and codec in ("h264", "hevc")

    try:
        if two_pass:
            # Pass 1 profiles complexity into the stats file (output
            # discarded); pass 2 reads it to hit the target average.
            # Both passes reuse the same filter + rate control. With a
            # chunk, this runs per chunk against its own stats file.
            run_ffmpeg_with_progress(
                build_ffmpeg_cmd(ctx, codec, tier, target_kbps,
                                 bitrate_override, pass_num=1, chunk=chunk),
                duration_s=total_duration_s,
                stage_key=stage_key,
            )
            run_ffmpeg_with_progress(
                build_ffmpeg_cmd(ctx, codec, tier, target_kbps,
                                 bitrate_override, pass_num=2, chunk=chunk),
                duration_s=total_duration_s,
                stage_key=stage_key,
            )
        else:
            run_ffmpeg_with_progress(
                build_ffmpeg_cmd(ctx, codec, tier, target_kbps,
                                 bitrate_override, chunk=chunk),
                duration_s=total_duration_s,
                stage_key=stage_key,
            )
    except subprocess.CalledProcessError as e:
        where = f" chunk{chunk.index}" if chunk is not None else ""
        raise EncodeError(
            f"encode failed: {codec} {tier.name}{where} @ {target_kbps}kbps "
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


def concat_stage_key(codec: str, tier_name: str) -> str:
    """Stage key for the per-variant chunk-concat step."""
    return f"concat:{codec}:{tier_name}"


def concat_chunks(
    output_dir: Path,
    codec: str,
    tier: Tier,
    n_chunks: int,
) -> Path:
    """Join the `n_chunks` per-chunk encodes into the whole-variant mp4 via
    the ffmpeg concat demuxer (stream copy — no re-encode, no quality loss).
    Returns the joined `_variant_path`, written fragmented like a whole-clip
    encode so the packager consumes it identically.

    Every chunk file (and its `.done` sidecar) must be present; a missing or
    incomplete chunk aborts rather than producing a truncated variant.
    """
    if n_chunks < 1:
        raise EncodeError(f"concat needs at least one chunk, got {n_chunks}")
    chunk_paths = [_chunk_path(output_dir, codec, tier, i) for i in range(n_chunks)]
    incomplete = [p.name for p in chunk_paths if not _is_complete(p)]
    if incomplete:
        raise EncodeError(
            f"cannot concat {codec} {tier.name}: incomplete/missing chunks {incomplete}"
        )

    # concat demuxer reads a list file of single-quoted absolute paths.
    list_file = output_dir / f"{codec}_{tier.name}_concat.txt"
    list_file.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in chunk_paths)
    )

    out_path = _variant_path(output_dir, codec, tier)
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "empty_moov+default_base_moof",
        "-frag_duration", str(_FRAG_DURATION_US),
        str(out_path),
        "-loglevel", "warning",
    ]

    # Concat is a fast stream copy — a running/done stage is enough, no need
    # for duration-based progress (which the phase doesn't have on hand).
    stage_key = concat_stage_key(codec, tier.name)
    emit_stage(stage_key, "running", 0.0)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise EncodeError(
            f"concat failed: {codec} {tier.name} (ffmpeg exit {proc.returncode})\n"
            f"{proc.stderr[-1500:]}"
        )
    emit_stage(stage_key, "done", 100.0)

    if not out_path.is_file():
        raise EncodeError(f"concat produced no output: {out_path}")
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
                # Emit "skipped" so the UI flips this variant's bar to
                # the distinct reused-style (gray hash pattern + "reused"
                # label) rather than the green done-style it'd get from
                # a fresh encode. Makes it obvious at a glance which
                # stages were picked up from the prior run.
                emit_stage(variant_stage_key(codec, tier.name), "skipped", 100.0)
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
