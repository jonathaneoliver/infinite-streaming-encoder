#!/usr/bin/env python3
"""#184 duration limit: the cache key and the plan-vs-media rule.

Both are things a passing encode does NOT prove. The cache key only misbehaves
across two runs of the same source, and the plan-vs-media rule only fires on a
mezzanine that disagrees with the plan — which is what a mixed-version fleet
produced during development, and what the tolerance change must keep catching.
"""
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder.cli_local_dist import (  # noqa: E402
    _apply_time_limit, _effective_time_limit, _mezz_cache_rel)

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


@dataclass(frozen=True)
class FakeInfo:
    duration_s: float


# --- the cache key -----------------------------------------------------------
# A truncated mezzanine filed under the full clip's key would silently shorten
# every later encode of that source, until the staging GC evicted it.
with mock.patch.object(Path, "stat") as st:
    st.return_value = mock.Mock(st_size=1234, st_mtime_ns=999)
    src = Path("/src/clip.mp4")
    full = _mezz_cache_rel(src)
    full_explicit = _mezz_cache_rel(src, None)
    limited = _mezz_cache_rel(src, 30)
    other = _mezz_cache_rel(src, 60)
    zero = _mezz_cache_rel(src, 0)

check("limited run must not share the unlimited key", full != limited)
check("different limits must not share a key", limited != other)
check("None limit == no limit", full == full_explicit)
# 0 is "no limit", not "a limit of zero" — it must hash as the unlimited case,
# or every mezzanine already in the bucket is orphaned by this change.
check("0 hashes as unlimited", zero == full)

# --- what counts as a limit --------------------------------------------------
info = FakeInfo(duration_s=20.0)
check("real limit is kept", _effective_time_limit(info, 10) == 10.0)
check("limit at clip length is not a limit", _effective_time_limit(info, 20) is None)
check("limit past clip length is not a limit", _effective_time_limit(info, 99) is None)
check("no limit", _effective_time_limit(info, None) is None)
check("zero is no limit", _effective_time_limit(info, 0) is None)
check("negative is no limit", _effective_time_limit(info, -5) is None)

# --- the plan is built against the truncated length --------------------------
check("clamped", _apply_time_limit(info, 10).duration_s == 10.0)
check("unclamped without a limit", _apply_time_limit(info, None).duration_s == 20.0)
# An over-length limit must not stretch the plan past the media.
check("never stretches", _apply_time_limit(info, 99).duration_s == 20.0)

# --- the plan-vs-media rule --------------------------------------------------
# Mirrors cli_phase's check. Under a limit the plan is a deliberate PREFIX: `-t`
# on a stream copy cuts on packet boundaries, so a 10s limit yields ~10.07s of
# media every time. Overshoot is fine (those frames are never encoded); a
# SHORTFALL is still fatal, because chunks would reference media that isn't there.
TOL = 1.0 / 24.0


def accepts(planned, actual, limit):
    if limit > 0:
        return (planned - actual) <= TOL
    return abs(actual - planned) <= TOL


check("limited: packet-boundary overshoot accepted", accepts(10.0, 10.066667, 10))
check("limited: large overshoot accepted (plan is a prefix)", accepts(10.0, 20.0, 10))
check("limited: shortfall still rejected", not accepts(10.0, 9.0, 10))
# The unlimited rule must not have been loosened: this is the case the guard was
# written for, and a mixed-version fleet reproduced it for real.
check("unlimited: drift still rejected both ways", not accepts(10.0, 20.0, 0))
check("unlimited: shortfall still rejected", not accepts(20.0, 10.0, 0))
check("unlimited: sub-frame drift still accepted", accepts(20.0, 20.02, 0))

if failures:
    for f in failures:
        print(f"FAIL: {f}")
    sys.exit(1)
print("ok")
