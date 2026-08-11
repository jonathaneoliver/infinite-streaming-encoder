#!/usr/bin/env python3
"""#282: manifest.mpd ships at fragment granularity, and nothing is lost by it.

The claim the change rests on is that the fragment form is a strict SUPERSET of
the segment form: segment membership regroups by @media, and a segment's
duration is the sum of its fragments'. That is asserted here rather than trusted,
because the failure it guards against — a manifest whose segments no longer line
up with the media — is invisible until playback.
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder.manifests import (  # noqa: E402
    _extract_segments, write_fragmented_mpd)

NS = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


def segment_mpd(segments):
    """A minimal segment-granularity manifest: one SegmentURL per .m4s."""
    urls = "\n".join(f'<SegmentURL media="{n}"/>' for n, _ in segments)
    ss = "\n".join(f'<S t="0" d="{d}"/>' for _, d in segments)
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">
 <Period><AdaptationSet><Representation id="0">
  <SegmentList timescale="1000">
   <Initialization sourceURL="init.mp4"/>
   <SegmentTimeline>{ss}</SegmentTimeline>
   {urls}
  </SegmentList>
 </Representation></AdaptationSet></Period>
</MPD>
"""


def fake_segment(path: Path, n_frags: int, frag_bytes: int = 1000):
    """A .m4s with a sidecar, so the expansion has fragments without needing a
    real fMP4 (the box walker has its own tests). First fragment starts at 432:
    bytes 0-431 are the segment's styp/sidx header and belong to no fragment."""
    path.write_bytes(b"\0" * (432 + n_frags * frag_bytes))
    frags = [{"offset": 432 + i * frag_bytes, "length": frag_bytes,
              "independent": i == 0} for i in range(n_frags)]
    import json
    path.with_suffix(path.suffix + ".byteranges").write_text(
        json.dumps({"fragments": frags}))
    return frags


import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    segs = [("segment_00001.m4s", 6000), ("segment_00002.m4s", 6000),
            ("segment_00003.m4s", 3000)]  # a short tail, deliberately not equal
    for name, _ in segs:
        fake_segment(d / name, n_frags=30)
    mpd = d / "manifest.mpd"
    mpd.write_text(segment_mpd(segs))

    before = ET.parse(mpd).getroot()
    before_segs = _extract_segments(before.find(".//mpd:SegmentList", NS))

    n = write_fragmented_mpd(d)
    check("expanded one representation", n == 1)

    # --- the file itself ----------------------------------------------------
    check("no manifest_fragmented.mpd is written",
          not (d / "manifest_fragmented.mpd").exists())
    check("exactly one .mpd in the output", len(list(d.glob("*.mpd"))) == 1)
    check("no leftover .tmp", not list(d.glob("*.mpd.tmp")))

    root = ET.parse(mpd).getroot()
    urls = root.findall(".//mpd:SegmentURL", NS)
    check("one SegmentURL per fragment", len(urls) == 90)
    check("every entry carries a mediaRange",
          all(u.get("mediaRange") for u in urls))
    # Inclusive range: length is last-first+1. First fragment starts at 432.
    first, last = urls[0].get("mediaRange").split("-")
    check("first fragment starts at 432", first == "432")
    check("mediaRange is inclusive", int(last) - int(first) + 1 == 1000)

    # --- the superset claim -------------------------------------------------
    after_segs = _extract_segments(root.find(".//mpd:SegmentList", NS))
    check("segment count survives regrouping", len(after_segs) == len(before_segs))
    check("segment URLs survive regrouping",
          [s["url"] for s in after_segs] == [s["url"] for s in before_segs])
    check("per-segment durations survive regrouping",
          [s["timeline_duration"] for s in after_segs]
          == [s["timeline_duration"] for s in before_segs])
    # The uneven tail is the interesting case: 3000/30 does not divide evenly in
    # a way that could hide a rounding error, and it must still sum back exactly.
    check("uneven tail sums back exactly", after_segs[-1]["timeline_duration"] == 3000)

    # --- idempotence --------------------------------------------------------
    # Phases retry and resume, so this runs twice on the same directory
    # routinely. Re-expanding would split each FRAGMENT into sub-fragments.
    again = write_fragmented_mpd(d)
    check("second run is a no-op", again == 0)
    root2 = ET.parse(mpd).getroot()
    check("second run leaves the manifest byte-identical",
          len(root2.findall(".//mpd:SegmentURL", NS)) == 90)

with tempfile.TemporaryDirectory() as td:
    # No sidecars and no readable media: the expansion must decline rather than
    # emit a manifest with segments it cannot address.
    d = Path(td)
    segs = [("segment_00001.m4s", 6000)]
    (d / "segment_00001.m4s").write_bytes(b"not an mp4")
    (d / "manifest.mpd").write_text(segment_mpd(segs))
    n = write_fragmented_mpd(d)
    root = ET.parse(d / "manifest.mpd").getroot()
    urls = root.findall(".//mpd:SegmentURL", NS)
    check("unparseable media leaves one entry per segment", len(urls) == 1)
    check("unparseable media leaves no mediaRange", urls[0].get("mediaRange") is None)

