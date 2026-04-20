"""Resume-package-from support (Phase 0).

If the caller passes `--resume-package-from <path>`, the encode
pipeline skips phases 1–4 and reuses existing variant MP4s in the
given directory: it expects files named `{codec}_{res}.mp4` plus an
optional `audio.mp4`. Segmentation, DASH packaging, fragment
sidecars, and HLS manifests still run — resume lets the user redo
only the packaging/manifest work without re-encoding.

This module is a pure discovery layer: it scans the directory,
figures out which (codec, tier) combinations have usable MP4s, and
returns a structured view the orchestrator can turn into a plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from encoder.ladder import LADDER, Tier


SUPPORTED_CODECS = ("hevc", "h264", "av1")


@dataclass(frozen=True)
class ResumeInventory:
    # Codec → list of tiers that have non-empty MP4s in the resume dir.
    # Only codecs with at least one tier present appear here.
    available: dict[str, list[Tier]]
    # True if `<dir>/audio.mp4` exists and is non-empty.
    has_audio: bool

    def codecs(self) -> list[str]:
        return list(self.available.keys())


class ResumeError(RuntimeError):
    pass


def discover(resume_dir: Path) -> ResumeInventory:
    """Return which codecs/tiers are reusable from `resume_dir`.

    Empty files count as absent — bash checks `-s` (size > 0) on the
    MP4s before including them, and we match that. Raises `ResumeError`
    if the directory itself doesn't exist.
    """
    resume_dir = Path(resume_dir)
    if not resume_dir.is_dir():
        raise ResumeError(f"resume directory not found: {resume_dir}")

    available: dict[str, list[Tier]] = {}
    for codec in SUPPORTED_CODECS:
        tiers_for_codec: list[Tier] = []
        for tier in LADDER:
            candidate = resume_dir / f"{codec}_{tier.name}.mp4"
            if candidate.is_file() and candidate.stat().st_size > 0:
                tiers_for_codec.append(tier)
        if tiers_for_codec:
            available[codec] = tiers_for_codec

    audio_path = resume_dir / "audio.mp4"
    has_audio = audio_path.is_file() and audio_path.stat().st_size > 0

    return ResumeInventory(available=available, has_audio=has_audio)


def resolve_codec_selection(
    inventory: ResumeInventory, requested: str | None,
) -> str:
    """Pick which codec mode to drive.

    If the caller passed an explicit `--codec`, we validate the
    inventory has at least one tier for that mode; otherwise we
    auto-select the broadest mode the inventory supports. Matches
    the `CODEC_SELECTION_EXPLICIT` / auto-select logic in bash.
    """
    present = set(inventory.codecs())
    if not present:
        raise ResumeError(
            "no encoded variant MP4s found under resume directory "
            "(expected hevc_*.mp4 / h264_*.mp4 / av1_*.mp4)"
        )

    if requested:
        required = {
            "hevc": {"hevc"},
            "h264": {"h264"},
            "av1":  {"av1"},
            "both": {"hevc", "h264"},
            "all":  {"hevc", "h264", "av1"},
        }.get(requested)
        if required is None:
            raise ResumeError(f"unknown codec selection: {requested}")
        missing = required - present
        if missing:
            raise ResumeError(
                f"resume mode requested '{requested}' but no files for: "
                f"{', '.join(sorted(missing))}"
            )
        return requested

    if {"hevc", "h264", "av1"} <= present:
        return "all"
    if {"hevc", "h264"} <= present:
        return "both"
    # Fall back to the single codec present, preferring hevc.
    for codec in ("hevc", "h264", "av1"):
        if codec in present:
            return codec
    raise ResumeError("unreachable: no codecs in inventory")
