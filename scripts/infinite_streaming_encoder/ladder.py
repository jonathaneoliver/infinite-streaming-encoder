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
# tier), so a distinct-height fill rung (e.g. 396p, 594p, 954p) inherits
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


# Standard-tier heights, kept as a fallback for any legacy tier name that
# isn't literally "<height>p".
_MAX_RES_HEIGHT = {
    "360p": 360, "540p": 540, "720p": 720,
    "1080p": 1080, "1440p": 1440, "2160p": 2160,
}


def res_height(name: str | None) -> int | None:
    """`"1080p"` -> 1080. None/empty/unparseable -> None (i.e. no bound).

    Parsed rather than looked up because the UI derives its min/max tier
    options from the SELECTED LADDER's actual rung heights, and the Apple-uniq
    ladders carry non-standard ones (954p, 1800p, 594p...). A fixed table
    would silently ignore every tier it didn't know about.
    """
    if not name:
        return None
    n = name.strip()
    if n.endswith("p") and n[:-1].isdigit():
        return int(n[:-1])
    return _MAX_RES_HEIGHT.get(n)


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
    [960, 540, 2000], [1056, 594, 3000], [1280, 720, 4500],
    [1696, 954, 6000], [1920, 1080, 7800],
]
_SEED_APPLE_HEVC_UNIQ = [
    [640, 360, 145], [768, 432, 300], [832, 468, 600], [896, 504, 900],
    [960, 540, 1600], [1056, 594, 2400], [1280, 720, 3400],
    [1696, 954, 4500], [1920, 1080, 5800], [2560, 1440, 8100],
    [3200, 1800, 11600], [3840, 2160, 16800],
]

# apple-uniq H.264 extended to 4K. Apple's spec caps H.264 at 1080p (HEVC
# above), but high-bitrate 4K H.264 is still wanted for maximum player
# compatibility. The three extra tiers mirror the HEVC-uniq top RESOLUTIONS
# (1440 / 1800 / 2160) with H.264-appropriate (higher) bitrates — roughly the
# legacy H.264 heights, so h264/hevc/av1 stay rung-count-parallel in this
# ladder.
_SEED_APPLE_H264_UNIQ_FULL = _SEED_APPLE_H264_UNIQ + [
    [2560, 1440, 13500], [3200, 1800, 19000], [3840, 2160, 27000],
]