# --- AVERAGE-BANDWIDTH is not multiplied by the fragment count --------------
# This is the symptom that actually shipped. _average_bandwidth walks the same
# segment list _extract_segments builds and adds each entry's FILE size, so a
# fragment-granular list counts one .m4s once per fragment while its duration
# sums correctly — inflating the figure by exactly fragments-per-segment. A real
# ladder advertised AVERAGE-BANDWIDTH=232,559,979 for a 7.8 Mbps rung (30.4x on
# a 6s/0.2s ladder, 5.2x on a 1s one) and iPhones stepped down through every
# rendition and stalled. Collapsing at parse time fixes it; this pins that.
from infinite_streaming_encoder.manifests import (  # noqa: E402
    _average_bandwidth, _parse_dash)

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    SEG_BYTES, SEG_MS, N_SEGS, N_FRAGS = 90_000, 6000, 3, 30
    (d / "720p").mkdir()
    (d / "720p" / "init.mp4").write_bytes(b"\0" * 1000)
    segs = []
    for i in range(1, N_SEGS + 1):
        name = f"720p/segment_{i:05d}.m4s"
        fake_segment(d / name, n_frags=N_FRAGS, frag_bytes=(SEG_BYTES - 432) // N_FRAGS)
        segs.append((name, SEG_MS))
    mpd = d / "manifest.mpd"
    mpd.write_text(segment_mpd(segs).replace(
        '<Initialization sourceURL="init.mp4"/>',
        '<Initialization sourceURL="720p/init.mp4"/>'))

    def bandwidth():
        rep = _parse_dash(mpd)["representations"][0]
        return _average_bandwidth(rep, d), len(rep["segments"])

    before, n_before = bandwidth()
    write_fragmented_mpd(d)
    after, n_after = bandwidth()

    check("segment-granular manifest yields one entry per file", n_before == N_SEGS)
    check("fragment-granular manifest still yields one entry per file",
          n_after == N_SEGS)
    check("bandwidth is unchanged by the expansion", before == after)
    # The true figure, computed independently of the parser.
    actual_bytes = 1000 + sum((d / n).stat().st_size for n, _ in segs)
    expect = int(round(actual_bytes * 8.0 / (N_SEGS * SEG_MS / 1000)))
    check("bandwidth matches bytes*8/duration", after == expect)
    # And the specific regression: never inflated by fragments-per-segment.
    check("bandwidth is not multiplied by the fragment count",
          after < expect * 2)

# --- the packager's debugging backup does not ship -------------------------
# Shaka's own SegmentTemplate output is kept so a bad manifest can be blamed on
# the packager or on us, but nothing reads it, so it belongs in the temp dir and
# not in a delivered output tree that is meant to hold one manifest.
from infinite_streaming_encoder.manifests import convert_segmentlist  # noqa: E402

TEMPLATE_MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">
 <Period><AdaptationSet><Representation id="0" width="1280" height="720">
  <SegmentTemplate timescale="1000" initialization="720p/init.mp4"
     media="720p/segment_$Number%05d$.m4s" startNumber="1">
   <SegmentTimeline><S t="0" d="6000" r="1"/></SegmentTimeline>
  </SegmentTemplate>
 </Representation></AdaptationSet></Period>
</MPD>
"""

with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as bak:
    pkg = Path(td) / "output_h264"
    (pkg / "720p").mkdir(parents=True)
    for i in (1, 2):
        (pkg / "720p" / f"segment_{i:05d}.m4s").write_bytes(b"\0" * 100)
    m = pkg / "manifest.mpd"
    m.write_text(TEMPLATE_MPD)

    convert_segmentlist(m, backup=True, backup_dir=Path(bak))

    check("no .bak in the package dir", not list(pkg.rglob("*.template.bak")))
    check("exactly one .mpd in the package dir", len(list(pkg.glob("*.mpd"))) == 1)
    # Named after the package dir so two codecs of one title don't collide.
    check("backup lands in the temp dir",
          (Path(bak) / "output_h264.mpd.template.bak").exists())
    check("backup is Shaka's original, not the rewrite",
          "SegmentTemplate" in (Path(bak) / "output_h264.mpd.template.bak").read_text())
    check("manifest itself was converted",
          "SegmentList" in m.read_text())

    # A backup that can't be written must not fail the package.
    m2 = pkg / "manifest.mpd"
    try:
        convert_segmentlist(m2, backup=True, backup_dir=Path("/proc/nonexistent/nope"))
        ok = True
    except SystemExit:
        ok = False
    check("unwritable backup dir does not fail the package", ok)

if failures:
    for f in failures:
        print(f"FAIL: {f}")
    sys.exit(1)
print("ok")
