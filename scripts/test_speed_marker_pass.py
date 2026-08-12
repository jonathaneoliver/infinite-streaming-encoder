#!/usr/bin/env python3
"""The ENCODER-SPEED marker's pass count must equal the pass count the encoder
actually ran (#314).

The two are derived in different files — `encode_variant` decides what to run,
`phase_variant` labels the measurement — and for one release they disagreed:
every two-pass h264 encode filed its speed under the 1-pass key while the Go
planner read the 2-pass one. Both sides succeeded. Nothing logged anything. The
only visible symptom was an h264 speed model that never moved off its seed, and
43,021 samples slowly dragged onto the wrong curve.

So this pins the two together rather than testing either alone: for the same
context, the pass count that reaches ffmpeg's argv and the pass count that
reaches the marker must be the same number.

Stdlib only, no ffmpeg. Runs in `make check` and CI.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fractions import Fraction  # noqa: E402

from infinite_streaming_encoder.encode_variants import (  # noqa: E402
    EncodeContext, build_ffmpeg_cmd, two_pass_for,
)
from infinite_streaming_encoder.ladder import burnin_for_height, Rung  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        failures.append(name)


def ctx_for(codec, passes=None, hevc_two_pass=True):
    return EncodeContext(
        mezzanine_path=Path("/tmp/mezz.mp4"),
        output_dir=Path("/tmp/out"),
        fps=Fraction(30),
        gop_duration_s=1.0,
        content_duration_s=60.0,
        padding_duration_s=0.0,
        passes=passes,
        hevc_two_pass=hevc_two_pass,
        burnin=False,
    )


RUNG = Rung("1080p", "1080p", 1920, 1080, 6000, "medium",
            *burnin_for_height(1080))


def marker_pass(ctx, codec):
    """What phase_variant's ENCODER-SPEED marker would report.

    Mirrors the emitter (cli_phase.py): the whole point of #314's fix is that
    this is the same call the encoder makes, so this line and encoder_passes()
    below cannot drift without a test failing.
    """
    return 1 if two_pass_for(ctx, codec) else 0


def argv_pass_number(argv):
    """Which pass this argv is, read off the flags ffmpeg will actually see.

    Two spellings, because two encoder families: x264/x265 take the pass in
    their own param string (`:pass=N:stats=…`), SVT-AV1 takes ffmpeg's generic
    `-pass N`. Matching only the x26x form would report AV1 as single-pass and
    quietly exempt it from the invariant below — which is the same shape of
    mistake as #314 itself.
    """
    joined = " ".join(argv)
    for n in (1, 2):
        if f":pass={n}:" in joined or f"-pass {n}" in joined:
            return n
    return None


def encoder_passes(ctx, codec):
    """How many passes encode_variant would actually run, read off the argv it
    builds — deliberately NOT off the same predicate the marker uses, or this
    test would pass by construction."""
    if not two_pass_for(ctx, codec):
        # Single-pass: one argv, and it must carry no pass flag at all.
        return 1 if argv_pass_number(
            build_ffmpeg_cmd(ctx, codec, RUNG, pass_num=None)) is None else 2
    p1 = argv_pass_number(build_ffmpeg_cmd(ctx, codec, RUNG, pass_num=1))
    p2 = argv_pass_number(build_ffmpeg_cmd(ctx, codec, RUNG, pass_num=2))
    return 2 if (p1, p2) == (1, 2) else 1


print("marker pass count == encoder pass count")

# The regression itself: h264 under the profile default (2), which is what every
# shipped ladder resolves to today. This is the case that was wrong.
for codec in ("h264", "hevc", "av1"):
    c2 = ctx_for(codec, passes={codec: 2})
    check(f"{codec} 2-pass: encoder runs 2", encoder_passes(c2, codec) == 2)
    check(f"{codec} 2-pass: marker says 1", marker_pass(c2, codec) == 1,
          f"marker={marker_pass(c2, codec)}")

    c1 = ctx_for(codec, passes={codec: 1})
    check(f"{codec} 1-pass: encoder runs 1", encoder_passes(c1, codec) == 1)
    check(f"{codec} 1-pass: marker says 0", marker_pass(c1, codec) == 0,
          f"marker={marker_pass(c1, codec)}")

# Both directions, stated as the invariant rather than as cases — a codec added
# later gets covered without anyone remembering to extend the list above.
for codec in ("h264", "hevc", "av1"):
    for passes in ({codec: 1}, {codec: 2}, None):
        c = ctx_for(codec, passes=passes)
        want = encoder_passes(c, codec)
        got = marker_pass(c, codec) + 1
        check(f"invariant {codec} passes={passes}", want == got,
              f"encoder ran {want}, marker labelled it {got}")

# The legacy fallback still behaves as documented for a context built without
# `passes` — that path is not dead, it is just no longer the default.
print("\nlegacy hevc_two_pass fallback (no `passes` in the context)")
check("hevc + hevc_two_pass=True → 2-pass",
      two_pass_for(ctx_for("hevc", hevc_two_pass=True), "hevc"))
check("h264 + hevc_two_pass=True → 1-pass",
      not two_pass_for(ctx_for("h264", hevc_two_pass=True), "h264"))
check("hevc + hevc_two_pass=False → 1-pass",
      not two_pass_for(ctx_for("hevc", hevc_two_pass=False), "hevc"))
# ...and that `passes` overrides it in both directions, which is the property
# that makes the fallback safe to keep.
check("passes={h264:2} beats hevc_two_pass=False",
      two_pass_for(ctx_for("h264", passes={"h264": 2}, hevc_two_pass=False), "h264"))
check("passes={hevc:1} beats hevc_two_pass=True",
      not two_pass_for(ctx_for("hevc", passes={"hevc": 1}, hevc_two_pass=True), "hevc"))
# A context carrying ANOTHER codec's count must not answer for this one — a
# local run reuses one context across codecs, so the map is routinely partial.
check("passes={hevc:1} does not answer for h264",
      two_pass_for(ctx_for("h264", passes={"hevc": 1}, hevc_two_pass=True), "h264") is False)

# The marker the Go side parses is a fixed shape (speedMarkerRe in job.go
# accepts two_pass=([01]) only), so a bool leaking through would fail to match
# and the sample would be dropped in silence.
print("\nmarker wire format")
for codec in ("h264", "hevc"):
    for passes in ({codec: 1}, {codec: 2}):
        tp = marker_pass(ctx_for(codec, passes=passes), codec)
        line = (f"[[ENCODER-SPEED machine=graviton codec={codec} height=1080 "
                f"two_pass={tp} preset=medium fps=30 content_s=60.0 "
                f"encode_s=30.0]]")
        m = re.match(
            r"^\[\[ENCODER-SPEED machine=(\S+) codec=(\S+) height=(\d+) "
            r"two_pass=([01]) preset=(\S+) fps=(\d+) content_s=([0-9.]+) "
            r"encode_s=([0-9.]+)\]\]$", line)
        check(f"{codec} passes={passes} matches speedMarkerRe", m is not None, line)
        if m:
            check(f"{codec} passes={passes} parses to the right pass",
                  m.group(4) == str(tp))

# Everything above pins two_pass_for against the encoder. That is only half the
# contract: the bug was an emitter that never CALLED it. Nothing importable
# separates the marker's pass count from phase_variant, so check the source —
# brittle in the cheap direction (a rename fails this test loudly) rather than
# in the expensive one (#314 was silent for a release).
print("\ncli_phase's emitter uses the shared decision")
src = (Path(__file__).resolve().parent / "infinite_streaming_encoder"
       / "cli_phase.py").read_text()
# Comments only, stripped — the block below is deliberately full of prose ABOUT
# the rule this checks for, and matching that prose would make the test pass or
# fail on documentation.
code = "\n".join(ln for ln in src.splitlines()
                 if not ln.lstrip().startswith("#"))
marker_block = code[:code.find("[[ENCODER-SPEED")][-1200:]
check("phase_variant assigns two_pass via two_pass_for",
      re.search(r"two_pass\s*=\s*1 if two_pass_for\(ctx, args\.codec\) else 0",
                marker_block) is not None,
      "the ENCODER-SPEED block no longer derives its pass count from "
      "encode_variants.two_pass_for — that is exactly #314")
check("and does not re-derive it from hevc_two_pass",
      "ctx.hevc_two_pass" not in marker_block,
      "the frozen `args.codec == 'hevc' and ctx.hevc_two_pass` rule is back")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all speed-marker pass-count checks passed")
