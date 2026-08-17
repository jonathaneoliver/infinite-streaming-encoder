#!/usr/bin/env python3
"""The chunk grid, checked against the PYTHON planner.

Go is pinned to Python by internal/encode/chunkplan_test.go — but that file is
GENERATED from Python, so it cannot catch Python being wrong. Regenerate the
goldens after breaking plan_chunks and the Go test happily follows it into the
same wrong answer. This is the half that pins Python's own invariants, and it is
also the half that covers local-dist, which plans its chunks here rather than in
Go.

Three invariants, in priority order:

  1. CORRECTNESS — every interior boundary is a whole number of segments. The
     packager segments the concatenated variant, so a boundary off a segment edge
     shifts every later segment.
  2. COMPARABILITY — every interior chunk is a multiple of 6s, whatever the
     ladder's segment duration, so profiles that differ only in delivery cut the
     clip in the same places and a four-ladder comparison isolates the profile.
  3. NO EMPTY WORK — never more chunks than the clip has grid units. Each surplus
     chunk was a real Batch job encoding nothing.

Stdlib only, no ffmpeg.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder.chunking import (  # noqa: E402
    CHUNK_GRID_S, chunk_grid_for, chunk_count, plan_chunks,
)

EPS = 1e-6
LADDER_SEGMENTS = [1.0, 2.0, 6.0]   # the shipped delivery profiles
DYNAMIC_SIZES = [12.0, 24.0, 96.0, 132.0, 2004.0]  # multiples of the 12s quantum
CLIPS = [334.0, 300.0, 62.0, 3600.0, 14400.0]

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        failures.append(name)


def is_multiple(value, unit):
    r = value / unit
    return abs(r - round(r)) < EPS


print("chunk_grid_for = lcm(segment, 6)")
for seg, want in [(1, 6), (2, 6), (3, 6), (6, 6), (4, 12), (5, 30),
                  (8, 24), (10, 30), (12, 12), (1.5, 6)]:
    got = chunk_grid_for(seg)
    check(f"seg={seg} -> {want}", got == want, f"got {got}")
# A segment longer than any grid in range falls back rather than inventing a
# boundary that is not a segment edge. Without the round(r) >= 1 guard this
# returns 6, because 6/1e9 rounds to 0 and sits inside the epsilon.
check("seg=1e9 falls back to the segment", chunk_grid_for(1e9) == 1e9,
      f"got {chunk_grid_for(1e9)}")
check("seg=0 is not a division by zero", chunk_grid_for(0) == CHUNK_GRID_S)

print("\n1. every interior boundary is a whole number of segments")
for clip in CLIPS:
    for size in DYNAMIC_SIZES:
        for seg in LADDER_SEGMENTS:
            chunks = plan_chunks(clip, size, seg)
            bad = [c.index for c in chunks[:-1] if not is_multiple(c.duration_s, seg)]
            if bad:
                check(f"clip={clip} chunk={size} seg={seg}", False,
                      f"chunks {bad} are not whole segments")
print(f"  ok   {len(CLIPS) * len(DYNAMIC_SIZES) * len(LADDER_SEGMENTS)} "
      f"(clip, chunk, segment) combinations")

print("\n2. every interior chunk is a multiple of 6s, on every ladder")
for clip in CLIPS:
    for size in DYNAMIC_SIZES:
        for seg in LADDER_SEGMENTS:
            chunks = plan_chunks(clip, size, seg)
            bad = [(c.index, c.duration_s) for c in chunks[:-1]
                   if not is_multiple(c.duration_s, CHUNK_GRID_S)]
            if bad:
                check(f"clip={clip} chunk={size} seg={seg}", False, f"{bad}")
print(f"  ok   interior chunks all land on the {CHUNK_GRID_S:.0f}s grid")

print("\n   ...and the three ladders agree on every boundary")
for clip in CLIPS:
    for size in DYNAMIC_SIZES:
        plans = {seg: [(c.start_s, c.duration_s) for c in plan_chunks(clip, size, seg)]
                 for seg in LADDER_SEGMENTS}
        first = plans[LADDER_SEGMENTS[0]]
        for seg, p in plans.items():
            if p != first:
                check(f"clip={clip} chunk={size}", False,
                      f"seg={seg} cuts differently from seg={LADDER_SEGMENTS[0]}")
                break
print("  ok   1s, 2s and 6s ladders produce identical plans")

print("\n3. no zero-duration chunks, and no more chunks than grid units")
for clip in [334.0, 300.0, 62.0, 6.0, 0.5]:
    for seg in LADDER_SEGMENTS:
        for size in [seg, seg * 2, seg * 3, 6.0, 12.0, 30.0]:
            chunks = plan_chunks(clip, size, seg)
            if not chunks:
                check(f"clip={clip} chunk={size} seg={seg}", False, "no chunks")
                continue
            empty = [c.index for c in chunks if c.duration_s <= 0]
            if empty:
                check(f"clip={clip} chunk={size} seg={seg}", False,
                      f"{len(empty)} zero-duration chunks of {len(chunks)}")
                continue
            # Contiguous indices — the concat step joins by index and a hole
            # would silently drop media.
            if [c.index for c in chunks] != list(range(len(chunks))):
                check(f"clip={clip} chunk={size} seg={seg}", False, "indices not contiguous")
                continue
            # Tiles the clip exactly.
            total = sum(c.duration_s for c in chunks)
            if abs(total - clip) > EPS:
                check(f"clip={clip} chunk={size} seg={seg}", False,
                      f"chunks total {total}, clip is {clip}")
print("  ok   every plan is non-empty, gapless and free of empty chunks")

# The specific case that used to break: chunk_count asks for far more pieces than
# the clip has grid units.
n_asked = chunk_count(334.0, 1.0)
plan = plan_chunks(334.0, 1.0, 1.0)
check("334s at chunk=1s on a 1s ladder clamps rather than emitting empties",
      len(plan) < n_asked and all(c.duration_s > 0 for c in plan),
      f"asked {n_asked}, planned {len(plan)}")


# ---------------------------------------------------------------------------
# Per-variant chunking on the local target (#362).
#
# Go sizes each (codec, rung) from learned encode speed and hands the answer
# over as --variant-chunk CODEC/LABEL:SECONDS. Two properties matter here and
# neither is visible from the Go side: that the parser agrees with the shape Go
# emits, and that an unsized rung still gets a plan.
from infinite_streaming_encoder.cli_local_dist import (  # noqa: E402
    _parse_variant_chunks,
)

parsed = _parse_variant_chunks(["h264/1080p:24", "hevc/2160p:132.0", "h264/360p:2004"])
check("--variant-chunk parses to (codec, label) -> seconds",
      parsed == {("h264", "1080p"): 24.0, ("hevc", "2160p"): 132.0,
                 ("h264", "360p"): 2004.0},
      f"got {parsed}")

# A label is free text from the ladder JSON. rsplit(":", 1) then split("/", 1)
# is what makes a slash in the label survive; pin it, because the failure is a
# silently unsized rung rather than an error.
check("a label containing a slash still parses",
      _parse_variant_chunks(["h264/1080p/hi:36"]) == {("h264", "1080p/hi"): 36.0},
      f"got {_parse_variant_chunks(['h264/1080p/hi:36'])}")

# Malformed entries are cosmetic-grade failures: the rung falls back to the
# uniform plan, which is what every local run did before this existed. Never
# worth failing a 40-minute encode over.
for bad in ["nonsense", "h264/1080p", "h264/1080p:abc", "h264/1080p:0",
            "h264/1080p:-12", "", None]:
    check(f"malformed {bad!r} is skipped, not fatal",
          _parse_variant_chunks([bad]) == {}, f"got {_parse_variant_chunks([bad])}")

# The planner is the SAME one either way — a sized rung and an unsized one differ
# only in the seconds handed to plan_chunks, so segment alignment, the 6s grid
# and the runt-tail fold cannot diverge between them.
for seg in LADDER_SEGMENTS:
    sized = plan_chunks(334.0, 132.0, seg)
    unsized = plan_chunks(334.0, 12.0, seg)
    check(f"seg={seg}: a sized rung and an unsized one both tile the clip",
          abs(sum(c.duration_s for c in sized) - 334.0) < EPS
          and abs(sum(c.duration_s for c in unsized) - 334.0) < EPS)
    check(f"seg={seg}: differently-sized variants really do differ",
          len(sized) != len(unsized),
          f"both planned {len(sized)} chunks — per-variant sizing is doing nothing")
    # The point of the whole change: a cheap rung is ONE chunk while an expensive
    # one is many, and both still land on segment edges.
    for c in sized[:-1]:
        check(f"seg={seg}: sized interior boundary is a whole segment",
              abs((c.duration_s / seg) - round(c.duration_s / seg)) < EPS,
              f"{c.duration_s} is not a multiple of {seg}")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all chunk-grid checks passed")
