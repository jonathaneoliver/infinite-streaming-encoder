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

# The two MEASURED Netflix ladders (#394): rungs read out of a live player
# rather than designed. Seeds because reference data that lives on one box is
# not a reference — a state wipe or a second machine loses it, and the
# measurement cost most of a day on real hardware.
#
# Bitrates are exactly as measured. Only repeated resolutions are nudged: HLS
# variant directories are named <height>p, so a ladder that reuses a resolution
# collides with itself. The lower rung of each repeat steps down by the
# smallest exact-16:9 increment (18px of height) and the measured resolution
# stays on the upper rung — the same device apple-uniq uses.
#
# The delivery profile (segment / partial / GOP / VBV) is OURS: none of it is
# observable from a player. See docs/ladders-and-delivery.md "The Netflix
# ladders". Kept in sync BY HAND with internal/encode/ladder_store.go.
_SEED_NETFLIX_AV1 = [
    [576, 324, 63],
    [608, 342, 97],
    [768, 432, 152],
    [960, 540, 209],
    [1280, 720, 322],
    [1888, 1062, 543],
    [1920, 1080, 1020],
    [3840, 2160, 1962],
]

_SEED_NETFLIX_DV_HEVC = [
    [608, 342, 111],
    [768, 432, 113],
    [960, 540, 138],
    [1280, 720, 232],
    [1920, 1080, 351],
    [2560, 1440, 528],
    [3712, 2088, 778],
    [3744, 2106, 1154],
    [3776, 2124, 4213],
    [3808, 2142, 8366],
    [3840, 2160, 11846],
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
                       "(peak <= 1.25x avg) split as maxrate 100% + a 0.25x buffer, so "
                       "the bound holds EVEN AT 1s (1.00 + 0.25) — that is what makes "
                       "it safe to re-chop. The split matters as much as the bound: at "
                       "110%/0.10x the same 1.25x ceiling left only 3 frames of buffer "
                       "and the encoder delivered just 64-68% of target, because a "
                       "3-frame buffer cannot absorb a keyframe and x264 stays "
                       "conservative rather than violate VBV. Measured, the peak never "
                       "reached 86-92% of that maxrate at any rung, so the ceiling was "
                       "never the constraint — trading it for buffer costs nothing and "
                       "yields 94-99% of target with the 1s peak still inside the cap. "
                       "H.264 climbs to 4K (1440p/1800p/2160p): Apple caps H.264 at "
                       "1080p and puts HEVC above, so this trades spec-compliance for "
                       "max-compatibility high-bitrate 4K H.264, matching the rung set "
                       "of the fixed-segment ladders it is compared against.",
        "seed": True,
        "maxrate_percent": 100,
        "bufsize_multiplier": 0.25,
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
                       "not confounded by peak. Split as maxrate 100% + 0.25x rather "
                       "than 110% + 0.15x: both satisfy the bound at T=1s, but the "
                       "first gives 7.5 frames of buffer instead of 4.5, which lifts "
                       "delivery from 91% to 94-99% of target AND brings the measured "
                       "1s peak back under the cap (110%/0.15x breached it at 540p). "
                       "GOP matched to the segment (1s), which is what makes this a "
                       "different ENCODE rather than a repackaging. NOTE gop == "
                       "segment means LL-HLS parts are INDEPENDENT only at segment "
                       "boundaries, so a player cannot join mid-segment: the "
                       "low-latency cost of a long GOP.",
        "seed": True,
        "maxrate_percent": 100,
        "bufsize_multiplier": 0.25,
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
    "netflix-av1": {
        "description": "MEASURED from a live Netflix player, not designed (Ladder B in issue #394). NETFLIX'S OWN STREAM TAGS: EVEAV1, MCCLEAREN_AV1, av1-hd-bitrate-capped, identPOI. 'av1-hd-bitrate-capped' is undocumented and is the best available explanation for how low this ladder tops out: every AV1 stream seen carrying it was served far cheaper at the same resolution than an untagged one (875 vs 2409 kbps at 1080p on two different titles). STREAM: av01.0.04M.08 (AV1 Main, profile 0, 8-bit, SDR), 24.000 fps, audio mp4a.40.5 HE-AAC 2.0 at 128 kbps. Served when the DISPLAY is SDR - dynamic range alone selects this ladder over the Dolby Vision one, live, mid-playback. MEASURED: title 81646429, 2026-09-13/14, 59 cap steps at 5% from 0.2 to 3.39 Mbit/s, every step guarded on title + codec + tags. RUNG NOTES: steps 1.38x-1.92x with no outlier. 63 + 97 share 342p as measured (a hold-resolution rung). 152->209 is the TIGHTEST step (1.38x) and therefore sets the largest peak/avg a VBR encode can carry before a rung's peak overlaps the next rung's average. 543->1020 is the 1080p hold rung: 1.88x the bits for +1 VMAF. 1020->1962 is the widest step (1.92x) and jumps 1080p straight to 4K - there is no 1440p rung, unlike the Dolby Vision ladder. Top-rung VMAF 103 is SATURATED; do not read it as quality headroom. The top rung may also be an artifact of the sweep stopping at a 3.39 Mbit/s cap, just above it. RESOLUTIONS NUDGED: Netflix repeats a resolution on two pairs (608x342 at 63+97, 1920x1080 at 543+1020). HLS variant dirs are named <height>p, so duplicates collide - the LOWER rung of each pair is stepped down by the smallest exact-16:9 increment (18px of height: 324p, 1062p) and the measured resolution is kept on the upper rung. Same device apple-uniq uses. DO NOT COPY THE BITRATES: they are the OUTPUT of per-shot optimisation on one piece of Netflix material. Three adjacent episodes of that show already disagree by up to 16%. Our encoder at 543 kbps/1080p will not reach their reported VMAF 95. DELIVERY PROFILE IS OURS - none of it is observable from a player: 6s segments, 0.2s LL-HLS parts, 6s GOP, VOD VBV (200% / 2.0x). With gop 6 == segment 6, parts are INDEPENDENT only at segment boundaries.",
        "seed": True,
        "maxrate_percent": 200,
        "bufsize_multiplier": 2.0,
        "segment_duration": "6",
        "partial_duration": "0.2",
        "gop_duration": "6",
        # Explicit: the derived tag would be "6s", which apple-uniq-live-6s
        # owns. Both Netflix ladders share this tag safely because their
        # codecs differ, and the codec is already in the directory name.
        "output_tag": "nf6s",
        "codecs": {
            "av1": _SEED_NETFLIX_AV1,
        },
    },
    "netflix-dv-hevc": {
        "description": "MEASURED from a live Netflix player, not designed (Ladder A in issue #394). NETFLIX'S OWN STREAM TAGS: CE4_DoVi_DO_v1, MCCLEAREN_DV, NOP, identPOI; content keys issued for 540/1080/2160. STREAM: dvhe.05.01 (HEVC Main 10, Dolby Vision profile 5), 24.000 fps, audio mp4a.40.5 HE-AAC 2.0 at 128 kbps. Served only when the DISPLAY is HDR - same machine, monitor, cable and session: toggling macOS HDR switched the ladder live, mid-playback. Browser, connector and colour gamut were each tested and none of them moved it. MEASURED: title 81646429, 2026-09-13/14, 5% cap steps, with 26 steps from 1.3 to 4.4 Mbit/s across the big gap alone. RUNG NOTES: the ladder climbs resolution fast at the bottom, then holds 4K and buys quality - five rungs at 2160p, the turn being at 778 kbps. Steps are ~1.5x except 1154->4213, a REAL 3.65x gap: swept at 5% throughout and the player never chose anything inside it. It sits exactly where VMAF saturates (94->100). 4213/8366/11846 score 100-102 = SATURATED, so they exist for bandwidth-rich clients, grain and dark scenes rather than for metric gain. The bottom rung's VMAF 30 is genuinely poor. A 1440p rung exists here (528 kbps) where the AV1 ladder has none. RESOLUTIONS NUDGED: the five 4K rungs are all 3840x2160 as measured and HLS variant dirs are named <height>p, so they would collide. The four lower ones step down by the smallest exact-16:9 increment (18px each: 2088p/2106p/2124p/2142p) and the measured 2160 stays on the top rung. Same device apple-uniq uses. DO NOT COPY THE BITRATES: they are the OUTPUT of per-shot optimisation on one title, and ours is an SDR encode, not Dolby Vision - these numbers describe an encode we do not produce. Their VMAF column is NOT comparable with the SDR ladder's (VMAF's standard model is built for SDR). Use this for the rung SHAPE. DELIVERY PROFILE IS OURS - none of it is observable from a player: 6s segments, 0.2s LL-HLS parts, 6s GOP, VOD VBV (200% / 2.0x). With gop 6 == segment 6, parts are INDEPENDENT only at segment boundaries.",
        "seed": True,
        "maxrate_percent": 200,
        "bufsize_multiplier": 2.0,
        "segment_duration": "6",
        "partial_duration": "0.2",
        "gop_duration": "6",
        # Explicit: the derived tag would be "6s", which apple-uniq-live-6s
        # owns. Both Netflix ladders share this tag safely because their
        # codecs differ, and the codec is already in the directory name.
        "output_tag": "nf6s",
        "codecs": {
            "hevc": _SEED_NETFLIX_DV_HEVC,
        },
    },
}

