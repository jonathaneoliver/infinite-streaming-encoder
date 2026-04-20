#!/usr/bin/env python3
"""Parse fMP4 fragment byteranges for LL-HLS EXT-X-PART manifests.

For each `moof` box in a segment, emits the (offset, length, independent)
triple the HLS manifest generator consumes. `independent` is true when
the fragment's first sample is a sync sample (IDR/keyframe for video;
always true for audio).

Writes a sidecar `<segment>.byteranges` JSON file:

    {"fragments": [{"offset": N, "length": N, "independent": bool}, ...]}

CLI (matches the --track-type/--segment-duration/--gop-duration surface
the bash orchestrator expects, even though we don't need most of it —
the durations are consumed by the caller, not here):

    python3 -m encoder.fragments \\
        --track-type <video|audio> \\
        --segment-duration <s> \\
        --gop-duration <s> \\
        <segment.m4s>
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

# ISO/IEC 14496-12 sample_flags layout — bit 16 (0-indexed LSB) is
# `sample_is_non_sync_sample`: 0 = sync sample (IDR), 1 = non-sync.
_NON_SYNC_MASK = 1 << 16

# tfhd flag bits (24-bit flags field).
_TFHD_BASE_DATA_OFFSET = 0x000001
_TFHD_SAMPLE_DESCRIPTION_INDEX = 0x000002
_TFHD_DEFAULT_SAMPLE_DURATION = 0x000008
_TFHD_DEFAULT_SAMPLE_SIZE = 0x000010
_TFHD_DEFAULT_SAMPLE_FLAGS = 0x000020

# trun flag bits.
_TRUN_DATA_OFFSET_PRESENT = 0x000001
_TRUN_FIRST_SAMPLE_FLAGS_PRESENT = 0x000004
_TRUN_SAMPLE_DURATION_PRESENT = 0x000100
_TRUN_SAMPLE_SIZE_PRESENT = 0x000200
_TRUN_SAMPLE_FLAGS_PRESENT = 0x000400


def _read_box_header(data: bytes, pos: int) -> tuple[int, str, int]:
    """Return (box_size, box_type, header_size) at `pos`.

    Sizes are as read from the file; a box with size 1 has a 64-bit
    extended size in the next 8 bytes, yielding a 16-byte header.
    """
    if pos + 8 > len(data):
        raise ValueError(f"truncated box header at offset {pos}")
    size = struct.unpack(">I", data[pos:pos + 4])[0]
    btype = data[pos + 4:pos + 8].decode("ascii", errors="replace")
    if size == 1:
        size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        return size, btype, 16
    if size == 0:
        # Box extends to end of file.
        return len(data) - pos, btype, 8
    return size, btype, 8


def _detect_independent(data: bytes, moof_start: int, moof_end: int, is_video: bool) -> bool:
    """Return True if the first sample of this moof is a sync sample.

    Audio is always treated as independent (no temporal prediction for
    the codecs we encode). Video inspects `tfhd.default_sample_flags`
    and `trun.first_sample_flags` to decide.
    """
    if not is_video:
        return True

    default_sample_flags: int | None = None
    first_sample_flags: int | None = None

    pos = moof_start + 8  # skip `moof` header (8 bytes; moof is never size=1)
    while pos < moof_end:
        size, btype, _ = _read_box_header(data, pos)
        if btype == "traf":
            traf_end = pos + size
            tpos = pos + 8
            while tpos < traf_end:
                t_size, t_type, _ = _read_box_header(data, tpos)
                if t_type == "tfhd":
                    # box header (8) + version(1) + flags(3) + track_ID(4) = 16
                    version_flags = struct.unpack(">I", data[tpos + 8:tpos + 12])[0]
                    flags = version_flags & 0xFFFFFF
                    fpos = tpos + 16  # after track_ID
                    if flags & _TFHD_BASE_DATA_OFFSET:
                        fpos += 8
                    if flags & _TFHD_SAMPLE_DESCRIPTION_INDEX:
                        fpos += 4
                    if flags & _TFHD_DEFAULT_SAMPLE_DURATION:
                        fpos += 4
                    if flags & _TFHD_DEFAULT_SAMPLE_SIZE:
                        fpos += 4
                    if flags & _TFHD_DEFAULT_SAMPLE_FLAGS:
                        default_sample_flags = struct.unpack(">I", data[fpos:fpos + 4])[0]
                elif t_type == "trun":
                    version_flags = struct.unpack(">I", data[tpos + 8:tpos + 12])[0]
                    flags = version_flags & 0xFFFFFF
                    fpos = tpos + 16  # past sample_count
                    if flags & _TRUN_DATA_OFFSET_PRESENT:
                        fpos += 4
                    if flags & _TRUN_FIRST_SAMPLE_FLAGS_PRESENT:
                        first_sample_flags = struct.unpack(">I", data[fpos:fpos + 4])[0]
                        fpos += 4
                    # If per-sample flags are present (ffmpeg's default for
                    # fMP4 output), the first sample's flags come after any
                    # preceding per-sample fields within its sample record.
                    elif flags & _TRUN_SAMPLE_FLAGS_PRESENT:
                        sample_offset = 0
                        if flags & _TRUN_SAMPLE_DURATION_PRESENT:
                            sample_offset += 4
                        if flags & _TRUN_SAMPLE_SIZE_PRESENT:
                            sample_offset += 4
                        first_sample_flags = struct.unpack(
                            ">I",
                            data[fpos + sample_offset:fpos + sample_offset + 4],
                        )[0]
                tpos += t_size
        pos += size

    effective = first_sample_flags if first_sample_flags is not None else default_sample_flags
    if effective is None:
        # No flag info anywhere — be conservative and call it non-independent.
        # (ffmpeg's -frag_keyframe output always sets one of these.)
        return False
    return (effective & _NON_SYNC_MASK) == 0


def parse_segment(path: Path, track_type: str) -> list[dict]:
    data = path.read_bytes()
    is_video = track_type == "video"
    fragments: list[dict] = []
    pos = 0
    while pos < len(data):
        size, btype, _ = _read_box_header(data, pos)
        if btype == "moof":
            moof_start = pos
            moof_end = pos + size

            # Fragment extends through the following `mdat` (or to EOF).
            total_length = size
            next_pos = moof_end
            if next_pos < len(data):
                m_size, m_type, _ = _read_box_header(data, next_pos)
                if m_type == "mdat":
                    total_length = (next_pos + m_size) - moof_start

            fragments.append({
                "offset": moof_start,
                "length": total_length,
                "independent": _detect_independent(data, moof_start, moof_end, is_video),
            })
            pos = moof_start + total_length
        else:
            pos += size
    return fragments


def main() -> None:
    parser = argparse.ArgumentParser(prog="encoder.fragments", description=__doc__)
    parser.add_argument("--track-type", choices=("video", "audio"), required=True)
    parser.add_argument("--segment-duration", type=float, default=0.0,
                        help="consumed upstream; ignored here")
    parser.add_argument("--gop-duration", type=float, default=0.0,
                        help="consumed upstream; ignored here")
    parser.add_argument("segment", type=Path)
    args = parser.parse_args()

    if not args.segment.exists():
        print(f"Error: segment not found: {args.segment}", file=sys.stderr)
        sys.exit(1)

    fragments = parse_segment(args.segment, args.track_type)
    if not fragments:
        print(f"Error: no moof boxes found in {args.segment}", file=sys.stderr)
        sys.exit(2)

    sidecar = args.segment.with_suffix(args.segment.suffix + ".byteranges")
    sidecar.write_text(json.dumps({"fragments": fragments}, indent=2))


if __name__ == "__main__":
    main()
