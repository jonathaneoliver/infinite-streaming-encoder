"""Commercial cloud-encoder cost model.

Estimates what a job's output ladder would have cost on a commercial hosted
transcoder, as a comparison baseline against our own spot / local cost — the
control plane surfaces it per job as "would've been $X on a commercial cloud
encoder".

Pricing is modeled on a commercial cloud encoder's public per-output-minute
rates (captured 2026-07). The service bills per OUTPUT MINUTE, per rendition,
with multipliers + small add-ons. Only the components our pipeline exercises are
charged; unused features (gif, smart thumbnails, extra destinations, per-GB
repack container) default to 0 and are listed so the model is easy to complete.

Everything is a pure function of the plan (ladder) + source probe, so any
orchestrator can call estimate_usd() once and print an ENCODER-COMMERCIAL marker.
"""
from __future__ import annotations

import math

# Transcoding rate ($/output-minute) by resolution tier x codec column.
# Codec columns: h264 -> Base, hevc -> HEVC/VP9, av1 -> AV1.
_TIERS = [  # (max_output_height, {codec: usd_per_min}) — SD is *below* 720p
    (719,        {"h264": 0.005, "hevc": 0.008, "av1": 0.010}),  # SD (<720p)
    (1080,       {"h264": 0.010, "hevc": 0.015, "av1": 0.020}),  # HD (720–1080)
    (1440,       {"h264": 0.025, "hevc": 0.038, "av1": 0.050}),  # 1440p
    (10 ** 9,    {"h264": 0.045, "hevc": 0.068, "av1": 0.090}),  # 4K+
]

# Add-ons ($/minute).
_REPACK_PER_MIN = 0.003   # packaging each rendition to HLS/DASH ("repack")
_AUDIO_PER_MIN = 0.001    # one audio track, when the source has audio


def _rate(height: int, codec: str) -> float:
    for max_h, rates in _TIERS:
        if height <= max_h:
            return rates.get(codec, rates["h264"])
    return _TIERS[-1][1]["h264"]


def _fps_multiplier(fps: float) -> int:
    """+100% for each additional 30 fps over 30 (30->1x, 60->2x, 90->3x)."""
    return max(1, math.ceil((fps or 30) / 30))


def _bitrate_multiplier(src_mbps: float) -> float:
    """+25% for each 50 mbps of source bitrate over 50 mbps."""
    if not src_mbps or src_mbps <= 50:
        return 1.0
    return 1.0 + 0.25 * math.ceil((src_mbps - 50) / 50)


def estimate_usd(rungs_by_codec, duration_s: float, *, fps: float = 30.0,
                 has_audio: bool = False, src_mbps: float = 0.0) -> float:
    """Commercial-cloud-equivalent cost for the whole output ladder.

    rungs_by_codec: {codec: [rung, ...]} where each rung has a `.height`.
    Multipliers (fps, source bitrate) apply to the transcoding term; repack is
    charged per rendition and audio once for the source's track.
    """
    minutes = max(0.0, duration_s or 0.0) / 60.0
    if minutes <= 0:
        return 0.0
    mult = _fps_multiplier(fps) * _bitrate_multiplier(src_mbps)
    total = 0.0
    for codec, rungs in rungs_by_codec.items():
        for r in rungs:
            total += minutes * _rate(r.height, codec) * mult   # transcode
            total += minutes * _REPACK_PER_MIN                 # package
    if has_audio:
        total += minutes * _AUDIO_PER_MIN
    return round(total, 4)


# --- AWS Elemental MediaConvert -------------------------------------------
# On-demand, per OUTPUT minute (us-east-1) "normalized minute" model: rate by
# resolution tier, Basic tier for H.264, Professional tier for advanced codecs
# (HEVC/AV1); doubled above 30 fps.
_MC_BASIC = ((719, 0.0075), (1080, 0.0150), (10 ** 9, 0.0300))   # SD(<720) / HD / UHD
_MC_PRO = ((719, 0.0170), (1080, 0.0340), (10 ** 9, 0.0680))


def _mc_rate(height: int, codec: str) -> float:
    table = _MC_BASIC if codec == "h264" else _MC_PRO
    for max_h, rate in table:
        if height <= max_h:
            return rate
    return table[-1][1]


# --- our own AWS Batch spot fleet ------------------------------------------
# Predict what THIS ladder would cost on our cloud path. Unlike the SaaS models
# above (per output-minute), this is COMPUTE-based: each variant's encode
# wall-time × its vCPU allocation × the spot rate — so it weights a slow 4K HEVC
# 2-pass rendition ~200× a fast 360p h264 one. Encode speed is modeled per
# codec/resolution/pass (mirrors the control plane's seeded speed model), so all
# variants + resolutions + codecs are accounted for individually.
_SPEED_1080P = {"h264": 1.5, "hevc": 0.14, "av1": 0.1}   # content-s per wall-s @1080p, 1-pass
_AWS_SPOT_VCPU_HR = 0.013     # Graviton (c7g) spot ≈ $/vCPU-hr
_AWS_VCPU_PER_VARIANT = 2     # each encode runs on ~2 vCPU (matches ENCODE_THREADS)


def _encode_wall_s(codec: str, height: int, two_pass: bool, content_s: float) -> float:
    base = _SPEED_1080P.get(codec, 0.14)
    sp = base * (1080.0 * 1080.0) / (max(1, height) ** 2)   # scale ~1/pixels
    if two_pass:
        sp /= 2
    return content_s / max(sp, 0.001)


def aws_spot_usd(rungs_by_codec, duration_s: float, *, hevc_two_pass: bool = True) -> float:
    """Compute-based cost to run the ladder on our AWS Batch spot fleet, summed
    over EVERY variant (codec × resolution × pass) by its modeled encode work."""
    minutes = max(0.0, duration_s or 0.0)
    if minutes <= 0:
        return 0.0
    vcpu_h = 0.0
    for codec, rungs in rungs_by_codec.items():
        tp = codec == "hevc" and hevc_two_pass
        for r in rungs:
            vcpu_h += _encode_wall_s(codec, r.height, tp, duration_s) * _AWS_VCPU_PER_VARIANT / 3600.0
    return round(vcpu_h * _AWS_SPOT_VCPU_HR, 4)


def mediaconvert_usd(rungs_by_codec, duration_s: float, *, fps: float = 30.0) -> float:
    """AWS MediaConvert-equivalent cost for the ladder. H.264 → Basic tier,
    HEVC/AV1 → Professional tier; ×2 above 30 fps."""
    minutes = max(0.0, duration_s or 0.0) / 60.0
    if minutes <= 0:
        return 0.0
    mult = 2 if (fps or 30) > 30.5 else 1
    total = 0.0
    for codec, rungs in rungs_by_codec.items():
        for r in rungs:
            total += minutes * _mc_rate(r.height, codec) * mult
    return round(total, 4)
