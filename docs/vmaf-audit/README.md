# VMAF Ladder Audit

Self-contained HTML reports auditing a full H.264 bitrate ladder with VMAF, produced
by the per-chunk quality audit (`scripts/infinite_streaming_encoder/vmaf_audit.py`)
plus a native whole-clip re-measurement. Open either file in a browser.

| Report | Question it answers |
|--------|---------------------|
| [`4k-reference.html`](4k-reference.html) | *How does each rung look on a 4K display?* Each rung upscaled to the source 4K and compared to the native 4K master with the `vmaf_4k` model. |
| [`1080p-reference.html`](1080p-reference.html) | *How clean is each rung's compression?* The 4K master downscaled to 1080p; each rung compared at 1080p. Includes the 1080p↔4K crossover. |
| [`codec-comparison-4k.html`](codec-comparison-4k.html) | *How do H.264, HEVC and AV1 compare?* Three rate-quality curves overlaid at 4K, with per-codec BD-rate. |
| [`codec-comparison-1080p.html`](codec-comparison-1080p.html) | Same three-codec comparison, graded at 1080p. |

**Codec comparison (this clip), BD-rate vs H.264 at equal VMAF:**

| | 4K reference | 1080p reference |
|---|---|---|
| HEVC (2-pass) | −26.5% | −21.2% |
| **AV1 (SVT-AV1)** | **−38.3%** | **−41.3%** |

AV1 is the most efficient (also ~16–26% under HEVC), and the gap widens with resolution. Trade-offs: AV1 is much slower to encode and has narrower playback support. All three ladders are essentially on the convex hull *except* H.264's 2124p (dominated) and 1044p (borderline) — see #91.

Each report has a rate–quality curve, a **cross-variant issues** panel (inversions,
cliffs, redundancy, dominated rungs, saturation), a per-rung table with per-30s
timeline sparklines, and explanations of mean-vs-harmonic pooling and why the two
comparison resolutions diverge.

## Subject
- **Source:** `insane_fpv_shots_hydrofoil_windsurfing.mkv` — 3840×2160, 29.97fps
  (30000/1001), 334.4s, extreme-motion FPV footage.
- **Ladder:** `apple-uniq-live-full`, H.264, 12 rungs (234p → 2160p).
- **Encode:** local Temporal fleet, 12s chunks, force re-encode.

## Method
- Each rung reassembled from `init.mp4` + 56 fMP4 segments.
- VMAF via ffmpeg `libvmaf`, `n_subsample=5` (~2005 frames/rung), `fps`-paired to
  30000/1001, run natively (macOS ffmpeg + libvmaf).
- Delivered bitrate = segment bytes × 8 / duration.

## Why these numbers are trustworthy
Both reports depend on the chunked encode being **frame-exact**. A fractional-fps
chunk-boundary bug ([#89](https://github.com/jonathaneoliver/infinite-streaming-encoder/issues/89),
fixed in [#90](https://github.com/jonathaneoliver/infinite-streaming-encoder/pull/90))
made chunked renditions run 9 frames long on this 29.97fps clip, which progressively
desynced them from the source and collapsed VMAF into noise. With that fixed, every
rung's per-30s timeline is flat — the scores reflect quality, not misalignment.

## Headline findings
- **Drop 2124p** — dominated (off the convex hull) under *both* references. The strongest call.
- **1044p is the next drop candidate** — zero efficiency headroom at 1080p, ≈1080p at 4K.
- **Diminishing returns above 1080p** — 1440p/2160p raise the mean but not the worst
  frames (harmonic plateaus, min stays ~2.5); justify them only for premium 4K delivery.
- **234p is a bandwidth-survival rung**, not a quality rung.
- No inversions; the ladder is otherwise well-ordered.
