#!/usr/bin/env python3
"""Local encode orchestrator (Python replacement for create_abr_ladder.sh).

Runs the full ABR pipeline against a single input:

  1. Probe input (ffprobe) — must have a video stream, 640x360 min
  2. Create a stream-copy mezzanine MP4 (so later phases share one input)
  3. Select which ladder tiers to encode (by source width + --max-res)
  4. Plan segment-boundary padding (LCM of segment/partial/gop)
  5. Encode each (codec, tier) variant — closed GOPs, drawtext burn-in
  6. Extract/transcode audio (if present)
  7. Per codec: Shaka Packager → DASH package + SegmentList manifest,
     Phase 6 byteranges sidecars, Phase 7 LL-HLS playlists, optional
     TS HLS variant

Hardware encoding (--force-hardware) is not supported. VideoToolbox
only works on macOS hosts; workers run in Linux.
"""
from __future__ import annotations

import argparse
import os
import resource
import shutil
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

from encoder.audio import AudioSpec, create_audio
from encoder.encode_variants import EncodeContext, encode_all, variant_stage_key
from encoder.ffprobe import ProbeError, probe
from encoder.hls import (
    TsHlsSpec, generate_byteranges_sidecars, generate_fmp4_hls, generate_ts_hls,
)
from encoder.ladder import (
    LADDER, Tier, parse_bitrate_override, select_tiers,
)
from encoder.mezzanine import MezzanineSpec, create_mezzanine
from encoder.packager import PackageSpec, package
from encoder.padding import multi_duration_lcm, plan_padding
from encoder.progress import Stage, emit_boot_ami, emit_plan, emit_stage
from encoder.resume import discover, resolve_codec_selection

# Matches bash's minimum.
MIN_WIDTH = 640
MIN_HEIGHT = 360

# Shared filesystem root for intermediates. Worker container has /tmp.
# Respect TMPDIR so per-clip work dirs land on the same filesystem as
# output_dir (avoids EXDEV on Shaka Packager's rename), and so on cloud
# encodes our persistent `encode_<stem>/` dir lives under /work/tmp —
# which the user-data syncs to S3 on spot interrupt and back on retry.
_TMP_ROOT = Path(os.environ.get("TMPDIR")
                  or os.environ.get("ENCODER_TMP_ROOT")
                  or "/tmp")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="encoder.cli_local",
        description="Encode one source file into a DASH + HLS ABR package.",
    )
    p.add_argument("--input", type=Path)
    p.add_argument("--resume-package-from", type=Path, dest="resume_package_from")
    p.add_argument("--output", default=None,
                   help="base name for output dirs (stem + partial/pad suffixes)")
    p.add_argument("--output-dir", type=Path, required=True, dest="output_dir")

    p.add_argument("--codec", default="both",
                   choices=("h264", "hevc", "av1", "both", "all"))
    p.add_argument("--max-res", default=None, dest="max_res",
                   choices=("360p", "540p", "720p", "1080p", "1440p", "2160p"))
    p.add_argument("--time", type=float, default=None, dest="time_limit_s",
                   help="limit mezzanine duration (seconds)")

    p.add_argument("--segment-duration", type=float, default=6.0,
                   dest="segment_duration_s")
    p.add_argument("--partial-duration", type=float, default=0.2,
                   dest="partial_duration_s")
    p.add_argument("--gop-duration", type=float, default=1.0,
                   dest="gop_duration_s")

    p.add_argument("--hls-format", default="fmp4",
                   choices=("fmp4", "ts", "both"), dest="hls_format")

    pad = p.add_mutually_exclusive_group()
    pad.add_argument("--padding", action="store_const", const="black",
                     dest="padding_mode")
    pad.add_argument("--padding-pink", action="store_const", const="pink",
                     dest="padding_mode")
    pad.add_argument("--no-padding", action="store_const", const="off",
                     dest="padding_mode")
    p.set_defaults(padding_mode="off")

    p.add_argument("--keep-mezzanine", action="store_true",
                   dest="keep_mezzanine")
    p.add_argument("--bitrate-override-hevc", default=None,
                   dest="bitrate_override_hevc")
    p.add_argument("--bitrate-override-h264", default=None,
                   dest="bitrate_override_h264")
    # HEVC (libx265) is two-pass by default — it's the accurate-average
    # path and the correct behaviour. This flag forces HEVC single-pass
    # (~2x faster) for a side-by-side bitrate comparison. H264 is always
    # single-pass (x264's VBV already hits target); av1 has no two-pass.
    p.add_argument("--hevc-single-pass", action="store_true",
                   dest="hevc_single_pass",
                   help="encode HEVC single-pass instead of the default "
                        "two-pass (faster, less accurate average — for "
                        "comparison; H264/AV1 are single-pass regardless)")

    p.add_argument("--vmaf-lookup-csv", default=None, dest="vmaf_lookup_csv",
                   help="unused for now; reserved for burn-in VMAF labels")
    p.add_argument("--vmaf-lookup-mode", default="auto",
                   dest="vmaf_lookup_mode")
    return p


