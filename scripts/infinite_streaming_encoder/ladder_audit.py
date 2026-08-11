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
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from infinite_streaming_encoder.ffprobe import ProbeError, probe
# #109's pre-scaled reference, built for the in-encode audit and reused here.
# Importing rather than reimplementing: the ref must be produced with the SAME
# filter chain measure_vmaf applies, and two copies of that chain drifting apart
# would move every score without anything failing.
from infinite_streaming_encoder.cli_phase import (
    _build_prescaled_ref, _source_slower_than_ref,
)
from infinite_streaming_encoder.vmaf_audit import (
    VmafError, common_dimensions, measure_vmaf, pick_model,
)

# Grading reference heights the curve store recognises. A rung graded against a
# 4K master ("how does this look on a 4K display") scores differently from the
# same rung graded at 1080p ("how clean is its compression"); the two are not
# interchangeable and are stored separately.
REFERENCES = (2160, 1080)


def ffmpeg_version() -> str:
    """The ffmpeg banner line, recorded with every curve point set.

    Scores from different ffmpeg/libvmaf builds are not strictly comparable, and
    this repo has two measurement paths on different builds: the in-encode
    per-chunk audit runs on workers with the image's pinned ffmpeg, while this
    runs natively. Recording which produced a run makes that visible instead of
    something a reader has to know.
    """
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        return out.stdout.splitlines()[0].strip() if out.stdout else "unknown"
    except (OSError, IndexError):
        return "unknown"


class AuditError(RuntimeError):
    """The output can't be audited — missing metadata, or burn-in was on."""


def _label_height(label: str) -> int:
    """`"1080p"` -> 1080, `"1080p_2"` -> 1080. 0 when unreadable.

    The ordinal suffix must be removed BEFORE the trailing "p": a ladder with
    two rungs at one resolution labels them `1080p_1`/`1080p_2`, which don't end
    in "p" at all. Stripping first left "1080p" and raised ValueError, so every
    Apple (non-uniq) ladder — which carries two 432p, two 720p and two 1080p
    rungs — crashed the audit.
    """
    head = label.split("_")[0]
    if head.endswith("p") and head[:-1].isdigit():
        return int(head[:-1])
    return 0


