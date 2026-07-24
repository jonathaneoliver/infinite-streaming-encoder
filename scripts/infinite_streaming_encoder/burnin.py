"""Drawtext filter builders for the per-variant burn-in overlay.

Five stacked labels, top-left by default:
  1. Timecode (yellow, incrementing at source fps)
  2. Rate label (cyan, "AVG~4.50Mbps / PEAK<=5.58Mbps")
  3. Codec + resolution + fps (cyan, "HEVC 1080p | 25fps")
  4. Encoder label ("SW", orange)
  5. Watermark ("JEO", white)

If the source is padded out to a segment boundary, we additionally
draw a large red "PADDING" label in the top-right, only enabled for
`t >= content_duration` (i.e. visible on padded frames only).

VMAF burn-in is plumbed through for parity but currently unused — the
reference bash script left the drawtext call commented out.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from infinite_streaming_encoder.ladder import Rung


FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# Match bash's escape style for the initial timecode.
INITIAL_TIMECODE = r"00\:00\:00\:00"


@dataclass(frozen=True)
class BurninContext:
    codec: str                # "hevc"/"h264"/"av1"
    tier: Rung
    fps: Fraction             # for timecode rate
    rate_label: str           # "AVG~4.50Mbps / PEAK<=5.58Mbps" or similar
    encoder_label: str        # e.g. "SW"
    content_duration_s: float # for PADDING-label enable expression
    padding_duration_s: float # 0 → no PADDING label at all
    # Absolute start offset of this encode within the full content. 0 for a
    # whole-clip encode; the chunk's start_s when encoding a single chunk, so
    # the burnt-in timecode stays continuous across concatenated chunks.
    timecode_start_s: float = 0.0


def format_timecode(start_s: float, fps: Fraction) -> str:
    """SMPTE-ish HH:MM:SS:FF (colons escaped for drawtext) for `start_s`.

    start_s=0 yields "00\\:00\\:00\\:00", matching the pre-chunking constant,
    so whole-clip encodes are unchanged.
    """
    fps_int = max(1, round(float(fps)))
    total_frames = round(start_s * float(fps))
    frames = total_frames % fps_int
    total_seconds = total_frames // fps_int
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return rf"{hh:02d}\:{mm:02d}\:{ss:02d}\:{frames:02d}"


def _escape(text: str) -> str:
    """drawtext's text arg needs colons escaped."""
    return text.replace(":", r"\:").replace("'", r"\'")


def _drawtext(
    text: str, *, fontsize: int, color: str, x: int | str, y: int | str,
    box_opacity: float = 0.7, enable: str | None = None, timecode: str | None = None,
    rate: Fraction | None = None,
) -> str:
    parts = [f"fontfile='{FONT_PATH}'"]
    if timecode is not None:
        parts.append(f"timecode='{timecode}'")
        if rate is not None:
            # ffmpeg's drawtext `rate` accepts the same N/D form as -framerate.
            parts.append(f"rate={rate}")
    else:
        parts.append(f"text='{_escape(text)}'")
    parts += [
        f"fontsize={fontsize}",
        f"fontcolor={color}",
        "box=1",
        f"boxcolor=black@{box_opacity}",
        "boxborderw=5",
        f"x={x}",
        f"y={y}",
    ]
    if enable is not None:
        parts.append(f"enable='{enable}'")
    return "drawtext=" + ":".join(parts)


def build_filter(ctx: BurninContext) -> str:
    """Return the full `-vf` filter expression for this variant.

    Filter chain: scale → (optional tpad) → drawtext×5 (+ optional PADDING).
    """
    tier = ctx.tier
    chain: list[str] = [f"scale={tier.width}:{tier.height}"]

    padding_enabled = ctx.padding_duration_s > 0
    if padding_enabled:
        # stop_mode=add keeps the timestamp advancing so the timecode
        # overlay continues incrementing on padded frames.
        chain.append(
            f"tpad=stop_mode=add:stop_duration={ctx.padding_duration_s}:color=black"
        )

    # Stack heights: timecode (tc), rate, codec/res/fps, encoder, watermark.
    y_tc = tier.burnin_y_tc
    y_rate = y_tc + tier.fontsize_tc + 5
    y_codec_res = y_rate + tier.fontsize_label + 5
    y_encoder = y_codec_res + tier.fontsize_label + 5
    y_watermark = y_encoder + tier.fontsize_label + 5

    # res_name (not label): apple dup rungs (1080p_1/1080p_2) both display
    # as their true resolution "1080p" in the burn-in overlay.
    codec_res_label = f"{ctx.codec.upper()} {tier.res_name} | {float(ctx.fps):.2f}fps"

    overlays = [
        _drawtext(
            "", fontsize=tier.fontsize_tc, color="yellow", box_opacity=1.0,
            x=tier.burnin_x, y=y_tc,
            timecode=format_timecode(ctx.timecode_start_s, ctx.fps), rate=ctx.fps,
        ),
        _drawtext(
            ctx.rate_label, fontsize=tier.fontsize_label, color="cyan",
            x=tier.burnin_x, y=y_rate,
        ),
        _drawtext(
            codec_res_label, fontsize=tier.fontsize_label, color="cyan",
            x=tier.burnin_x, y=y_codec_res,
        ),
        _drawtext(
            ctx.encoder_label, fontsize=tier.fontsize_label, color="orange",
            x=tier.burnin_x, y=y_encoder,
        ),
        _drawtext(
            "JEO", fontsize=tier.fontsize_label, color="white",
            x=tier.burnin_x, y=y_watermark,
        ),
    ]

    if padding_enabled:
        overlays.append(_drawtext(
            "PADDING",
            fontsize=tier.fontsize_tc * 2,
            color="red",
            box_opacity=0.9,
            x="w-tw-10",
            y=10,
            enable=f"gte(t,{ctx.content_duration_s})",
        ))

    chain.extend(overlays)
    return ",".join(chain)


def rate_label(
    target_kbps: int, maxrate_percent: int, avg_kbps: float | None = None,
) -> str:
    """Build the rate label shown under the timecode.

    With no VMAF-derived average, this is just "AVG~<target>Mbps /
    PEAK<=<maxrate>Mbps". With a VMAF-derived average (hardware-only
    in bash; unused in the Python port since we dropped HW encode),
    the average uses that estimate.
    """
    target_mbps = target_kbps / 1000.0
    avg_mbps = avg_kbps / 1000.0 if avg_kbps is not None else target_mbps
    peak_mbps = max(target_kbps * maxrate_percent / 100.0 / 1000.0, avg_mbps)
    return f"AVG~{avg_mbps:.2f}Mbps / PEAK<={peak_mbps:.2f}Mbps"