# ---------------------------------------------------------------------------
# Output path helpers
# ---------------------------------------------------------------------------

def _output_stem(input_path: Path, explicit: str | None) -> str:
    """If the caller passed --output, use that; else derive from input stem.

    The Go server always passes --output, so this fallback is mostly
    relevant for manual invocations and tests.
    """
    if explicit:
        return explicit
    return input_path.stem


def _codec_package_dir(output_dir: Path, stem: str, codec: str) -> Path:
    return output_dir / f"{stem}_{codec}"


def _ts_package_dir(output_dir: Path, stem: str, codec: str) -> Path:
    # Matches bash's OUTPUT_DIR_HEVC_TS="${stem}_hevc_ts"
    return output_dir / f"{stem}_{codec}_ts"


def _codec_list(selection: str) -> list[str]:
    if selection == "both":
        return ["hevc", "h264"]
    if selection == "all":
        return ["hevc", "h264", "av1"]
    return [selection]


def _emit_plan(
    *,
    tiers: list[Tier],
    codecs: list[str],
    has_audio: bool,
    want_fmp4: bool,
    want_ts: bool,
) -> None:
    """Announce every stage the pipeline will visit, in display order."""
    stages: list[Stage] = [Stage(key="mezzanine", label="mezzanine")]
    for tier in tiers:
        for codec in codecs:
            stages.append(Stage(
                key=variant_stage_key(codec, tier.name),
                label=f"encode {codec} {tier.name}",
            ))
    if has_audio:
        stages.append(Stage(key="audio", label="audio"))
    for codec in codecs:
        stages.append(Stage(key=f"package:{codec}", label=f"package {codec}"))
        if want_fmp4:
            stages.append(Stage(key=f"fragments:{codec}",
                                label=f"fragments {codec}"))
            stages.append(Stage(key=f"hls:{codec}", label=f"HLS {codec}"))
        if want_ts:
            stages.append(Stage(key=f"hls-ts:{codec}",
                                label=f"HLS TS {codec}"))
    emit_plan(stages)


# ---------------------------------------------------------------------------
# Normal (non-resume) pipeline
# ---------------------------------------------------------------------------

