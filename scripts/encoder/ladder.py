"""Data-driven bitrate ladders + burn-in layout.

A *ladder* is a named quality contract: for each codec, an ordered list
of *rungs* (a resolution + target bitrate + encoder preset), plus the VBV
shaping applied on top of every rung's target. This is the encode
pipeline's core policy — changing a bitrate changes visible quality.

The model is deliberately JSON-shaped so ladders can live in a data store
(see the Go control plane's `ladders.json`) and be added/edited by users,
not just hardcoded. A ladder definition is a plain dict:

    {
      "description": "...",
      "seed": true,                 # built-in, read-only (optional)
      "maxrate_percent": 124,       # optional VBV peak ceiling (default 124)
      "bufsize_multiplier": 2,      # optional VBV buffer size  (default 2)
      "codecs": {
        "h264": [ [width, height, bitrate_kbps, preset?], ... ],
        "hevc": [ ... ],
        "av1":  [ ... ]
      }
    }

Rungs may be `[w, h, kbps]` / `[w, h, kbps, preset]` lists or
`{"width","height","bitrate","preset"}` dicts — the loader normalizes
both. Everything else about a rung (its label, its displayed resolution
name, and its burn-in font sizes / offsets) is DERIVED, never stored:

- res_name = f"{height}p".
- label    = res_name, or res_name_N when a codec's ladder has more than
             one rung at the same resolution (Apple's multi-rung tiers).
             The label is the variant's identity everywhere downstream —
             temp MP4 stem, package subdir, playlist path, stage key. For
             single-rung-per-resolution ladders label == res_name, so
             their output is byte-identical to the old tier-named layout.
- burn-in  = auto-derived from height (burnin_for_height) so a
             user-authored rung never has to hand-enter font geometry.
"""
from __future__ import annotations

from dataclasses import dataclass


# Peak bitrate cap applied on top of each rung's target, as a percentage of
# the target. Ladder-level `maxrate_percent` overrides this default.
DEFAULT_MAXRATE_PERCENT = 124

# VBV bufsize as a multiple of the target bitrate. Ladder-level
# `bufsize_multiplier` overrides this default. 0.25× matches smashing dev
# (history 2× → 1× #829 → 0.25× #868): the peak a window of length T can
# reach is maxrate + bufsize/T, so a SMALL buffer stops short segments from
# bursting — 0.25× holds peaks to ~1.5× target at 1s and ~1.28× at 6s, so
# avg/peak stay consistent when the same encode is (re-)segmented at 1s/2s/6s.
# The trade is less room for the encoder to spend bits on hard scenes (watch
# VMAF on the lowest rungs). Two-pass HEVC compensates for the ~17% undershoot
# a tight buffer causes on x265.
BUFSIZE_MULTIPLIER = 0.25


@dataclass(frozen=True)
class Rung:
    """One encode unit: a codec-specific resolution + target bitrate.

    Replaces the old per-resolution `Tier` (which carried all three codec
    bitrates in one row). A rung is single-codec and single-bitrate, so a
    ladder can give H264 and HEVC different rung counts / resolutions —
    which is exactly what the Apple ladders need.
    """
    label: str          # variant identity: "1080p" or "1080p_1" (dup res)
    res_name: str       # "1080p" — display + override lookup + resource sizing
    width: int
    height: int
    bitrate: int        # kbps (any --bitrate-override already applied)
    preset: str         # libx264/libx265 preset (e.g. "medium")
    fontsize_tc: int    # burn-in: timecode font size (px), derived from height
    fontsize_label: int # burn-in: other labels' font size (px)
    burnin_x: int       # burn-in: x offset from left edge (px)
    burnin_y_tc: int    # burn-in: timecode y offset (px)
    burnin_y_label: int # burn-in: first label y offset (labels stack below)


# ---------------------------------------------------------------------------
# Burn-in geometry, derived from height
# ---------------------------------------------------------------------------
# Anchor font/offset params per standard tier height. A rung's params are
# the anchor for the smallest anchor-height >= the rung's height (ceil to
# tier), so a distinct-height fill rung (e.g. 396p, 684p, 1044p) inherits
# the params of the standard tier it stands in for — reproducing the bash
# ladder's hand-picked values for legacy AND apple/apple-uniq exactly.
# (fontsize_tc, fontsize_label, x, y_tc, y_label)
_BURNIN_ANCHORS: tuple[tuple[int, tuple[int, int, int, int, int]], ...] = (
    (234,  (16, 12, 8, 8, 24)),
    (360,  (20, 16, 10, 10, 30)),
    (432,  (22, 18, 10, 10, 32)),
    (540,  (24, 20, 10, 10, 34)),
    (720,  (28, 24, 10, 10, 38)),
    (1080, (36, 32, 10, 10, 45)),
    (1440, (42, 36, 10, 10, 52)),
    (2160, (54, 48, 10, 10, 64)),
)


