"""Phase 5b — DASH packaging via Shaka Packager.

Given a directory of encoded variant MP4s (`hevc_540p.mp4` etc.) and
an optional audio MP4, runs Shaka Packager once to emit:

  <output_dir>/
    <res>/init.mp4, <res>/segment_NNNNN.m4s  (per resolution)
    audio/init.mp4, audio/segment_NNNNN.m4s  (if audio present)
    manifest.mpd

The resulting manifest.mpd uses SegmentTemplate URL patterns; we then
rewrite it to SegmentList form via encoder.manifests. That rewrite is
driven from the same module on purpose — it's the same tree walker
used by the caller today.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from encoder.ladder import Tier
from encoder.manifests import convert_segmentlist


@dataclass(frozen=True)
class PackageSpec:
    tmp_dir: Path            # holds {codec}_{res}.mp4 + audio.mp4
    output_dir: Path         # final DASH package directory
    codec: str               # "h264" | "hevc" | "av1"
    tiers: tuple[Tier, ...]  # resolutions to include
    segment_duration_s: float
    partial_duration_s: float
    include_audio: bool


class PackagerError(RuntimeError):
    pass


def build_packager_cmd(spec: PackageSpec) -> list[str]:
    """Return the shaka-packager argv.

    Each stream descriptor encodes the input MP4 plus where to emit
    init and segments *relative to the output_dir* (packager is run
    with cwd=output_dir, matching bash).
    """
    cmd: list[str] = ["packager"]

    for tier in spec.tiers:
        in_file = spec.tmp_dir / f"{spec.codec}_{tier.name}.mp4"
        cmd.append(
            f"in={in_file},stream=video,"
            f"init_segment={tier.name}/init.mp4,"
            f"segment_template={tier.name}/segment_$Number%05d$.m4s"
        )

    if spec.include_audio:
        cmd.append(
            f"in={spec.tmp_dir / 'audio.mp4'},stream=audio,"
            f"init_segment=audio/init.mp4,"
            f"segment_template=audio/segment_$Number%05d$.m4s"
        )

    cmd += [
        f"--segment_duration", str(spec.segment_duration_s),
        f"--fragment_duration", str(spec.partial_duration_s),
        "--fragment_sap_aligned=false",
        "--mpd_output", "manifest.mpd",
        "--generate_static_live_mpd",
    ]
    return cmd


def package(spec: PackageSpec) -> Path:
    """Run Shaka Packager and return the path to the generated MPD.

    After packager exits, we rewrite the MPD from SegmentTemplate
    (packager's output form) to SegmentList — explicit per-segment
    URLs, which is what the HLS generator and player downstream
    expect. Raises PackagerError on any failure.
    """
    spec.output_dir.mkdir(parents=True, exist_ok=True)
    for tier in spec.tiers:
        (spec.output_dir / tier.name).mkdir(exist_ok=True)
    if spec.include_audio:
        (spec.output_dir / "audio").mkdir(exist_ok=True)

    cmd = build_packager_cmd(spec)
    try:
        subprocess.run(cmd, check=True, cwd=spec.output_dir)
    except subprocess.CalledProcessError as e:
        raise PackagerError(f"shaka packager failed ({e.returncode})") from e

    manifest_path = spec.output_dir / "manifest.mpd"
    if not manifest_path.is_file():
        raise PackagerError(
            f"manifest.mpd not found under {spec.output_dir} "
            f"(shaka packager succeeded but output is missing)"
        )

    # Convert SegmentTemplate → SegmentList. `backup=True` keeps the
    # original packager output as .mpd.template.bak for debugging.
    convert_segmentlist(manifest_path, backup=True)
    return manifest_path