def run_full(args: argparse.Namespace) -> int:
    input_path = args.input
    if input_path is None:
        print("Error: --input required (or pass --resume-package-from)",
              file=sys.stderr)
        return 2

    if not os.access(input_path, os.R_OK):
        print(f"Error: cannot read input: {input_path}", file=sys.stderr)
        return 1

    try:
        info = probe(input_path)
    except ProbeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if info.width < MIN_WIDTH or info.height < MIN_HEIGHT:
        print(
            f"Error: input resolution too low ({info.width}x{info.height}); "
            f"minimum is {MIN_WIDTH}x{MIN_HEIGHT}",
            file=sys.stderr,
        )
        return 1

    print(
        f"[input] {input_path.name}: {info.width}x{info.height} "
        f"@ {float(info.fps):.3f} fps, {info.duration_s:.1f}s"
        + ("" if info.has_audio else " (no audio)"),
        flush=True,
    )

    stem = _output_stem(input_path, args.output)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Pre-compute the tier/codec plan so we can announce all stages up front
    # and the UI can render a fully-laid-out table before any encode starts.
    tiers = select_tiers(args.max_res, info.width)
    if not tiers:
        print("Error: no tiers fit this source resolution", file=sys.stderr)
        return 1

    codecs = _codec_list(args.codec)
    want_fmp4 = args.hls_format in ("fmp4", "both")
    want_ts = args.hls_format in ("ts", "both")

    _emit_plan(
        tiers=tiers, codecs=codecs, has_audio=info.has_audio,
        want_fmp4=want_fmp4, want_ts=want_ts,
    )

    # Per-clip persistent work dir. Lives under $ENCODER_TMP_ROOT /
    # $TMPDIR so on cloud encodes it maps to /work/tmp — which the
    # user-data rsyncs to S3 on spot interrupt and back down on
    # retry. Variant MP4s, the mezzanine, and audio.mp4 all land
    # here and survive across retries (cleaned up on successful
    # completion below).
    work_dir = _TMP_ROOT / f"encode_{stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    # Preflight: sweep out partially-encoded files left over from a
    # prior interrupt. A file without a matching .done sidecar (or
    # whose .done size disagrees with the file) is partial — keeping
    # it around wastes disk and can confuse the next ffmpeg pass.
    _sweep_partials(work_dir)
    try:
        mezz_path = work_dir / "mezzanine.mp4"

        # Skip phase 2 if a complete mezzanine is already present —
        # happens on retry after a prior interrupt. Size-checked via
        # the .done sidecar, so a partial rsync of mezzanine.mp4
        # doesn't falsely skip.
        if _is_complete(mezz_path):
            print(f"[phase 2] reusing mezzanine from prior run "
                  f"({mezz_path.name})", flush=True)
            emit_stage("mezzanine", "skipped", 100.0)
        else:
            print("[phase 2] creating mezzanine...", flush=True)
            # Duration for progress calc: source duration, capped by --time.
            mezz_expected_duration = info.duration_s
            if args.time_limit_s is not None and args.time_limit_s > 0:
                mezz_expected_duration = min(mezz_expected_duration, args.time_limit_s)
            create_mezzanine(
                MezzanineSpec(
                    input_path=input_path,
                    output_path=mezz_path,
                    time_limit_s=args.time_limit_s,
                ),
                stage_key="mezzanine",
                duration_s=mezz_expected_duration,
            )
            _write_done(mezz_path)

        # Re-probe after the time limit might have truncated duration.
        mezz_info = probe(mezz_path)
        effective_duration_s = mezz_info.duration_s or info.duration_s

        print(f"[phase 2b] tiers: {[t.name for t in tiers]}", flush=True)

        pad_boundary = multi_duration_lcm(
            args.segment_duration_s, args.partial_duration_s, args.gop_duration_s,
        )
        pad = plan_padding(
            video_duration_s=effective_duration_s,
            audio_duration_s=(effective_duration_s if info.has_audio else None),
            boundary_s=pad_boundary,
        ) if args.padding_mode != "off" else None
        video_pad_s = pad.video_pad_s if pad else 0.0
        audio_pad_s = pad.audio_pad_s if pad else 0.0
        if pad and (video_pad_s > 0 or audio_pad_s > 0):
            print(
                f"[phase 2c] padding to {pad.boundary_s}s boundary: "
                f"video +{video_pad_s:.3f}s, audio +{audio_pad_s:.3f}s",
                flush=True,
            )

        encode_ctx = EncodeContext(
            mezzanine_path=mezz_path,
            output_dir=work_dir,
            fps=info.fps,
            gop_duration_s=args.gop_duration_s,
            content_duration_s=effective_duration_s,
            padding_duration_s=video_pad_s,
            hevc_two_pass=not args.hevc_single_pass,
        )
        ovr_hevc = parse_bitrate_override(args.bitrate_override_hevc)
        ovr_h264 = parse_bitrate_override(args.bitrate_override_h264)

        print(f"[phase 3] encoding variants: codec={args.codec}", flush=True)
        t0 = time.monotonic()
        encode_all(
            encode_ctx, tiers, args.codec,
            bitrate_override_hevc=ovr_hevc, bitrate_override_h264=ovr_h264,
        )
        print(f"[phase 3] done in {time.monotonic() - t0:.1f}s", flush=True)

        if info.has_audio:
            audio_path = work_dir / "audio.mp4"
            if _is_complete(audio_path):
                print(f"[phase 4] reusing audio from prior run "
                      f"({audio_path.name})", flush=True)
                emit_stage("audio", "skipped", 100.0)
            else:
                print("[phase 4] creating audio mezzanine...", flush=True)
                create_audio(
                    AudioSpec(
                        mezzanine_path=mezz_path,
                        output_path=audio_path,
                        padding_s=audio_pad_s,
                    ),
                    stage_key="audio",
                    duration_s=effective_duration_s + audio_pad_s,
                )

        for codec in codecs:
            pkg_dir = _codec_package_dir(args.output_dir, stem, codec)
            print(f"[phase 5] packaging {codec} → {pkg_dir.name}", flush=True)
            emit_stage(f"package:{codec}", "running", 0.0)
            package(PackageSpec(
                tmp_dir=work_dir,
                output_dir=pkg_dir,
                codec=codec,
                tiers=tuple(tiers),
                segment_duration_s=args.segment_duration_s,
                partial_duration_s=args.partial_duration_s,
                include_audio=info.has_audio,
            ))
            emit_stage(f"package:{codec}", "done", 100.0)

            if want_fmp4:
                print(f"[phase 6] fragment sidecars for {codec}...", flush=True)
                emit_stage(f"fragments:{codec}", "running", 0.0)
                count = generate_byteranges_sidecars(pkg_dir)
                emit_stage(f"fragments:{codec}", "done", 100.0)
                print(f"[phase 6] wrote {count} byteranges sidecars", flush=True)

                print(f"[phase 7] fMP4 HLS playlists for {codec}...", flush=True)
                emit_stage(f"hls:{codec}", "running", 0.0)
                generate_fmp4_hls(pkg_dir)
                emit_stage(f"hls:{codec}", "done", 100.0)

            if want_ts:
                ts_dir = _ts_package_dir(args.output_dir, stem, codec)
                print(f"[phase 7b] TS HLS for {codec} → {ts_dir.name}",
                      flush=True)
                emit_stage(f"hls-ts:{codec}", "running", 0.0)
                generate_ts_hls(TsHlsSpec(
                    tmp_dir=work_dir,
                    ts_output_dir=ts_dir,
                    codec=codec,
                    tiers=tuple(tiers),
                    segment_duration_s=args.segment_duration_s,
                    include_audio=info.has_audio,
                ))
                emit_stage(f"hls-ts:{codec}", "done", 100.0)

        if args.keep_mezzanine:
            mezzanine_out = args.output_dir / f"{stem}_mezzanine"
            mezzanine_out.mkdir(exist_ok=True)
            shutil.copy(mezz_path, mezzanine_out / "mezzanine.mp4")
            if info.has_audio:
                shutil.copy(work_dir / "audio.mp4", mezzanine_out / "audio.mp4")
            for codec in _codec_list(args.codec):
                for tier in tiers:
                    src = work_dir / f"{codec}_{tier.name}.mp4"
                    if src.is_file():
                        shutil.copy(src, mezzanine_out / src.name)

        # Successful end: work_dir is no longer needed for retry.
        shutil.rmtree(work_dir, ignore_errors=True)
    except BaseException:
        # Any failure (encode error, Ctrl-C, uncaught exception) leaves
        # work_dir in place — the cloud's user-data syncs it to S3, and
        # a retry's sync pulls it back so we skip work that's already
        # done. Cleanup on abandoned local runs happens via TMP_DIR
        # normal hygiene (or user rm).
        raise

    print("[done]", flush=True)
    return 0


