#!/usr/bin/env python3
"""The periodicity detector has to find a signal we planted, and NOT find one
we didn't. Both halves matter equally: a detector that always says "pulsing"
would have "confirmed" the hypothesis it was built to test.

Every series here is synthetic with a known answer, so these run in
milliseconds and need no encode, no libvmaf and no source clip.

Run: python3 scripts/test_vmaf_periodicity.py
"""
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_vmaf_periodicity import (  # noqa: E402
    autocorr, detrend, diagnose, fold, fundamental, significance)

GOP = 30          # 1s at 30fps
N_CYCLES = 200


def content_drift(i, rng):
    """Slow scene-driven wander plus per-frame noise — the confound that buries
    a small pulse in a raw series."""
    return 12.0 * math.sin(i / 900.0) + rng.gauss(0, 0.8)


def starved_series(amplitude=3.0, seed=1):
    """VBV starvation: spike at the IDR, dip below the mean just after, ramp
    back up across the GOP."""
    rng = random.Random(seed)
    out = []
    for i in range(GOP * N_CYCLES):
        pos = i % GOP
        if pos == 0:
            shape = amplitude          # the I-frame itself looks good
        elif pos < GOP // 4:
            shape = -amplitude         # starved frames repaying the I-frame
        else:
            shape = -amplitude + 1.6 * amplitude * (pos / GOP)  # recovery ramp
        out.append(70.0 + shape + content_drift(i, rng))
    return out


def drift_series(amplitude=3.0, seed=2):
    """Normal GOP behaviour: peak at the IDR, monotonic decay across the GOP."""
    rng = random.Random(seed)
    out = []
    for i in range(GOP * N_CYCLES):
        pos = i % GOP
        shape = amplitude * (1.0 - 2.0 * pos / GOP)   # +A at IDR -> -A at end
        out.append(70.0 + shape + content_drift(i, rng))
    return out


def noise_series(seed=3):
    """No periodic component at all — only content drift and noise."""
    rng = random.Random(seed)
    return [70.0 + content_drift(i, rng) for i in range(GOP * N_CYCLES)]


def analyse(scores):
    resid = detrend(scores, window=4 * GOP | 1)
    max_lag = 3 * GOP
    acf = autocorr(resid, max_lag)
    return resid, acf, fundamental(acf), max_lag


def test_finds_the_planted_period():
    for name, series in (("starved", starved_series()), ("drift", drift_series())):
        _, _, period, _ = analyse(series)
        assert period is not None, f"{name}: found no period at all"
        # Allow the fundamental to land on the exact GOP; a harmonic would be
        # 60 or 90 and is the specific failure `fundamental` exists to avoid.
        assert abs(period - GOP) <= 1, \
            f"{name}: period {period}, want {GOP} (a harmonic means fundamental() is wrong)"
    print("ok  finds the planted period, not a harmonic")


def test_planted_period_is_significant():
    series = starved_series()
    resid, acf, period, max_lag = analyse(series)
    pct = significance(resid, acf[period], max_lag, trials=100)
    assert pct >= 95.0, f"planted pulse scored only {pct:.1f}% against shuffles"
    print(f"ok  planted pulse is significant ({pct:.1f}%)")


def test_pure_noise_is_not_significant():
    """The half that stops this tool from rubber-stamping the hypothesis."""
    resid, acf, period, max_lag = analyse(noise_series())
    if period is None:
        print("ok  pure noise: no period found")
        return
    pct = significance(resid, acf[period], max_lag, trials=100)
    assert pct < 95.0, \
        f"noise reported as significant ({pct:.1f}%) — the detector cries wolf"
    print(f"ok  pure noise is not significant ({pct:.1f}%)")


def test_fold_separates_the_two_mechanisms():
    """The whole reason for folding: both are periodic at the GOP and only the
    SHAPE tells them apart."""
    starved = diagnose(fold(starved_series(), GOP))
    drift = diagnose(fold(drift_series(), GOP))
    assert "STARVATION" in starved, f"starved series diagnosed as: {starved}"
    assert "NORMAL GOP DRIFT" in drift, f"drift series diagnosed as: {drift}"
    print("ok  the fold separates VBV starvation from normal GOP drift")


def test_fold_recovers_the_amplitude():
    """Amplitude has to survive content noise ~4x its size, or the number the
    tool reports is not the pulse."""
    amp = 3.0
    folded = fold(starved_series(amplitude=amp), GOP)
    got = max(folded) - min(folded)
    assert 1.2 * amp <= got <= 3.0 * amp, \
        f"folded amplitude {got:.2f} is not recognisably the planted {2 * amp:.2f}"
    print(f"ok  fold recovers the amplitude through 12-point content drift "
          f"({got:.2f} pts)")


def test_detrend_removes_drift_but_keeps_the_pulse():
    resid = detrend(starved_series(), window=4 * GOP | 1)
    # The 12-point sine must be gone; the 3-point pulse must not be.
    assert max(resid) - min(resid) < 12.0, "detrend left the content drift in"
    folded = fold(resid, GOP)
    assert max(folded) - min(folded) > 2.0, "detrend flattened the pulse too"
    print("ok  detrend removes content drift and keeps the pulse")


def test_a_chunk_period_is_reported_as_the_chunk():
    """If the pulse really is at the chunk boundary, the tool must say so
    rather than forcing everything onto the GOP."""
    rng = random.Random(4)
    chunk = 360
    scores = []
    for i in range(chunk * 12):
        shape = -6.0 if i % chunk < 3 else 0.0   # a seam, not a GOP effect
        scores.append(70.0 + shape + content_drift(i, rng))
    resid = detrend(scores, window=4 * chunk | 1)
    period = fundamental(autocorr(resid, 3 * chunk))
    assert period is not None and abs(period - chunk) <= 2, \
        f"chunk-period pulse reported as {period}, want ~{chunk}"
    print("ok  a chunk-period pulse is reported at the chunk period")


if __name__ == "__main__":
    test_finds_the_planted_period()
    test_planted_period_is_significant()
    test_pure_noise_is_not_significant()
    test_fold_separates_the_two_mechanisms()
    test_fold_recovers_the_amplitude()
    test_detrend_removes_drift_but_keeps_the_pulse()
    test_a_chunk_period_is_reported_as_the_chunk()
    print("PASS")
