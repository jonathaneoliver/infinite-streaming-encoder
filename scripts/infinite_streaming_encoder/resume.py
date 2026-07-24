"""Resume-package-from support (Phase 0).

If the caller passes `--resume-package-from <path>`, the encode
pipeline skips phases 1–4 and reuses existing variant MP4s in the
given directory: it expects files named `{codec}_{res}.mp4` plus an
optional `audio.mp4`. Segmentation, DASH packaging, fragment
sidecars, and HLS manifests still run — resume lets the user redo
only the packaging/manifest work without re-encoding.

This module is a pure discovery layer: it scans the directory for
`{codec}_{label}.mp4` variant files, figures out which (codec, label)
combinations have usable MP4s, and returns a structured view the
orchestrator can turn into a plan. It is ladder-agnostic — it reads
whatever labels are on disk, so it works for any ladder (legacy or
apple, with its ordinal-suffixed labels like `1080p_1`).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


SUPPORTED_CODECS = ("hevc", "h264", "av1")

# Per-chunk encodes are `{codec}_{label}_chunkNNN.mp4`; the concat phase
# joins them into the whole-variant `{codec}_{label}.mp4`. Resume reuses
# only whole-variant files, so chunk files are excluded from discovery.
_CHUNK_RE = re.compile(r"_chunk\d+$")


def _is_complete(mp4: Path) -> bool:
    """A variant MP4 is 'complete' iff it has a matching .done sidecar
    whose recorded size equals the current file size.

    The sidecar is written atomically (encode_variants._write_done_marker)
    after ffmpeg exits successfully and the file is mux'd. A file rsynced
    mid-write — most commonly by the spot-interrupt rescue sync — will
    have no .done marker (or a stale one pointing at a different size),
    and is treated as incomplete so retry re-encodes it instead of
    producing corrupt output.
    """
    if not mp4.is_file() or mp4.stat().st_size == 0:
        return False
    marker = mp4.with_suffix(mp4.suffix + ".done")
    if not marker.is_file():
        return False
    try:
        recorded = int(marker.read_text().strip())
    except (OSError, ValueError):
        return False
    return recorded == mp4.stat().st_size


@dataclass(frozen=True)
class ResumeInventory:
    # Codec → list of rung labels that have complete (size-verified) MP4s.
    # Only codecs with at least one complete variant appear here.
    available: dict[str, list[str]]
    # True if `<dir>/audio.mp4` is complete (has a matching .done).
    has_audio: bool
    # Partial files detected (MP4 exists but no/mismatched .done). Kept
    # so callers can log a summary — "3 of 6 variants reused, 1 partial".
    partial: list[str] = field(default_factory=list)

    def codecs(self) -> list[str]:
        return list(self.available.keys())


class ResumeError(RuntimeError):
    pass


def discover(resume_dir: Path) -> ResumeInventory:
    """Return which codecs/tiers are reusable from `resume_dir`.

    Only counts files with a matching `.done` sidecar whose recorded
    size matches the MP4 — see `_is_complete`. Prints a summary line
    noting any partials found (the resume orchestrator re-encodes
    those instead of reusing them). Raises `ResumeError` if the
    directory itself doesn't exist.
    """
    resume_dir = Path(resume_dir)
    if not resume_dir.is_dir():
        raise ResumeError(f"resume directory not found: {resume_dir}")

    available: dict[str, list[str]] = {}
    partial: list[str] = []
    for codec in SUPPORTED_CODECS:
        labels_for_codec: list[str] = []
        # Scan for {codec}_{label}.mp4 (excluding per-chunk files). Sorted so
        # the label order is stable across runs.
        for candidate in sorted(resume_dir.glob(f"{codec}_*.mp4")):
            label = candidate.stem[len(codec) + 1:]
            if not label or _CHUNK_RE.search(label):
                continue
            if candidate.stat().st_size == 0:
                continue
            if _is_complete(candidate):
                labels_for_codec.append(label)
            else:
                partial.append(candidate.name)
        if labels_for_codec:
            available[codec] = labels_for_codec

    audio_path = resume_dir / "audio.mp4"
    if audio_path.is_file() and audio_path.stat().st_size > 0:
        if _is_complete(audio_path):
            has_audio = True
        else:
            has_audio = False
            partial.append("audio.mp4")
    else:
        has_audio = False

    if partial:
        # Visible so the user sees why a retry still re-encodes some
        # variants — typically these are the ones that were mid-encode
        # when spot interrupted.
        print(f"    resume: {len(partial)} partial file(s) will be re-encoded: "
              f"{', '.join(partial)}", file=sys.stderr, flush=True)

    return ResumeInventory(available=available, has_audio=has_audio,
                           partial=partial)


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