SEED_LADDERS: dict[str, dict] = {
    "apple": {
        "description": "Apple HLS Authoring Spec bitrates — per-codec, multi-rung.",
        "seed": True,
        "codecs": {
            "h264": _SEED_APPLE_H264,
            "hevc": _SEED_APPLE_HEVC,
            "av1":  _SEED_APPLE_HEVC,
        },
    },
    "apple-uniq": {
        "description": "Apple bitrates with every rung given a unique 16:9 resolution.",
        "seed": True,
        "codecs": {
            "h264": _SEED_APPLE_H264_UNIQ,
            "hevc": _SEED_APPLE_HEVC_UNIQ,
            "av1":  _SEED_APPLE_HEVC_UNIQ,
        },
    },
    "apple-uniq-live-xs": {
        "description": "The FLEXIBLE base: no pinned segment length, so go-live "
                       "repackages one encode into 1s/2s/6s. Apple's live/linear VBV "
                       "(peak <= 1.25x avg) with maxrate 110% and a tight 0.10x "
                       "buffer, which keeps the delivered peak <=~1.20x EVEN AT 1s — "
                       "that is what makes it safe to re-chop, and why the buffer is "
                       "smaller than any apple-uniq-live-Ns ladder can afford to give "
                       "itself. H.264 climbs to 4K (1440p/1800p/2160p): Apple caps "
                       "H.264 at 1080p and puts HEVC above, so this trades "
                       "spec-compliance for max-compatibility high-bitrate 4K H.264, "
                       "matching the rung set of the fixed-segment ladders it is "
                       "compared against.",
        "seed": True,
        "maxrate_percent": 110,
        "bufsize_multiplier": 0.10,
        # Flexible base: no pinned segment_duration → suffix derives to _xs.
        "partial_duration": "0.2",
        "gop_duration": "1.0",
        "codecs": {
            "h264": _SEED_APPLE_H264_UNIQ_FULL,
            "hevc": _SEED_APPLE_HEVC_UNIQ,
            "av1":  _SEED_APPLE_HEVC_UNIQ,
        },
    },
    "apple-uniq-live-1s": {
        "description": "apple-uniq bitrates encoded NATIVELY for 1s segments. "
                       "Delivered peak (maxrate + bufsize/T) is held at 1.25x avg — "
                       "Apple's live/linear guidance — the SAME as the other "
                       "apple-uniq-live-Ns ladders, so a comparison between them is "
                       "not confounded by peak. Committing to 1s is what buys the "
                       "bigger buffer: 0.15x here versus 0.10x on the flexible base "
                       "(apple-uniq-live-xs), which must survive re-chopping to 1s and "
                       "so pays the 1s price at every length — that difference IS the "
                       "cost of re-choppability. GOP matched to the segment (1s), "
                       "which is what makes this a different ENCODE rather than a "
                       "repackaging. NOTE gop == segment means LL-HLS parts are "
                       "INDEPENDENT only at segment boundaries, so a player cannot "
                       "join mid-segment: the low-latency cost of a long GOP.",
        "seed": True,
        "maxrate_percent": 110,
        "bufsize_multiplier": 0.15,
        "segment_duration": "1",
        "partial_duration": "0.2",
        "gop_duration": "1",
        "codecs": {
            "h264": _SEED_APPLE_H264_UNIQ_FULL,
            "hevc": _SEED_APPLE_HEVC_UNIQ,
            "av1":  _SEED_APPLE_HEVC_UNIQ,
        },
    },
    "apple-uniq-live-2s": {
        "description": "apple-uniq bitrates encoded NATIVELY for 2s segments. "
                       "Delivered peak (maxrate + bufsize/T) is held at 1.25x avg — "
                       "Apple's live/linear guidance — the SAME as the other "
                       "apple-uniq-live-Ns ladders, so a comparison between them is "
                       "not confounded by peak. Committing to 2s is what buys the "
                       "bigger buffer: 0.3x here versus 0.10x on the flexible base "
                       "(apple-uniq-live-xs), which must survive re-chopping to 1s and "
                       "so pays the 1s price at every length — that difference IS the "
                       "cost of re-choppability. GOP matched to the segment (2s), "
                       "which is what makes this a different ENCODE rather than a "
                       "repackaging. NOTE gop == segment means LL-HLS parts are "
                       "INDEPENDENT only at segment boundaries, so a player cannot "
                       "join mid-segment: the low-latency cost of a long GOP.",
        "seed": True,
        "maxrate_percent": 110,
        "bufsize_multiplier": 0.3,
        "segment_duration": "2",
        "partial_duration": "0.2",
        "gop_duration": "2",
        "codecs": {
            "h264": _SEED_APPLE_H264_UNIQ_FULL,
            "hevc": _SEED_APPLE_HEVC_UNIQ,
            "av1":  _SEED_APPLE_HEVC_UNIQ,
        },
    },
    "apple-uniq-live-6s": {
        "description": "apple-uniq bitrates encoded NATIVELY for 6s segments. "
                       "Delivered peak (maxrate + bufsize/T) is held at 1.25x avg — "
                       "Apple's live/linear guidance — the SAME as the other "
                       "apple-uniq-live-Ns ladders, so a comparison between them is "
                       "not confounded by peak. Committing to 6s is what buys the "
                       "bigger buffer: 0.9x here versus 0.10x on the flexible base "
                       "(apple-uniq-live-xs), which must survive re-chopping to 1s and "
                       "so pays the 1s price at every length — that difference IS the "
                       "cost of re-choppability. GOP matched to the segment (6s), "
                       "which is what makes this a different ENCODE rather than a "
                       "repackaging. NOTE gop == segment means LL-HLS parts are "
                       "INDEPENDENT only at segment boundaries, so a player cannot "
                       "join mid-segment: the low-latency cost of a long GOP.",
        "seed": True,
        "maxrate_percent": 110,
        "bufsize_multiplier": 0.9,
        "segment_duration": "6",
        "partial_duration": "0.2",
        "gop_duration": "6",
        "codecs": {
            "h264": _SEED_APPLE_H264_UNIQ_FULL,
            "hevc": _SEED_APPLE_HEVC_UNIQ,
            "av1":  _SEED_APPLE_HEVC_UNIQ,
        },
    },
    "apple-uniq-vod": {
        "description": "apple-uniq bitrates tuned for VOD: 6s segments, NO LL-HLS "
                       "parts, long 6s GOP (fewer keyframes -> better efficiency), and "
                       "a relaxed VBV (peak <= 2x avg per Apple's VOD guidance, 2.0x "
                       "buffer). Bits redistribute toward complex scenes; average "
                       "bitrate and size are unchanged.",
        "seed": True,
        "maxrate_percent": 200,
        "bufsize_multiplier": 2.0,
        "segment_duration": "6",
        "partial_duration": "0",
        "gop_duration": "6",
        # Explicit: the derived tag would be "6s", colliding with
        # apple-uniq-live-6s, which is a different encode (gop 6 vs 1.0, no
        # parts vs 0.2s, 200%/2x vs 150%/1x). Segment duration is a good
        # default name, not a unique one.
        "output_tag": "vod",
        "codecs": {
            "h264": _SEED_APPLE_H264_UNIQ,
            "hevc": _SEED_APPLE_HEVC_UNIQ,
            "av1":  _SEED_APPLE_HEVC_UNIQ,
        },
    },
}

