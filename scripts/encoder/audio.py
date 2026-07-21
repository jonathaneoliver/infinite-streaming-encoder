"""Phase 4 — extract + transcode audio from the mezzanine.

Produces a fragmented MP4 at `<tmp>/audio.mp4`. Shaka Packager later
reads this alongside the video variant MP4s to package them together.

Audio is ALWAYS transcoded to AAC-LC 192k stereo (2-channel) 48kHz — a
single canonical output regardless of source (ports smashing dev
#269/6398f299 + the #3e09a700 stereo fix). No codec/profile/channel
gating:
- Shaka Packager can't reliably multiplex non-AAC into the fMP4 our
  LL-HLS/DASH ladder needs (MP3 fails outright, Opus/Vorbis-in-fMP4 is
  undependable), so transcoding to AAC is mandatory for those anyway.
- The `-ac 2` downmix is the key player-compatibility fix: HLS.js (and
  several other players) choke on multichannel AAC, so a 5.1 source
  must be downmixed to stereo even when its codec is already AAC. That
  rules out the old stream-copy path — a copied 5.1 AAC track would
  break playback.
- Every master.m3u8 then carries CODECS="...,mp4a.40.2" and decodes on
  every platform. Cost is one cheap audio re-encode per clip (audio is
  a few MB) for certainty across AVPlayer / ExoPlayer / hls.js / Roku.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from encoder.ffprobe import probe
from encoder.progress import run_ffmpeg_with_progress


# Canonical AAC encode params — match dev bash (AAC-LC 192k, stereo, 48kHz).
# ffmpeg's native `aac` encoder emits AAC-LC (mp4a.40.2) by default, so no
# explicit profile flag is needed. _AAC_CHANNELS="2" is the stereo downmix
# that keeps multichannel sources playable in HLS.js et al.
_AAC_BITRATE = "192k"
_AAC_SAMPLE_RATE = "48000"
_AAC_CHANNELS = "2"
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


def build_ffmpeg_cmd(spec: AudioSpec) -> list[str]:
    # Always transcode to AAC-LC 192k stereo 48kHz (no stream-copy path —
    # see the module docstring for why the -ac 2 downmix rules it out).
    cmd = [
        "ffmpeg", "-y",
        "-i", str(spec.mezzanine_path),
        "-vn",
        "-c:a", "aac",
        "-b:a", _AAC_BITRATE,
        "-ar", _AAC_SAMPLE_RATE,
        "-ac", _AAC_CHANNELS,
    ]
    if spec.padding_s > 0:
        cmd += ["-af", f"apad=pad_dur={spec.padding_s}"]

    cmd += [
        "-movflags", "empty_moov+default_base_moof",
        "-frag_duration", str(_FRAG_DURATION_US),
        str(spec.output_path),
        "-loglevel", "error", "-stats",
    ]
    return cmd


def create_audio(spec: AudioSpec, stage_key: str = "audio",
                 duration_s: float = 0.0,
                 pct_lo: float = 0.0, pct_hi: float = 100.0,
                 terminal: bool = True) -> Path:
    """Run the audio extraction/transcode. Returns the output path.

    `pct_lo`/`pct_hi`/`terminal` place the transcode's progress in a sub-band of
    the stage (the phase brackets it with the mezzanine download before and the
    audio upload after), so the bar fills continuously instead of jumping.
    """
    spec.output_path.parent.mkdir(parents=True, exist_ok=True)

    # Probe only to validate an audio stream exists and to log the source
    # codec — the encode is unconditional (always AAC-LC stereo), so the
    # codec no longer gates anything.
    source_codec = detect_source_codec(spec.mezzanine_path)
    print(f"[audio] transcoding {source_codec or '?'} → AAC-LC "
          f"{_AAC_BITRATE} stereo {_AAC_SAMPLE_RATE}Hz", flush=True)
    cmd = build_ffmpeg_cmd(spec)
    try:
        if duration_s > 0:
            run_ffmpeg_with_progress(cmd, duration_s, stage_key,
                                     pct_lo=pct_lo, pct_hi=pct_hi,
                                     terminal=terminal)
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