def _rendition_seconds(variant_dir: Path) -> float:
    """Playing time of one rendition, summed from its playlist's EXTINF.

    Read from the playlist rather than the source because the two differ
    whenever the encode carried a time limit — which is the case this exists
    for. 0.0 when unreadable, which the caller treats as "no bound".
    """
    try:
        pl = (variant_dir / "playlist.m3u8").read_text()
    except OSError:
        return 0.0
    return sum(float(m) for m in re.findall(r"#EXTINF:([\d.]+),", pl))


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
    """(codec, profile, [(label, variant_dir)...]) for an output directory.

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
    # The ladder this output was encoded from. Already in encode.json and
    # already parsed here; recording it means a curve point can say WHICH
    # ladder's rung it measured, so points from different profiles are
    # distinguishable rather than merging on (codec, height, kbps) alone.
    profile = meta.get("profile") or ""

    rungs = []
    for d in sorted(output_dir.iterdir()):
        if not d.is_dir() or d.name == "audio":
            continue
        if (d / "playlist.m3u8").is_file():
            rungs.append((d.name, d))
    if not rungs:
        raise AuditError(f"{output_dir.name}: no variant directories with a playlist")
    return codec, profile, rungs


def _safe(fn, *a):
    """(True, value) or (False, error). Keeps one rung's failure from aborting
    the pool — a single unreadable rendition should cost that rung, not the
    other eleven and the hours already spent on them."""
    try:
        return True, fn(*a)
    except VmafError as e:
        return False, e


def audit_output(output_dir: Path, source: Path, reference: int,
                 n_subsample: int = 5, limit_s: float | None = None,
                 clip: str | None = None, progress=print,
                 jobs: int = 1, n_threads: int = 0,
                 prescale: bool = True) -> list[dict]:
    """Measure every rung of `output_dir` against `source`. Returns curve points.

    `jobs` rungs are measured concurrently and `n_threads` is handed to libvmaf.
    Both default to the serial, library-default behaviour.

    Measured on a 10-core box (4 performance), 4K reference, one rung: a single
    libvmaf held ~148% CPU — about 1.5 cores of 10, with the rest idle for the
    ~6 minutes it took. Twelve rungs across six outputs at that rate is seven
    hours of a mostly-idle machine.

    Rungs are independent — each is one subprocess reading the source and one
    rendition — so the parallelism is safe. What it is NOT is free: each 4K
    libvmaf holds ~1.5-2 GB, and each job decodes the 4K source again, so
    `jobs` is bounded by RAM and source-decode bandwidth long before it is
    bounded by cores. Left at 1 by default for that reason.

    Ordering is preserved regardless of completion order: futures are consumed
    in ladder order, so each rung prints as soon as IT is ready and a parallel
    run reads exactly like a serial one — just with gaps where a later rung
    finished first and is waiting its turn to be printed.
    """
    codec, profile, rungs = discover_rungs(output_dir)
    info = probe(source)
    fps = f"{info.fps.numerator}/{info.fps.denominator}" if info.fps else None

    # Both streams are compared at a common resolution. `reference` picks which:
    # 2160 grades against the 4K master, 1080 against a 1080p downscale. The
    # model follows the comparison height, never the source height — the 4K
    # model on <=1080p content saturates near 100 and stops discriminating.
    common_w, common_h = common_dimensions(info.width, info.height, max_h=reference)
    model = pick_model(common_h)

    progress(f"[audit] ffmpeg: {ffmpeg_version()}")
    progress(f"[audit] {output_dir.name} codec={codec} rungs={len(rungs)} "
             f"reference={reference}p common={common_w}x{common_h} model={model}")

    # ---- pre-scaled reference (#109) ------------------------------------
    # Every rung otherwise re-decodes the SOURCE and downscales it to the
    # comparison resolution. With a 4K AV1 master that is the whole cost of the
    # audit: twelve rungs, twelve identical 4K AV1 decodes, and AV1 is rated 2.5x
    # H.264 to decode. Parallelising them just runs the redundant work at once
    # and contends for memory bandwidth.
    #
    # So decode it ONCE into a near-lossless H.264 file at the comparison
    # resolution, and point every rung at that. Same gate as the encode path —
    # skipped when the source is not actually more expensive than the ref would
    # be (an already-1080p H.264 master has nothing to gain) and when there are
    # too few rungs to amortise the build.
    ref_for_rungs = source
    tmpdir = None
    if prescale and len(rungs) >= 2 and _source_slower_than_ref(info, common_w, common_h):
        # Only as long as the renditions: the master here is 334s while the
        # outputs under test are 60s, and libvmaf stops at the shorter stream,
        # so building the full master would spend 5x the time on frames nothing
        # ever compares against.
        span = limit_s or _rendition_seconds(rungs[0][1]) or info.duration_s
        tmpdir = tempfile.mkdtemp(prefix="ladder-audit-ref-")
        ref = Path(tmpdir) / "ref.mp4"
        t0 = time.monotonic()
        progress(f"[audit] building pre-scaled reference "
                 f"{common_w}x{common_h} over {span:.0f}s "
                 f"(source is {info.width}x{info.height} "
                 f"{getattr(info, 'video_codec', '?')})")
        try:
            _build_prescaled_ref(source, ref, common_w, common_h, fps or "30",
                                 crf=8, keyint=120, duration_s=span)
            ref_for_rungs = ref
            progress(f"[audit] reference built in {time.monotonic()-t0:.0f}s "
                     f"({ref.stat().st_size/1e6:.0f} MB) — "
                     f"{len(rungs)} rungs now decode THIS instead of the master")
        except Exception as e:  # noqa: BLE001 - fall back to the master
            progress(f"[audit] pre-scale failed ({e}); using the master directly")

    def one(label, vdir):
        """Measure one rung. Returns (kbps, result) or raises."""
        # The RENDITION's own playing time, which is neither the source's nor
        # --limit-s. The byte total covers the whole rendition, so scoring only
        # a --limit-s window must NOT divide by that window. But the source is
        # not the answer either: a time-limited encode (--time 60) produces a
        # 60s rendition from a 334s master, and dividing its bytes by 334
        # understated every bitrate by 5.57x — the exact ratio — turning a
        # 5287 kbps rung into "949 kbps" on the curve.
        #
        # It fails silently and in the worst possible place: the numbers stay
        # plausible and monotonic, and the curve store is what the Ladders tab
        # uses to estimate quality per rung, so a poisoned curve misinforms
        # every future ladder decision rather than erroring.
        kbps = _delivered_kbps(vdir, _rendition_seconds(vdir) or info.duration_s)
        return kbps, measure_vmaf(
            distorted=vdir / "playlist.m3u8", reference=ref_for_rungs,
            common_w=common_w, common_h=common_h, model=model,
            ref_duration_s=limit_s, dist_duration_s=limit_s,
            n_subsample=n_subsample, fps=fps, n_threads=n_threads)

    # Results are consumed IN LADDER ORDER as each becomes ready, so a rung
    # prints the moment it is done rather than at the end of the run. The first
    # cut collected every result before printing anything, which on a 15-minute
    # run looked identical to a hang — and did so on the serial path too, which
    # had been printing incrementally for its whole life. A long job with no
    # output is indistinguishable from a stuck one, and this job is long by
    # nature.
    if jobs > 1:
        progress(f"[audit] {jobs} rungs at a time"
                 + (f", libvmaf n_threads={n_threads}" if n_threads else ""))
        pool = ThreadPoolExecutor(max_workers=jobs)
        pending = [pool.submit(_safe, one, label, vdir) for label, vdir in rungs]
        results = (f.result() for f in pending)
    else:
        pool = None
        results = (_safe(one, label, vdir) for label, vdir in rungs)

    points = []
    for (label, vdir), (ok, val) in zip(rungs, results):
        if not ok:
            progress(f"[audit]   {label}: FAILED — {val}")
            continue
        kbps, r = val
        height = _label_height(label)
        if height == 0:
            progress(f"[audit]   {label}: skipped — can't read a height from the label")
            continue
        points.append({
            "clip": clip or source.name,
            "codec": codec, "reference": reference, "height": height,
            "kbps": kbps, "vmaf": round(r["mean"], 2),
            "harmonic": round(r["harmonic_mean"], 2),
            "min": round(r["min"], 2), "p1": round(r.get("p1", r["min"]), 2),
            "pct_lt10": round(r.get("pct_lt10", 0.0), 1),
            # PROVENANCE. A VMAF number is only comparable against another
            # measured the same way, and the two things that move it most are
            # the model and the resolution both streams were scaled to. Neither
            # was recorded before, which left a curve file unable to explain
            # itself: a set of reference=1080 points measured on 2026-07-29 sat
            # up to +15 VMAF above the seed at mid rungs with no way to tell
            # from the file what had differed. `profile` names the ladder, so
            # rungs from different ladders stay distinguishable.
            "model": model, "common_w": common_w, "common_h": common_h,
            "profile": profile,
        })
        progress(
            f"[audit]   {label:>6}: {kbps:>6} kbps  vmaf {r['mean']:6.2f}  "
            f"harmonic {r['harmonic_mean']:6.2f}  p1 {r.get('p1', 0):6.2f}  "
            f"worst {r['min']:6.2f} @frame {r.get('min_frame', -1)}  "
            f"<10 {r.get('pct_lt10', 0):5.1f}%  <50 {r.get('pct_lt50', 0):5.1f}%  "
            f"std {r.get('std', 0):5.2f}  ({r['frames']} frames)")
    if pool is not None:
        pool.shutdown()
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return points


def audit_tree(root: Path, source_dir: Path, reference: int,
               n_subsample: int = 5, limit_s: float | None = None,
               jobs: int = 1, n_threads: int = 0, prescale: bool = True,
               clip: str | None = None,
               match: str | None = None, latest: bool = False,
               progress=print) -> list[dict]:
    """Audit every eligible output under `root`, SKIPPING the rest with a reason.

    `match` (optional) restricts the sweep to output dirs whose NAME contains the
    substring — e.g. match='insane_fpv' audits just that one clip's h264/hevc/av1
    outputs (and still auto-matches each to its source + skips burn-in).

    Skipping rather than failing is the point of batch mode: most of a real
    OUTPUT_DIR is ineligible (burn-in on, pre-dating encode.json, source since
    deleted) and one bad directory must not abandon the rest. Naming a single
    output explicitly still errors — there, you meant that one.

    The source is matched by the `source` recorded in encode.json, looked up in
    `source_dir`. An output whose source is gone can't be audited: there is
    nothing to compare against, and quietly substituting another file would
    produce confidently wrong numbers.
    """
    if not root.is_dir():
        raise AuditError(f"not a directory: {root} — note these paths are "
                         "resolved inside the container, not on the host")
    if not source_dir.is_dir():
        raise AuditError(f"not a directory: {source_dir} — note these paths are "
                         "resolved inside the container, not on the host")
    # Gather eligible candidates first (needed so --latest can pick newest-per-codec).
    candidates = []  # (mtime, codec, dir, src)
    skipped = 0
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if match and match not in d.name:
            continue
        try:
            codec, _, _ = discover_rungs(d)
        except AuditError as e:
            skipped += 1
            progress(f"[audit] SKIP {d.name}: {str(e).split(': ', 1)[-1]}")
            continue
        meta = json.loads((d / "encode.json").read_text())
        src_name = meta.get("source") or ""
        src = source_dir / Path(src_name).name if src_name else None
        if not src or not src.is_file():
            skipped += 1
            progress(f"[audit] SKIP {d.name}: source {src_name!r} not found in {source_dir}")
            continue
        candidates.append((d.stat().st_mtime, codec, d, src))

    if latest:
        # Keep only the NEWEST dir per codec — "the last encode" of a source that's
        # been re-encoded many times, without auditing every archived copy. Pair
        # with match=<source stem> to get one clip's current h264/hevc/av1.
        newest: dict = {}
        for mtime, codec, d, src in candidates:
            if codec not in newest or mtime > newest[codec][0]:
                newest[codec] = (mtime, d, src)
        chosen = sorted(((d, src) for _, d, src in newest.values()),
                        key=lambda ds: ds[0].name)
        dropped = len(candidates) - len(chosen)
        if dropped:
            progress(f"[audit] --latest: newest per codec kept, {dropped} older copy(ies) dropped")
    else:
        chosen = [(d, src) for _, _, d, src in candidates]

    points: list[dict] = []
    eligible = 0
    for d, src in chosen:
        try:
            got = audit_output(d, src, reference, n_subsample=n_subsample,
                               jobs=jobs, n_threads=n_threads, prescale=prescale,
                               clip=clip,
                               limit_s=limit_s, progress=progress)
        except (AuditError, ProbeError) as e:
            skipped += 1
            progress(f"[audit] SKIP {d.name}: {e}")
            continue
        points.extend(got)
        eligible += 1
    progress(f"[audit] {eligible} output(s) audited, {skipped} skipped")
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
        # All five. Height and bitrate are BOTH needed — two rungs can share a
        # bitrate at different heights, and two can share a height at different
        # bitrates — and clip because quality-vs-bitrate is content-dependent.
        # A narrower key silently drops real samples.
        return (p.get("clip"), p.get("codec"), p.get("reference"),
                p.get("height"), p.get("kbps"))

    merged = {key(p): p for p in doc.get("points", [])}
    for p in points:
        merged[key(p)] = p
    doc["points"] = sorted(
        merged.values(),
        key=lambda p: (p.get("clip", ""), p["codec"], -p["reference"], p["kbps"]))
    # Top-level clip is the MOST RECENTLY audited content — a display default
    # only. Points carry their own clip, so a store holding several clips is no
    # longer mislabelled as all belonging to the last one.
    doc["clip"] = clip
    doc["ffmpeg"] = ffmpeg_version()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ladder_audit",
        description="Measure a finished output's ladder into VMAF-vs-bitrate "
                    "curve points the Ladders tab uses for design-time estimates.")
    ap.add_argument("--output-dir", type=Path, default=None,
                    help="a single directory under OUTPUT_DIR (must contain encode.json)")
    ap.add_argument("--source", type=Path, default=None,
                    help="the original source the output was encoded from")
    ap.add_argument("--all", type=Path, default=None, metavar="OUTPUT_DIR",
                    help="audit every eligible output under this directory, "
                         "skipping the rest (needs --source-dir)")
    ap.add_argument("--source-dir", type=Path, default=None, dest="source_dir",
                    help="where to find sources for --all (matched by encode.json's source)")
    ap.add_argument("--match", default=None,
                    help="with --all, only audit output dirs whose NAME contains "
                         "this substring — e.g. 'insane_fpv' for one clip's 3 codecs")
    ap.add_argument("--latest", action="store_true",
                    help="with --all, audit only the NEWEST output per codec — the "
                         "last encode, ignoring archived re-encodes. Pair with --match.")
    ap.add_argument("--reference", type=int, default=2160, choices=REFERENCES,
                    help="grading reference height (default 2160)")
    ap.add_argument("--store", type=Path, default=None,
                    help="curve store to merge into (default: print only)")
    # --clip reaches BOTH paths. It used to be passed only to audit_output, so
    # an --all run silently labelled every point with the source FILENAME — and
    # a 60s time-limited audit then landed in the same (clip, codec, reference,
    # height) space as the full-length points from the same file, with nothing
    # in the store able to tell them apart.
    ap.add_argument("--clip", default=None,
                    help="label for the content (default: the source filename)")
    ap.add_argument("--n-subsample", type=int, default=5, dest="n_subsample",
                    help="score every Nth frame (default 5)")
    ap.add_argument("--limit-s", type=float, default=None, dest="limit_s",
                    help="only score the first N seconds — for a quick check")
    ap.add_argument("--prescale", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="decode the source ONCE into a near-lossless file at "
                         "the comparison resolution and score every rung "
                         "against that (#109). On by default; gated off "
                         "automatically when the source is no more expensive "
                         "than the reference would be.")
    ap.add_argument("--jobs", type=int, default=1, metavar="N",
                    help="measure N rungs concurrently (default 1). Rungs are "
                         "independent, but each 4K libvmaf holds ~1.5-2 GB and "
                         "re-decodes the source, so RAM and source-decode "
                         "bandwidth bound this well before core count does.")
    ap.add_argument("--n-threads", type=int, default=0, dest="n_threads",
                    metavar="N",
                    help="threads for libvmaf itself (default: its own). "
                         "Complements --jobs rather than replacing it: one "
                         "measurement was observed using ~1.5 of 10 cores.")
    ap.add_argument("--json", action="store_true", help="emit the points as JSON")
    args = ap.parse_args(argv)

    quiet = args.json
    def progress(msg):
        if not quiet:
            print(msg, flush=True)

    if bool(args.all) == bool(args.output_dir):
        print("error: pass either --output-dir (one output) or --all (a tree), "
              "not both", file=sys.stderr)
        return 2
    if args.all and not args.source_dir:
        print("error: --all needs --source-dir to resolve each output's source",
              file=sys.stderr)
        return 2
    if args.output_dir and not args.source:
        print("error: --output-dir needs --source", file=sys.stderr)
        return 2

    try:
        if args.all:
            points = audit_tree(args.all, args.source_dir, args.reference,
                                jobs=args.jobs, n_threads=args.n_threads,
                                prescale=args.prescale,
                                n_subsample=args.n_subsample, limit_s=args.limit_s,
                                match=args.match, latest=args.latest,
                                clip=args.clip, progress=progress)
        else:
            points = audit_output(args.output_dir, args.source, args.reference,
                                  jobs=args.jobs, n_threads=args.n_threads,
                                  prescale=args.prescale,
                                  n_subsample=args.n_subsample, limit_s=args.limit_s,
                                  clip=args.clip, progress=progress)
    except (AuditError, ProbeError) as e:
        # A missing/unreadable source is a user error like an ineligible output
        # is — report it, don't traceback.
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not points:
        print("error: no rung could be measured", file=sys.stderr)
        return 1

    clip = args.clip or (args.source.name if args.source else "multiple")
    if args.store:
        merge_into_store(args.store, points, clip)
        progress(f"[audit] merged {len(points)} point(s) into {args.store}")
    if args.json:
        print(json.dumps({"clip": clip, "points": points}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
