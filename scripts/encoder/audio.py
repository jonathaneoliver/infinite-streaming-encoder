"""Phase 4 — extract (and optionally transcode) audio from the mezzanine.

Produces a fragmented MP4 at `<tmp>/audio.mp4`. Shaka Packager later
reads this alongside the video variant MP4s to package them together.

Codec handling:
- AAC → stream copy when possible (no padding)
- Everything else (MP3 / Opus / Vorbis / AC-3 / E-AC-3 / DTS / TrueHD /
  PCM / ...) → transcode to AAC 192k/48kHz. Shaka Packager can't reliably
  multiplex non-AAC audio into the fMP4 our LL-HLS/DASH ladder needs — MP3
  fails outright ("Unsupported audio codec"), and Opus/Vorbis-in-fMP4 is
  not dependable for HLS. AAC is the universally-safe target.
- Any codec + padding required → always transcode to AAC
  (can't apply filters with -c copy)
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from encoder.ffprobe import probe
from encoder.progress import run_ffmpeg_with_progress


# The only codec we stream-copy into fMP4. Everything else transcodes to AAC:
# MP3-in-fMP4 fails in Shaka ("Unsupported audio codec"), and Opus/Vorbis in
# fMP4 aren't dependable for our LL-HLS/DASH ladder. AAC is universally safe.
_SHAKA_COMPATIBLE = frozenset({"aac"})

# Kept for clarity; _needs_transcode already transcodes anything not in
# _SHAKA_COMPATIBLE, so these are a documented subset of "definitely not AAC".
_INCOMPATIBLE_PREFIXES = ("pcm",)
_INCOMPATIBLE_NAMES = frozenset({"ac3", "eac3", "dts", "truehd"})

# Default AAC encode params — match bash (192k, 48kHz).
_AAC_BITRATE = "192k"
_AAC_SAMPLE_RATE = "48000"
_FRAG_DURATION_US = 1_000_000


@dataclass(frozen=True)
class AudioSpec:
    mezzanine_path: Path
    output_path: Path        # usually $tmp/audio.mp4
    padding_s: float = 0.0   # 0 = no apad filter


class AudioError(RuntimeError):
    pass


def detect_source_codec(mezzanine_path: Path) -> str:
    """Return the first audio stream's codec_name (e.g. "aac")."""
    info = probe(mezzanine_path)
    if not info.has_audio:
        raise AudioError(f"no audio stream in mezzanine: {mezzanine_path}")
    # ffprobe wrapper only exposes video_codec — reach for audio via direct call.
    import json
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            str(mezzanine_path),
        ],
        capture_output=True, check=True, text=True,
    )
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise AudioError(f"no audio stream in mezzanine: {mezzanine_path}")
    return streams[0].get("codec_name", "").lower()


def _needs_transcode(codec: str) -> bool:
    if any(codec.startswith(p) for p in _INCOMPATIBLE_PREFIXES):
        return True
    if codec in _INCOMPATIBLE_NAMES:
        return True
    return codec not in _SHAKA_COMPATIBLE


def build_ffmpeg_cmd(spec: AudioSpec, source_codec: str) -> list[str]:
    transcode = _needs_transcode(source_codec) or spec.padding_s > 0

    cmd = [
        "ffmpeg", "-y",
        "-i", str(spec.mezzanine_path),
        "-vn",
    ]

    if transcode:
        cmd += ["-c:a", "aac", "-b:a", _AAC_BITRATE, "-ar", _AAC_SAMPLE_RATE]
        if spec.padding_s > 0:
            cmd += ["-af", f"apad=pad_dur={spec.padding_s}"]
    else:
        cmd += ["-c:a", "copy"]

    cmd += [
        "-movflags", "empty_moov+default_base_moof",
        "-frag_duration", str(_FRAG_DURATION_US),
        str(spec.output_path),
        "-loglevel", "error", "-stats",
    ]
    return cmd


def create_audio(spec: AudioSpec, stage_key: str = "audio",
                 duration_s: float = 0.0) -> Path:
    """Run the audio extraction/transcode. Returns the output path."""
    spec.output_path.parent.mkdir(parents=True, exist_ok=True)

    source_codec = detect_source_codec(spec.mezzanine_path)
    cmd = build_ffmpeg_cmd(spec, source_codec)
    try:
        if duration_s > 0:
            run_ffmpeg_with_progress(cmd, duration_s, stage_key)
        else:
            subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise AudioError(f"audio extraction failed ({e.returncode})") from e

    if not spec.output_path.is_file() or spec.output_path.stat().st_size == 0:
        raise AudioError(f"audio not produced: {spec.output_path}")
    # Completion marker so resume can distinguish a finished audio.mp4
    # from one rsynced mid-write on spot interrupt.
    size = spec.output_path.stat().st_size
    marker = spec.output_path.with_suffix(spec.output_path.suffix + ".done")
    tmp = marker.with_suffix(marker.suffix + ".tmp")
    tmp.write_text(f"{size}\n")
    tmp.rename(marker)
    return spec.output_path
