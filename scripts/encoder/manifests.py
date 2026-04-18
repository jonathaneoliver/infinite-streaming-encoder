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
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_DASH_NS = "urn:mpeg:dash:schema:mpd:2011"
_NS = {"mpd": _DASH_NS}


# ---------------------------------------------------------------------------
# SegmentTemplate → SegmentList
# ---------------------------------------------------------------------------

def convert_segmentlist(manifest_path: Path, backup: bool = True) -> None:
    manifest_path = Path(manifest_path)
    output_dir = manifest_path.parent

    if not manifest_path.exists():
        print(f"Error: Manifest not found: {manifest_path}")
        sys.exit(1)

    if backup:
        backup_path = manifest_path.with_suffix(".mpd.template.bak")
        manifest_path.replace(backup_path)
        print(f"Backup created: {backup_path}")
        shutil.copy(backup_path, manifest_path)

    ET.register_namespace("", _DASH_NS)
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")

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
                "init_segment": (
                    seg_list.find("mpd:Initialization", _NS).get("sourceURL")
                    if seg_list.find("mpd:Initialization", _NS) is not None
                    else None
                ),
                "segments": _extract_segments(seg_list),
                "timescale": int(seg_list.get("timescale", 1)),
            })
    return {"duration": duration_sec, "representations": representations}


def _extract_segments(seg_list: ET.Element) -> list[dict[str, Any]]:
    timescale = int(seg_list.get("timescale", 1))
    segments: list[dict[str, Any]] = []

    timeline = seg_list.find("mpd:SegmentTimeline", _NS)
    if timeline is not None:
        for s in timeline.findall("mpd:S", _NS):
            t = int(s.get("t", 0))
            d = int(s.get("d", 0))
            r = int(s.get("r", 0))
            duration_s = d / timescale
            for _ in range(r + 1):
                segments.append({"duration": duration_s, "timeline_duration": d})

    urls = [u.get("media") for u in seg_list.findall("mpd:SegmentURL", _NS) if u.get("media")]
    for i, url in enumerate(urls):
        if i < len(segments):
            segments[i]["url"] = url
        else:
            segments.append({"url": url, "duration": 4.0})
    return segments


def _load_byteranges(segment_path: Path) -> list[dict[str, Any]] | None:
    byteranges_path = Path(str(segment_path) + ".byteranges")
    if not byteranges_path.exists():
        return None
    try:
        return json.loads(byteranges_path.read_text()).get("fragments", [])
    except (json.JSONDecodeError, OSError):
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
        fragments = _load_byteranges(package_dir / url)

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

        stream_info = f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth}"
        if average > 0:
            stream_info += f",AVERAGE-BANDWIDTH={average}"
        if rep["width"] and rep["height"]:
            stream_info += f",RESOLUTION={rep['width']}x{rep['height']}"
        stream_info += f',CODECS="{rep["codecs"]}"'
        if audio_reps:
            stream_info += ',AUDIO="audio"'
        stream_info += ",FRAME-RATE=25.000"

        lines.append(stream_info)
        lines.append(f"{res_name.lower()}/playlist.m3u8")

    master_path = output_dir / "master.m3u8"
    master_path.write_text("\n".join(lines) + "\n")
    return master_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(prog="encoder.manifests", description=__doc__)
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
