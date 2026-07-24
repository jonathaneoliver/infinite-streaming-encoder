# Design: Apple / apple-uniq ladders (per-codec rung model)

Status: proposed · Issue #14 sub-task · Ports smashing `generate_abr` #868/#871

## Goal

Bring the `apple` and `apple-uniq` bitrate ladders from the smashing
`create_abr_ladder.sh` into this encoder, alongside the existing `legacy`
ladder. Selectable via a new `--ladder <legacy|apple|apple-uniq>` flag
(default `legacy`, so nothing changes unless asked).

- **apple** — Apple's HLS Authoring Spec bitrates. Multiple rungs share a
  resolution (e.g. HEVC has 3×540p, 2×720p, 2×1080p, 2×2160p → 12 rungs;
  H.264 → 9). Same-resolution quality steps are the point.
- **apple-uniq** — identical bitrates, but each rung gets a *unique*
  resolution (stepped down in clean 16:9 increments) so a rung is
  distinguishable by decoded frame size alone (dashboards / player metrics
  report resolution, not bitrate). Strictly monotonic in both bitrate and
  resolution.

## The core problem

Today's model assumes **one rung per resolution per codec**, and uses the
resolution name as the rung's identity. That assumption is baked into a
single unified `Tier` (one row carrying `bitrate_h264` + `bitrate_h265` +
`bitrate_av1`), which the apple ladders break three ways:

1. **Duplicate resolutions per codec** — `540p` appears 3× in Apple HEVC.
   `tier.name` is no longer unique, so `{codec}_{tier.name}.mp4`,
   `variant_stage_key`, and the HLS/DASH representation names collide.
2. **Different rung counts across codecs** — Apple H.264 has 9 rungs,
   HEVC/AV1 have 12. A single cross-codec `Tier` table can't represent that.
3. **Non-standard resolutions** — apple-uniq uses 416×234, 832×468,
   1216×684, 1856×1044, 3776×2124 — not the fixed six tier widths that
   `select_tiers` and `--max-res` filter against.

## Decision (per @jonathaneoliver)

- **Per-codec rung lists for _all_ ladders**, including legacy. Drop the
  unified cross-codec `Tier`; each codec owns an ordered list of rungs.
- **Label = resolution name, suffixed with `_<bitrate_kbps>` only when a
  resolution repeats within that codec's ladder.** No `_1/_2/_3` ordinals.
  - Legacy (unique resolutions) → bare `1080p` — **byte-for-byte unchanged.**
  - Apple HEVC 540p group → `540p_600`, `540p_900`, `540p_1600`.
  - apple-uniq (already unique resolutions) → bare `540p` etc.

  > Alternative considered: *always* suffix (`1080p_5000` even when unique).
  > Rejected — it renames every legacy output and its intra-dir playlists for
  > no benefit. Flag if uniform labels are wanted regardless.

The label is the rung's identity everywhere a resolution name is used today:
temp file stem, `.done` sidecar, S3 key, stage key, HLS/DASH representation.

## What is preserved vs. what changes

**Preserved — the durable output contract is untouched.** The packaged
output dir is still `<stem>_<codec>/` (one per codec). `OutputStem`,
`resolveCodec`, the watcher's `alreadyEncoded`, and per-codec Batch fan-out
all key on the codec, not the rung — so they need **no change**. Apple
ladders still produce exactly one packaged dir per codec.

**Changes — rung-level naming _inside_ that dir:**
- Temp variant files: `{codec}_{label}.mp4` (was `{codec}_{tier.name}.mp4`).
- HLS variant playlists + DASH representations: one per rung (more of them).
- `parseOutputMeta` (`internal/api/handlers.go`) infers resolutions from dir
  contents — must handle multiple rungs per resolution.
- The ladder-matrix UI (`internal/api/ladder.go`) — rows become rungs.

## New data model (`scripts/infinite_streaming_encoder/ladder.py`)

