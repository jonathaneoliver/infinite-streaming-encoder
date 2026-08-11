#!/usr/bin/env python3
"""The two HLS-side guards that survive #282's parse-time collapse.

`_extract_segments` now regroups fragment entries by @media before anything
downstream sees them (#282/#285), and `test_fragment_manifest.py` covers that
thoroughly — including that the bandwidth figure is not multiplied by the
fragment count. This file deliberately does NOT retest it. What it keeps is the
two things that collapse does not reach:

  1. `_average_bandwidth` handed a fragment-granular list DIRECTLY. The collapse
     makes this unreachable through the normal path, which is exactly why it
     needs its own test: a symptom that is no longer observable is not evidence
     that the cause is fixed. Mutation-verified — the pre-dedupe form returns
     228,000,000 bps against a true 7,600,000 on this fixture (30.0x), matching
     the 232,559,979 that shipped to within the audio track and container
     overhead the fixture omits.

  2. The PLAYLIST built from a collapsed list. `test_fragment_manifest.py` stops
     at the segment list; these assert what `_write_variant_playlist` then emits,
     which is what a player actually reads.

Why both matter, in one sentence: two of four ladders of one source shipped
unplayable with every other signal healthy — manifests valid, all segments
present, byte totals reconciling — and the only visible symptoms were an
`AVERAGE-BANDWIDTH` of 232,559,979 for a 7.8 Mbps rung and one EXTINF per
fragment.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder.manifests import (  # noqa: E402
    _average_bandwidth,
    _write_variant_playlist,
)

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" {detail}"))
    if not cond:
        FAILURES.append(name)


def rep(segments, rep_id="0"):
    return {"id": rep_id, "segments": segments, "init_segment": None}


def whole(n, dur=6.0):
    return [{"url": f"segment_{i:05d}.m4s", "duration": dur} for i in range(n)]


def fragmented(n_files, parts, seg_dur=6.0):
    """An UNCOLLAPSED list — what _extract_segments used to hand downstream, and
    what it would hand downstream again if the regrouping regressed."""
    return [{"url": f"segment_{i:05d}.m4s", "duration": seg_dur / parts}
            for i in range(n_files) for _ in range(parts)]


N, PARTS, SEG_BYTES = 56, 30, 5_700_000
print("HLS guards downstream of the #282 collapse")

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    for i in range(N):
        (d / f"segment_{i:05d}.m4s").write_bytes(b"\0" * SEG_BYTES)
    expect = round(N * SEG_BYTES * 8 / (N * 6.0))       # ~7.6 Mbps

    # --- 1. the dedupe, which the collapse makes otherwise unreachable -------
    got = _average_bandwidth(rep(whole(N)), d)
    check("whole-segment AVERAGE-BANDWIDTH is right",
          abs(got - expect) <= 1, f"(got {got:,}, want {expect:,})")

    got = _average_bandwidth(rep(fragmented(N, PARTS)), d)
    check("AVERAGE-BANDWIDTH counts each FILE once, not once per fragment",
          abs(got - expect) <= 1,
          f"(got {got:,}, want {expect:,} — {got / max(1, expect):.1f}x; "
          f"the pre-dedupe form returns {expect * PARTS:,})")

    # --- 2. the playlist a player actually reads ----------------------------
    out = d / "playlist.m3u8"
    _write_variant_playlist(rep(whole(N)), out, d)
    text = out.read_text()
    check("playlist lists each segment once",
          text.count("#EXTINF") == N, f"(got {text.count('#EXTINF')}, want {N})")
    check("EXTINF is the segment duration, not a fragment's",
          "#EXTINF:6.000000," in text)
    check("TARGETDURATION reflects whole segments",
          "#EXT-X-TARGETDURATION:7" in text,
          f"(got {[l for l in text.splitlines() if 'TARGETDURATION' in l]})")

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
