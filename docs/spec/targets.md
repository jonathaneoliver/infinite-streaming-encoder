# Targets: `local` and `cloud`, and what genuinely differs

Two ways to run the same encode. The design goal is that they differ in *where
work happens* and in nothing else, because a difference in output makes every
quality comparison meaningless. This file specifies where that holds and where
it does not.

## The core model

**The same Python encoder runs on both targets. Only the scheduler differs.**

| concern | `local` | `cloud` |
| --- | --- | --- |
| decides what runs when | Temporal workflow | Step Functions state machine |
| decides which machine | Temporal worker pool | AWS Batch |
| shared blob store | MinIO | S3 |
| unit of identity | workflow ID | execution name |

Neither scheduler can be dropped from its side: Step Functions cannot run a
container or manage a fleet, and Batch has no concept of "these 336 jobs are one
encode" — it is a queue, not a graph.

### Why the encoder is shared and the scheduler is not

The encoder is where quality lives. If `local` and `cloud` produced different
bytes, no measurement taken on one would transfer to the other, and the whole
point of having a cheap local path is to iterate before paying for a cloud run.
Schedulers, by contrast, are exactly the part that has no portable
implementation — so they are the seam.

## The rules that must hold

1. **`cli_phase` is the single phase implementation.** Both orchestrators shell
   out to the same subcommand for any phase they run themselves, with
   `--s3-in` / `--s3-out` pointing at a local path. A host phase is the same
   code as the worker phase, not a reimplementation.
2. **Both targets run the mezzanine on the HOST.** The source is already on the
   control plane's disk; uploading it so a worker can download it back and
   stream-copy it was a full round trip to run `ffmpeg -c copy`.
3. **Both targets package on the HOST by default**, for every codec unless
   disabled.
4. **Neither host phase required a state-machine change**, and that is the rule
   to follow if a third phase moves: each reuses a Choice the graph already had.
   `mezz_cached` routes past Mezzanine; `do_h264`/`do_hevc`/`do_av1` gate nothing
   but the per-codec packaging branch.
5. **`do_h264` means "the STATE MACHINE packages h264"**, not "h264 was
   encoded". `buildSFNInput` computes them as `doX && !packageOnHost`. A caller
   needing the set of encoded codecs must take the union with `host_package`, or
   a host-packaged run reports itself as encoding no codecs.
6. **Host packaging is forced OFF when the run is leaving its media in S3.** The
   two features want opposite things — one exists so segments never come home,
   the other cannot package without pulling every chunk. Honouring both would
   fetch the whole ladder and upload the packaged result back, which is strictly
   more transfer than either alone.
7. **There is no `upload:source` stage row on the local path**, because nothing
   stages the source. A declared stage that never fires is a row that sits
   pending forever.
8. **`download:outputs` is dropped from the plan when the state machine packaged
   nothing.** Same rule, cloud side: `_emit_plan` takes `sync_back`.
9. **Retries are Batch's, not the state machine's.** SFN's `Retry` covers only
   `Batch.AWSBatchException` / `States.Timeout` — submit-time blips. What retries
   a spot reclaim is the job definition's `retry_strategy`
   (`attempts = 3`, `evaluate_on_exit` on `HostTerminated`).
10. **Host phases have NO retry, deliberately.** A packaging failure fails the
    job rather than being resubmitted onto fresh capacity. The trade is minutes
    of local CPU on a machine already up, against an hour of spot-reclaimable
    encoding.
11. **Each codec gets its own `ENCODER_WORK_DIR`.** `cli_phase` rmtree's it on
    entry, so a shared scratch would delete a sibling codec's inputs — and only
    ever on a multi-codec run.
12. **`ENCODER_TELEMETRY_EXEC` is explicitly unset for host phases.** On the host
    stdout *is* the channel to the server, and the orchestrator is the telemetry
    queue's consumer; leaving it set would have it drain its own output back.

**Enforced by:** rule 5 by `cmd_poll`'s run-plan construction; rules 7–8 by
`test_dist_stage_state` and `_emit_plan`; rule 6 by config resolution. Rules 1–4
are **architectural, not enforced** — nothing fails if a fourth phase is
reimplemented rather than reused.

## Blast radius — what does NOT change

Moving a phase to the host changes `infra/` **not at all**. Rebuild the server
and it takes effect. That is a property worth protecting: it means the
local/cloud split can be retuned without a Terraform apply, and therefore
without the deploy hazard below.

The chunk plan is identical on both paths — see [`chunk-plan.md`](chunk-plan.md).
`chunkplan_test.go` pins the Go planner to golden vectors from the Python one
precisely so this stays true.

## Where they genuinely differ

| difference | why |
| --- | --- |
| chunk sizing modes | `local` applies ONE chunk duration to every variant; `cloud` sizes per variant |
| telemetry transport | `local`: stdout → Temporal heartbeat/result. `cloud`: stdout → CloudWatch **and** a per-execution SQS queue |
| failure of a host phase | `local` recovery is *worse*: a Retry mints a new job ID and therefore a new staging prefix, so it re-encodes everything. Re-running against the **same `--job-prefix`** reuses the staged chunks |
| stage row liveness | on `local`, host-packaged package/fragments/hls rows are LIVE and move independently; on the worker path the three can only move together, on completion |
| cost | `local` costs wall time; `cloud` costs money — see [`cost.md`](cost.md) |

**The local path has no `_STAGE_RANK` chokepoint.** `progress.emit_stage` is a
plain print and Go's `upsertStage` is last-writer-wins, so nothing stops a cell
walking backwards. `_SELF_RUN_STAGES` is the narrow guard: it names the activity
IDs this process drove itself, so the workflow-history reader cannot re-announce
them. The cloud path *does* have the chokepoint (`_emit_stage`), which refuses to
announce anything but `done` over an existing `done` — and `failed` is
deliberately not final there, because a reclaimed chunk is retried and
`failed → running` is real.

## The trade

| option | what it costs | what it buys | status |
| --- | --- | --- | --- |
| host mezzanine | no Batch retry for it | removes the source upload and an entire Batch job | shipped (#266) |
| host packaging | no Batch retry; egress is the CHUNKS pulled, not the packaged output | removes the pkgall job, its queue wait, and `download:outputs` | shipped (#197) |
| worker packaging (`PACKAGE_ON_HOST=0`) | queue wait and a full-ladder round trip | retry on fresh spot capacity | available, not default |

## As it stands

The state machine's shape is a **superset of what a default run does**: the
graph still contains Mezzanine and the PackageAll tasks and their job
definitions are still registered, but a default run skips both via Choice
states.

Batch job definitions pin a content-hash `IMAGE_TAG` rather than `:latest`. The
farm's workers take `:latest` unless `make farm-test-up` gave them a throwaway
tag.

**The job definition is the contract between the two layers, and it is where
they break.** A `Ref::` added on the Batch side that the SFN caller does not
supply fails at submit (#176), and `make deploy` mid-run deregisters job-def
revisions — pulling the contract out from under a live execution.

## What is unmeasured

- **Whether local and cloud output is byte-identical** for the same source and
  ladder. Divergences have been found and fixed individually (#171/#173/#175);
  no standing check asserts it.
- **The cost of the no-retry trade on host phases.** No packaging failure has
  been observed in production, so the risk named in #197 is still theoretical.
- **Pre-flight fleet uniformity.** `make fleet-check` answers the question after
  a chunk has run, not before (#248). A mixed-version fleet's symptom is
  telemetry that is a subset and reads as complete.
