#!/usr/bin/env python3
"""The MINIMAL burn-in overlay: one static rung label, and the seams it rides on.

Light mode exists so an encode whose picture is being judged carries as few
drawn pixels as possible while still naming its own rung. Three things have to
hold, and each fails silently:

1. LIGHT really is one label. A stray timecode layer re-renders every frame and
   costs bits continuously — precisely what this mode exists to avoid — and
   nothing downstream would notice, because the encode still succeeds and the
   overlay still looks plausible.
2. An UNKNOWN mode is FULL, on both sides. That is the degradation a worker on
   older code gets from BURNIN=light, and the safe direction: an unasked-for
   legible overlay beats an encode missing the label it is identified by.
3. The mode reaches cli_phase from BOTH transports — the local --burnin-mode
   flag and the cloud's BURNIN env — since they are different code paths that
   arrive at the same EncodeContext field.
"""
import argparse
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder.burnin import (  # noqa: E402
    MODE_FULL, MODE_LIGHT, BurninContext, build_filter, light_label,
)
from infinite_streaming_encoder.ladder import Rung  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


RUNG = Rung(
    label="1080p", res_name="1080p", width=1920, height=1080, bitrate=5800,
    preset="medium", fontsize_tc=48, fontsize_label=36,
    burnin_x=40, burnin_y_tc=40, burnin_y_label=100,
)


def ctx(**kw) -> BurninContext:
    base = dict(
        codec="hevc", tier=RUNG, fps=Fraction(25, 1),
        rate_label="AVG~5.80Mbps / PEAK<=7.25Mbps", encoder_label="SW",
        content_duration_s=60.0, padding_duration_s=0.0, vmaf_label="VMAF~93",
    )
    base.update(kw)
    return BurninContext(**base)


# --- 1. light draws exactly one label, and it is the rung's -----------------
light = build_filter(ctx(), burnin=True, mode=MODE_LIGHT)
full = build_filter(ctx(), burnin=True, mode=MODE_FULL)
off = build_filter(ctx(), burnin=False)

check(light.count("drawtext") == 1,
      f"light mode must draw exactly ONE label, got {light.count('drawtext')}: {light}")
check("timecode=" not in light,
      "light mode must not draw the timecode layer — it re-renders every frame")
check("JEO_1080p_5800k" in light,
      f"light label must be JEO_<rung>_<kbps>k, got: {light}")
check(light_label(ctx()) == "JEO_1080p_5800k",
      f"light_label: got {light_label(ctx())}")
check("VMAF~93" not in light, "light mode must not draw the VMAF-estimate row")
check(light.startswith("scale=1920:1080,"),
      f"light mode must keep the scale filter first: {light}")

# The full overlay is unchanged by this work: 5 labels + the VMAF row.
check(full.count("drawtext") == 6,
      f"full mode should draw 6 labels (5 + vmaf), got {full.count('drawtext')}")
check("drawtext" not in off, "burnin=False must draw nothing")

# A duplicate-resolution ladder still gets distinguishable labels, because the
# rung's LABEL carries the suffix while res_name does not.
dup = Rung(label="540p_2", res_name="540p", width=960, height=540, bitrate=1600,
           preset="medium", fontsize_tc=32, fontsize_label=24,
           burnin_x=20, burnin_y_tc=20, burnin_y_label=60)
check(light_label(ctx(tier=dup)) == "JEO_540p_2_1600k",
      f"dup-resolution rung label: got {light_label(ctx(tier=dup))}")

# --- 2. unknown mode is FULL ------------------------------------------------
for unknown in ("", "FULL", "minimal", "lite", "tiny"):
    got = build_filter(ctx(), burnin=True, mode=unknown)
    check(got == full,
          f"mode={unknown!r} must fall back to the FULL overlay, got {got.count('drawtext')} labels")

# LIGHT is case-sensitively spelled by its constant; the CALLERS lowercase.
check((MODE_FULL, MODE_LIGHT) == ("full", "light"),
      f"mode spellings are a cross-language contract with Go: {MODE_FULL}/{MODE_LIGHT}")

# --- 3. the PADDING label survives light mode -------------------------------
padded = build_filter(ctx(padding_duration_s=2.0), burnin=True, mode=MODE_LIGHT)
check(padded.count("drawtext") == 2,
      f"light + padding = rung label + PADDING, got {padded.count('drawtext')}")
check("PADDING" in padded and "enable='gte(t,60.0)'" in padded,
      f"PADDING label must stay enable-gated onto padded frames: {padded}")
check("tpad=" in padded, "tpad geometry must survive light mode")

# --- 4. both transports reach cli_phase -------------------------------------
import os  # noqa: E402

from infinite_streaming_encoder.cli_phase import _burnin_mode  # noqa: E402

os.environ.pop("BURNIN", None)
check(_burnin_mode(argparse.Namespace(burnin_mode="light")) == MODE_LIGHT,
      "--burnin-mode light must select the light overlay")
check(_burnin_mode(argparse.Namespace(burnin_mode="")) == MODE_FULL,
      "no flag, no env = full")
check(_burnin_mode(argparse.Namespace()) == MODE_FULL,
      "an args namespace without the attribute at all = full")

os.environ["BURNIN"] = "light"
check(_burnin_mode(argparse.Namespace(burnin_mode="")) == MODE_LIGHT,
      "BURNIN=light must select the light overlay — this is the CLOUD transport, "
      "which carries the mode inside BURNIN rather than in a new env var")
os.environ["BURNIN"] = "true"
check(_burnin_mode(argparse.Namespace(burnin_mode="")) == MODE_FULL,
      "BURNIN=true = full")
os.environ.pop("BURNIN", None)

if failures:
    print("FAIL")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print(f"ok (light={light.count('drawtext')} label, full={full.count('drawtext')} labels)")
