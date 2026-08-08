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
# Snapped to whole 6s segments: chunk boundaries land on segments, so a limit
# that isn't a multiple leaves a plan that cannot end where the media does.
info = FakeInfo(duration_s=60.0)
check("already a multiple is unchanged", _effective_time_limit(info, 12) == 12.0)
check("rounds up to nearest segment", _effective_time_limit(info, 10) == 12.0)
check("rounds down to nearest segment", _effective_time_limit(info, 8) == 6.0)
check("half rounds up", _effective_time_limit(info, 9) == 12.0)
check("never snaps to zero", _effective_time_limit(info, 1) == 6.0)
check("idempotent on a snapped value",
      _effective_time_limit(info, _effective_time_limit(info, 10)) == 12.0)

# The clip test is applied to the SNAPPED value, not the request, so the rule is
# "the limit is a whole number of segments, and it must be shorter than the clip".
# On a 20s clip (not itself a segment multiple) a request of 19, 20 or 21 all land
# on the last whole segment, 18 — asking for the full length is what a BLANK field
# is for. A request that snaps up past the clip is dropped, so a limit can never
# describe more media than exists.
short = FakeInfo(duration_s=20.0)
check("snapped down stays a limit", _effective_time_limit(short, 19) == 18.0)
check("request at clip length snaps to the last segment",
      _effective_time_limit(short, 20) == 18.0)
check("request that snaps up past the clip is not a limit",
      _effective_time_limit(short, 21) is None)  # 21 -> 24 > 20
check("limit past clip length is not a limit", _effective_time_limit(short, 99) is None)
# Half-segment requests round UP (floor(x+0.5)), matching Go's math.Round rather
# than Python's banker's round() — which would give 12 here.
check("half rounds away from zero, like Go", _effective_time_limit(info, 15) == 18.0)
check("snapped past the clip is not a limit", _effective_time_limit(short, 22) is None)
check("no limit", _effective_time_limit(short, None) is None)
check("zero is no limit", _effective_time_limit(short, 0) is None)
check("negative is no limit", _effective_time_limit(short, -5) is None)

# --- the plan is built against the truncated length --------------------------
check("clamped", _apply_time_limit(short, 12).duration_s == 12.0)
check("unclamped without a limit", _apply_time_limit(short, None).duration_s == 20.0)
# An over-length limit must not stretch the plan past the media.
check("never stretches", _apply_time_limit(short, 99).duration_s == 20.0)

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
