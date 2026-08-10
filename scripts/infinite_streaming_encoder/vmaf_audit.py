"""Per-rendition VMAF quality audit (#24) — measure REAL perceptual quality of an
encoded rendition against the mezzanine reference, at a common (source) resolution.

Off by default; enabled per-encode via the `measure_vmaf` flag. Designed to run
per-chunk on the same worker that just encoded the chunk, so it fans out across
the fleet like the encode itself (the control plane aggregates the per-chunk
scores into a per-rung number).

Two decisions that make the scores meaningful (see #24):
  1. COMMON comparison resolution — both the rendition and the reference are
     bicubic-scaled to a common res: the SOURCE native res, CAPPED at
     MAX_VMAF_HEIGHT (default 1080p). Below the cap a higher-res rung
     legitimately scores higher (models what a viewer sees on a source-res
     display); at/above the cap, rungs are compared at the cap. The cap exists
     because an uncapped 4K comparison makes each libvmaf ~1.5-2 GB and ~2x
     slower than the encode (#24 field data) — enough to OOM-kill the audit on
     an 8-10 GB Docker VM. Capped it is ~0.5 GB and ~2x faster, so the audit is
     portable across the fleet. Raise VMAF_MAX_HEIGHT on boxes with the RAM for
     true 4K fidelity (the trade-off: rungs above the cap stop being
     differentiated from each other).
  2. Model follows the CAPPED comparison height — vmaf_4k_v0.6.1 only when the
     capped common res is >=1440p, else the default 1080p model.

KNOWN BIASES (experimental — refine later):
  - Burn-in overlay: renditions carry a burn-in the mezzanine lacks, so VMAF
    penalizes those fixed overlay regions — a roughly CONSTANT offset across
    rungs, so absolute scores read a little low but RELATIVE ladder comparisons
    (inversion/cliff/redundant) still hold.
  - Padding: a padded rendition tail has no reference frames; libvmaf simply
    stops at the shorter stream, so those frames aren't scored (negligible).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

# The common resolution both streams are scaled to for the comparison is capped
# at this height: uncapped (4K) it's ~1.5-2 GB / ~2x slower per libvmaf and
# OOM-kills on 8-10 GB Docker VMs; capped to 1080p it's ~0.5 GB and ~2x faster.
# Override per-box (e.g. VMAF_MAX_HEIGHT=2160 on a 32 GB worker) for 4K fidelity.
MAX_VMAF_HEIGHT = int(os.environ.get("VMAF_MAX_HEIGHT", "1080") or "1080")


def pick_model(common_height: int) -> str:
    """VMAF model keyed off the CAPPED comparison height (not the raw source):
    the 4K-trained model only when the common res is >=1440p, else the default
    1080p model (the 4K model on <=1080p content mis-scores)."""
    return "vmaf_4k_v0.6.1" if common_height >= 1440 else "vmaf_v0.6.1"


def common_dimensions(src_w: int, src_h: int,
                      max_h: int = MAX_VMAF_HEIGHT) -> tuple[int, int]:
    """The (even) resolution both streams are bicubic-scaled to for the VMAF
    comparison: the source res, but never taller than `max_h` and never
    UP-scaled past the source. Width tracks the capped height to preserve aspect
    ratio. Even dims because libvmaf runs on yuv420p."""
    if src_h <= max_h:
        return (src_w - (src_w % 2), src_h - (src_h % 2))
    w = round(src_w * max_h / src_h)
    return (w - (w % 2), max_h - (max_h % 2))


class VmafError(RuntimeError):
    pass


def measure_vmaf(distorted: Path, reference: Path, common_w: int, common_h: int,
                 model: str, ref_start_s: float = 0.0,
                 ref_duration_s: float | None = None,
                 n_subsample: int = 5, n_threads: int = 0,
                 fps: str | None = None, n_frames: int | None = None,
                 dist_duration_s: float | None = None,
                 keep_frames: bool = False) -> dict:
    """Score `distorted` against a [ref_start_s, +ref_duration_s) window of
    `reference`, both bicubic-scaled to (common_w x common_h). Returns
    {mean, harmonic_mean, min, frames, inv_sum} — inv_sum = sum(1/frame_vmaf),
    which lets the control plane recombine a correct harmonic mean across chunks
    (chunk harmonic-means can't just be averaged).

    `fps` (e.g. "30000/1001", from the source) forces both streams onto the SAME
    constant frame grid before comparison. libvmaf pairs frames by presentation
    order, so any cadence jitter or PTS discontinuity between the two inputs
    desyncs them and craters the score (VMAF is unforgiving of a 1-frame slip on
    high-motion content). Regenerating a common CFR clock from decode order
    sidesteps that — pass the source fps whenever it's known.

    `keep_frames` adds the raw per-frame score list as `frame_scores`. OFF by
    default, and it must stay that way on the encode path: a rung is thousands
    of floats per chunk, and the telemetry markers exist precisely to keep that
    volume off the queue. This is for OFFLINE callers holding both files, where
    the analysis is a filesystem away — see analyze_vmaf_periodicity.py, which
    needs the series to fold quality against position-in-GOP. Pair it with
    `n_subsample=1`; the default 5 aliases anything faster than ~6 samples per
    second and a 1s GOP at 30fps is exactly that fast.

    `n_frames` clamps BOTH streams to exactly that many frames after the CFR
    re-time. Per-chunk audits seek the reference window with `-ss`/`-t`, whose
    frame count drifts from the chunk's frame-exact length (#90) by ~1 at the
    seam; libvmaf then pairs the leftover frame against the wrong partner and
    logs a spurious ~0-VMAF frame, pinning `min`/harmonic to noise even on a
    clean rung (#108). Trimming both to the chunk's known frame count removes
    that mispair. Omit for whole-clip audits.

    Raises VmafError on ffmpeg failure or an unparseable log.
    """
    log_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    try:
        # Distorted (input 0) is already just this window (the chunk). The
        # reference (input 1) is seeked to the matching window. `fps` re-times
        # both to one CFR grid (decode order); `trim` clamps both to the chunk's
        # exact frame count so no leftover seam frame mispairs; then setpts
        # re-zeros PTS so they align frame-for-frame.
        ref_seek = ["-ss", f"{ref_start_s:.6f}"] if ref_start_s > 0 else []
        ref_dur = ["-t", f"{ref_duration_s:.6f}"] if ref_duration_s else []
        # Trimming only the reference leaves the distorted stream running past
        # the end of it; libvmaf then scores frames with nothing to compare
        # against and the result collapses to noise. A windowed comparison MUST
        # bound both inputs. (Chunk callers pass neither — a chunk's distorted
        # file is already exactly its window.)
        dist_dur = ["-t", f"{dist_duration_s:.6f}"] if dist_duration_s else []
        threads_opt = f":n_threads={n_threads}" if n_threads else ""
        fps_pre = f"fps={fps}," if fps else ""
        trim = f"trim=end_frame={n_frames}," if n_frames else ""
        lavfi = (
            f"[0:v]{fps_pre}scale={common_w}:{common_h}:flags=bicubic,"
            f"format=yuv420p,setsar=1,{trim}setpts=PTS-STARTPTS[dist];"
            f"[1:v]{fps_pre}scale={common_w}:{common_h}:flags=bicubic,"
            f"format=yuv420p,setsar=1,{trim}setpts=PTS-STARTPTS[ref];"
            f"[dist][ref]libvmaf=model=version={model}:n_subsample={n_subsample}"
            f"{threads_opt}:log_fmt=json:log_path={log_path}"
        )
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats",
               *dist_dur, "-i", str(distorted),
               *ref_seek, *ref_dur, "-i", str(reference),
               "-lavfi", lavfi, "-f", "null", "-"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise VmafError(f"libvmaf failed (exit {proc.returncode}): "
                            f"{proc.stderr.strip()[-300:]}")
        return _parse_vmaf_log(Path(log_path), keep_frames=keep_frames)
    finally:
        Path(log_path).unlink(missing_ok=True)


def _parse_vmaf_log(log_path: Path, keep_frames: bool = False) -> dict:
    data = json.loads(log_path.read_text())
    frames = [f.get("metrics", {}).get("vmaf")
              for f in data.get("frames", [])]
    frames = [s for s in frames if isinstance(s, (int, float))]
    if frames:
        n = len(frames)
        inv_sum = sum(1.0 / s for s in frames if s > 0)
        mean = sum(frames) / n
        lo = min(frames)
        srt = sorted(frames)
        # 1st-percentile floor — a robust "hard frames" worst case. A single min
        # frame can be a lone outlier (one bad cut); p1 is what actually drags
        # perceived quality, and it's the standard low-percentile QA metric.
        p1 = srt[min(n - 1, int(n * 0.01))]
        std = (sum((s - mean) ** 2 for s in frames) / n) ** 0.5
        # Fraction of essentially-unwatchable frames (VMAF < 10) and poor frames
        # (< 50) — the "how much of the clip is broken" read that a mean hides.
        pct_lt10 = 100.0 * sum(1 for s in frames if s < 10) / n
        pct_lt50 = 100.0 * sum(1 for s in frames if s < 50) / n
        return {
            "mean": mean,
            "harmonic_mean": (n / inv_sum) if inv_sum else 0.0,
            "min": lo,
            "min_frame": frames.index(lo),  # index of the worst frame (seek to it)
            "p1": p1,
            "max": max(frames),
            "std": std,
            "pct_lt10": pct_lt10,
            "pct_lt50": pct_lt50,
            # Only when asked: the series is the whole point for periodicity
            # analysis and pure weight for everyone else. The default keeps the
            # returned dict identical to what every existing caller reads.
            **({"frame_scores": frames} if keep_frames else {}),
            "frames": n,
            "inv_sum": inv_sum,
        }
    # Fall back to libvmaf's own pooled_metrics if per-frame is absent.
    pooled = data.get("pooled_metrics", {}).get("vmaf", {})
    if not pooled:
        raise VmafError("VMAF log had no frames or pooled_metrics")
    mean = float(pooled.get("mean", 0.0))
    lo = float(pooled.get("min", mean))
    return {
        "mean": mean,
        "harmonic_mean": float(pooled.get("harmonic_mean", mean)),
        "min": lo,
        "min_frame": -1,  # per-frame data absent — no index/percentile available
        "p1": lo,
        "max": float(pooled.get("max", mean)),
        "std": 0.0,
        "pct_lt10": 0.0,  # unknown without per-frame data
        "pct_lt50": 0.0,
        "frames": 0,
        "inv_sum": 0.0,
    }


def vmaf_marker(codec: str, label: str, height: int, chunk_index: int,
                r: dict, model: str = "", common_h: int = 0) -> str:
    """The [[ENCODER-VMAF ...]] marker the control plane scans and aggregates
    per (codec, label). One per chunk (chunk=-1 for a whole-variant encode).
    inv_sum + frames let the control plane recombine mean/harmonic/min correctly.

    `model` (vmaf_v0.6.1 | vmaf_4k_v0.6.1) and `common_h` (the resolution both
    streams were scaled to for the comparison) are the score's PROVENANCE (#117):
    a VMAF number is only interpretable against the model + comparison height that
    produced it, and scores from different sources sit on different scales and must
    never be pooled. They trail the record so an older control plane (regex without
    them) still matches — its optional-group parser reads them as unknown."""
    tail = ""
    if model or common_h:
        tail = f" model={model} common_h={common_h}"
    return (f"[[ENCODER-VMAF codec={codec} label={label} height={height} "
            f"chunk={chunk_index} mean={r['mean']:.4f} "
            f"harmonic={r['harmonic_mean']:.4f} min={r['min']:.4f} "
            f"frames={r['frames']} inv_sum={r['inv_sum']:.6f}{tail}]]")