def _sweep_partials(work_dir: Path) -> None:
    """Delete .mp4 files without a matching .done sidecar, and orphan
    .done sidecars without a backing .mp4 — leaves only the clean
    complete-pair files for the retry to reuse."""
    removed = []
    for mp4 in work_dir.glob("*.mp4"):
        if not _is_complete(mp4):
            marker = mp4.with_suffix(mp4.suffix + ".done")
            try:
                mp4.unlink()
                if marker.is_file():
                    marker.unlink()
                removed.append(mp4.name)
            except OSError:
                pass
    for marker in work_dir.glob("*.mp4.done"):
        # Orphan markers: sidecar without a corresponding .mp4
        mp4 = marker.with_suffix("")  # .mp4.done -> .mp4
        if not mp4.is_file():
            try:
                marker.unlink()
                removed.append(marker.name)
            except OSError:
                pass
    if removed:
        print(f"[preflight] cleared partial/orphan files: "
              f"{', '.join(removed)}", flush=True)


def _is_complete(mp4: Path) -> bool:
    """True iff mp4 exists and has a matching .done sidecar whose
    recorded size equals the current file size. Matches
    resume._is_complete but duplicated to avoid an import chain."""
    if not mp4.is_file() or mp4.stat().st_size == 0:
        return False
    marker = mp4.with_suffix(mp4.suffix + ".done")
    if not marker.is_file():
        return False
    try:
        return int(marker.read_text().strip()) == mp4.stat().st_size
    except (OSError, ValueError):
        return False


