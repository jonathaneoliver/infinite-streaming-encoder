# Ladders, delivery options and output tags

What a "ladder" actually is in this codebase, what each field does, and the rules
that connect them. Written because none of it was documented and a mis-specified
profile shipped an unplayable output as a result.

`docs/apple-ladder-design.md` is the historical design of the **rung model** and
predates everything below.

## A ladder is not just bitrates

A ladder is **the rung table AND the delivery profile**, in one object
(`LadderDef`, `internal/encode/ladder_store.go`), persisted to
`$STATE_DIR/ladders.json` (which defaults to `$TMP_DIR`, its pre-#331 home). It
is user configuration, not learned state — unlike `encode_speeds.json` and
`quality-curves.json` which sit beside it. Nothing else holds a copy of what you
author here, which is why the directory is now something you can point at a
backed-up volume.

| field | what it is |
| --- | --- |
| `codecs` | the rungs: `codec -> [[width, height, kbps], ...]` |
| `maxrate_percent` | VBV ceiling, as a % of target bitrate |
| `bufsize_multiplier` | VBV buffer, as a multiple of target bitrate |
| `segment_duration` | HLS segment length, seconds |
| `partial_duration` | LL-HLS part length; `"0"` = off (plain VOD, no parts) |
| `gop_duration` | keyframe interval, seconds |
| `output_tag` | suffix on the output directory name |
| `extra_args`, `passes` | per-codec encoder extras |

**`""` means "inherit the global default"; `"0"` means an explicit zero.** That
distinction is load-bearing for `partial_duration` — `""` inherits parts, `"0"`
turns them off.

### Why they are one object and not two

It is tempting to split "the ladder" (rungs) from "the delivery profile"
(timing + VBV), since four profiles over the same rungs duplicate the rung
table. Two reasons not to:

1. The struct already models it as one, and `output_tag`'s own comment states
   the intent — a tag distinguishes a **repackage-once** profile from the
   default **repackage-into-1s/2s/6s** one.
2. **The ladder chart is only meaningful for a specific (rungs + VBV + T)
   triple.** `_ladderBandsCharts` draws peak/avg bands from
   `maxrate% + bufsize/T`; separating the halves leaves nothing to draw until a
   profile is chosen.

The cost of keeping them together is duplicated rung tables across profiles that
differ only in timing. Those can **drift** — and if they do, a comparison
intended to isolate VBV silently becomes a comparison of bitrates, with the
charts still looking correct. See #286.

## VBV: peak is a function of the chop length

The peak bitrate a window of length `T` can reach is:

```
peak / avg  =  maxrate_percent/100  +  bufsize_multiplier / T
```

A **large** buffer lets a **short** segment burst. This is the whole reason a
tight VBV is needed if one encode is to be cut at several segment durations:

What the chart colours against: **<=2x avg for VOD, <=1.25x avg for live**.
Only the first of those is Apple's. Read the spec carefully, because the two
halves are not the same kind of rule:

| rule | says | is it peak/avg? |
| --- | --- | --- |
| 1.30 (VOD) | peak SHOULD be no more than **200% of the average bit rate** | **yes** |
| 1.26/1.27 (VOD) | average and peak MUST be **within 10%** of the declared `AVERAGE-BANDWIDTH` / `BANDWIDTH` | no — accuracy of the declaration |
| 1.28 (live) | average over ~1h MUST be **< 110% of `AVERAGE-BANDWIDTH`** | no — declaration again |
| 1.29 (live) | measured peak MUST be **< 125% of `BANDWIDTH`** | no — measured against the DECLARED peak, not the average |

**There is no peak/avg bound for live in the spec.** 1.29's 125% is the
tolerance between what you measure and what you advertised, so a live stream
satisfies it at any peak/avg as long as `BANDWIDTH` is honest. The 1.25x here is
OURS: holding peak within 1.25x of average is what keeps a rung's declared
`BANDWIDTH` close to its average, and adjacent rungs therefore far enough apart
for a player selecting on `BANDWIDTH` to use them. It is stricter than Apple
requires, and the reason to keep it is re-choppability at 1s, not compliance.

The live ladders are deliberately **peak-matched** at that 1.25x figure, so a
comparison between them is not confounded by peak. What differs is the BUFFER
each can afford, which is what committing to a segment length buys:

| ladder | maxrate | bufsize | frames | at its own T | at 1s |
| --- | --- | --- | --- | --- | --- |
| `apple-uniq-live-xs` (flexible) | **100%** | **0.25** | 7.5 | 1.04x at 6s | **1.25x** |
| `apple-uniq-live-1s` | **100%** | **0.25** | 7.5 | 1.25x | 1.25x |
| `apple-uniq-live-2s` | 110% | 0.30 | 9.0 | 1.25x | (n/a) |
| `apple-uniq-live-6s` | 110% | 0.90 | 27.0 | 1.25x | (n/a) |
| `apple-uniq-vod` | 200% | 2.00 | 60.0 | 2.33x at 6s | — |

The flexible base still pays the 1s price at every length — that has not
changed, and it is the cost of re-choppability. What changed is HOW our 1.25x
budget is split between the two knobs.

**The split matters as much as the bound.** `peak/avg = maxrate% + bufsize/T`,
so at T=1s every point of maxrate given up buys 0.01x of buffer directly. The
ladders originally spent that allowance on maxrate (110%) and left the buffer at
0.10x — **three frames**. A three-frame buffer cannot absorb a keyframe, and
x264 respects VBV strictly, so rather than risk violating it the rate control
stays conservative everywhere and the average lands far below `-b:v`:

| bufsize | frames | delivered, as % of target |
| --- | --- | --- |
| 0.10x | 3.0 | **64-68%** |
| 0.15x | 4.5 | 91% |
| 0.25x | 7.5 | **94-99%** |
| 0.30x | 9.0 | 96-99% |
| 0.90x | 27.0 | 99-104% |

Two measurements made the trade obvious. First, the peak never reached more than
**86-92% of its own maxrate at any rung** — the ceiling was never what
constrained the encode, the buffer was. Second, `100%/0.25x` peaks *lower* over
1s windows than `110%/0.15x` despite a 67% bigger buffer, because the reduced
ceiling does the containing. See #292.

Express the buffer in FRAMES (`bufsize_multiplier x fps`) rather than as a
multiple of the bitrate and this stops being surprising: the number is
rung-independent, and single-digit frames is not a burst allowance, it is a
straitjacket.

The default `bufsize_multiplier` is **0.25x**. It was **2x** until July 2026,
which was the pre-#829 value from smashing's `create_abr_ladder.sh`; the port
had silently inherited the loose one, which is why re-chopping tolerance had
been lost. Ladder VBV reaching **both** the local and cloud paths has regressed
once since (#167).

## GOP: three drivers that disagree

`gop_duration` is not derivable from `segment_duration`, because three separate
requirements push it in different directions:

| driver | wants |
| --- | --- |
| compression efficiency | GOP as long as possible -> `gop = segment` |
| LL-HLS independent parts | GOP **shorter** than segment, so a player can join mid-segment |
| choppability | GOP = the **smallest** duration the encode might be cut to |

Observable in the output: `EXT-X-PART ... INDEPENDENT=YES` appears every 5th
part in a `gop 1.0` / `partial 0.2` profile — 5 x 0.2s = **1.0s**, exactly the
GOP. A player can join every second instead of every six.

`apple-uniq-live-6s` **used** to use `gop 1.0`, making it an LL encode that
happened to use 6s segments rather than a native 6s one. It is now `gop 6`, and
`-1s` / `-2s` match their segments too — which is what makes each of them a
distinct ENCODE. With VBV and GOP identical they would have been one encode
packaged three ways, and there would be nothing to compare.

The trade is visible in the same place: `gop = segment` means parts are
`INDEPENDENT` only at segment boundaries, so a player can no longer join
mid-segment. That is the low-latency cost of a long GOP.

**Short GOP is the price of choppability**, and it is a real cost: six times the
keyframes at `gop 1.0` versus `gop 6`. How large that cost is has not been
measured, and it bounds the value of the whole one-encode-serves-all idea.

It is NOT cheap to find out from the Ladders tab as it stands: those VMAF
estimates are keyed by `(codec, height, bitrate, clip)` and ignore VBV and GOP
entirely, so every ladder here reports the same numbers — including the GOP
difference this paragraph is about. Measuring it means a real audit run, and
recording which encode settings the resulting curve belongs to. See #288.

### Constraints

- `segment % gop == 0` — otherwise segments do not start on keyframes. This is
  the one that silently produces unplayable output.
- `gop % partial == 0` when parts are on — otherwise `INDEPENDENT=YES` cannot
  land on a part boundary.
- `gop <= segment` — a GOP longer than the segment means segments with no
  keyframe.

None of these are currently enforced.

## Output tags

The tag is appended to the output directory name, **after the codec**:

```
<stem>_p<partial>[_padblack|_padpink]_<codec>[_<tag>]
```

e.g. `insane_fpv_..._p200_h264_xs`. CLAUDE.md's naming contract requires the tag
go last so the `_p200_<codec>` shape that `OutputStem`, `resolveCodec`,
`parseOutputMeta` and the watcher key off stays intact.

### Derivation

`deriveOutputTag` (`internal/encode/job.go`), mirrored in the page by
`_ladderSuffix`:

```
explicit tag set           -> use it
no pinned segment_duration -> "xs"     (flexible; go-live repackages into 1/2/6s)
pinned segment_duration    -> "<n>s"   (6 -> "6s"; served as-is)
```

Equivalent spellings collapse (`6` and `6.0` are one profile, not two
directories), and a zero, negative or unparseable segment derives no tag at all
rather than inventing `0s`.

It can be set three ways, in increasing precedence: the derivation above, the
ladder's own `output_tag`, and a per-encode override (the encode form's tag box,
`--output-tag` on the CLI).

### What the tags MEAN

`_6s`, `_2s` and `_1s` describe themselves: the output is served at that segment
length, as encoded. `_xs` does not, and the difference matters more than the
spelling suggests.

**`_xs` is an interface with a system that is not in this repository.** It marks
the FLEXIBLE base — the one master go-live repackages into 1s, 2s and 6s — and
go-live keys off the tag to decide which outputs it may re-chop. Those three
lengths are go-live's repertoire, not an arbitrary set, which is also why the
four `apple-uniq-live-*` ladders exist: they are the natively-encoded arm of the
comparison against them. The name is not a size, either — "xs" reads as
extra-small and means *any chop*.

**Nothing in THIS repository reads the tag back.** Every use here is
concatenation into a directory name, plus `ValidPathSegment` on the way in and
the encode form's collision check at submit. No code branches on the value. So
renaming `xs`, or letting a ladder derive it by accident, breaks nothing that
any test or any encode here would notice — the damage is entirely downstream,
and `TestFlexibleBaseStillDerivesXs`'s failure message is the only place in the
tree that says so.

**`_xs` is a claim about the encode, and no check ties it to the claim.** The
flexible base's VBV is split 100% maxrate + 0.25x buffer precisely so that
`peak/avg = 1.00 + 0.25/T` still lands on our 1.25x live figure **at T=1s** (see
the VBV section: that figure is ours, not a rule from Apple's spec) — holding it
at the shortest chop is what makes the encode safe to re-chop at all. But the tag is derived from `segment_duration` being unset and
from nothing else. A ladder with looser VBV that pins no segment derives `xs`
just the same, and asserts a re-choppability its own numbers do not support.
**Not enforced** — the relation is in `ladder_store.go`'s description of
`apple-uniq-live-xs` and in the VBV section above, and in no guard.

### The gap this closed

Pinned-segment ladders used to derive `""`. That held while there was ONE of
them; with 6s, 2s and 1s ladders all three produced `<stem>_<codec>` and
silently overwrote each other. It is also why a `_6s` output once had to be
tagged by hand, and why it landed as `..._h264__6s` — the typed value carried
its own separator.

Deriving from the segment length fixed that, but it does NOT make the tag
unique and cannot: two profiles can pin the same length and differ in
everything else (see `apple-uniq-vod` above). Explicit tags remain necessary;
the submit-time collision check is what makes their absence loud rather than
destructive.

## The ladders as they stand

| ladder | maxrate% | bufsize x | seg | part | gop | tag |
| --- | --- | --- | --- | --- | --- | --- |
| `apple` | — | — | — | — | — | `xs` (derived) |
| `apple-uniq` | — | — | — | — | — | `xs` (derived) |
| `apple-uniq-live-xs` | 100 | 0.25 | — | 0.2 | 1.0 | `xs` (derived) |
| `apple-uniq-live-1s` | 100 | 0.25 | 1 | 0.2 | 1 | `1s` (derived) |
| `apple-uniq-live-2s` | 110 | 0.30 | 2 | 0.2 | 2 | `2s` (derived) |
| `apple-uniq-live-6s` | 110 | 0.90 | 6 | 0.2 | 6 | `6s` (derived) |
| `apple-uniq-vod` | 200 | 2.00 | 6 | 0 | 6 | **`vod` (explicit)** |
| `netflix-av1` | 200 | 2.00 | 6 | 0.2 | 6 | **`nf6s` (explicit)** |
| `netflix-dv-hevc` | 200 | 2.00 | 6 | 0.2 | 6 | **`nf6s` (explicit)** |

`apple-uniq-vod` carries an EXPLICIT tag because it pins 6s and would otherwise
derive `6s` — the same as `apple-uniq-live-6s`, which is a different encode
entirely. Two encodes, one output directory, second overwrites first. Segment
duration is a good default name, not a unique one; the encode form refuses a
selection whose derived tags collide.

The four `apple-uniq-live-*` ladders share one 12-rung h264 set (to 2160p) so a
comparison between them is like-for-like at every rung.

No stored ladder sets `output_tag`; every tag seen on disk is either derived
(`xs`) or was typed per encode.

## The Netflix ladders: measured, then altered

`netflix-av1` and `netflix-dv-hevc` are the only ladders here whose rungs were
**not chosen**. They were read out of a live Netflix player while a transparent
bridge walked the client's downlink cap up in 5% steps, and each rung's
resolution and bitrate come from the player's own diagnostics. The full record,
with the method and its caveats, is [issue
#394](https://github.com/jonathaneoliver/infinite-streaming-encoder/issues/394);
this section says only what was CHANGED to make them ladders in this system.

They are seeds rather than user ladders because reference data that lives in one
box's `ladders.json` is not a reference — a state wipe, a fresh install or a
second machine each loses it, and the measurement cost most of a day on real
hardware.

**What is theirs, unaltered:** every bitrate, the rung count, and the ordering.
`netflix-av1` is 8 rungs from 63 to 1962 kbps (AV1, 8-bit SDR, 24 fps);
`netflix-dv-hevc` is 11 rungs from 111 to 11846 kbps (Dolby Vision HEVC, same
title, selected purely by the DISPLAY's dynamic range).

**What we changed, and why:**

1. **Resolutions, where Netflix repeated one.** HLS variant directories are
   named `<height>p` (`manifests._resolution_name`), so a ladder that reuses a
   resolution collides with itself — one rung overwrites the other. Netflix
   repeats 608x342 (63 + 97 kbps), 1920x1080 (543 + 1020) and 3840x2160 (all
   five of the DV ladder's top rungs). The **lower** rung of each repeat steps
   down by the smallest exact-16:9 increment and the measured resolution stays
   on the **upper** one:

   | ladder | as measured | as stored |
   | --- | --- | --- |
   | `netflix-av1` | 608x342 @ 63 | **576x324** @ 63 |
   | `netflix-av1` | 1920x1080 @ 543 | **1888x1062** @ 543 |
   | `netflix-dv-hevc` | 3840x2160 @ 778 / 1154 / 4213 / 8366 | **3712x2088 / 3744x2106 / 3776x2124 / 3808x2142** |

   18px of height is the minimum step that stays exactly 16:9 with even
   dimensions, because an exact 16:9 height must divide by 18. It is the same
   device `apple-uniq` uses on Apple's own duplicate (768x432 at 730 and 1100
   became 704x396 and 768x432). Keeping the measured resolution on the TOP rung
   of each group matters: that rung is then the pixel-exact one, so the sharpest
   scaling and the highest bitrate coincide and quality still rises
   monotonically.

   The cost is a ~1.7% pixel reduction at an unchanged bitrate, so a nudged
   rung's bits/pixel runs ~1.7% high against Netflix's figure. Negligible beside
   1.38x-1.92x rung steps, but systematic — do not read a nudged rung as a
   like-for-like against the measured number.

2. **The delivery profile is entirely ours.** Segment duration, partials, GOP
   and VBV are **not observable from outside a player**, so they had to be
   chosen rather than copied: 6s segments, 0.2s LL-HLS parts, 6s GOP, and the
   VOD VBV (200% / 2.0x) on the grounds that these are VOD encodes and a loose
   VBV is what lets per-shot bit allocation show up at all. With `gop == segment`,
   parts are INDEPENDENT only at segment boundaries — the same tradeoff
   `apple-uniq-live-6s` carries.

3. **An explicit `nf6s` tag**, for the reason `apple-uniq-vod` carries `vod`:
   pinning 6s would otherwise derive `6s` and collide with
   `apple-uniq-live-6s`. Both Netflix ladders share the one tag safely, because
   their codecs differ and the codec is already in the directory name.

**One rung per codec column, and no h264.** Each ladder carries only the codec
it was measured on, so a job selecting h264 against either fails with the #289
"no rungs for this codec" error. That is the intended behaviour: there is no
measured h264 ladder to invent one from.

**The bitrates are not targets.** They are the output of Netflix's per-shot
optimiser on one title, and #394's own data shows three adjacent episodes
disagreeing by up to 16% at 4K. Our encoder at 543 kbps/1080p will not reach the
VMAF 95 the player reported. These exist to measure our ladders AGAINST, and for
the SHAPE — climb resolution fast at the bottom, then hold the top resolution
and buy quality.

## Measured: what bit depth and preset actually cost (AV1)

Five arms of one clip at two rungs, each differing from the baseline in ONE
thing. The point is the ratios; the absolute seconds are this machine on this
content, and the absolute VMAF is this content at a deliberately starved
bitrate.

**Exactly what was encoded** (measured 2026-09-17; nothing here generalises
beyond this configuration):

| | |
| --- | --- |
| source | `insane_fpv_shots_hydrofoil_windsurfing.mkv` — 3840x2160, AV1, 8-bit, 29.97fps (30000/1001), 334s |
| window | the **first 30s** (`time: "30"`, already a whole number of 6s segments, so nothing was snapped) |
| codec | **AV1 only** (libsvtav1) |
| **rungs** | **two, run separately**: `1920x1080 @ 1020 kbps` and `3840x2160 @ 1962 kbps` — the `netflix-av1` 1080p and TOP rungs, each isolated into single-rung `nf-av1-1080-*` / `nf-av1-2160-*` ladders so a job is one encode |
| profile | 6s segments, 0.2s parts, **6s GOP** (keyint 180), 2-pass, burn-in **OFF** |
| VBV | 200% / 2.0x on the ladder, **inert for av1** (see below) |
| **where** | **one machine — the MacBook Pro.** macmini and ubuntu disabled via `POST /api/dist/workers/{machine}`, the UI's own toggle |
| **how** | **`chunk_duration: "whole"`** — no chunking, so each arm is ONE ffmpeg process, and arms run one at a time |
| image | `dev-worktree-zippy-sleep-b124048` — ffmpeg `N-126593-gbc46eab87c-20260916`, SVT-AV1 `v4.2.0-73-gfb0ed7e59` |
| VMAF | `vmaf_4k_v0.6.1` for both rungs, scored against the same 30s of source, both sides converted to 10-bit, 901 frames per arm. The 1080p arms are upscaled to 2160p first (what a viewer on a 4K screen sees); the 2160p arms are native, no resampling |

Every arm differs only in `extra_args`. **Delivered bitrate is measured** — the
rung directory's bytes over the media's real duration — not assumed from the
target, because an arm that quietly spent less is being judged on a smaller
budget.

### 1080p @ 1020 kbps

| arm | delivered | encode | vs base | mean | vs base |
| --- | --- | --- | --- | --- | --- |
| preset 6, 8-bit (what the pipeline does) | 1027.1 kbps | 48.1s | 1.00x | 49.25 | — |
| 10-bit, preset 6 | 1027.3 kbps | 52.9s | **1.10x** | 49.63 | **+0.37** |
| 10-bit, preset 4 | 1026.3 kbps | 76.0s | 1.58x | 50.01 | +0.75 |
| 10-bit, preset 2 | 1027.2 kbps | 253.9s | 5.28x | 51.16 | +1.91 |

All four delivered 1027 kbps to within 1 kbps — a genuinely equal-budget
comparison.

| arm | min | p5 | p25 | med | p75 | p95 | mean |
| --- | --- | --- | --- | --- | --- | --- | --- |
| preset 6, 8-bit | 4.9 | 19.0 | 33.9 | 46.9 | 68.4 | 79.7 | 49.25 |
| 10-bit, preset 6 | 4.9 | 18.6 | 33.5 | 47.5 | 69.1 | 81.0 | 49.63 |
| 10-bit, preset 4 | 5.9 | 18.8 | 33.2 | 48.1 | 70.2 | 82.4 | 50.01 |
| 10-bit, preset 2 | 6.2 | 19.1 | 34.0 | 49.6 | 72.2 | 83.3 | 51.16 |

### 2160p @ 1962 kbps

| arm | delivered | encode | vs base | mean | vs base |
| --- | --- | --- | --- | --- | --- |
| preset 6, 8-bit | 1950.7 kbps | 125.9s | 1.00x | 53.67 | — |
| 10-bit, preset 6 | 1952.5 kbps | 153.0s | **1.22x** | 53.72 | **+0.05** |
| 10-bit, preset 4 | **1872.6 kbps** | 227.6s | 1.81x | 54.21 | +0.54 |
| 10-bit, preset 2 | **1866.0 kbps** | 716.0s | 5.69x | 54.63 | +0.96 |
| 10-bit, preset 2, target +5% | 2038.8 kbps | 843.9s | 6.70x | **55.40** | **+1.73** |

| arm | min | p5 | p25 | med | p75 | p95 | mean | harm |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| preset 6, 8-bit | 6.1 | 20.2 | 36.5 | 57.8 | 71.0 | 80.3 | 53.67 | 43.57 |
| 10-bit, preset 6 | 5.8 | 20.1 | 36.3 | 57.3 | 71.0 | 80.9 | 53.72 | 43.53 |
| 10-bit, preset 4 | 5.7 | 20.1 | 36.8 | 57.2 | 72.6 | 81.6 | 54.21 | 43.81 |
| 10-bit, preset 2 | 5.3 | 20.2 | 37.2 | 58.3 | 73.7 | 82.2 | 54.63 | 44.03 |
| 10-bit, preset 2, +5% | 5.3 | 20.2 | 37.3 | 59.7 | 74.0 | 83.2 | 55.40 | 44.48 |

### What the numbers say

**10-bit earns its keep at 1080p and NOT at 2160p** — the opposite of the
intuition that it should matter most where quantisation is heaviest. At 1080p it
buys +0.37 mean for +10% encode time. At 2160p it buys **+0.05 for +22%**, and
its harmonic mean is *lower* than the baseline's (43.53 vs 43.57) — noise, not a
gain. At 4K the per-pixel budget is so small that quantisation error dwarfs the
rounding error 10-bit removes, and this content has no smooth gradients for it
to protect. Cost rises with resolution (bytes per sample double, and a 4K
working set stops fitting in cache) exactly as the benefit falls.

**The slow presets UNDERSPEND at 2160p, which hides their gain.** Preset 4 and
preset 2 delivered ~1870 kbps against the baseline's 1951 — 4.5% fewer bits for
the same target, because better decisions reach the rate-distortion point with
less. Raising preset 2's target by 5% (to 2060, delivering 2039) recovered
**+0.77 mean**, so preset 2's real gain over base is ~**+1.7**, not +0.96. A
comparison at nominally equal targets is not a comparison at equal bitrate.

**The bits-to-VMAF slope at this rung is ~0.084 VMAF per 1% of bitrate**
(+0.77 over +9.2%), and it converts the preset question into a budget one:
buying preset 2's ~+1.7 with bitrate instead would take roughly 20% more bits at
preset 6 and NO extra CPU, against 5.7x the encode time. Which lever is right
depends on whether CPU or bandwidth is the scarce resource — for a delivery
ladder it is usually bandwidth, which is the case FOR the slow preset.

**Every arm is flat or slightly WORSE at p5/p25 and better from the median up.**
The hardest frames are bitrate-starved and no amount of encoder effort rescues
them; the gain lands in the moderate frames. A mean alone hides that, which is
why the percentiles are recorded.

### How the measurement was got wrong first — six times

Each of these produced a confident, plausible, wrong table:

1. **A chunked farm measurement cannot answer "what does this option cost".**
   Three chunks land on three boxes that differ ~3x in speed, and each arm draws
   a different mix — so the totals compare machines, not options. Measured
   against the single-machine truth, the farm overstated every cost: 10-bit by
   **27%** (1.66x vs 1.22x), preset 4 by 25%, preset 2 by 16%. The smallest
   effect was distorted most. The farm answers "when is my encode done"; one
   box, one process answers "what does this cost".
2. **Concurrency.** Under `MAX_CONCURRENT=2` two arms shared the fleet and two
   did not, which made preset 4 look FASTER than the baseline (42.8s vs 52.1s
   active). See "Four numbers for how long did this job take".
3. **`active` is not encoder time.** It is the union of every stage, and about
   half of a 30s job is mezzanine + audio + packaging + dispatch. At job level
   10-bit measured 0.97x — i.e. nothing. Only the `encode:*` stages move.
4. **Chunk content differs.** chunk2 (the clip's last 10s) was consistently the
   cheapest in every round, so which box drew it changed the total.
5. **`-t` before the OUTPUT does not truncate the reference.** With `-f null`
   the source decodes to the end of the clip while the 30s distorted stream
   freezes on its last frame: 10,022 frames scored for a 899-frame window, mean
   25, min 0.58. `-t` goes before each `-i`. **The frame count is the check** —
   every table above is 901 frames.
6. **Alignment must be proven.** Score a short window with the distorted stream
   shifted -1/0/+1 frames: +1 collapsed to 26.09 against 48.15 aligned, so the
   pair is not running ahead. The chunk joins are the other tell — 3 chunks over
   30s put keyframes at frames 300 and 600, and the scores spike there
   (57.4 -> 75.9, 41.9 -> 58.1), which can only line up if the timelines do.

**The reference can be the source.** `mezzanine.py` builds the mezzanine with
`-c copy`, so its frames ARE the source's and the first 30s of either is the
same pixels — convenient, because local-dist's mezzanine lives in
`$TMP_DIR/<jobid>/.mezz-work` and is deleted on every terminal outcome. Audio
and mezzanine are in any case excluded by summing only `encode:*`: audio
measured 4.1s in every arm, and the mezzanine was a cache hit throughout.

### Two knobs reachable from a ladder, one not

`extra_args` are appended AFTER the codec args, and ffmpeg takes the LAST
occurrence of an option, so a ladder can override what the encoder hardcodes —
verified, not assumed (`-preset 6 ... -preset 4` yields preset 4, and
`-pix_fmt yuv420p ... -pix_fmt yuv420p10le` yields 10-bit):

| knob | from a ladder? | how |
| --- | --- | --- |
| bit depth | yes | `"av1": "-pix_fmt yuv420p10le"` |
| preset | yes | `"av1": "-preset 4"` (av1 presets are NUMBERS; the pipeline hardcodes 6) |
| scaler flags | **no** | the scale filter is built inside `burnin.build_filter`; a second `-vf` replaces the whole chain, burn-in and tpad included |

**`-maxrate`/`-bufsize` never reach av1 at all** — libsvtav1 rejects a peak cap
outside CRF mode ("Max Bitrate only supported with CRF mode"), so
`encode_variants` omits them for that codec and av1 runs plain VBR to the target
average. A ladder's VBV fields are therefore inert for AV1: they shape h264 and
hevc only.

**10-bit is for HEVC and AV1, never h264.** H.264 High 10 has essentially no
hardware decode support, and the h264 ladder exists for maximum compatibility.

### The trap this experiment walked into

**A preset set through `extra_args` is invisible to the learned-speed model.**
`speedKey` is `machine:codec:height:passes:preset:fps` and takes `preset` from
`Rung.preset` — which is `"medium"` for every av1 rung, because the av1 preset
is hardcoded in `_codec_specific_args` and an override arrives later in the
argv. So preset 2 chunks (**5.3x slower at 1080p, 5.7x at 2160p**) write samples
onto the same key as preset 6, and `dynamicChunkSecondsAt` then sizes av1 chunks
from a blend of three presets.

It is the #314 shape exactly: one rule, copied somewhere that cannot see the
override. Running these arms contaminated `av1:1080` and `av1:2160` on this
fleet; `scripts/purge_speed_keys.py` clears them, with the server stopped. The
real fix is for the key to carry the preset that was USED.

## The intended experiment

The reason the tight-VBV work exists: **compare 1s/2s/6s segments cut
dynamically from one tight-VBV encode against content encoded natively for each
duration with looser live-TV VBV.** If the tight arm holds up, one encode can
serve all three durations instead of three.

Status: all four ladders exist as built-ins and are peak-matched, so the
comparison can be run. Nothing has been encoded through them yet.

One caveat before reading results: **VMAF estimates in the Ladders tab are keyed
by (codec, height, bitrate, clip) and ignore VBV and GOP**, so all four ladders
report identical numbers — including the GOP difference the experiment exists to
measure. See #288.
