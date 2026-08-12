#!/usr/bin/env python3
"""Regenerate internal/encode/testdata_chunkplan.txt from the Python planner.

Python is the authority on chunk boundaries; the Go planner is pinned to it by
TestChunkPlanMatchesPython. Whenever the Python planner intentionally changes,
run this and commit the result:

    python3 scripts/gen_chunkplan_golden.py > internal/encode/testdata_chunkplan.txt

That test's doc comment has always claimed the regeneration snippet lived in the
doc comment. It did not — there was no generator at all, so the only way to
update the vectors was to hand-write them, which is exactly how a golden file
drifts from the thing it is supposed to pin.

Line format: duration|chunk|segment|idx,start,duration;idx,start,duration;...
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder.chunking import plan_chunks  # noqa: E402
from infinite_streaming_encoder.encode_variants import _coalesce_runt_tail  # noqa: E402

# (duration, chunk, segment). Chosen to cover, in order: the reference clip at
# the old default; exact multiples; a clip shorter than one chunk; a sub-second
# clip; the runt-tail fold; every shipped ladder's segment duration (1/2/6) at
# the sizes the dynamic selector actually emits (multiples of 12); a chunk
# SHORTER than the grid (the clamp); and a segment duration coprime-ish with 6
# so the LCM branch of chunk_grid_for is exercised rather than assumed.
CASES = [
    (334.4, 30, 6), (330.0, 30, 6), (360.0, 30, 6), (6.0, 30, 6), (0.5, 30, 6),
    (334.0, 30, 6), (334.0, 60, 6), (334.0, 12, 6), (334.0, 24, 6),
    (334.0, 96, 6), (334.0, 132, 6), (334.0, 2004, 6),
    (334.0, 12, 1), (334.0, 24, 1), (334.0, 96, 1), (334.0, 132, 1),
    (334.0, 12, 2), (334.0, 24, 2), (334.0, 96, 2), (334.0, 132, 2),
    (300.0, 12, 1), (300.0, 12, 2), (300.0, 12, 6),
    (7200.0, 96, 6), (7200.0, 96, 1), (14400.0, 24, 6),
    (62.0, 30, 6), (62.0, 30, 2), (62.0, 30, 1),
    (334.0, 6, 1), (334.0, 6, 2), (334.0, 6, 6),
    # chunk SHORTER than the grid: the clamp. These pass _validate (a whole
    # multiple of a 1s/2s segment) but ask for more chunks than the clip has grid
    # units, which used to yield zero-duration chunks on the Go side.
    (334.0, 1, 1), (334.0, 2, 1), (334.0, 3, 1), (334.0, 2, 2), (334.0, 4, 2),
    (334.0, 30, 5), (334.0, 30, 3),
    (100.0, 30, 10),
]


def main() -> int:
    for duration, chunk, segment in CASES:
        try:
            spans = _coalesce_runt_tail(plan_chunks(duration, chunk, segment))
        except ValueError as e:
            # A case the Python planner refuses is not a vector — Go has no
            # equivalent guard, so pinning it would pin a disagreement.
            print(f"# skipped {duration}|{chunk}|{segment}: {e}", file=sys.stderr)
            continue
        body = ";".join(f"{c.index},{c.start_s:.6f},{c.duration_s:.6f}" for c in spans)
        print(f"{duration}|{chunk}|{segment}|{body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