def burnin_for_height(height: int) -> tuple[int, int, int, int, int]:
    """Return (fontsize_tc, fontsize_label, x, y_tc, y_label) for a height.

    Uses the smallest anchor whose height is >= `height` (ceil-to-tier);
    anything above 2160 clamps to the 2160 anchor. This makes burn-in
    geometry a pure function of resolution, so user-authored rungs never
    hand-enter font sizes.
    """
    for anchor_h, params in _BURNIN_ANCHORS:
        if height <= anchor_h:
            return params
    return _BURNIN_ANCHORS[-1][1]


def res_name_for_height(height: int) -> str:
    """Displayed resolution name for a rung — always `{height}p`."""
    return f"{height}p"


# Standard-tier heights, for `--max-res` capping (max_res names -> height).
_MAX_RES_HEIGHT = {
    "360p": 360, "540p": 540, "720p": 720,
    "1080p": 1080, "1440p": 1440, "2160p": 2160,
}


# ---------------------------------------------------------------------------
# Seed ladders (built-in, read-only). JSON-shaped so they can be written
# straight to the store file. Rungs are [width, height, bitrate_kbps]
# (preset defaults to "medium").
# ---------------------------------------------------------------------------

# The default "legacy" ladder: one rung per resolution per codec. Bitrates
# track smashing dev's distinct-height geometric ladder (#763, refined by
# #834). av1 mirrors hevc. label == res_name here (single rung per res), so
# legacy output is byte-identical to the historical tier-named layout.
_SEED_LEGACY_H264 = [
    [640, 360, 600], [960, 540, 1722], [1280, 720, 2779],
    [1920, 1080, 6957], [2560, 1440, 16995], [3840, 2160, 26453],
]
_SEED_LEGACY_HEVC = [
    [640, 360, 300], [960, 540, 1001], [1280, 720, 1662],
    [1920, 1080, 4273], [2560, 1440, 10547], [3840, 2160, 16458],
]

# Apple HLS Authoring Spec ladders (#868): PER-CODEC, MULTI-RUNG. H264 tops
# out at 1080p (9 rungs); HEVC extends to 2160p (12 rungs). Several
# resolutions carry more than one rung (e.g. two 1080p). av1 mirrors hevc.
_SEED_APPLE_H264 = [
    [416, 234, 145], [640, 360, 365], [768, 432, 730], [768, 432, 1100],
    [960, 540, 2000], [1280, 720, 3000], [1280, 720, 4500],
    [1920, 1080, 6000], [1920, 1080, 7800],
]
_SEED_APPLE_HEVC = [
    [640, 360, 145], [768, 432, 300], [960, 540, 600], [960, 540, 900],
    [960, 540, 1600], [1280, 720, 2400], [1280, 720, 3400],
    [1920, 1080, 4500], [1920, 1080, 5800], [2560, 1440, 8100],
    [3840, 2160, 11600], [3840, 2160, 16800],
]

# Apple-uniq (#868/#871): Apple's EXACT bitrates, but every rung gets a
# UNIQUE resolution per codec so a same-bitrate rung is distinguishable
# downstream by decoded frame size. Within each duplicate-resolution group
# the highest-bitrate rung keeps Apple's resolution; lower rungs step down
# in clean 16:9 increments (<= Apple's original). av1 mirrors hevc.
_SEED_APPLE_H264_UNIQ = [
    [416, 234, 145], [640, 360, 365], [704, 396, 730], [768, 432, 1100],
    [960, 540, 2000], [1216, 684, 3000], [1280, 720, 4500],
    [1856, 1044, 6000], [1920, 1080, 7800],
]
_SEED_APPLE_HEVC_UNIQ = [
    [640, 360, 145], [768, 432, 300], [832, 468, 600], [896, 504, 900],
    [960, 540, 1600], [1216, 684, 2400], [1280, 720, 3400],
    [1856, 1044, 4500], [1920, 1080, 5800], [2560, 1440, 8100],
    [3776, 2124, 11600], [3840, 2160, 16800],
]

