"""The CFR relabel must snap by the packet's own PTS, never by its index (#406).

`setts` is a BITSTREAM filter, so it sees packets in DECODE order. On a source
with B-frames that is not presentation order: decode order runs I, P, B, B while
those pictures present as I, B, B, P. Numbering the packets `N*ticks` therefore
hands the P frame the stamp belonging to the first B, and the mezzanine comes out
with an exact grid whose timestamps are PERMUTED.

Nothing obvious catches that. The pictures still decode in the right order
(the decoder reorders through its own buffer), the PTS still form a perfect
grid, and the file plays. Only a downstream RE-ENCODE notices: it places frames
by those timestamps, loses the one at t=1/fps, and every rung comes out a frame
short with its alignment shifting at each chunk join — which silently cost a
20-encode VMAF sweep 21.5 points and read as a content result.

An AV1 source numbers identically either way (`has_b_frames=0`), which is why
this survived until an H.264 MKV went through it. So the guard cannot be "did it
work on the clip I tried".

Run directly or via `make check`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder.mezzanine import (  # noqa: E402
    MezzanineSpec, _cfr_grid, build_setts_cmd)

SPEC = MezzanineSpec(input_path=Path("/in.mkv"), output_path=Path("/out.mp4"),
                     fps_num=30, fps_den=1)


def _bsf(spec: MezzanineSpec = SPEC) -> str:
    cmd = build_setts_cmd(spec, Path("/pre.mp4"))
    return cmd[cmd.index("-bsf:v") + 1]


def test_slot_is_derived_from_pts_not_packet_index() -> None:
    """The regression itself: `N` must not decide the slot."""
    bsf = _bsf()
    assert "PTS" in bsf, (
        "setts no longer references PTS — the slot must come from the packet's "
        "own timestamp (#406)")
    assert "N*" not in bsf.replace("PTS", "").replace("DTS", ""), (
        f"setts numbers packets by arrival index again: {bsf!r}. On a B-frame "
        "source that permutes the timestamps and every downstream encode loses "
        "the frame at t=1/fps (#406)")


def test_both_pts_and_dts_are_snapped() -> None:
    """DTS left raw would keep the jitter the relabel exists to remove, and can
    leave DTS above its own PTS once PTS is snapped."""
    bsf = _bsf()
    assert "pts=" in bsf and "dts=" in bsf, f"setts must set both: {bsf!r}"
    assert "round(DTS" in bsf, (
        f"DTS is not snapped to the grid: {bsf!r}")


def test_snaps_to_the_grid_the_timescale_established() -> None:
    """Pass 1 forces the timebase so pass 2's rounding lands on whole ticks.
    If the two disagree the snap is to the wrong quantum."""
    _, ticks = _cfr_grid(30, 1)
    bsf = _bsf()
    assert f"/{ticks})*{ticks}" in bsf, (
        f"setts rounds to a quantum other than the {ticks}-tick grid "
        f"_cfr_grid defines: {bsf!r}")


def test_ntsc_rates_use_their_own_quantum() -> None:
    """24000/1001 and friends resolve to a different tick count; the expression
    has to follow _cfr_grid rather than hardcode the integer-rate value."""
    spec = MezzanineSpec(input_path=Path("/in.mkv"), output_path=Path("/out.mp4"),
                         fps_num=30000, fps_den=1001)
    _, ticks = _cfr_grid(30000, 1001)
    assert ticks == 1001, f"unexpected NTSC quantum {ticks}"
    assert f"/{ticks})*{ticks}" in _bsf(spec), (
        "the NTSC grid is not being used for an NTSC source")


def test_relabel_still_requires_an_fps() -> None:
    """Without a nominal rate there is no grid to snap to, and silently doing
    nothing would leave a VFR mezzanine that the VMAF audit drifts against."""
    spec = MezzanineSpec(input_path=Path("/in.mkv"), output_path=Path("/out.mp4"))
    try:
        build_setts_cmd(spec, Path("/pre.mp4"))
    except Exception as e:
        assert "fps_num" in str(e), f"unexpected error: {e}"
    else:
        raise AssertionError("build_setts_cmd accepted a spec with no fps")


def test_it_is_still_a_stream_copy() -> None:
    """The relabel must never become a re-encode — that is what makes it
    lossless and what keeps it cheap enough to run on every source."""
    cmd = build_setts_cmd(SPEC, Path("/pre.mp4"))
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy", (
        f"pass 2 is no longer a stream copy: {cmd}")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
