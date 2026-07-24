# Design: Chunked variant encoding (spot-resumable)

Status: proposed · Issue #14 sub-task · Enables cheap + resumable cloud encoding

## Goal

Make each variant encode **resumable on spot interruption** by splitting it
into independently-encoded time chunks. Today a variant is one whole-clip
Batch job: a 40-minute 4K encode reclaimed at minute 38 loses 38 minutes.
With chunking, a reclaim loses **one 30-second chunk** — seconds of work,
re-run idempotently on fresh capacity. This is the concrete mechanism behind
the project's "low cost + resumeable" goal (spot without the interruption tax).

## Core model: two decoupled granularities

The critical idea — **encode-chunk size and delivery-segment size are
different numbers**:

| | Purpose | Size | Produced by |
| --- | --- | --- | --- |
| **Encode chunk** | parallel + resumable unit of encoding | **30s** (decided) | the split step |
| **Delivery segment** | what the player fetches (HLS/DASH) | 6s | Shaka, at the end |

**Decision: 30s chunks.** With GOP = 1s and 6s segments:

```
30s chunk  =  5 × 6s segments  =  30 GOPs
```

30s keeps chunks well under AWS's "keep spot jobs ≤ 30 min" guidance while
being large enough that per-chunk overhead (container start, S3 round-trip)
is amortized. A 120s clip → 4 chunks; a 40-min clip → 80 chunks.

## Pipeline

```
mezzanine ──split──► chunk_0 chunk_1 … chunk_N     (30s each, GOP-aligned)
                        │       │        │
                     encode  encode   encode        ← parallel Batch jobs; the costly, resumable step
                     (2-pass per chunk)             ← two-pass runs per chunk
                        │       │        │
                        └───────┴────────┘
                             concat (join)           ← stream copy, ~free
                                │
                          {codec}_{tier}.mp4
                                │
                          Shaka Packager (once)      ← 6s fMP4 segments + playlists/manifests
```

Only the **encode** step is expensive. Split, join, and Shaka's segmentation
are all stream copies — no re-encode, no quality loss. We parallelize the one
costly step and leave the cheap muxing whole.

## Concrete mechanics

### Split + encode (one Batch job per chunk)

Each chunk job re-encodes a fixed time window `[t0, t0+30)` of the mezzanine.
Frame-accurate input seeking + a forced IDR at the window start make chunk
boundaries land exactly on the 30s grid regardless of where the mezzanine's
own keyframes fall:

```sh
ffmpeg -ss {t0} -t 30 -i mezzanine.mp4 \
  -vf "{scale+burnin}" \
  -c:v libx265 \
  -x265-params keyint={K}:min-keyint={K}:scenecut=0:open-gop=0:...:pass={p}:stats={chunk}.log \
  -b:v {kbps}k -maxrate {maxrate}k -bufsize {bufsize}k \
  -force_key_frames "expr:gte(t,0)" \
  -movflags empty_moov+default_base_moof -frag_duration 1000000 \
  -tag:v hvc1 {codec}_{tier}_chunk{n}.mp4
```

- `-ss {t0} -t 30` — input seek to the window, exactly 30s (last chunk is the
  remainder). Re-encoding makes the cut frame-accurate, not keyframe-limited.
- `keyint = min-keyint = K` (`K = round(fps × 1.0)`), `open-gop=0` — closed,
  fixed 1s GOPs, so every chunk starts with an IDR and has IDRs at every
  second (including the future 6s segment boundaries).
- Two-pass is unchanged from the whole-clip port — it just runs **per chunk**
  (each chunk writes/reads its own `{chunk}.log` stats).

### Join (concat, stream copy)

```sh
printf "file '%s'\n" chunk_0.mp4 chunk_1.mp4 … > chunks.txt
ffmpeg -f concat -safe 0 -i chunks.txt -c copy {codec}_{tier}.mp4
```

The concat demuxer stitches each chunk's PTS (each chunk starts at 0 after the
`-ss` reset) into a monotonic timeline. Because all chunks share identical
encode params, `-c copy` concatenation is clean.

### Package (Shaka, once per variant — unchanged)

The joined `{codec}_{tier}.mp4` feeds the existing `package` phase. Shaka sees
a continuous stream with IDRs every second and segments at 6s cleanly.

## Alignment invariants (must hold)

1. **GOP divides segment divides chunk**: 1s | 6s | 30s. Guarantees a 6s
   boundary (and an IDR) at every segment and chunk edge.
2. **Closed, fixed GOP** (`scenecut=0`, `open-gop=0`, `min-keyint=keyint`) —
   no open-GOP references across a boundary.
3. **IDR at every chunk start** — each chunk is independently decodable, which
   is what makes concat seamless and retry idempotent.
4. **Last chunk is the remainder** (clip duration mod 30). Still GOP-aligned;
   Shaka handles a short final segment.

## Resumability (the payoff)