DEFAULT_LADDER = "apple-uniq-live-xs"


class LadderError(ValueError):
    pass


def _store_path() -> str | None:
    """Filesystem path to the persisted ladder store, or None.

    The Go control plane owns the store (ladders.json) and sets LADDER_STORE
    on worker containers; we also probe the mounted state/temp dirs as a
    fallback. STATE_DIR leads because that is where the store lives once it has
    been moved out of $TMP_DIR (#331); the rest are the pre-#331 locations, and
    a default install still finds it there.
    """
    import os
    p = os.environ.get("LADDER_STORE")
    if p:
        return p
    for env in ("STATE_DIR", "TMPDIR", "ENCODER_TMP_ROOT", "TMP_DIR"):
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


# Default pass count when a ladder does not pin one. MUST equal
# LadderDef.passesFor's fallback in internal/encode/ladder_store.go.
#
# This is a MIRROR, not a reference, and the local-dist path uses THIS copy:
# cli_local_dist resolves the ladder in Python, so Go's value never reaches it.
# When h264 moved from 1 to 2 the Go side was changed alone, and four
# full-length encodes came out single-pass while their output tags said "2p" —
# nothing failed, the bitrates just stayed low. Only the worker's
# `ENCODER-ARGV ... pass=0` markers gave it away.
#
# scripts/test_ladder_passes.py asserts the two agree.
_DEFAULT_PASSES = 2


def ladder_passes(ladder_def: dict, codec: str) -> int:
    """Encode pass count for a codec on this ladder, falling back to
    _DEFAULT_PASSES when unset."""
    n = (ladder_def.get("passes") or {}).get(codec)
    if isinstance(n, int) and n > 0:
        return n
    return _DEFAULT_PASSES


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