DEFAULT_LADDER = "apple-uniq-live-xs"


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


# Profile timing. These mirror cli_phase's env defaults exactly — SEGMENT 6.0,
# GOP 1.0, PARTIAL 0.2 — because cli_phase is what consumes them and a
# disagreement here would be the same silent-default trap these accessors exist
# to close (#172). A ladder that omits one genuinely wants the default; "0" is a
# real value (PARTIAL_DURATION=0 turns LL-HLS parts off for VOD), so these read
# the key's presence rather than its truthiness.
def ladder_segment_duration(ladder_def: dict) -> float:
    v = ladder_def.get("segment_duration")
    return 6.0 if v is None or v == "" else float(v)


def ladder_gop_duration(ladder_def: dict) -> float:
    v = ladder_def.get("gop_duration")
    return 1.0 if v is None or v == "" else float(v)


def ladder_partial_duration(ladder_def: dict) -> float:
    v = ladder_def.get("partial_duration")
    return 0.2 if v is None or v == "" else float(v)


def ladder_extra_args(ladder_def: dict, codec: str) -> str:
    """Raw per-codec ffmpeg extra args for a codec on this ladder ("" when
    unset — the default). Mirrors LadderDef.extraArgsFor on the Go side."""
    return str((ladder_def.get("extra_args") or {}).get(codec, "") or "")


def ladder_passes(ladder_def: dict, codec: str) -> int:
    """Encode pass count for a codec on this ladder, falling back to the
    per-codec default when unset: h264 → 1, hevc → 2, av1 → 2 (two-pass gives
    AV1 an accurate target average, like HEVC). Mirrors LadderDef.passesFor on
    the Go side — the single source of truth for the two-pass decision."""
    n = (ladder_def.get("passes") or {}).get(codec)
    if isinstance(n, int) and n > 0:
        return n
    return 1 if codec == "h264" else 2


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
    min_res: str | None = None,
) -> list[Rung]:
    """Rungs to actually encode for a codec: the full ladder, filtered to
    those that fit the source (no upscale) and the `--min-res`/`--max-res`
    band, with any per-resolution bitrate override applied.

    - No upscale: keep rungs whose width <= source_width (skipped when
      source_width <= 0, i.e. the probe was unavailable).
    - max_res / min_res: keep rungs whose height is within [min, max].
      Both bounds are inclusive, so min == max encodes exactly that tier.
    - override: {res_name: kbps} replaces the rung's bitrate by resolution.

    `min_res` is keyword-only in practice — it's appended after `override` so
    every existing positional call site keeps working unchanged.
    """
    override = override or {}
    max_h = res_height(max_res)
    min_h = res_height(min_res)

    out: list[Rung] = []
    for rung in build_rungs(ladder_def, codec):
        if source_width > 0 and rung.width > source_width:
            continue
        if max_h is not None and rung.height > max_h:
            continue
        if min_h is not None and rung.height < min_h:
            continue
        if rung.res_name in override:
            rung = Rung(**{**rung.__dict__, "bitrate": override[rung.res_name]})
        out.append(rung)
    return out
