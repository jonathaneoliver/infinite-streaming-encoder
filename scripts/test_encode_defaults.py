#!/usr/bin/env python3
"""The encode defaults that changed, and the contracts holding them together.

Three things moved at once: HEVC and AV1 encode 10-bit, every downscale uses
lanczos, and AV1 finally honours its rung's preset instead of a hardcoded 6.
Each fails silently — the encode succeeds either way and the output looks
right — so each is pinned here.

The cross-language half matters most: Go records pix_fmt / scale_flags /
preset into encode.json while PYTHON is what actually passes them to ffmpeg.
Nothing makes them agree at runtime, so an output could be labelled 10-bit and
encoded 8-bit, which is worse than not recording it at all.
"""
import re
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from infinite_streaming_encoder.burnin import (  # noqa: E402
    SCALE_FLAGS, BurninContext, build_filter,
)
from infinite_streaming_encoder.encode_variants import (  # noqa: E402
    _PIX_FMT, _av1_preset, _codec_specific_args,
)
from infinite_streaming_encoder.ladder import Rung  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


# --- 1. bit depth per codec -------------------------------------------------
for codec, want in (("hevc", "yuv420p10le"), ("av1", "yuv420p10le"),
                    ("h264", "yuv420p")):
    args = _codec_specific_args(codec, 5800, 180, "medium")
    got = args[args.index("-pix_fmt") + 1]
    check(got == want, f"{codec}: -pix_fmt {got}, want {want}")

check(_PIX_FMT["h264"] == "yuv420p",
      "h264 must stay 8-bit: High 10 has no hardware decode on most devices, "
      "and the h264 ladder exists for maximum compatibility")

# --- 2. lanczos on every scale ----------------------------------------------
RUNG = Rung(label="1080p", res_name="1080p", width=1920, height=1080,
            bitrate=5800, preset="medium", fontsize_tc=48, fontsize_label=36,
            burnin_x=40, burnin_y_tc=40, burnin_y_label=100)
ctx = BurninContext(codec="hevc", tier=RUNG, fps=Fraction(25, 1),
                    rate_label="r", encoder_label="SW",
                    content_duration_s=60.0, padding_duration_s=0.0)
for burnin in (True, False):
    f = build_filter(ctx, burnin=burnin)
    check(f"scale=1920:1080:flags={SCALE_FLAGS}" in f,
          f"burnin={burnin}: scale must carry the lanczos flags, got {f[:80]}")
check("lanczos" in SCALE_FLAGS and "accurate_rnd" in SCALE_FLAGS,
      f"SCALE_FLAGS lost its content: {SCALE_FLAGS}")

# --- 3. av1 honours the rung preset -----------------------------------------
check(_av1_preset("2") == "2", "a numeric rung preset must reach SVT-AV1")
check(_av1_preset("4") == "4", "a numeric rung preset must reach SVT-AV1")
for name in ("medium", "slower", "", None, "veryslow"):
    check(_av1_preset(name) == "6",
          f"an x26x preset name ({name!r}) must fall back to 6, not be passed "
          "to SVT-AV1, which would reject it and fail the encode")

args = _codec_specific_args("av1", 1962, 180, "2")
check(args[args.index("-preset") + 1] == "2",
      f"av1 must encode at the rung's preset: {args}")
args = _codec_specific_args("av1", 1962, 180, "medium")
check(args[args.index("-preset") + 1] == "6",
      f"av1 default preset must stay 6: {args}")

# x26x pass their preset through untouched.
for codec in ("h264", "hevc"):
    a = _codec_specific_args(codec, 5800, 180, "slower")
    check(a[a.index("-preset") + 1] == "slower",
          f"{codec} must pass its preset through: {a}")

# --- 4. Go records what Python applies --------------------------------------
GO = (ROOT.parent / "internal" / "encode" / "ladder.go").read_text()

m = re.search(r'const ScaleFlags = "([^"]+)"', GO)
check(m is not None, "internal/encode/ladder.go lost its ScaleFlags constant")
if m:
    check(m.group(1) == SCALE_FLAGS,
          f"Go records scale_flags={m.group(1)!r} but Python applies "
          f"{SCALE_FLAGS!r} — encode.json would describe an encode that did not "
          "happen")

# Go's pixFmtForCodec must agree with Python's table for every codec.
for codec, want in _PIX_FMT.items():
    check(f'"{want}"' in GO,
          f"Go's pixFmtForCodec has no {want!r} for {codec} — it labels "
          "encode.json with a bit depth Python did not use")

# The Slow-preset mapping: av1 numeric, x26x named, and NOT the same string.
check('return "2"' in GO and 'return "slower"' in GO,
      "Go's presetForCodec must map av1 -> 2 and x26x -> slower; SVT-AV1 takes "
      "a NUMBER and would reject a name")

if failures:
    print("FAIL")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print(f"ok (pix_fmt {_PIX_FMT}, scale flags {SCALE_FLAGS})")
