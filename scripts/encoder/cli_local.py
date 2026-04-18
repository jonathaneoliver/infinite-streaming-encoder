#!/usr/bin/env python3
"""Local encode entry point.

Progressively replacing create_abr_ladder.sh. Currently:

  Phase 1 (input validation + ffprobe) — handled here in Python.
  Phases 2+ (mezzanine, encode variants, audio, segmentation, DASH,
  fragments, HLS) — still delegated to the bash script via execvp.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from encoder.ffprobe import ProbeError, probe

BASH_SCRIPT = "/app/scripts/create_abr_ladder.sh"

# Matches create_abr_ladder.sh's `INPUT_WIDTH -lt 640 || INPUT_HEIGHT -lt 360`
MIN_WIDTH = 640
MIN_HEIGHT = 360


def _parse_input_path(argv: list[str]) -> Path | None:
    """Pull out `--input <path>` without touching anything else.

    Uses parse_known_args so all other flags pass through to bash
    unchanged. Returns None when no --input is present (e.g. a
    `--resume-package-from` invocation), in which case we skip the
    preflight entirely.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input", type=Path)
    known, _unknown = parser.parse_known_args(argv)
    return known.input


def _preflight(input_path: Path) -> None:
    # HLS master playlists are still handled by bash (variant selection
    # lives there); only preflight real container files here.
    if input_path.suffix.lower() == ".m3u8":
        return

    if not os.access(input_path, os.R_OK):
        print(f"Error: cannot read input file: {input_path}", file=sys.stderr)
        sys.exit(1)

    try:
        info = probe(input_path)
    except ProbeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if info.width < MIN_WIDTH or info.height < MIN_HEIGHT:
        print(
            f"Error: input resolution too low ({info.width}x{info.height}); "
            f"minimum is {MIN_WIDTH}x{MIN_HEIGHT}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"[preflight] {input_path.name}: "
        f"{info.width}x{info.height} @ {float(info.fps):.3f} fps, "
        f"{info.duration_s:.1f}s"
        + ("" if info.has_audio else " (no audio)"),
        flush=True,
    )


def main() -> None:
    input_path = _parse_input_path(sys.argv[1:])
    if input_path is not None:
        _preflight(input_path)
    os.execvp(BASH_SCRIPT, [BASH_SCRIPT, *sys.argv[1:]])


if __name__ == "__main__":
    main()
