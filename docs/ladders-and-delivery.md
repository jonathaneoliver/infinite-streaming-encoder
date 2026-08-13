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

Apple's guidance, which the chart colours against: **<=1.25x avg for live,
<=2x for VOD**.

The live ladders are deliberately **peak-matched** at that 1.25x cap, so a
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
changed, and it is the cost of re-choppability. What changed is HOW the 1.25x
allowance is split between the two knobs.

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

`apple-uniq-vod` carries an EXPLICIT tag because it pins 6s and would otherwise
derive `6s` — the same as `apple-uniq-live-6s`, which is a different encode
entirely. Two encodes, one output directory, second overwrites first. Segment
duration is a good default name, not a unique one; the encode form refuses a
selection whose derived tags collide.

The four `apple-uniq-live-*` ladders share one 12-rung h264 set (to 2160p) so a
comparison between them is like-for-like at every rung.

No stored ladder sets `output_tag`; every tag seen on disk is either derived
(`xs`) or was typed per encode.

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