```python
@dataclass(frozen=True)
class Rung:
    label: str          # "1080p", "540p_900" — unique within a codec ladder
    res_name: str       # "540p" (for --max-res filtering + burn-in sizing)
    width: int
    height: int
    bitrate: int        # kbps (this codec, this rung)
    preset: str
    fontsize_tc: int
    fontsize_label: int
    burnin_x: int
    burnin_y_tc: int
    burnin_y_label: int

# Ladder = name -> codec -> ordered rungs
LADDERS: dict[str, dict[str, tuple[Rung, ...]]] = {
    "legacy":     {"h264": (...), "hevc": (...), "av1": (...)},
    "apple":      {"h264": (...), "hevc": (...), "av1": (...)},
    "apple-uniq": {"h264": (...), "hevc": (...), "av1": (...)},
}
```

Label assignment happens once, over the full codec ladder (count
resolution occurrences → suffix the repeats with `_<bitrate>`), so the
normal and resume paths derive identical labels — same invariant smashing
relies on. `select_tiers` becomes `select_rungs(rungs, max_res, source_width)`
filtering on `res_name`/`width`.

### Bitrate tables to port (from smashing `create_abr_ladder.sh`)

apple HEVC (kbps): 360→145, 432→300, 540→600/900/1600, 720→2400/3400,
1080→4500/5800, 1440→8100, 2160→11600/16800. AV1 mirrors HEVC. H.264:
540→2000, 720→3000/4500, 1080→6000/7800 (+ lower rungs).

apple-uniq HEVC steps the duplicate-resolution rungs down to unique 16:9
sizes: 540 group → 832×468 / 896×504 / 960×540; 720 → 1216×684 / 1280×720;
1080 → 1856×1044 / 1920×1080; 2160 → 3776×2124 / 3840×2160. (Full tables in
smashing lines ~595–668 — port verbatim.)

## Touch points

| Layer | File | Change |
| --- | --- | --- |
| Ladder data | `scripts/infinite_streaming_encoder/ladder.py` | `Rung` + `LADDERS` + `select_rungs` + label assignment |
| Encode | `scripts/infinite_streaming_encoder/encode_variants.py` | `_variant_path`/`variant_stage_key` key on `label`; loop over rungs |
| Local CLI | `scripts/infinite_streaming_encoder/cli_local.py` | `--ladder` flag → select codec rung lists |
| Batch phase | `scripts/infinite_streaming_encoder/cli_phase.py` | `--tier` → `--rung`/`--label`; drop fixed `choices`; pass `--ladder` |
| Resume | `scripts/infinite_streaming_encoder/resume.py` | discover by `{codec}_{label}.mp4`, not `{res}` |
| Packaging | `scripts/infinite_streaming_encoder/packager.py`, `hls.py` | representations keyed by rung label |
| Go config | `internal/encode/job.go` | `JobConfig.Ladder`; `buildSFNInput` emits per-codec rung lists (not `tier`×`codec` cross-product) |
| Go UI mirror | `internal/api/ladder.go`, `handlers.go` | rung-aware ladder matrix + `parseOutputMeta` |
| SFN template | `infra/terraform/modules/workflow/definition.json.tpl` | variant Map item = `{codec, label, width, height, bitrate}`; job def `--rung`/`--label` |
| UI | `static/index.html` | ladder selector (`legacy`/`apple`/`apple-uniq`) |

## Sequencing

1. `ladder.py` rung model + tables + label rule (pure, unit-testable in
   isolation via the LocalStack S3 harness's phase runner).
2. `encode_variants.py` + `cli_local.py` — local path end to end.
3. `cli_phase.py` + `buildSFNInput` + SFN template — Batch path.
4. `parseOutputMeta` / `ladder.go` / UI — read-side + selector.

Legacy stays the default throughout, so each step is shippable without
changing existing behavior.

## Open questions

- **`buildSFNInput` shape.** Today it emits a `{codec, tier}` cross-product.
  With per-codec rungs it must emit each codec's actual rung list (differing
  lengths). Confirm the Map `ItemSelector` carries `width/height/bitrate`
  per rung, or whether the phase re-derives them from `--ladder --codec
  --label` (less data in the state, single source of truth in `ladder.py`).
  Recommendation: pass only `{codec, label}` + `--ladder`; re-derive in
  `ladder.py`.
- **av1 under apple** — smashing mirrors AV1 onto the HEVC ladder. Keep, or
  give AV1 its own targets later?
- **Interaction with two-pass** — apple's tighter same-res steps are exactly
  where accurate averages matter most; expect `--ladder apple* --two-pass`
  to be the common pairing.
