"""Measure a finished output's ladder and write VMAF-vs-bitrate curve points.

This is the repeatable form of the one-off analysis in `docs/vmaf-audit/` — the
reports that produced the built-in seed curves in `internal/encode/quality_curve.go`.
Running it on your own content replaces those estimates with measurements of
material you actually care about, without a rebuild: the control plane overlays
`$TMP_DIR/quality-curves.json` on the built-in seed (see `LoadCurveStore`).

What it measures, and what it does NOT:

  - It scores each RUNG of an already-encoded output against the original
    source. It does not encode anything, so it can be run repeatedly and
    cheaply over outputs you already have.
  - The result is a rate-quality CURVE for a codec — the input to "is every
    rung of this ladder earning its place?". It is not a per-encode quality
    score, and nothing here should be presented as one.

Burn-in makes an output ineligible. The overlay is drawn on the rendition but
not on the source, so libvmaf scores it as distortion — and because a low rung
encoded that overlay with fewer pixels, the penalty GROWS as the rung shrinks,
biasing exactly the rung-to-rung comparison the curve exists to make. Audit
only outputs encoded with the overlay off (`encode.json` `burnin: false`).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from infinite_streaming_encoder.ffprobe import ProbeError, probe
from infinite_streaming_encoder.vmaf_audit import (
    VmafError, common_dimensions, measure_vmaf, pick_model,
)

# Grading reference heights the curve store recognises. A rung graded against a
# 4K master ("how does this look on a 4K display") scores differently from the
# same rung graded at 1080p ("how clean is its compression"); the two are not
# interchangeable and are stored separately.
REFERENCES = (2160, 1080)


class AuditError(RuntimeError):
    """The output can't be audited — missing metadata, or burn-in was on."""


def _delivered_kbps(variant_dir: Path, duration_s: float) -> int:
    """Delivered bitrate = total segment bytes x 8 / duration.

    The TRUE average, not the ladder's target or the manifest's declared one —
    a curve built on targets would describe the ladder we asked for rather than
    the one we got.
    """
    if duration_s <= 0:
        return 0
    total = sum(f.stat().st_size for f in variant_dir.glob("*.m4s"))
    total += sum(f.stat().st_size for f in variant_dir.glob("init.mp4"))
    return int(total * 8 / duration_s / 1000)


def discover_rungs(output_dir: Path) -> tuple[str, list[tuple[str, Path]]]:
    """(codec, [(label, variant_dir)...]) for an output directory.

    A rung is any subdirectory holding a playlist.m3u8, except `audio`.
    """
    meta_path = output_dir / "encode.json"
    if not meta_path.is_file():
        raise AuditError(f"{output_dir.name}: no encode.json — can't tell which "
                         "codec or profile produced this output")
    meta = json.loads(meta_path.read_text())
    if meta.get("burnin") is not False:
        raise AuditError(
            f"{output_dir.name}: encoded WITH the burn-in overlay (or the flag "
            "wasn't recorded). The overlay biases VMAF more at low rungs than "
            "high ones, which corrupts the rung-to-rung comparison this curve "
            "is for. Re-encode with burn-in off to audit it.")
    codec = meta.get("codec") or ""
    if not codec:
        raise AuditError(f"{output_dir.name}: encode.json has no codec")

    rungs = []
    for d in sorted(output_dir.iterdir()):
        if not d.is_dir() or d.name == "audio":
            continue
        if (d / "playlist.m3u8").is_file():
            rungs.append((d.name, d))
    if not rungs:
        raise AuditError(f"{output_dir.name}: no variant directories with a playlist")
    return codec, rungs


