"""Chunk boundaries must name the frame the seek actually lands on (#408).

A chunk is cut with `-ss` in TIME but bounded with `-frames:v`, a COUNT. Those
agree only while the frame grid is continuous. On a source with a dropped frame
they diverge by one past the drop, so every interior boundary after it re-reads
the frame the previous chunk already emitted: a duplicate at the join, and every
frame after it one place out.

**A total frame count cannot see this.** The concatenated output came out at
exactly the right 1800 frames, because the duplicate is offset by the drop -
which is why #89's verification (a real encode matching the source's total)
passed it cleanly. What is wrong is *which* frame sits at each position, so the
assertion has to be per-JOIN: the last frame of chunk N and the first frame of
chunk N+1 must be ADJACENT source frames, never the same one.

Third instance of one fault: #89 (`-t` seconds vs the frame grid), #406 (packet
index vs presentation order), #408 (frame count vs time seek). Each mixes frame
arithmetic with time addressing and breaks where the mapping is not the identity.

Run directly or via `make check`.
"""
from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder import ffprobe  # noqa: E402
from infinite_streaming_encoder.ffprobe import frames_before  # noqa: E402

FPS = Fraction(30, 1)
# A 60s clip at 30fps MISSING the frame at 31.7s - the shape that exposed #408.
CONTINUOUS = tuple(round(i / 30, 6) for i in range(1800))
WITH_HOLE = tuple(round((i if i <= 951 else i + 1) / 30, 6) for i in range(1800))


def _use(pts):
    """Point the lookup at a synthetic timestamp table."""
    ffprobe.video_pts.cache_clear()
    ffprobe.video_pts.__wrapped__.__globals__  # touch, keeps linters honest
    fake = Path("/synthetic.mp4")
    ffprobe.video_pts.cache_clear()
    orig = ffprobe.video_pts
    ffprobe.video_pts = lambda p, _pts=pts: _pts        # type: ignore[assignment]
    return fake, orig


def _restore(orig):
    ffprobe.video_pts = orig                             # type: ignore[assignment]


def test_continuous_grid_matches_the_arithmetic() -> None:
    """Where the old rule was right, the new one must agree with it exactly —
    otherwise this 'fix' silently re-cuts every well-formed source."""
    path, orig = _use(CONTINUOUS)
    try:
        for t in (0.0, 6.0, 12.0, 24.0, 31.7, 42.0, 59.0):
            got = frames_before(t, FPS, path)
            want = frames_before(t, FPS, None)          # ceil(t*fps)
            assert got == want, f"t={t}: real timestamps {got}, arithmetic {want}"
    finally:
        _restore(orig)


def test_a_dropped_frame_moves_the_boundary() -> None:
    """Past the drop the two rules MUST disagree — that disagreement is the bug."""
    path, orig = _use(WITH_HOLE)
    try:
        assert frames_before(24.0, FPS, path) == frames_before(24.0, FPS, None), (
            "before the drop the rules should still agree")
        real, arith = frames_before(42.0, FPS, path), frames_before(42.0, FPS, None)
        assert real == arith - 1, (
            f"after the drop, real={real} arithmetic={arith}; expected one fewer "
            "— if these match, the synthetic table has no hole and the test is "
            "vacuous")
    finally:
        _restore(orig)


def test_chunks_tile_without_overlap_or_gap() -> None:
    """The property the encode depends on, asserted per JOIN rather than on the
    total: consecutive chunks must be adjacent, never overlapping.

    The FINAL chunk is deliberately unbounded (runs to EOF) so container-duration
    imprecision cannot truncate the tail, so it is checked as "covers whatever
    remains" rather than by a count.
    """
    for name, pts in (("continuous", CONTINUOUS), ("dropped frame", WITH_HOLE)):
        path, orig = _use(pts)
        try:
            for bounds in ([0.0, 24.0, 42.0, 60.0],          # the #408 layout
                           [0.0, 30.0, 60.0],
                           [i * 6.0 for i in range(11)],
                           [0.0, 12.0, 24.0, 36.0, 48.0, 60.0]):
                cursor = frames_before(bounds[0], FPS, path)
                for a, b in list(zip(bounds, bounds[1:]))[:-1]:   # interior only
                    start = frames_before(a, FPS, path)
                    n = frames_before(b, FPS, path) - start
                    assert n > 0, f"{name}: empty chunk {a}-{b}"
                    assert start == cursor, (
                        f"{name} {bounds}: chunk at {a}s starts at frame {start} "
                        f"but the previous chunk ended at {cursor} — "
                        + ("overlap, a frame is emitted twice"
                           if start < cursor else "gap, a frame is lost"))
                    cursor = start + n
                last = frames_before(bounds[-2], FPS, path)
                assert last == cursor, (
                    f"{name} {bounds}: the final chunk starts at frame {last}, "
                    f"but the chunks before it ended at {cursor}")
        finally:
            _restore(orig)


def test_the_arithmetic_rule_overlaps_on_a_holed_source() -> None:
    """Proves this file is load-bearing rather than decorative.

    The pre-#408 rule is still reachable — it is the fallback — so ask it the
    same questions on a source with a drop. At every boundary AFTER the drop it
    must name a different frame than the file actually has; that difference is
    the duplicated frame at the join.

    Note the boundary that breaks is the START of the final chunk (42.0s in the
    layout that exposed this). Its END is unbounded, but its start is still a
    join, so a check that skips the last pair sees nothing.
    """
    path, orig = _use(WITH_HOLE)
    try:
        before = [t for t in (0.0, 6.0, 24.0, 30.0) ]
        after = [t for t in (36.0, 42.0, 48.0, 54.0)]
        for t in before:
            assert frames_before(t, FPS, path) == frames_before(t, FPS, None), (
                f"boundaries before the drop must still agree (t={t})")
        diffs = [(t, frames_before(t, FPS, None) - frames_before(t, FPS, path))
                 for t in after]
        assert all(d == 1 for _, d in diffs), (
            f"after the drop the arithmetic should name one frame too many at "
            f"every boundary; got {diffs}. If these are 0 the synthetic table "
            "has no hole and every assertion in this file is vacuous")
    finally:
        _restore(orig)


def test_it_falls_back_rather_than_failing_an_encode() -> None:
    """An unreadable timestamp table must restore the OLD behaviour, not stop the
    encode and not invent a third answer."""
    path, orig = _use(())          # ffprobe failed / no timestamps
    try:
        for t in (6.0, 24.0, 42.0):
            assert frames_before(t, FPS, path) == frames_before(t, FPS, None)
    finally:
        _restore(orig)


def test_both_call_sites_share_one_rule() -> None:
    """The encode bound and the VMAF clamp must not drift apart: measuring a
    chunk against a differently-counted window is the misalignment the clamp
    exists to prevent."""
    for mod in ("encode_variants", "cli_phase"):
        src = (Path(__file__).resolve().parent / "infinite_streaming_encoder"
               / f"{mod}.py").read_text()
        assert "frames_before(" in src, f"{mod} no longer uses the shared rule"
        # code only: the comments SHOULD still explain the arithmetic and why it
        # is not used, so matching them would make this assertion unfixable.
        code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
        assert "-(-x.numerator // x.denominator)" not in code, (
            f"{mod} has grown its own copy of the ceil(t*fps) arithmetic "
            "again (#408) — it must call the shared frames_before")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
