#!/usr/bin/env python3
"""hls_from_dash must handle a per-fragment manifest.mpd.

A fragment-granular manifest.mpd is INTENDED, not a defect: every live ladder
sets partial_duration (only apple-uniq-vod turns it off) and write_fragmented_mpd
expands each segment into per-fragment SegmentURLs so a player needs no
.byteranges sidecar. DASH and HLS just put fragments in different places — DASH
addresses each as its own SegmentURL, HLS keeps whole segments on the #EXTINF
media lines and describes fragments as #EXT-X-PART with BYTERANGE.

Reading fragment entries straight onto the media lines shipped two of four
ladders unplayable, and nothing in the artifacts said so: manifests valid, every
segment present, file lists and byte totals reconciling. The only symptom was a
player walking down every rendition and stalling. Measured, per representation:

    tag  entries  distinct files  AVERAGE-BANDWIDTH shipped
    6s   1672     56              232,559,979   (30.4x; rung target 7.8 Mbps)
    1s   1672     335              33,757,127   (5.2x)
    2s    168     168               7,556,915   (correct; whole-segment manifest)
    xs   1672     56                5,668,035   (correct, but by luck — master.m3u8
                                                 was written one second before
                                                 manifest.mpd was replaced)

Numbers below are those, so a regression reproduces the shipped failure rather
than a plausible-looking one.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder.manifests import (  # noqa: E402
    _average_bandwidth,
    _fold_fragments_to_segments,
    _write_variant_playlist,
)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def rep(segments, rep_id="0"):
    return {"id": rep_id, "segments": segments, "init_segment": None}


def whole(n, dur=6.0):
    return [{"url": f"segment_{i:05d}.m4s", "duration": dur} for i in range(n)]


def fragmented(n_files, parts, seg_dur=6.0):
    """What write_fragmented_mpd produces: `parts` entries per file, each
    carrying an equal share of the segment's duration."""
    return [{"url": f"segment_{i:05d}.m4s", "duration": seg_dur / parts}
            for i in range(n_files) for _ in range(parts)]


MP = Path("manifest.mpd")
print("hls granularity repair")

# --- the fold ---------------------------------------------------------------
info = {"representations": [rep(whole(56))]}
check("a whole-segment manifest is left alone",
      _fold_fragments_to_segments(info, MP) == 0
      and len(info["representations"][0]["segments"]) == 56)

info = {"representations": [rep(fragmented(56, 30), "1080p")]}
n = _fold_fragments_to_segments(info, MP)
segs = info["representations"][0]["segments"]
check("6s shape (30 entries/segment) is folded", n == 1)
check("folds to one entry per FILE", len(segs) == 56, f"(got {len(segs)})")
check("durations sum back to the whole segment",
      all(abs(s["duration"] - 6.0) < 1e-9 for s in segs),
      f"(got {segs[0]['duration']})")
check("segment order is preserved",
      [s["url"] for s in segs] == [f"segment_{i:05d}.m4s" for i in range(56)])

info = {"representations": [rep(fragmented(335, 5, seg_dur=1.0))]}
_fold_fragments_to_segments(info, MP)
segs = info["representations"][0]["segments"]
check("1s shape (5 entries/segment) is folded", len(segs) == 335)
check("1s durations sum back", all(abs(s["duration"] - 1.0) < 1e-9 for s in segs))

info = {"representations": [rep(whole(56), "a"), rep(fragmented(56, 30), "b")]}
check("every representation is scanned, not just the first",
      _fold_fragments_to_segments(info, MP) == 1
      and len(info["representations"][1]["segments"]) == 56)

# --- what the fold protects -------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    SEG_BYTES = 5_700_000      # ~6s of the 1080p rung
    N, PARTS = 56, 30
    for i in range(N):
        (d / f"segment_{i:05d}.m4s").write_bytes(b"\0" * SEG_BYTES)
    expect = round(N * SEG_BYTES * 8 / (N * 6.0))       # ~7.6 Mbps

    got = _average_bandwidth(rep(whole(N)), d)
    check("whole-segment AVERAGE-BANDWIDTH is right",
          abs(got - expect) <= 1, f"(got {got:,}, want {expect:,})")

    # Belt and braces: correct even WITHOUT the fold, because each file's bytes
    # are counted once. This is the arithmetic that produced 232 Mbps.
    got = _average_bandwidth(rep(fragmented(N, PARTS)), d)
    check("AVERAGE-BANDWIDTH is not inflated even unfolded",
          abs(got - expect) <= 1,
          f"(got {got:,}, want {expect:,} — {got / max(1, expect):.1f}x)")

    # The playlist is what a player actually reads.
    info = {"representations": [rep(fragmented(N, PARTS))]}
    _fold_fragments_to_segments(info, MP)
    out = d / "playlist.m3u8"
    _write_variant_playlist(info["representations"][0], out, d)
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
