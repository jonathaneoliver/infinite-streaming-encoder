#!/usr/bin/env python3
"""DASH and HLS manifest helpers.

Two subcommands:

  convert-segmentlist <manifest.mpd>
      Rewrites a Shaka Packager SegmentTemplate manifest into explicit
      SegmentList form (better for CDN caching + explicit VOD URLs).
      Called from create_abr_ladder.sh after the packager runs.

  hls-from-dash <package-dir>
      Generates LL-HLS master + variant playlists from an existing DASH
      package (reuses the same .m4s segments and init.mp4). Not currently
      wired into the bash pipeline, but kept here for the upcoming step
      that replaces the bash-generated HLS manifests.
"""
from __future__ import annotations

import argparse
import glob
import json
import shutil
import sys
import tempfile
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_DASH_NS = "urn:mpeg:dash:schema:mpd:2011"
_NS = {"mpd": _DASH_NS}


def _strip_leading_junk(path: Path) -> bool:
    """Remove any stray bytes before the XML declaration in an .mpd and rewrite.
    A stray log line can leak into the file (we've seen "Make manifest.mpd"
    prepended), which makes it invalid XML — every ET.parse of a manifest goes
    through this first so a corrupt prefix self-heals instead of breaking
    packaging/serving. No-op (returns False) when the file is already clean or
    missing. Returns True if it healed the file."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    idx = data.find(b"<?xml")
    if idx < 0:
        idx = data.find(b"<MPD")  # no declaration but starts at the root element
    if idx > 0:
        path.write_bytes(data[idx:])
        return True
    return False


# ---------------------------------------------------------------------------
# Fragment byte-ranges → mpd (self-contained, no sidecar at serve time)
# ---------------------------------------------------------------------------

def write_fragmented_mpd(package_dir: Path) -> int:
    """Rewrite manifest.mpd IN PLACE at fragment granularity: expand each
    whole-segment <SegmentURL> into one per-fragment <SegmentURL media mediaRange>
    (standard DASH byte-range addressing into the same .m4s), with a matching
    per-fragment SegmentTimeline — each segment's duration split evenly across its
    fragments, the same way go-live derives them. This puts the fragment index IN
    the manifest, mirroring how the HLS m3u8 already carries EXT-X-PART, so nothing
    needs a `.byteranges` sidecar at serve time.

    One file, not two (#282). It used to write a second manifest_fragmented.mpd and
    leave manifest.mpd alone for A/B; the fragment form is a strict superset —
    segment membership regroups by @media, and a segment's duration is the sum of
    its fragments' — so shipping only it loses nothing, and go-live opens
    `manifest.mpd` and detects granularity from content rather than the filename.

    IDEMPOTENT: a manifest already at fragment granularity is left alone. Phases
    retry and resume, so this runs twice on the same directory routinely, and
    expanding an expanded manifest would produce fragments of fragments.

    Returns the number of representations expanded (0 if already fragmented).
    """
    package_dir = Path(package_dir)
    src = package_dir / "manifest.mpd"
    if not src.exists():
        cands = [p for p in package_dir.glob("*.mpd") if "_fragmented" not in p.name]
        if not cands:
            return 0
        src = cands[0]

    ET.register_namespace("", _DASH_NS)
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
    _strip_leading_junk(src)
    tree = ET.parse(src)
    root = tree.getroot()

    # Already expanded — @mediaRange is the marker, the same signal go-live uses
    # to tell the two shapes apart. Re-expanding would split each FRAGMENT into
    # sub-fragments and destroy the manifest.
    if root.find(".//mpd:SegmentURL[@mediaRange]", _NS) is not None:
        return 0

    n = 0
    for sl in root.findall(".//mpd:SegmentList", _NS):
        timeline = sl.find("mpd:SegmentTimeline", _NS)
        seg_urls = sl.findall("mpd:SegmentURL", _NS)
        if timeline is None or not seg_urls:
            continue
        # Flatten <S t d r> into a per-segment duration list.
        durs: list[int] = []
        for s in timeline.findall("mpd:S", _NS):
            try:
                d = int(s.get("d"))
            except (TypeError, ValueError):
                d = 0
            durs.extend([d] * (int(s.get("r", "0")) + 1))
        if len(durs) != len(seg_urls):
            continue  # can't map fragments to segment durations safely — skip rep

        new_durs: list[int] = []
        new_urls: list[tuple[str, str | None]] = []
        for i, surl in enumerate(seg_urls):
            media = surl.get("media") or ""
            seg_dur = durs[i]
            frags = _segment_fragments(package_dir / media) if media else None
            if frags:
                base, rem = divmod(seg_dur, len(frags))
                for k, fr in enumerate(frags):
                    fd = base + (1 if k < rem else 0)
                    off = int(fr.get("offset", 0))
                    end = off + int(fr.get("length", 0)) - 1
                    new_durs.append(max(fd, 1))
                    new_urls.append((media, f"{off}-{max(end, off)}"))
            else:
                new_durs.append(seg_dur)
                new_urls.append((media, None))

        # Rebuild in DASH order: Initialization, SegmentTimeline, SegmentURL*.
        for surl in seg_urls:
            sl.remove(surl)
        sl.remove(timeline)
        new_tl = ET.SubElement(sl, "SegmentTimeline")
        t = i = 0
        while i < len(new_durs):
            d = new_durs[i]
            r = 0
            while i + r + 1 < len(new_durs) and new_durs[i + r + 1] == d:
                r += 1
            s = ET.SubElement(new_tl, "S")
            s.set("t", str(t))
            s.set("d", str(d))
            if r:
                s.set("r", str(r))
            t += d * (r + 1)
            i += r + 1
        for media, mr in new_urls:
            u = ET.SubElement(sl, "SegmentURL")
            u.set("media", media)
            if mr:
                u.set("mediaRange", mr)
        n += 1

    if n == 0:
        return 0
    dom = minidom.parseString(ET.tostring(root, encoding="UTF-8"))
    pretty = dom.toprettyxml(indent="  ", encoding="UTF-8").decode("UTF-8")
    body = "\n".join(l for l in pretty.split("\n") if l.strip()) + "\n"
    # Write-then-rename: manifest.mpd is the file every consumer opens, and a
    # crash mid-write would leave a truncated one that parses as "no segments"
    # rather than as an error.
    tmp = src.with_name(src.name + ".tmp")
    tmp.write_text(body, encoding="UTF-8")
    tmp.replace(src)
    print(f"Wrote {src.name}: {n} representation(s) expanded to per-fragment mediaRange")
    return n


# ---------------------------------------------------------------------------
# SegmentTemplate → SegmentList
# ---------------------------------------------------------------------------

def convert_segmentlist(manifest_path: Path, backup: bool = True,
                        backup_dir: Path | None = None) -> None:
    """Rewrite a SegmentTemplate MPD in place as SegmentList.

    `backup` keeps Shaka's own output — the input to this rewrite — so a manifest
    that looks wrong can be traced to the packager or to us. It is a debugging
    artifact that nothing reads, so it goes to the TEMP dir, not beside the
    manifest: it used to ship in every output directory, where it was a third
    manifest-shaped file in a tree that is meant to hold one.

    Named after the package directory, so concurrent codecs of the same title
    don't overwrite each other's. Best-effort — failing to keep a debugging copy
    must never fail a package.
    """
    manifest_path = Path(manifest_path)
    output_dir = manifest_path.parent

    if not manifest_path.exists():
        print(f"Error: Manifest not found: {manifest_path}")
        sys.exit(1)

    if backup:
        dest_dir = Path(backup_dir) if backup_dir else Path(tempfile.gettempdir())
        backup_path = dest_dir / f"{output_dir.name}.mpd.template.bak"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(manifest_path, backup_path)
            print(f"Backup created: {backup_path}")
        except OSError as e:
            print(f"Backup skipped ({e})")

    ET.register_namespace("", _DASH_NS)
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")

    _strip_leading_junk(manifest_path)
    try:
        tree = ET.parse(manifest_path)
    except ET.ParseError as e:
        print(f"Error parsing manifest: {e}")
        sys.exit(1)
    root = tree.getroot()

    normalized = _normalize_video_dimensions(root)
    conversions = _rewrite_representations(root, output_dir)

    if conversions == 0:
        print("No SegmentTemplate elements found to convert")
        return

    xml_str = ET.tostring(root, encoding="UTF-8")
    dom = minidom.parseString(xml_str)
    pretty_xml = dom.toprettyxml(indent="  ", encoding="UTF-8").decode("UTF-8")
    lines = [line for line in pretty_xml.split("\n") if line.strip()]

    manifest_path.write_text("\n".join(lines) + "\n", encoding="UTF-8")

    if normalized > 0:
        print(f"Normalized {normalized} representation(s) (copied dimensions from AdaptationSet)")
    print(f"Converted {conversions} representation(s) to SegmentList")
    print(f"Updated manifest: {manifest_path}")


def _normalize_video_dimensions(root: ET.Element) -> int:
    """DASH-IOP allows dimensions at AdaptationSet level in single-variant
    manifests; copy them down to each video Representation for uniformity."""
    count = 0
    for adaptation_set in root.findall(".//mpd:AdaptationSet", _NS):
        as_width = adaptation_set.get("width")
        as_height = adaptation_set.get("height")
        if not (as_width or as_height):
            continue

        for rep in adaptation_set.findall("mpd:Representation", _NS):
            mime_type = rep.get("mimeType", "")
            content_type = adaptation_set.get("contentType", "")
            if not ("video" in mime_type or content_type == "video"):
                continue

            changed = False
            if not rep.get("width") and as_width:
                rep.set("width", as_width)
                changed = True
                print(f"  Normalized width for Representation {rep.get('id', 'unknown')}: {as_width}")
            if not rep.get("height") and as_height:
                rep.set("height", as_height)
                if not changed:
                    changed = True
                print(f"  Normalized height for Representation {rep.get('id', 'unknown')}: {as_height}")
            if changed:
                count += 1
    return count


def _rewrite_representations(root: ET.Element, output_dir: Path) -> int:
    count = 0
    for rep in root.findall(".//mpd:Representation", _NS):
        seg_template = rep.find("mpd:SegmentTemplate", _NS)
        if seg_template is None:
            continue

        init_seg = seg_template.get("initialization")
        media_template = seg_template.get("media")
        timescale = seg_template.get("timescale")
        duration = seg_template.get("duration")
        seg_timeline = seg_template.find("mpd:SegmentTimeline", _NS)

        if not media_template:
            continue

        pattern = (
            media_template
            .replace("$Number%05d$", "*")
            .replace("$Number$", "*")
            .replace("$RepresentationID$", rep.get("id", "*"))
        )
        segment_files = sorted(glob.glob(str(output_dir / pattern)))
        if not segment_files:
            print(f"  Warning: No segments found for pattern: {pattern}")
            continue

        print(f"  Converting {rep.get('id', 'unknown')}: {len(segment_files)} segments")

        rep.remove(seg_template)
        seg_list = ET.SubElement(rep, "SegmentList")
        if timescale:
            seg_list.set("timescale", timescale)
        if duration:
            seg_list.set("duration", duration)

        if init_seg:
            ET.SubElement(seg_list, "Initialization").set("sourceURL", init_seg)
        if seg_timeline is not None:
            seg_list.append(seg_timeline)

        for seg_file in segment_files:
            seg_path = Path(seg_file).relative_to(output_dir)
            ET.SubElement(seg_list, "SegmentURL").set("media", str(seg_path))

        count += 1
    return count


# ---------------------------------------------------------------------------
# DASH package → HLS master + variants
# ---------------------------------------------------------------------------

def hls_from_dash(package_dir: Path) -> bool:
    package_dir = Path(package_dir)
    manifest_path = package_dir / "manifest.mpd"
    if not manifest_path.exists():
        print(f"No manifest.mpd found in {package_dir}")
        return False

    print(f"Processing: {package_dir.name}")
    dash_info = _parse_dash(manifest_path)
    print(f"  Found {len(dash_info['representations'])} representations")

    master_path = _write_master(dash_info, package_dir)
    print(f"  Created: {master_path.name}")

    for playlist in sorted(package_dir.glob("*/playlist.m3u8")):
        print(f"  Created: {playlist.relative_to(package_dir)}")
    return True


def _parse_dash(mpd_path: Path) -> dict[str, Any]:
    ET.register_namespace("", _DASH_NS)
    _strip_leading_junk(mpd_path)
    root = ET.parse(mpd_path).getroot()

    duration_attr = root.get("mediaPresentationDuration") or ""
    if duration_attr.startswith("PT") and duration_attr.endswith("S"):
        duration_sec = float(duration_attr[2:-1])
    else:
        duration_sec = 0.0

    representations: list[dict[str, Any]] = []
    for adaptation_set in root.findall(".//mpd:AdaptationSet", _NS):
        content_type = adaptation_set.get("contentType", "video")
        as_width = adaptation_set.get("width")
        as_height = adaptation_set.get("height")
        # DASH frameRate is either a float ("30") or a rational ("30000/1001").
        as_framerate = adaptation_set.get("frameRate")

        for rep in adaptation_set.findall(".//mpd:Representation", _NS):
            seg_list = rep.find("mpd:SegmentList", _NS)
            if seg_list is None:
                continue

            representations.append({
                "id": rep.get("id"),
                "content_type": content_type,
                "bandwidth": int(rep.get("bandwidth", 0)),
                "codecs": rep.get("codecs", ""),
                "mime_type": rep.get("mimeType", ""),
                "width": rep.get("width") or as_width,
                "height": rep.get("height") or as_height,
                "frame_rate": rep.get("frameRate") or as_framerate,
                "init_segment": (
                    seg_list.find("mpd:Initialization", _NS).get("sourceURL")
                    if seg_list.find("mpd:Initialization", _NS) is not None
                    else None
                ),
                "segments": _extract_segments(seg_list),
                "timescale": int(seg_list.get("timescale", 1)),
            })
    return {"duration": duration_sec, "representations": representations}


def _parse_frame_rate(fr: str | None) -> float | None:
    """Convert a DASH frameRate attribute to a float.

    Accepts "30", "30000/1001", or anything parseable as a number.
    Returns None if the input is missing or malformed.
    """
    if not fr:
        return None
    try:
        if "/" in fr:
            num, _, den = fr.partition("/")
            n, d = float(num), float(den)
            return n / d if d else None
        return float(fr)
    except ValueError:
        return None


def _extract_segments(seg_list: ET.Element) -> list[dict[str, Any]]:
    """Whole SEGMENTS from a <SegmentList>, whichever granularity it is written at.

    Since #282 manifest.mpd is written at FRAGMENT granularity — many
    <SegmentURL @media @mediaRange> per .m4s — so entries are collapsed back by
    @media, summing the members' timeline durations, exactly as go-live does at
    load time. A segment-granularity manifest (library content, or this same run
    before the expansion) has one entry per file and collapses to itself.

    Doing it here rather than relying on phase order is deliberate: HLS
    generation and the manifest rewrite both run over the same directory, phases
    retry, and an ordering rule is a thing to get wrong later. This way neither
    order can produce a playlist with one EXTINF per fragment.
    """
    timescale = int(seg_list.get("timescale", 1))
    durations: list[int] = []

    timeline = seg_list.find("mpd:SegmentTimeline", _NS)
    if timeline is not None:
        for s in timeline.findall("mpd:S", _NS):
            d = int(s.get("d", 0))
            durations.extend([d] * (int(s.get("r", 0)) + 1))

    urls = [u.get("media") for u in seg_list.findall("mpd:SegmentURL", _NS) if u.get("media")]

    segments: list[dict[str, Any]] = []
    by_url: dict[str, int] = {}
    for i, url in enumerate(urls):
        d = durations[i] if i < len(durations) else 0
        if url in by_url:                      # another fragment of the same file
            seg = segments[by_url[url]]
            seg["timeline_duration"] += d
            seg["duration"] = seg["timeline_duration"] / timescale
            continue
        by_url[url] = len(segments)
        segments.append({
            "url": url,
            "timeline_duration": d,
            "duration": (d / timescale) if d else 4.0,
        })
    # A timeline with no URLs at all (shouldn't happen) still yields durations.
    if not urls and durations:
        segments = [{"duration": d / timescale, "timeline_duration": d} for d in durations]
    return segments


def _segment_fragments(segment_path: Path) -> list[dict[str, Any]] | None:
    """This segment's fragments (offset/length/independent), or None.

    Read from the `.byteranges` sidecar when one exists — library content
    predating #282 still has them — and otherwise parsed straight out of the
    .m4s. Since #282 the sidecars are no longer written, so the parse is the
    normal path; the sidecar branch exists only so an old package still
    repackages identically.

    The fMP4 walk is the same one that produced the sidecars in the first place
    (fragments.parse_segment), so the two branches cannot disagree.
    """
    byteranges_path = Path(str(segment_path) + ".byteranges")
    if byteranges_path.exists():
        try:
            return json.loads(byteranges_path.read_text()).get("fragments", [])
        except (json.JSONDecodeError, OSError):
            pass  # fall through to parsing the media
    try:
        from infinite_streaming_encoder.fragments import parse_segment
        track_type = "audio" if "audio" in segment_path.parts else "video"
        return parse_segment(segment_path, track_type) or None
    except Exception:  # noqa: BLE001 — a corrupt segment must not fail the package
        return None


_RES_BY_WIDTH = {3840: "2160p", 2560: "1440p", 1920: "1080p", 1280: "720p", 960: "540p", 640: "360p"}


def _resolution_name(width: str | None, height: str | None) -> str:
    if height:
        return f"{height}p"
    if width and int(width) in _RES_BY_WIDTH:
        return _RES_BY_WIDTH[int(width)]
    return "unknown"


def _write_variant_playlist(rep: dict[str, Any], output_path: Path, package_dir: Path) -> None:
    segments = rep["segments"]
    if not segments:
        return

    target_duration = int(max(seg.get("duration", 4.0) for seg in segments)) + 1
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:10",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        "#EXT-X-PART-INF:PART-TARGET=1.0",
    ]
    if rep["init_segment"]:
        lines.append(f'#EXT-X-MAP:URI="{Path(rep["init_segment"]).name}"')

    for seg in segments:
        url = seg.get("url", "")
        duration = seg.get("duration", 4.0)
        if not url:
            continue
        seg_filename = Path(url).name
        fragments = _segment_fragments(package_dir / url)

        if fragments:
            fragment_duration = duration / len(fragments)
            for fragment in fragments:
                independent = "YES" if fragment.get("independent", False) else "NO"
                byterange = f"{fragment['length']}@{fragment['offset']}"
                lines.append(
                    f"#EXT-X-PART:DURATION={fragment_duration:.6f},"
                    f'URI="{seg_filename}",BYTERANGE="{byterange}",INDEPENDENT={independent}'
                )
        lines.append(f"#EXTINF:{duration:.6f},")
        lines.append(seg_filename)

    lines.append("#EXT-X-ENDLIST")
    output_path.write_text("\n".join(lines) + "\n")


def _average_bandwidth(rep: dict[str, Any], package_dir: Path) -> int:
    total_duration = 0.0
    total_bytes = 0

    init_segment = rep.get("init_segment")
    if init_segment and (package_dir / init_segment).exists():
        total_bytes += (package_dir / init_segment).stat().st_size

    for seg in rep.get("segments", []):
        duration = float(seg.get("duration", 0.0) or 0.0)
        url = seg.get("url")
        if duration <= 0 or not url:
            continue
        seg_path = package_dir / url
        if seg_path.exists():
            total_duration += duration
            total_bytes += seg_path.stat().st_size

    if total_duration <= 0 or total_bytes <= 0:
        return 0
    return int(round((total_bytes * 8.0) / total_duration))


def _write_master(dash_info: dict[str, Any], output_dir: Path) -> Path:
    reps = dash_info["representations"]
    video_reps = [r for r in reps if r["content_type"] == "video"]
    audio_reps = [r for r in reps if r["content_type"] == "audio"]

    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]

    audio_avg = 0
    audio_peak = 0
    if audio_reps:
        lines += ["", "# Audio"]
        audio_rep = audio_reps[0]
        audio_dir = output_dir / "audio"
        audio_dir.mkdir(exist_ok=True)
        _write_variant_playlist(audio_rep, audio_dir / "playlist.m3u8", output_dir)
        lines.append(
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",LANGUAGE="en",'
            'AUTOSELECT=YES,DEFAULT=YES,URI="audio/playlist.m3u8"'
        )
        audio_avg = _average_bandwidth(audio_rep, output_dir)
        audio_peak = max(int(r.get("bandwidth", 0) or 0) for r in audio_reps) or audio_avg

    lines += ["", "# Video variants"]

    # Audio codec string for the CODECS attribute. When an AUDIO group is
    # referenced from EXT-X-STREAM-INF, HLS requires CODECS to list every
    # codec that appears in the combined rendition — not just the video
    # one. Players (especially Safari/AVFoundation) decode the audio
    # track using this string; omitting it has made players misinterpret
    # audio bytes as video NAL units in practice.
    audio_codec = audio_reps[0].get("codecs") if audio_reps else ""

    for rep in video_reps:
        res_name = _resolution_name(rep["width"], rep["height"])
        variant_dir = output_dir / res_name.lower()
        variant_dir.mkdir(exist_ok=True)
        _write_variant_playlist(rep, variant_dir / "playlist.m3u8", output_dir)

        video_bw = int(rep.get("bandwidth", 0) or 0)
        bandwidth = video_bw + audio_peak
        if bandwidth <= 0:
            bandwidth = _average_bandwidth(rep, output_dir) + audio_avg
        average = _average_bandwidth(rep, output_dir) + audio_avg

        codecs = rep["codecs"]
        if audio_codec:
            codecs = f"{codecs},{audio_codec}"

        stream_info = f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth}"
        if average > 0:
            stream_info += f",AVERAGE-BANDWIDTH={average}"
        if rep["width"] and rep["height"]:
            stream_info += f",RESOLUTION={rep['width']}x{rep['height']}"
        stream_info += f',CODECS="{codecs}"'
        if audio_reps:
            stream_info += ',AUDIO="audio"'

        fps = _parse_frame_rate(rep.get("frame_rate"))
        if fps is not None:
            stream_info += f",FRAME-RATE={fps:.3f}"

        lines.append(stream_info)
        lines.append(f"{res_name.lower()}/playlist.m3u8")

    master_path = output_dir / "master.m3u8"
    master_path.write_text("\n".join(lines) + "\n")
    return master_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(prog="infinite_streaming_encoder.manifests", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_convert = sub.add_parser("convert-segmentlist",
                               help="rewrite SegmentTemplate MPD as SegmentList")
    p_convert.add_argument("manifest", type=Path)
    p_convert.add_argument("--no-backup", action="store_true")

    p_hls = sub.add_parser("hls-from-dash",
                           help="generate HLS master/variant playlists from a DASH package")
    p_hls.add_argument("package_dir", type=Path)

    args = parser.parse_args()
    if args.cmd == "convert-segmentlist":
        convert_segmentlist(args.manifest, backup=not args.no_backup)
    elif args.cmd == "hls-from-dash":
        hls_from_dash(args.package_dir)


if __name__ == "__main__":
    main()