SEED_LADDERS: dict[str, dict] = {
    "legacy": {
        "description": "Default distinct-height geometric ladder "
                       "(one rung per resolution per codec).",
        "seed": True,
        "codecs": {
            "h264": _SEED_LEGACY_H264,
            "hevc": _SEED_LEGACY_HEVC,
            "av1":  _SEED_LEGACY_HEVC,
        },
    },
    "apple": {
        "description": "Apple HLS Authoring Spec bitrates — per-codec, "
                       "multi-rung (some resolutions repeat at higher bitrate).",
        "seed": True,
        "codecs": {
            "h264": _SEED_APPLE_H264,
            "hevc": _SEED_APPLE_HEVC,
            "av1":  _SEED_APPLE_HEVC,
        },
    },
    "apple-uniq": {
        "description": "Apple bitrates with every rung given a unique 16:9 "
                       "resolution (distinguishable by decoded frame size).",
        "seed": True,
        "codecs": {
            "h264": _SEED_APPLE_H264_UNIQ,
            "hevc": _SEED_APPLE_HEVC_UNIQ,
            "av1":  _SEED_APPLE_HEVC_UNIQ,
        },
    },
    "apple-uniq-live": {
        "description": "apple-uniq bitrates under Apple's live/linear VBV: "
                       "peak <= 1.25x avg. maxrate 110% + tight 0.10x buffer "
                       "keep delivered peak <=~1.20x even at 1s segments; "
                       "unique resolutions keep the bands distinct.",
        "seed": True,
        "maxrate_percent": 110,
        "bufsize_multiplier": 0.10,
        # No pinned segment_duration: the flexible base (tight VBV is safe to
        # repackage into 1s/2s/6s); partial/gop are its LL-HLS live settings.
        "partial_duration": "0.2",
        "gop_duration": "1.0",
        "codecs": {
            "h264": _SEED_APPLE_H264_UNIQ,
            "hevc": _SEED_APPLE_HEVC_UNIQ,
            "av1":  _SEED_APPLE_HEVC_UNIQ,
        },
    },
    "apple-uniq-live-6s": {
        "description": "apple-uniq LL-HLS for 6s segments ONLY. The tight "
                       "110%/0.10x VBV on apple-uniq-live kept the delivered "
                       "per-segment peak reasonable even at 1s (peak ~= maxrate "
                       "+ bufsize/T); fixed at 6s the bufsize/T term is 6x "
                       "smaller, so relax to 150%/1.0x for better quality while "
                       "the delivered peak stays ~1.67x avg. Keeps 0.2s parts + "
                       "1s GOP. Outputs tagged _6s so go-live only makes 6s.",
        "seed": True,
        "maxrate_percent": 150,
        "bufsize_multiplier": 1.0,
        "segment_duration": "6",
        "partial_duration": "0.2",
        "gop_duration": "1.0",
        "output_tag": "6s",
        "codecs": {
            "h264": _SEED_APPLE_H264_UNIQ,
            "hevc": _SEED_APPLE_HEVC_UNIQ,
            "av1":  _SEED_APPLE_HEVC_UNIQ,
        },
    },
    "apple-uniq-vod": {
        "description": "apple-uniq bitrates tuned for VOD: 6s segments, NO "
                       "LL-HLS parts, long 6s GOP (fewer keyframes -> better "
                       "efficiency), relaxed VBV (peak <= 2x avg per Apple's "
                       "VOD guidance, 2.0x buffer). Bits redistribute toward "
                       "complex scenes; average bitrate + size unchanged.",
        "seed": True,
        "maxrate_percent": 200,
        "bufsize_multiplier": 2.0,
        "segment_duration": "6",
        "partial_duration": "0",
        "gop_duration": "6",
        "codecs": {
            "h264": _SEED_APPLE_H264_UNIQ,
            "hevc": _SEED_APPLE_HEVC_UNIQ,
            "av1":  _SEED_APPLE_HEVC_UNIQ,
        },
    },
}

DEFAULT_LADDER = "apple-uniq-live"


class LadderError(ValueError):
    pass


def _store_path() -> str | None:
    """Filesystem path to the persisted ladder store, or None.

    The Go control plane owns the store (ladders.json) and sets LADDER_STORE
    on worker containers; we also probe the mounted temp dir as a fallback.
    """
    import os
    p = os.environ.get("LADDER_STORE")
    if p:
        return p
    for env in ("TMPDIR", "ENCODER_TMP_ROOT", "TMP_DIR"):
        base = os.environ.get(env)
        if base:
            return os.path.join(base, "ladders.json")
    return None


def load_ladders() -> dict:
    """Built-in seeds overlaid with the persisted store (user-defined ladders
    and any edits). Reading the same file the Go control plane writes is what
    lets custom ladders resolve for local encodes too. Missing/corrupt store
    → just the seeds."""
    import json
    import os
    ladders = dict(SEED_LADDERS)
    path = _store_path()
    if path and os.path.isfile(path):
        try:
            with open(path) as f:
                data = json.load(f)
            for name, definition in (data.get("ladders") or {}).items():
                if isinstance(definition, dict) and definition.get("codecs"):
                    ladders[name] = definition
        except (OSError, ValueError):
            pass
    return ladders