def _write_done(path: Path) -> None:
    """Atomically write `<path>.done` with the source file's size."""
    size = path.stat().st_size
    marker = path.with_suffix(path.suffix + ".done")
    tmp = marker.with_suffix(marker.suffix + ".tmp")
    tmp.write_text(f"{size}\n")
    tmp.rename(marker)


# ---------------------------------------------------------------------------
# Resume (skip phases 1-4, reuse existing variant MP4s)
# ---------------------------------------------------------------------------

def run_resume(args: argparse.Namespace) -> int:
    resume_dir = Path(args.resume_package_from)
    # If the resume directory doesn't exist (or exists but has no
    # completed variant MP4s), there's nothing to skip — fall through
    # to a full encode. This is the cloud-retry case where the prior
    # run got spot-interrupted before any variant finished: the remote
    # pre-warmed /work from the dead instance's S3, but /work/output
    # is empty (or only had partial files that lack .done markers).
    if not resume_dir.is_dir():
        print(f">>> resume: {resume_dir} missing; running full encode",
              flush=True)
        return run_full(args)
    inventory = discover(resume_dir)
    if not inventory.available:
        print(">>> resume: no complete variants found under "
              f"{resume_dir}; running full encode", flush=True)
        return run_full(args)
    codec_selection = resolve_codec_selection(inventory, args.codec or None)

    # Use the union of tiers available across the selected codecs.
    selected_codecs = _codec_list(codec_selection)
    tiers: list[Tier] = []
    seen: set[str] = set()
    for codec in selected_codecs:
        for tier in inventory.available.get(codec, []):
            if tier.name not in seen:
                tiers.append(tier)
                seen.add(tier.name)
    if args.max_res:
        tiers = [t for t in tiers if LADDER.index(t) <= [x.name for x in LADDER].index(args.max_res)]

    stem = args.output or resume_dir.name
    args.output_dir.mkdir(parents=True, exist_ok=True)

    want_fmp4 = args.hls_format in ("fmp4", "both")
    want_ts = args.hls_format in ("ts", "both")

    for codec in selected_codecs:
        pkg_dir = _codec_package_dir(args.output_dir, stem, codec)
        print(f"[resume] packaging {codec} → {pkg_dir.name}", flush=True)
        package(PackageSpec(
            tmp_dir=resume_dir, output_dir=pkg_dir, codec=codec,
            tiers=tuple(tiers),
            segment_duration_s=args.segment_duration_s,
            partial_duration_s=args.partial_duration_s,
            include_audio=inventory.has_audio,
        ))

        if want_fmp4:
            generate_byteranges_sidecars(pkg_dir)
            generate_fmp4_hls(pkg_dir)

        if want_ts:
            ts_dir = _ts_package_dir(args.output_dir, stem, codec)
            generate_ts_hls(TsHlsSpec(
                tmp_dir=resume_dir, ts_output_dir=ts_dir, codec=codec,
                tiers=tuple(tiers),
                segment_duration_s=args.segment_duration_s,
                include_audio=inventory.has_audio,
            ))

    print("[done]", flush=True)
    return 0


