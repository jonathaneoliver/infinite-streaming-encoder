#!/usr/bin/env python3
"""Does a rendition's quality PULSE, at what period, and why?

Measures per-frame VMAF and answers three questions a pooled score cannot:

  1. Is there a periodic component at all, or is the variation just content?
  2. What is its period — and does that period match the GOP, the segment, or
     the chunk? Those are far apart, so the period NAMES the cause.
  3. What SHAPE does quality take within one period? This is the part that
     distinguishes the two mechanisms, which are both periodic at the GOP and
     look identical to any "is it periodic" test:

       normal GOP drift   I-frame is coded at low QP and everything after
                          predicts from a fresh reference, so quality PEAKS at
                          the IDR and decays across the GOP. Present in every
                          encode. Not a defect.

       VBV starvation     the I-frame eats a large share of the budget; with a
                          tight buffer the encoder repays immediately, so the
                          frames right AFTER the IDR are starved and quality
                          dips BELOW the GOP mean before recovering.

    Same period, opposite slope out of the IDR. Only the fold separates them.

Usage:
    python3 scripts/analyze_vmaf_periodicity.py \\
        --distorted OUT/h264_234p.mp4 --reference source.mp4 \\
        --fps 30000/1001 --gop-frames 30 [--chunk-frames 360] [--segment-frames 180]

    # or re-analyse a saved series without re-measuring:
    python3 scripts/analyze_vmaf_periodicity.py --scores scores.json --gop-frames 30

Writes the per-frame series with --save-scores so a slow measurement is done
once. stdlib only (the image has no numpy) — the autocorrelation is O(n*maxlag),
about a second for a 10-minute clip.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


# ---------------------------------------------------------------------------
# detrend
# ---------------------------------------------------------------------------

def detrend(scores: list[float], window: int) -> list[float]:
    """Subtract a centred moving average.

    Raw VMAF swings tens of points with scene complexity, which buries a
    one-point pulse. Everything below operates on the residual. The window must
    be several times the longest period under test or it eats the signal it is
    meant to expose.
    """
    n = len(scores)
    if window < 3 or window >= n:
        mean = sum(scores) / n
        return [s - mean for s in scores]
    half = window // 2
    # Running sum, so this stays O(n) rather than O(n*window).
    out, acc = [], sum(scores[:min(window, n)])
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        if i == 0:
            acc = sum(scores[lo:hi])
        else:
            plo, phi = max(0, i - 1 - half), min(n, i + half)
            if hi > phi:
                acc += scores[hi - 1]
            if lo > plo:
                acc -= scores[plo]
        out.append(scores[i] - acc / (hi - lo))
    return out


# ---------------------------------------------------------------------------
# periodicity
# ---------------------------------------------------------------------------

def autocorr(x: list[float], max_lag: int) -> list[float]:
    """Normalised autocorrelation for lags 0..max_lag."""
    n = len(x)
    mean = sum(x) / n
    dev = [v - mean for v in x]
    denom = sum(d * d for d in dev)
    if denom <= 0:
        return [0.0] * (max_lag + 1)
    out = []
    for lag in range(max_lag + 1):
        s = 0.0
        for i in range(n - lag):
            s += dev[i] * dev[i + lag]
        out.append(s / denom)
    return out


def fundamental(acf: list[float], min_lag: int = 2, tol: int = 2) -> int | None:
    """The period, reduced from the strongest peak to its fundamental.

    Reporting the tallest ACF peak alone is wrong — a 1s pulse peaks at 1s, 2s,
    3s… and the 2s harmonic can come out taller. But scanning upward for the
    "smallest lag whose harmonics also peak" is worse, and this is the version
    that failed: a narrow spike train (a 3-frame seam every 360) puts enough
    structure at small lags that a noisy lag 8 won, reporting 8 instead of 360.

    So: anchor on the STRONGEST peak, which is reliably a true period or one of
    its harmonics, then reduce — accept a smaller lag only if the strong peak is
    a multiple of it AND that smaller lag is itself strongly correlated. A
    genuine fundamental always satisfies both; a noise lag satisfies neither.
    """
    peaks = [l for l in range(min_lag, len(acf) - 1)
             if acf[l] > acf[l - 1] and acf[l] >= acf[l + 1] and acf[l] > 0]
    if not peaks:
        return None
    peakset = set(peaks)

    def is_peak(lag: int) -> bool:
        return any(abs(lag - p) <= tol for p in peakset)

    def prominence(l: int) -> float:
        """Height above the troughs either side.

        Ranking by raw acf[l] does not work: near lag 0 the ACF of any smooth
        signal is high, so a small bump riding that slope outranks a real
        period further out. On a real chunk-seam series acf[360] was +0.52 and
        the function still returned 8, because acf[8] was higher in absolute
        terms while being a ripple on a hill. Prominence measures the thing
        that actually distinguishes them.
        """
        lo = l
        while lo > 1 and acf[lo - 1] <= acf[lo]:
            lo -= 1
        hi = l
        while hi < len(acf) - 1 and acf[hi + 1] <= acf[hi]:
            hi += 1
        return acf[l] - max(acf[lo], acf[hi])

    strongest = max(peaks, key=prominence)
    # The floor is on PROMINENCE, not raw acf, for the same reason the selection
    # is. Testing raw acf here re-admits exactly what prominence just excluded:
    # on a residual with leftover low-frequency content, lag 8 sat at acf 0.64
    # against the true period's 0.40, divided 360 exactly, and had a peak at 16
    # — so it satisfied every reduction test and won. Its prominence was 0.010
    # against 0.104.
    floor = 0.5 * prominence(strongest)
    for cand in peaks:
        if cand >= strongest or prominence(cand) < floor:
            continue
        k = round(strongest / cand)
        if k < 2 or abs(strongest - k * cand) > tol:
            continue
        # THE discriminating condition. Near lag 0 the ACF of any smooth signal
        # is high simply because neighbouring samples correlate, so a floor test
        # alone accepts lag 2 and reports a 1s pulse as a 0.07s one (it did).
        # A real fundamental RECURS: its second harmonic is a peak too. A point
        # on a monotonic decay has no peak at twice its lag.
        if 2 * cand < len(acf) and is_peak(2 * cand):
            return cand
    return strongest


def significance(x: list[float], observed: float, max_lag: int,
                 trials: int = 200, seed: int = 12345) -> float:
    """Percentile of `observed` against the max ACF of shuffled copies.

    A periodic-looking ACF peak means nothing without a null: short series
    produce impressive-looking peaks from noise alone. Shuffling destroys any
    time structure while keeping the value distribution exactly, so the
    comparison is assumption-free.
    """
    rng = random.Random(seed)
    y = list(x)
    beaten = 0
    for _ in range(trials):
        rng.shuffle(y)
        if max(autocorr(y, max_lag)[2:], default=0.0) >= observed:
            beaten += 1
    return 100.0 * (1.0 - beaten / trials)


# ---------------------------------------------------------------------------
# the fold — the part that names the mechanism
# ---------------------------------------------------------------------------

def fold(scores: list[float], period: int) -> list[float]:
    """Mean score at each position 0..period-1, averaged over every cycle.

    Averaging over cycles is what makes a 1-point pulse visible under content
    noise that is tens of points: the pulse is phase-locked to the period and
    the content is not, so the content averages away and the pulse does not.
    """
    if period < 2:
        return []
    buckets: list[list[float]] = [[] for _ in range(period)]
    for i, s in enumerate(scores):
        buckets[i % period].append(s)
    return [statistics.fmean(b) if b else 0.0 for b in buckets]


def diagnose(folded: list[float]) -> str:
    """Read the mechanism off the folded curve's shape out of the IDR."""
    if len(folded) < 6:
        return "period too short to read a shape"
    mean = statistics.fmean(folded)
    idr = folded[0]
    after = statistics.fmean(folded[1:max(2, len(folded) // 6)])   # first ~sixth
    late = statistics.fmean(folded[-max(2, len(folded) // 6):])    # last ~sixth
    if idr > mean and after < mean and late > after:
        return ("VBV STARVATION — quality spikes at the IDR, drops BELOW the "
                "cycle mean immediately after, then recovers. The frames after "
                "each keyframe are paying for it. Try a larger bufsize_multiplier.")
    if idr > mean and after > mean and late < after:
        return ("NORMAL GOP DRIFT — quality peaks at the IDR and decays across "
                "the GOP as prediction drift accumulates. Expected in every "
                "encode; not the pulsing you are chasing.")
    if idr < mean:
        return ("IDR ITSELF IS THE LOW POINT — unusual. Suspect a keyframe "
                "placement or reference mismatch rather than rate control.")
    return "no clear shape — amplitude may be below the noise floor"


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------

def measure(distorted: Path, reference: Path, fps: str | None) -> list[float]:
    from infinite_streaming_encoder.ffprobe import probe
    from infinite_streaming_encoder.vmaf_audit import (
        common_dimensions, measure_vmaf, pick_model)
    info = probe(reference)
    cw, ch = common_dimensions(info.width, info.height)
    # n_subsample=1 is mandatory here: the default 5 gives ~6 samples per second
    # and a 1s GOP at 30fps needs every frame to resolve the recovery ramp.
    r = measure_vmaf(distorted, reference, cw, ch, pick_model(ch),
                     n_subsample=1, fps=fps or str(info.fps), keep_frames=True)
    scores = r.get("frame_scores") or []
    if not scores:
        raise SystemExit("libvmaf returned no per-frame scores")
    print(f"measured {len(scores)} frames  mean={r['mean']:.2f}  "
          f"min={r['min']:.2f}  std={r['std']:.2f}")
    return scores


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="analyze_vmaf_periodicity",
        description="Detect and diagnose periodic quality pulsing from per-frame VMAF.")
    ap.add_argument("--distorted", type=Path)
    ap.add_argument("--reference", type=Path)
    ap.add_argument("--scores", type=Path,
                    help="re-analyse a previously saved series instead of measuring")
    ap.add_argument("--save-scores", type=Path)
    ap.add_argument("--fps", default=None, help='e.g. "30000/1001"')
    ap.add_argument("--gop-frames", type=int, required=True,
                    help="keyint — fps x gop_duration_s")
    ap.add_argument("--segment-frames", type=int, default=0)
    ap.add_argument("--chunk-frames", type=int, default=0)
    ap.add_argument("--max-lag", type=int, default=0,
                    help="default: 3x the largest candidate period")
    ap.add_argument("--trials", type=int, default=200,
                    help="shuffle trials for the significance test (0 skips)")
    args = ap.parse_args()

    if args.scores:
        scores = json.loads(args.scores.read_text())
    elif args.distorted and args.reference:
        scores = measure(args.distorted, args.reference, args.fps)
    else:
        ap.error("need --scores, or both --distorted and --reference")

    if args.save_scores:
        args.save_scores.write_text(json.dumps(scores))
        print(f"saved series -> {args.save_scores}")

    candidates = {"GOP": args.gop_frames}
    if args.segment_frames:
        candidates["segment"] = args.segment_frames
    if args.chunk_frames:
        candidates["chunk"] = args.chunk_frames
    max_lag = args.max_lag or min(len(scores) // 3, 3 * max(candidates.values()))
    if max_lag < 4:
        raise SystemExit("series too short for this period")

    # Detrend over several times the longest candidate, so the moving average
    # removes content drift without swallowing the pulse.
    resid = detrend(scores, window=4 * max(candidates.values()) | 1)

    acf = autocorr(resid, max_lag)
    period = fundamental(acf)

    print(f"\nframes {len(scores)}   max lag {max_lag}")
    print("\nautocorrelation at the known candidate periods:")
    for name, p in sorted(candidates.items(), key=lambda kv: kv[1]):
        val = acf[p] if p < len(acf) else float("nan")
        print(f"  {name:<8} {p:>5} frames   acf={val:+.3f}")

    if period is None:
        print("\nno periodic component found")
        return 0

    label = next((n for n, p in candidates.items() if abs(p - period) <= 1), None)
    print(f"\nfundamental period: {period} frames"
          + (f"  == the {label}" if label else "  (matches no known candidate)"))

    if args.trials:
        pct = significance(resid, acf[period], max_lag, trials=args.trials)
        print(f"significance: stronger than {pct:.1f}% of shuffled series"
              f" ({args.trials} trials)")
        if pct < 95:
            print("  -> NOT significant; treat the shape below as noise")

    folded = fold(scores, period)
    lo, hi = min(folded), max(folded)
    print(f"\nfolded over {len(scores) // period} cycles — "
          f"amplitude {hi - lo:.2f} VMAF points (peak {hi:.2f}, trough {lo:.2f})")
    span = hi - lo or 1.0
    for i, v in enumerate(folded):
        if len(folded) > 40 and i % (len(folded) // 40) and i != len(folded) - 1:
            continue
        bar = "#" * int(round(40 * (v - lo) / span))
        print(f"  +{i:<4} {v:7.2f} |{bar}")

    print(f"\n{diagnose(folded)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
