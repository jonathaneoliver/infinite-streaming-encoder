"""Per-rendition VMAF quality audit (#24) — measure REAL perceptual quality of an
encoded rendition against the mezzanine reference, at a common (source) resolution.

Off by default; enabled per-encode via the `measure_vmaf` flag. Designed to run
per-chunk on the same worker that just encoded the chunk, so it fans out across
the fleet like the encode itself (the control plane aggregates the per-chunk
scores into a per-rung number).

Two decisions that make the scores meaningful (see #24):
  1. COMMON comparison resolution — both the rendition and the reference are
     bicubic-scaled to the SOURCE native res, so a higher-res rung legitimately
     scores higher (this models what a viewer sees on a source-res display).
  2. SOURCE-driven model — vmaf_4k_v0.6.1 for >=1440p sources (the 1080p model
     saturates above 1080p), else the default 1080p model.

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
import subprocess
import tempfile
from pathlib import Path


def pick_model(source_height: int) -> str:
    """VMAF model keyed off the SOURCE height (globally, not per-rung): the
    4K-trained model for >=1440p sources, else the default 1080p model."""
    return "vmaf_4k_v0.6.1" if source_height >= 1440 else "vmaf_v0.6.1"


class VmafError(RuntimeError):
    pass


def measure_vmaf(distorted: Path, reference: Path, common_w: int, common_h: int,
                 model: str, ref_start_s: float = 0.0,
                 ref_duration_s: float | None = None,
                 n_subsample: int = 5, n_threads: int = 0) -> dict:
    """Score `distorted` against a [ref_start_s, +ref_duration_s) window of
    `reference`, both bicubic-scaled to (common_w x common_h). Returns
    {mean, harmonic_mean, min, frames, inv_sum} — inv_sum = sum(1/frame_vmaf),
    which lets the control plane recombine a correct harmonic mean across chunks
    (chunk harmonic-means can't just be averaged).

    Raises VmafError on ffmpeg failure or an unparseable log.
    """
    log_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    try:
        # Distorted (input 0) is already just this window (the chunk). The
        # reference (input 1) is seeked to the matching window; setpts on both
        # re-zeros PTS so they align frame-for-frame.
        ref_seek = ["-ss", f"{ref_start_s:.6f}"] if ref_start_s > 0 else []
        ref_dur = ["-t", f"{ref_duration_s:.6f}"] if ref_duration_s else []
        threads_opt = f":n_threads={n_threads}" if n_threads else ""
        lavfi = (
            f"[0:v]scale={common_w}:{common_h}:flags=bicubic,format=yuv420p,"
            f"setsar=1,setpts=PTS-STARTPTS[dist];"
            f"[1:v]scale={common_w}:{common_h}:flags=bicubic,format=yuv420p,"
            f"setsar=1,setpts=PTS-STARTPTS[ref];"
            f"[dist][ref]libvmaf=model=version={model}:n_subsample={n_subsample}"
            f"{threads_opt}:log_fmt=json:log_path={log_path}"
        )
        cmd = ["ffmpeg", "-hide_banner", "-nostats",
               "-i", str(distorted),
               *ref_seek, *ref_dur, "-i", str(reference),
               "-lavfi", lavfi, "-f", "null", "-"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise VmafError(f"libvmaf failed (exit {proc.returncode}): "
                            f"{proc.stderr.strip()[-300:]}")
        return _parse_vmaf_log(Path(log_path))
    finally:
        Path(log_path).unlink(missing_ok=True)


def _parse_vmaf_log(log_path: Path) -> dict:
    data = json.loads(log_path.read_text())
    frames = [f.get("metrics", {}).get("vmaf")
              for f in data.get("frames", [])]
    frames = [s for s in frames if isinstance(s, (int, float))]
    if frames:
        n = len(frames)
        inv_sum = sum(1.0 / s for s in frames if s > 0)
        return {
            "mean": sum(frames) / n,
            "harmonic_mean": (n / inv_sum) if inv_sum else 0.0,
            "min": min(frames),
            "frames": n,
            "inv_sum": inv_sum,
        }
    # Fall back to libvmaf's own pooled_metrics if per-frame is absent.
    pooled = data.get("pooled_metrics", {}).get("vmaf", {})
    if not pooled:
        raise VmafError("VMAF log had no frames or pooled_metrics")
    mean = float(pooled.get("mean", 0.0))
    return {
        "mean": mean,
        "harmonic_mean": float(pooled.get("harmonic_mean", mean)),
        "min": float(pooled.get("min", mean)),
        "frames": 0,
        "inv_sum": 0.0,
    }


def vmaf_marker(codec: str, label: str, height: int, chunk_index: int,
                r: dict) -> str:
    """The [[ENCODER-VMAF ...]] marker the control plane scans and aggregates
    per (codec, label). One per chunk (chunk=-1 for a whole-variant encode).
    inv_sum + frames let the control plane recombine mean/harmonic/min correctly.
    """
    return (f"[[ENCODER-VMAF codec={codec} label={label} height={height} "
            f"chunk={chunk_index} mean={r['mean']:.4f} "
            f"harmonic={r['harmonic_mean']:.4f} min={r['min']:.4f} "
            f"frames={r['frames']} inv_sum={r['inv_sum']:.6f}]]")