def get_ladder(name: str) -> dict:
    """Return a ladder definition by name (seeds + persisted store)."""
    ladders = load_ladders()
    try:
        return ladders[name]
    except KeyError:
        raise LadderError(
            f"unknown ladder {name!r} (have: {', '.join(sorted(ladders))})"
        ) from None


def ladder_names() -> list[str]:
    return sorted(load_ladders())


def label_res_name(label: str) -> str:
    """The resolution name embedded in a rung label ("1080p_2" -> "1080p")."""
    return label.split("_", 1)[0]


def label_height(label: str) -> int:
    """Pixel height a rung label encodes ("1080p_2" -> 1080)."""
    return int(label_res_name(label).rstrip("p"))


# ---------------------------------------------------------------------------
# Bitrate overrides (unchanged CLI surface)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Ladder access + rung selection
# ---------------------------------------------------------------------------

def ladder_maxrate_percent(ladder_def: dict) -> int:
    return int(ladder_def.get("maxrate_percent") or DEFAULT_MAXRATE_PERCENT)


def ladder_bufsize_multiplier(ladder_def: dict) -> float:
    return float(ladder_def.get("bufsize_multiplier") or BUFSIZE_MULTIPLIER)


def _normalize_rung_row(row) -> tuple[int, int, int, str]:
    """Accept a [w,h,b] / [w,h,b,preset] list or a rung dict; return
    (width, height, bitrate, preset)."""
    if isinstance(row, dict):
        w = int(row["width"]); h = int(row["height"]); b = int(row["bitrate"])
        preset = str(row.get("preset") or "medium")
        return w, h, b, preset
    w = int(row[0]); h = int(row[1]); b = int(row[2])
    preset = str(row[3]) if len(row) > 3 and row[3] else "medium"
    return w, h, b, preset


def build_rungs(ladder_def: dict, codec: str) -> list[Rung]:
    """Build the FULL ordered rung list for a codec under a ladder, BEFORE
    any max-res / source-width / override filtering.

    Labels are assigned over the full ladder (bare res_name when unique
    within the codec, res_name_N when a resolution repeats) so that the
    normal and resume paths derive identical labels for whichever rungs
    they keep. Burn-in geometry is derived from each rung's height.
    """
    rows = ladder_def.get("codecs", {}).get(codec)
    if not rows:
        return []
    parsed = [_normalize_rung_row(r) for r in rows]

    # Count resolution occurrences for label disambiguation.
    counts: dict[str, int] = {}
    for _w, h, _b, _p in parsed:
        rn = res_name_for_height(h)
        counts[rn] = counts.get(rn, 0) + 1

    rungs: list[Rung] = []
    idx: dict[str, int] = {}
    for w, h, b, preset in parsed:
        rn = res_name_for_height(h)
        if counts[rn] > 1:
            idx[rn] = idx.get(rn, 0) + 1
            label = f"{rn}_{idx[rn]}"
        else:
            label = rn
        ftc, flbl, x, ytc, ylbl = burnin_for_height(h)
        rungs.append(Rung(
            label=label, res_name=rn, width=w, height=h, bitrate=b,
            preset=preset, fontsize_tc=ftc, fontsize_label=flbl,
            burnin_x=x, burnin_y_tc=ytc, burnin_y_label=ylbl,
        ))
    return rungs


def select_rungs(
    ladder_def: dict,
    codec: str,
    max_res: str | None,
    source_width: int,
    override: dict[str, int] | None = None,
) -> list[Rung]:
    """Rungs to actually encode for a codec: the full ladder, filtered to
    those that fit the source (no upscale) and `--max-res`, with any
    per-resolution bitrate override applied.

    - No upscale: keep rungs whose width <= source_width (skipped when
      source_width <= 0, i.e. the probe was unavailable).
    - max_res: keep rungs whose height <= that tier's height.
    - override: {res_name: kbps} replaces the rung's bitrate by resolution.
    """
    override = override or {}
    max_h = _MAX_RES_HEIGHT.get(max_res or "", None)

    out: list[Rung] = []
    for rung in build_rungs(ladder_def, codec):
        if source_width > 0 and rung.width > source_width:
            continue
        if max_h is not None and rung.height > max_h:
            continue
        if rung.res_name in override:
            rung = Rung(**{**rung.__dict__, "bitrate": override[rung.res_name]})
        out.append(rung)
    return out
