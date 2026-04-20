"""VMAF lookup from the CRF bandwidth sweep CSV.

The CSV is produced separately (not in this repo) and maps
(codec, encoding mode, resolution, bitrate_mbps) → VMAF score. The
encoder reads it when the user passes `--vmaf-lookup-csv`; the
only effect is a "VMAF~NN" label in the burn-in overlay.

If the CSV is missing or the (codec, mode, res) key doesn't have a
matching row for the requested bitrate, the caller must tolerate a
None return — burn-in silently omits the VMAF label in that case.

Bash's lookup does linear interpolation on the bitrate axis when an
exact match isn't present; this module matches that behaviour.
"""
from __future__ import annotations

import csv
from pathlib import Path


def lookup_vmaf(
    csv_path: Path | None,
    codec: str,
    encoding_mode: str,
    resolution: str,
    bitrate_kbps: int,
) -> float | None:
    """Return a VMAF estimate, or None if the CSV is absent/unmappable.

    `encoding_mode` is "sw" or "hw". The caller should pass "sw"
    since this image only has software encoders.
    """
    if csv_path is None:
        return None
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        return None

    # AV1 isn't in the characterization CSV in the reference workflow.
    if codec == "av1":
        return None

    target_mbps = bitrate_kbps / 1000.0
    matches: list[tuple[float, float]] = []  # (bitrate_mbps, vmaf)

    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("codec") != codec:
                continue
            if row.get("mode") != encoding_mode:
                continue
            if row.get("resolution") != resolution:
                continue
            try:
                bitrate = float(row["bitrate_mbps"])
                vmaf = float(row["vmaf"])
            except (KeyError, ValueError):
                continue
            matches.append((bitrate, vmaf))

    if not matches:
        return None

    matches.sort()

    # Exact (or closest-above) match wins first; interpolate if between.
    for bitrate, vmaf in matches:
        if abs(bitrate - target_mbps) < 1e-6:
            return vmaf

    below = [m for m in matches if m[0] < target_mbps]
    above = [m for m in matches if m[0] > target_mbps]
    if below and above:
        lo_br, lo_vmaf = below[-1]
        hi_br, hi_vmaf = above[0]
        t = (target_mbps - lo_br) / (hi_br - lo_br)
        return lo_vmaf + t * (hi_vmaf - lo_vmaf)

    # Out of range — fall back to the nearest endpoint.
    if below:
        return below[-1][1]
    return above[0][1]
