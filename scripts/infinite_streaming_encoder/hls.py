"""Phases 6 & 7 — fragment sidecars, fMP4 HLS playlists, TS HLS variant.

The DASH package produced by infinite_streaming_encoder.packager already has all the
media bytes we need. This module adds:

- Phase 6: walk every `.m4s` in the package and write its
  `.byteranges` JSON sidecar (offset/length/independent per fragment).
  LL-HLS EXT-X-PART tags downstream depend on these.

- Phase 7: generate HLS master + per-variant playlists from the DASH
  package (fMP4 segments). Reuses `infinite_streaming_encoder.manifests.hls_from_dash`.

- Phase 7b (optional): produce a parallel MPEG-TS HLS package from
  the same variant MP4s, for older clients. Skipped unless the caller
  asks for `--hls-format ts|both`.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from infinite_streaming_encoder.fragments import parse_segment
from infinite_streaming_encoder.manifests import hls_from_dash as _hls_from_dash
import json


# ---------------------------------------------------------------------------
# Phase 6 — .byteranges sidecars
# ---------------------------------------------------------------------------

def generate_byteranges_sidecars(package_dir: Path) -> int:
    """Walk package_dir for *.m4s, write <segment>.byteranges next to each.

    Returns the count of sidecars written. Audio tracks are detected
    by directory name ("audio/"); everything else is treated as video.
    Silently skips segments with no moof boxes (ffmpeg's -t 0 init
    extractions, etc.).
    """
    package_dir = Path(package_dir)
    count = 0
    for segment in sorted(package_dir.rglob("*.m4s")):
        track_type = "audio" if "audio" in segment.parts else "video"
        try:
            fragments = parse_segment(segment, track_type)
        except Exception:
            # Defensive: bail on a single corrupt segment, don't fail
            # the whole package. Matches bash's "warn, keep going".
            continue
        if not fragments:
            continue
        sidecar = segment.with_suffix(segment.suffix + ".byteranges")
        sidecar.write_text(json.dumps({"fragments": fragments}, indent=2))
        count += 1
    return count


# ---------------------------------------------------------------------------
# Phase 7 — fMP4 HLS playlists
# ---------------------------------------------------------------------------

def generate_fmp4_hls(package_dir: Path) -> bool:
    """Generate master.m3u8 + per-variant playlists in `package_dir`.

    Assumes the .byteranges sidecars already exist (Phase 6 runs
    first). Playlists embed EXT-X-PART tags per fragment for LL-HLS.
    """
    return _hls_from_dash(package_dir)


# ---------------------------------------------------------------------------
# Phase 7b — TS HLS variant (optional, for legacy clients)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TsHlsSpec:
    tmp_dir: Path              # holds {codec}_{label}.mp4 + audio.mp4
    ts_output_dir: Path        # top-level dir for the TS package
    codec: str
    labels: tuple[str, ...]    # rung labels ("1080p", "1080p_1", ...)
    segment_duration_s: float
    include_audio: bool


class HlsError(RuntimeError):
    pass


def _run_ffmpeg_hls(input_file: Path, out_dir: Path, segment_duration: float,
                    reencode_audio: bool = False) -> None:
    """Produce TS segments + playlist.m3u8 from one MP4 via ffmpeg's HLS muxer."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-i", str(input_file)]
    if reencode_audio:
        # Re-encode audio to AAC so segment boundaries align exactly;
        # -c copy can drift on fractional-frame sources.
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    else:
        cmd += ["-c", "copy"]
    cmd += [
        "-f", "hls",
        "-hls_time", str(segment_duration),
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", str(out_dir / "segment_%03d.ts"),
        "-hls_playlist_type", "vod",
    ]
    if reencode_audio:
        cmd += ["-hls_flags", "independent_segments"]
    cmd += [
        str(out_dir / "playlist.m3u8"),
        "-loglevel", "warning", "-stats",
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise HlsError(f"ffmpeg HLS muxer failed for {input_file} ({e.returncode})") from e


def generate_ts_hls(spec: TsHlsSpec) -> Path:
    """Produce a complete TS HLS package mirroring the fMP4 tiers.

    Layout:
        <ts_output_dir>/
            <res>/segment_NNN.ts, <res>/playlist.m3u8   (per tier)
            audio/segment_NNN.ts, audio/playlist.m3u8   (if audio)
            master.m3u8

    Master playlist writing is deferred to the caller for now — bash's
    TS master had its own peculiarities (different CODECS strings) and
    matching those exactly requires probing the encoded MP4s. That's
    a later-session polish; for now this returns the dir and the
    caller owns master.m3u8.
    """
    spec.ts_output_dir.mkdir(parents=True, exist_ok=True)

    for label in spec.labels:
        source_mp4 = spec.tmp_dir / f"{spec.codec}_{label}.mp4"
        if not source_mp4.is_file():
            continue
        _run_ffmpeg_hls(
            source_mp4, spec.ts_output_dir / label, spec.segment_duration_s,
        )

    if spec.include_audio:
        audio_mp4 = spec.tmp_dir / "audio.mp4"
        if audio_mp4.is_file():
            _run_ffmpeg_hls(
                audio_mp4, spec.ts_output_dir / "audio", spec.segment_duration_s,
                reencode_audio=True,
            )

    return spec.ts_output_dir
