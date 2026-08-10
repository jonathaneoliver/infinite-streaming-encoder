# Ladders, delivery options and output tags

What a "ladder" actually is in this codebase, what each field does, and the rules
that connect them. Written because none of it was documented and a mis-specified
profile shipped an unplayable output as a result.

`docs/apple-ladder-design.md` is the historical design of the **rung model** and
predates everything below.

## A ladder is not just bitrates

A ladder is **the rung table AND the delivery profile**, in one object
(`LadderDef`, `internal/encode/ladder_store.go`), persisted to
`$TMP_DIR/ladders.json`. It is user configuration, not learned state — unlike
`encode_speeds.json` and `quality-curves.json` which sit beside it.

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

| ladder | 1s | 2s | 6s |
| --- | --- | --- | --- |
| `apple-uniq-live` / `-full` (110% / 0.1x) | 1.20x | 1.15x | 1.12x |
| `apple-uniq-live-6s` (150% / 1x) | 2.50x | 2.00x | 1.67x |
| `apple-uniq-vod` (200% / 2x) | 3.00x | 2.50x | 2.33x |

Apple's guidance, which the chart colours against: **<=1.25x avg for live,
<=2x for VOD**. So the tight ladder is within live guidance at *every* chop
length; the loose ones are only sane at the duration they were built for.

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

**A consequence worth stating plainly:** `apple-uniq-live-6s` uses `gop 1.0`, so
it is *not* a "native 6s encode" in the efficiency sense — it is an LL encode
that happens to use 6s segments. A genuinely native 6s encode would use `gop 6`.

**Short GOP is the price of choppability**, and it is a real cost: six times the
keyframes at `gop 1.0` versus `gop 6`. How large that cost is has not been
measured; VMAF per rung is already computed, so it is cheap to find out, and it
bounds the value of the whole one-encode-serves-all idea.

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
explicit tag set          -> use it
no pinned segment_duration -> "xs"     (flexible; go-live repackages into 1/2/6s)
pinned segment_duration    -> ""       (fixed-segment, served as-is)
```

It can be set three ways, in increasing precedence: the derivation above, the
ladder's own `output_tag`, and a per-encode override (the encode form's tag box,
`--output-tag` on the CLI).

### The gap

**Every pinned-segment ladder derives the same empty tag.** So `_1s`, `_2s` and
`_6s` ladders would all produce `<stem>_<codec>` and overwrite each other. That
is why a `_6s` output had to be tagged by hand — and why it landed as
`..._h264__6s`, with a doubled underscore, because the typed value already had
the separator.

Deriving from the segment duration instead (`6 -> "6s"`) would make the four
names automatic and collision-free. It changes existing output names, so it
touches the naming contract above. Tracked in #286.

## The ladders as they stand

| ladder | maxrate% | bufsize x | seg | part | gop | derives tag |
| --- | --- | --- | --- | --- | --- | --- |
| `apple` | — | — | — | — | — | `xs` |
| `apple-uniq` | — | — | — | — | — | `xs` |
| `apple-uniq-live` | 110 | 0.1 | — | 0.2 | 1.0 | `xs` |
| `apple-uniq-live-full` | 110 | 0.1 | — | 0.2 | 1.0 | `xs` |
| `apple-uniq-live-6s` | 150 | 1 | 6 | 0.2 | 1.0 | `""` |
| `apple-uniq-vod` | 200 | 2 | 6 | 0 | 6 | `""` |
| `legacy` | — | — | — | — | — | `xs` |

No stored ladder sets `output_tag`; every tag seen on disk is either derived
(`xs`) or was typed per encode.

## The intended experiment

The reason the tight-VBV work exists: **compare 1s/2s/6s segments cut
dynamically from one tight-VBV encode against content encoded natively for each
duration with looser live-TV VBV.** If the tight arm holds up, one encode can
serve all three durations instead of three.

Status: the tight arm's encode exists (`apple-uniq-live-full` -> `_xs`). The
native arm has only its 6s point, and that point uses `gop 1.0` rather than
`gop 6`, so it may not be measuring what it appears to. See #286.