- The chunk is the **resumable unit**. Each chunk uploads
  `{codec}_{tier}_chunk{n}.mp4` + a `.done` sidecar (size-checked), reusing the
  existing `_write_done`/`_download_if_complete` invariant.
- The **concat step** validates that all `N` chunk `.done` markers are present
  and sizes match before joining; a missing/partial chunk aborts rather than
  producing a truncated variant.
- A spot reclaim kills one chunk job. Batch's `evaluate_on_exit` retry
  re-runs **only that chunk** on fresh capacity; sibling chunks and other
  variants are untouched. Re-encode cost = one 30s window.
- Idempotent by construction: chunk `n` is a pure function of `[t0,t1)` + encode
  params, so a retry reproduces the same output.

## Rate control across chunks

Independently-encoded chunks can drift in bitrate/quality; the closed fixed GOP
already prevents a *visual* seam, but the *average* needs care:

| Approach | Average accuracy | Complexity | Status |
| --- | --- | --- | --- |
| Per-chunk VBV-capped (today) | ok | none | current |
| **Per-chunk two-pass** | good | low | **the just-landed two-pass, per chunk** |
| Distributed two-pass (global bit allocation) | best | high | future (Netflix-style) |

Per-chunk two-pass is the recommended default: each chunk hits its slice of the
target average. A globally-optimal distribution is a later refinement, not a
correctness requirement.

## Orchestration mapping (Batch + Step Functions)

Two viable shapes; recommend **A**.

**A. Batch array job per variant (recommended).** The `variant` phase becomes
a Batch **array job** of size `N` (chunk count); each element reads
`AWS_BATCH_JOB_ARRAY_INDEX` → encodes chunk `idx`. One `SubmitJob` covers all
chunks of a variant; a dependent `concat` job (array dependency) joins them.
The SFN `Map` still fans out over variants — minimal change to the state
machine, chunk fan-out pushed into Batch where it's cheap.

**B. Nested SFN Map.** Outer `Map` over `(codec, tier)`, inner `Map` over
chunks, then a `concat` task. More visible in the SFN execution view, but more
state-machine overhead and history events.

Either way the DAG gains a `concat` step between `variant` and `package`:

```
mezzanine → Map(variant): [ array(chunk 0..N) → concat ] → per-codec: package → hls → byteranges
```

## Codebase touch points

| Layer | File | Change |
| --- | --- | --- |
| Chunk math | `scripts/infinite_streaming_encoder/gop.py` / new `chunking.py` | chunk count + `[t0,t1)` windows from duration (30s, remainder) |
| Encode | `scripts/infinite_streaming_encoder/encode_variants.py` | encode a time window (`-ss/-t`, forced IDR); output `_chunk{n}.mp4`; two-pass per chunk |
| New phase | `scripts/infinite_streaming_encoder/cli_phase.py` | `phase variant` takes `--chunk-index`/`--chunk-count`; new `phase concat-variant` |
| Resume | `scripts/infinite_streaming_encoder/resume.py` | discover by `{codec}_{tier}_chunk{n}.mp4`; variant "done" = all chunks done |
| Go SFN input | `internal/encode/job.go` (`buildSFNInput`) | emit chunk count per variant (from probed duration) |
| SFN template | `infra/terraform/modules/workflow/definition.json.tpl` | array-job (or nested Map) for chunks + `concat` state |
| Batch jobs | `infra/terraform/modules/jobs/main.tf` | `infinite-streaming-encoder-variant` as array-capable; new `encoder-concat` job def |
| Local path | `scripts/infinite_streaming_encoder/cli_local.py` | optional: chunk locally too, or keep whole-clip for local (spot only matters in cloud) |

## Interactions

- **Apple ladder** (`docs/apple-ladder-design.md`): chunking is orthogonal —
  the unit becomes `(codec, rung, chunk)`. Label stays `{codec}_{label}`; chunk
  index is a suffix on the temp file only, gone after concat.
- **Two-pass**: already per-encode, so it becomes per-chunk automatically. No
  extra work beyond passing the chunk window through.

## Open questions

- **Mezzanine access per chunk.** Simplest: each chunk job downloads the full
  mezzanine and seeks (fine at 4 chunks; wasteful at 80). Options: S3 range
  reads, or a shared FSx for Lustre mount (as in AWS's `aws-batch-with-ffmpeg`
  reference) so chunks read one staged mezzanine. Decide by expected chunk count.
- **Chunk count derivation.** Probe duration in the mezzanine phase and pass
  `N = ceil(duration / 30)` into the SFN input, vs. re-deriving in each phase
  from `--ladder`-style shared config. Recommend: probe once, pass `N`.
- **Local path.** Spot resilience only matters in the cloud; local encodes can
  stay whole-clip to avoid concat overhead. Keep chunking cloud-only unless
  local parallelism is wanted.
- **Audio** is not chunked (small, single job) — the `audio` phase is unchanged.
