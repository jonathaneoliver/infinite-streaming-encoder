"""Bitrate ladder + burn-in layout for each resolution tier.

The data here is the encode pipeline's quality contract: which
resolutions we produce, what target bitrates each codec gets, and
where the drawtext burn-ins sit on the frame. Changing any kbps
value changes visible quality; changing any font-size/offset value
changes how the burn-in overlay looks.
"""
from __future__ import annotations

from dataclasses import dataclass


# Peak bitrate cap applied on top of each target, expressed as a
# percentage of the target. Match bash's default MAXRATE_PERCENT=124.
DEFAULT_MAXRATE_PERCENT = 124

# bufsize is always 2x the target bitrate (hard-coded in bash).
BUFSIZE_MULTIPLIER = 2


@dataclass(frozen=True)
class Tier:
    name: str           # "360p", "540p", ...
    width: int
    height: int
    bitrate_h264: int   # kbps
    bitrate_h265: int   # kbps
    bitrate_av1: int    # kbps
    preset: str         # libx264/libx265 preset (e.g. "medium")
    fontsize_tc: int    # timecode burn-in font size (px)
    fontsize_label: int # other burn-in labels' font size (px)
    burnin_x: int       # burn-in x offset from left edge (px)
    burnin_y_tc: int    # timecode y offset (px)
    burnin_y_label: int # first label y offset — subsequent labels stack below

    def bitrate_for(self, codec: str) -> int:
        if codec == "h264":
            return self.bitrate_h264
        if codec == "hevc":
            return self.bitrate_h265
        if codec == "av1":
            return self.bitrate_av1
        raise ValueError(f"unknown codec: {codec}")


# The six-tier default ("legacy") ladder. Bitrates track smashing dev's
# distinct-height geometric ladder (#763 c72b147d, refined by #834's 1.5x
# ladder) — the current ALL_RESOLUTION_TIERS on origin/dev. Note the column
# order here is (h264, h265, av1); the bash tuple is (h265, h264, av1), so
# these are the same numbers with those two swapped. H264 sits above HEVC/AV1
# at every tier (it's the least efficient codec, so it needs more bitrate for
# the same quality). Changing any kbps value is a quality-policy call.
LADDER: tuple[Tier, ...] = (
    Tier("360p",  640,  360,  600,   300,   300,   "medium", 20, 16, 10, 10, 30),
    Tier("540p",  960,  540,  1722,  1001,  1001,  "medium", 24, 20, 10, 10, 34),
    Tier("720p",  1280, 720,  2779,  1662,  1662,  "medium", 28, 24, 10, 10, 38),
    Tier("1080p", 1920, 1080, 6957,  4273,  4273,  "medium", 36, 32, 10, 10, 45),
    Tier("1440p", 2560, 1440, 16995, 10547, 10547, "medium", 42, 36, 10, 10, 52),
    Tier("2160p", 3840, 2160, 26453, 16458, 16458, "medium", 54, 48, 10, 10, 64),
)


def parse_bitrate_override(mapping: str | None) -> dict[str, int]:
    """Parse `--bitrate-override-h264`/`-hevc` strings.

    Format: "360p=1421,540p=2762". Whitespace is tolerated around tokens.
    Malformed entries are silently skipped — matches bash, which uses
    a regex match on each entry.
    """
    if not mapping:
        return {}
    out: dict[str, int] = {}
    for entry in mapping.split(","):
        normalized = entry.replace(" ", "")
        if "=" not in normalized:
            continue
        key, val = normalized.split("=", 1)
        if val.isdigit():
            out[key] = int(val)
    return out


def resolve_bitrate(tier: Tier, codec: str, override: dict[str, int]) -> int:
    """Override takes precedence, else fall back to the tier's default."""
    return override.get(tier.name, tier.bitrate_for(codec))


def select_tiers(max_res: str | None, source_width: int) -> list[Tier]:
    """Pick which tiers to encode.

    Keeps every tier whose resolution is <= the source's native width
    (no upscaling) and, if the caller passed `--max-res`, caps at that
    tier's name. Matches bash's Phase 2b tier selection.
    """
    tiers = [t for t in LADDER if t.width <= source_width]
    if max_res:
        kept: list[Tier] = []
        for t in tiers:
            kept.append(t)
            if t.name == max_res:
                break
        tiers = kept
    return tiers