def audit_output(output_dir: Path, source: Path, reference: int,
                 n_subsample: int = 5, limit_s: float | None = None,
                 progress=print) -> list[dict]:
    """Measure every rung of `output_dir` against `source`. Returns curve points."""
    codec, rungs = discover_rungs(output_dir)
    info = probe(source)
    fps = f"{info.fps.numerator}/{info.fps.denominator}" if info.fps else None

    # Both streams are compared at a common resolution. `reference` picks which:
    # 2160 grades against the 4K master, 1080 against a 1080p downscale. The
    # model follows the comparison height, never the source height — the 4K
    # model on <=1080p content saturates near 100 and stops discriminating.
    common_w, common_h = common_dimensions(info.width, info.height, max_h=reference)
    model = pick_model(common_h)

    progress(f"[audit] {output_dir.name} codec={codec} rungs={len(rungs)} "
             f"reference={reference}p common={common_w}x{common_h} model={model}")

    points = []
    for label, vdir in rungs:
        # ALWAYS the full content duration: the byte total covers the whole
        # rendition, so dividing by a shortened --limit-s window would inflate
        # the bitrate by the ratio between them.
        kbps = _delivered_kbps(vdir, info.duration_s)
        try:
            r = measure_vmaf(
                distorted=vdir / "playlist.m3u8", reference=source,
                common_w=common_w, common_h=common_h, model=model,
                ref_duration_s=limit_s, dist_duration_s=limit_s,
                n_subsample=n_subsample, fps=fps)
        except VmafError as e:
            progress(f"[audit]   {label}: FAILED — {e}")
            continue
        height = int(label.rstrip("p").split("_")[0]) if label[0].isdigit() else 0
        points.append({
            "codec": codec, "reference": reference, "height": height,
            "kbps": kbps, "vmaf": round(r["mean"], 2),
            "harmonic": round(r["harmonic_mean"], 2),
        })
        progress(f"[audit]   {label}: {kbps} kbps  vmaf {r['mean']:.2f} "
                 f"(harmonic {r['harmonic_mean']:.2f}, {r['frames']} frames)")
    return points


def merge_into_store(store_path: Path, points: list[dict], clip: str) -> dict:
    """Merge points into the curve store, replacing same-(codec,reference,height).

    Mirrors the Go-side overlay so a re-audit updates a rung rather than
    appending a duplicate the interpolator would then average over.
    """
    doc = {"clip": clip, "points": []}
    if store_path.is_file():
        try:
            existing = json.loads(store_path.read_text())
            if isinstance(existing.get("points"), list):
                doc = existing
        except (json.JSONDecodeError, OSError):
            pass  # unreadable store: start fresh rather than refuse to record

    def key(p):
        return (p.get("codec"), p.get("reference"), p.get("height"))

    merged = {key(p): p for p in doc.get("points", [])}
    for p in points:
        merged[key(p)] = p
    doc["points"] = sorted(merged.values(),
                           key=lambda p: (p["codec"], -p["reference"], p["kbps"]))
    doc["clip"] = clip
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ladder_audit",
        description="Measure a finished output's ladder into VMAF-vs-bitrate "
                    "curve points the Ladders tab uses for design-time estimates.")
    ap.add_argument("--output-dir", required=True, type=Path,
                    help="a directory under OUTPUT_DIR (must contain encode.json)")
    ap.add_argument("--source", required=True, type=Path,
                    help="the original source the output was encoded from")
    ap.add_argument("--reference", type=int, default=2160, choices=REFERENCES,
                    help="grading reference height (default 2160)")
    ap.add_argument("--store", type=Path, default=None,
                    help="curve store to merge into (default: print only)")
    ap.add_argument("--clip", default=None,
                    help="label for the content (default: the source filename)")
    ap.add_argument("--n-subsample", type=int, default=5, dest="n_subsample",
                    help="score every Nth frame (default 5)")
    ap.add_argument("--limit-s", type=float, default=None, dest="limit_s",
                    help="only score the first N seconds — for a quick check")
    ap.add_argument("--json", action="store_true", help="emit the points as JSON")
    args = ap.parse_args(argv)

    quiet = args.json
    def progress(msg):
        if not quiet:
            print(msg, flush=True)

    try:
        points = audit_output(args.output_dir, args.source, args.reference,
                              n_subsample=args.n_subsample, limit_s=args.limit_s,
                              progress=progress)
    except (AuditError, ProbeError) as e:
        # A missing/unreadable source is a user error like an ineligible output
        # is — report it, don't traceback.
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not points:
        print("error: no rung could be measured", file=sys.stderr)
        return 1

    clip = args.clip or args.source.name
    if args.store:
        merge_into_store(args.store, points, clip)
        progress(f"[audit] merged {len(points)} point(s) into {args.store}")
    if args.json:
        print(json.dumps({"clip": clip, "points": points}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