# Rough Graviton (c7g/c8g/c6g) spot price per vCPU-hour in us-west-2. We can't
# read the exact instance type from inside the container, but nproc x this is a
# solid cost estimate for the encode compute. Override via SPOT_USD_PER_VCPU_HR.
_SPOT_USD_PER_VCPU_HR = 0.0155


def _log_run_summary(wall_s: float, cpu_s: float) -> None:
    """Emit a run-time time/efficiency/cost summary for the encode — computed
    from the process's own clock and getrusage (the ffmpeg/packager children's
    CPU-seconds), so it's reliable even for short runs where CloudWatch is too
    sparse. efficiency = cpu_s / (wall_s x vCPU) = how much of the box's cores
    the encode actually used."""
    vcpu = os.cpu_count() or 1
    cores_busy = cpu_s / wall_s if wall_s else 0.0
    util = cores_busy / vcpu if vcpu else 0.0
    rate = float(os.environ.get("SPOT_USD_PER_VCPU_HR", _SPOT_USD_PER_VCPU_HR))
    cost = wall_s / 3600.0 * vcpu * rate
    # Machine-readable marker (parsed like the other ENCODER-* markers) + human.
    print(f"[[ENCODER-SUMMARY wall_s={wall_s:.1f} cpu_s={cpu_s:.1f} vcpu={vcpu} "
          f"cores_busy={cores_busy:.2f} efficiency_pct={util * 100:.0f} "
          f"cost_usd={cost:.3f}]]", flush=True)
    print(f"[run summary] wall={wall_s:.1f}s  cpu-time={cpu_s:.1f}s  "
          f"efficiency={util * 100:.0f}% ({cores_busy:.1f} of {vcpu} cores busy)"
          f"  est encode cost=${cost:.3f} (Graviton spot)", flush=True)


def main() -> int:
    # Batch job definitions invoke us as
    # `python -m encoder.cli_local phase <name> ...`. Dispatch to the
    # phase handler before argparse gets hold of local-encode flags
    # that don't apply in the Batch context.
    if len(sys.argv) >= 2 and sys.argv[1] == "phase":
        from encoder.cli_phase import main as phase_main
        return phase_main(sys.argv[2:])

    args = build_parser().parse_args()
    emit_boot_ami()  # report which AMI this instance booted from (cloud remote)
    t0 = time.monotonic()
    ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    rc = run_resume(args) if args.resume_package_from else run_full(args)
    ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    _log_run_summary(
        time.monotonic() - t0,
        (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime),
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
