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

The VMAF row shows a DESIGN-TIME estimate interpolated from the quality
curves (`VMAF~93`), or the nearest measured endpoint when the rung sits above
the curve (`VMAF>=97`) — not the measured VMAF of this encode. It is optional:
`has_vmaf` gates the row and the layers below close the gap, so an absent
estimate yields a valid overlay with one fewer line.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from infinite_streaming_encoder.ladder import Rung


FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Overlay modes. FULL is the five-label stack this module was written for;
# LIGHT is one static rung label (see build_filter). The strings are a
# cross-language contract: Go sends them as the BURNIN env value on the cloud
# path and as --burnin-mode locally, and an unrecognised value means FULL on
# both sides, which is what an older worker does with "light" already.
MODE_FULL = "full"
MODE_LIGHT = "light"
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
    # Pre-formatted VMAF-estimate label for the overlay, e.g. "VMAF~93" (curve
    # interpolation) or "VMAF≥97" (rung above the measured range → nearest
    # endpoint). Empty → the row is omitted entirely. It's a DESIGN-TIME estimate
    # from the quality curves, not the measured VMAF of this encode.
    vmaf_label: str = ""


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


def light_label(ctx: BurninContext) -> str:
    """The single static label drawn in LIGHT mode: `JEO_<rung>_<kbps>k`.

    Self-describing across ladders — the rung's label (its resolution, with the
    `_1`/`_2` suffix a ladder that repeats one carries) plus the target bitrate,
    so a frame identifies its rung without knowing which ladder produced it.
    """
    return f"JEO_{ctx.tier.label}_{ctx.tier.bitrate}k"


def build_filter(ctx: BurninContext, burnin: bool = True, mode: str = MODE_FULL) -> str:
    """Return the full `-vf` filter expression for this variant.

    Filter chain: scale → (optional tpad) → drawtext×5 (+ optional PADDING).

    With `burnin=False` the drawtext overlays (the 5 stacked labels AND the
    PADDING label) are omitted — the chain is just scale (+ optional tpad), so
    the output carries no burnt-in text. The tpad segment-boundary padding is
    NOT text and always stays; only the drawn labels are toggled off.

    With `mode=MODE_LIGHT` a SINGLE static label is drawn instead of the stack
    (see `light_label`). The point is bitrate, not tidiness: the timecode layer
    re-renders every frame, so it is new content in every single frame and the
    encoder pays for it continuously. One static label is inter-predicted for
    free after the first frame of each GOP. The PADDING label survives because
    it is `enable`-gated onto padded frames only, where there is nothing else to
    spend bits on — and losing it would make padding invisible.

    An unknown mode is FULL. This is the degradation an older caller gets, and
    the safe direction: a legible overlay nobody asked for beats an encode
    silently missing the label it is supposed to be identified by.
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

    if not burnin:
        # No text overlay: keep only the scale (+ tpad) geometry.
        return ",".join(chain)

    if mode == MODE_LIGHT:
        chain.append(_drawtext(
            light_label(ctx), fontsize=tier.fontsize_label, color="white",
            x=tier.burnin_x, y=tier.burnin_y_tc,
        ))
        if padding_enabled:
            chain.append(_drawtext(
                "PADDING",
                fontsize=tier.fontsize_tc * 2,
                color="red",
                box_opacity=0.9,
                x="w-tw-10",
                y=10,
                enable=f"gte(t,{ctx.content_duration_s})",
            ))
        return ",".join(chain)

    # Stack heights: timecode (tc), rate, [vmaf], codec/res/fps, encoder,
    # watermark. The VMAF-estimate row sits right after the AVG-bandwidth (rate)
    # row and pushes the rest down; when absent it's omitted with no gap.
    has_vmaf = bool(ctx.vmaf_label)
    y_tc = tier.burnin_y_tc
    y_rate = y_tc + tier.fontsize_tc + 5
    y_vmaf = y_rate + tier.fontsize_label + 5
    y_codec_res = (y_vmaf if has_vmaf else y_rate) + tier.fontsize_label + 5
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
    ]
    if has_vmaf:
        overlays.append(_drawtext(
            ctx.vmaf_label, fontsize=tier.fontsize_label, color="lime",
            x=tier.burnin_x, y=y_vmaf,
        ))
    overlays += [
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
