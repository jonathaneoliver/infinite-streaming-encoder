# Chunk planning: how a clip is divided

The chunk is the unit of parallelism and the unit of loss. This is how its size
is chosen, what constrains it, and why the plan and the media can legitimately
disagree about length.

## The core model

**Encode-chunk size and delivery-segment size are different numbers.** A chunk
is a resumable unit of *encoding*, sized so a lost worker or a reclaimed spot
instance costs little. A segment is what a player fetches, fixed by the ladder's
delivery profile. One chunk contains a whole number of segments.

Everything below follows from that separation. It is the oldest surviving idea
in the project and it outlived a complete rewrite of the orchestration around
it.

### Why chunk size is chosen per variant, not per job

A 2160p HEVC rung and a 234p H.264 rung differ by more than an order of
magnitude in encode time. One chunk size for the whole job either makes the
cheap rungs pay per-chunk overhead they do not need, or leaves the expensive
rungs with chunks so long that a reclaim is costly. Sizing per variant from
*learned speed* lets every rung finish at roughly the same time, which is what
determines the job's makespan.

The cost is that the plan depends on learned state (`encode_speeds.json`), so a
cold model plans badly until it has samples.

## The modes

`ChunkDuration` selects the strategy:

| value | behaviour |
| --- | --- |
| `""` or `dynamic` | size each variant from its learned encode speed against a wall-time target |
| `whole` | one job per variant, no chunking, no join |
| a number | fixed seconds, the same for every variant |

On the `local` target the per-variant modes collapse: `cli_local` applies one
chunk duration to every variant, so `""`/`dynamic` mean a fixed **2 × segment**
(12 s at the 6 s default).

## The rules that must hold

1. **Chunk boundaries land on segment boundaries.** A chunk that ends
   mid-segment produces a segment that cannot be assembled from one chunk.
2. **The duration limit is snapped to the nearest whole segment**, with a floor
   of one segment. Nearest rather than floor, so a 2 s request on 6 s segments
   yields one segment rather than zero. Both languages round half **away from
   zero** — Python's `round()` is banker's rounding and would disagree with Go's
   `math.Round` at exactly half a segment.
3. **A limit that reaches the clip length is not a limit.** `TimeLimitFor`
   drops it and the whole clip is encoded, because a limit can never describe
   more media than exists.
4. **An unknown clip duration KEEPS the limit.** `clipDurationS <= 0` means "not
   probed"; dropping the limit on a number nobody measured would encode the
   whole clip when a short one was asked for.
5. **An unparseable or non-positive `Time` means no limit**, identically to
   leaving it blank. The field is free text.
6. **The limit is part of the mezzanine cache key.** The mezzanine is only a
   pure function of the source while the *whole* source is copied; keying on the
   source alone would serve a 30 s mezzanine to every later full encode of that
   file. An unset limit hashes exactly as before, so existing cached mezzanines
   still hit.
7. **The chunk plan is a deliberate PREFIX of a limited mezzanine, not a match.**
   `-t` on a stream copy cuts on packet boundaries, so a 10 s limit yields
   ~10.07 s of media. Media *shorter* than the plan is fatal; an overshoot is
   expected.
8. **The whole-job chunk budget is a ceiling on the entire execution**, not per
   variant (#316). A Step Functions execution's history is capped at 25,000
   events; the plan is held to 80% of that at an estimated 8 events per chunk —
   **2,500 chunks**. When a plan does not fit, the wall-time target is doubled
   until it does.
9. **A fixed `--chunk-duration` that cannot survive its own execution is
   refused at submit** (#328/#312), rather than launching spot capacity and
   dying hours later when the history fills.

**Enforced by:** rule 1 and the Go/Python agreement by `chunkplan_test.go`,
which pins the Go planner to golden vectors generated from the Python one —
both paths must cut a clip in the same places or local and cloud encodes stop
being comparable. Rules 2–5 by `TimeLimitSeconds`/`TimeLimitFor` and their
tests. Rules 8–9 by `chunkbudget.go` and `chunkbudget_test.go`. Rule 7 is
enforced by `cli_phase`'s plan-vs-media check, whose tolerance **flips** under a
limit.

## Blast radius — what does NOT change

Nothing downstream applies the duration limit a second time. It is applied in
**exactly one place** — `cli_phase`'s mezzanine step truncates the mezzanine,
and every variant, chunk and the audio are cut from that (#184). The chunk plan,
the ladder's chunk sizing, the cost estimate and the progress totals all describe
the truncated clip without any of them knowing a limit exists, because
`cli_local_dist` clamps the probed duration immediately after probing and
`buildSFNInput` clamps `clipDurationS`.

`TIME_LIMIT_S` travels as an **environment variable, never a `Ref::`
parameter**: a caller that does not set it gets the full clip, rather than a job
definition that fails to launch (#176).

## The trade

| option | what it costs | what it buys | status |
| --- | --- | --- | --- |
| dynamic per-variant | depends on learned state; a cold model plans badly | rungs finish together, so makespan tracks the slowest *chunk* not the slowest *rung* | shipped, default |
| `whole` | a reclaim loses the entire variant | no join, maximum encode efficiency | shipped |
| fixed seconds | uniform loss exposure, ignores rung cost | reproducible plans for A/B comparison | shipped |
| raising the wall-time target to fit the budget | halves peak concurrency, lifts the makespan floor | the run completes instead of dying at ~5,100 chunks | shipped (#316), explicitly rationing |
| `Mode: DISTRIBUTED` | unbuilt | removes the 25,000-event ceiling entirely | open (#313) |

## As it stands

The budget constant `sfnEventsPerChunk = 8` is **an estimate, not a
measurement**. It has never been checked against a real
`get_execution_history`. Everything downstream scales by it: if the true figure
is 6, the budget is 4,166 rather than 2,500 and a 4 h HEVC encode nearly fits
untouched. Being wrong high is the safe direction, which is why it has not been
tuned down on a guess.

For HEVC especially, **chunk count is the parallelism** —
`variantResourcesFor` reserves a flat 2 vCPU because x265 tops out around two
cores — so the budget's rationing is felt there first.

## What is unmeasured

- **`sfnEventsPerChunk`.** One call against a finished execution turns the
  constant into a measurement. Nothing has made it.
- **The right loss-exposure target.** Chunk sizing currently assumes reclaims
  happen; sizing against AWS's published per-type interruption *frequency*
  would justify much larger chunks and less per-chunk overhead. Raised, not
  resolved.
- **What a 2 h or 4 h source actually does.** The budget arithmetic for those
  lengths exists (#312), but no clip of that length has been encoded end to end.
