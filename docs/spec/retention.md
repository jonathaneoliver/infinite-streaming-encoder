# Retention: what is reclaimed, when, and what is structurally exempt

Four stores accumulate: local job staging, MinIO, S3, and superseded outputs.
Each has a sweeper, and all four share one design rule that is worth stating
before the specifics.

## The core model

**Eligibility is decided by SHAPE where possible, and by age only within a
shape.** A sweeper that decides purely on age has to be told about every kind of
thing that shares its directory, and that list is a thing someone forgets. A
sweeper that first asks "is this the kind of thing I reclaim?" is wrong only
when a new kind of thing is *named like* an old one.

`$TMP_DIR` is the worked example: it holds four kinds of thing and only one is
garbage, so eligibility is the `^[0-9]+$` job-ID directory shape.

### Why not just age everything

`$TMP_DIR` also holds the learned state that sizes every chunk plan
(`encode_speeds.json`, `quality-curves.json`), the ladder *configuration*
(`ladders.json` — user-authored, not learned) and the permanent record (`logs/`,
`history.md`, `failed/`). All of it is old by construction. An age rule would
take the lot on its first pass.

## The four sweepers

| store | sweeper | interval | default max age |
| --- | --- | --- | --- |
| `$TMP_DIR/<job_id>/` | `internal/tmpstage` | 30 min | 24 h (`TMP_STAGING_MAX_AGE_H`) |
| MinIO `jobs/<prefix>/` | `internal/diststage` | 30 min | 24 h (`DIST_STAGING_MAX_AGE_H`) |
| S3 failed staging | `awswatch` `gc_failed_staging` | 30 min | 1 h — **inert, see below** |
| `OUTPUT_DIR/.archive/` | `internal/outarchive` | 6 h | 30 days (`OUTPUT_ARCHIVE_MAX_AGE_D`) |

Plus two backstops that are not sweepers: the S3 bucket's `jobs/` lifecycle rule
(re-asserted on every dist sweep) and the success-path cleanup each orchestrator
does for itself.

## The rules that must hold

1. **`MaxAge` of 0 means DISABLED, at both the loop and the sweep.** Read
   literally it puts the cutoff at now and takes everything on the first pass.
2. **Every sweeper takes a keep-list of what is live.** `tmpstage` gets
   `ActiveJobIDs()` plus the `jobs/*.json` state files; `diststage` gets
   `ActiveDistPrefixes()`. A running encode can never be reclaimed out from
   under itself.
3. **Idle is measured over the whole tree, not the top-level directory's mtime**,
   which an encode writing deep inside never touches. The walk stops at the first
   recent file, so a live directory costs a few stats and only a doomed one is
   walked in full.
4. **`failed/<job_id>/` is job-ID-shaped but NESTED**, so only the top level is
   scanned.
5. **`dist_staging` requires an explicit MinIO endpoint and never falls back to
   the default boto3 chain.** Without that guard it resolves to real AWS S3 and
   starts deleting the *cloud* bucket's staging.
6. **The staging key is defined once.** `encode.DistJobPrefix` →
   `jobs/<jobID>-<base>/` is shared by the orchestrator's `--job-prefix` and the
   GC's keep-list. Deriving it separately in either place is how you get a GC
   that deletes a running encode's chunks.
7. **The telemetry-queue sweep must never run unscoped.** `--state-machine-arn`
   is what fills the keep-list; `_active_execution_cores` returns an *empty* set
   without it, and a run outliving the 1 h message retention sits at zero
   messages looking exactly like an orphan. So an unscoped sweep can delete a
   live run's queue. The CLI requires the flag and `maybeGCTelemetryQueues`
   declines without `STATE_MACHINE_ARN` — **degrading open here would be worse
   than the leak.**
8. **That sweep has two triggers and needs both**: `cmd_submit`, and the
   server's hourly `cli_batch gc`. Submit alone meant an orphan waited for the
   next cloud encode — and waited forever once you stopped encoding (#191).
9. **An archived output whose base output no longer exists is NOT swept by
   default.** It is not a superseded copy, it is the last one. Ten of the
   master's were in exactly that state; `OUTPUT_ARCHIVE_SWEEP_ORPHANS` opts in.
10. **`.keep` in an archived directory makes it permanent.** Nothing writes it —
    it is for a human keeping an A/B pair as evidence.
11. **A multipart upload killed mid-flight leaves parts that hold space but do
    not appear in a `list_objects_v2` scan.** The dist sweep aborts stale ones,
    because nothing else can see them. MinIO silently drops an
    `AbortIncompleteMultipartUpload` directive paired with `Expiration`, which is
    why this lives in the GC rather than in the lifecycle rule.

**Enforced by:** rule 1 by `tmpstage`/`outarchive` tests; rules 9–10 by
`internal/outarchive` tests and `scripts/test_output_archive.py`, verified
against a fixture server rather than by unit test alone. Rules 5–8 are
**enforced by refusal** — the code declines to act rather than acting wrongly.

## Blast radius — what does NOT change

The job-ID shape is the load-bearing contract. Give job IDs a prefix and
`tmpstage` stops seeing them — a silent leak. Name anything else in `$TMP_DIR`
with digits alone and it becomes eligible for deletion. See
[`job-lifecycle.md`](job-lifecycle.md).

Superseded outputs moving to `.archive/` (#332) changed *where*, not *whether*:
a re-encode has always preserved the copy it replaced under a dated name.

## The trade

| option | what it costs | what it buys | status |
| --- | --- | --- | --- |
| 24 h idle window on job staging | a day of disk held after a crash | that window IS the debugging window for a crashed job | shipped |
| in-place superseded copies | 168 dirs / ~158 GB against a 208 GB `OUTPUT_DIR`, and `/api/outputs` walking every one | nothing | replaced (#332) |
| `.archive/` + age sweep | a 30-day clock on evidence | one directory to skip beats N entries to stat | shipped |
| sweeping orphans by default | would delete the last copy of an output | — | rejected, opt-in only |

## As it stands

**`gc_failed_staging` is inert and has been since 2026-07-22.** Its eligibility
shape is a `<prefix>_FAILED` object, and the only thing that ever wrote one was
the EC2 user-data retired with `cli_cloud.py` in `5c4b581`. Nothing in the tree
writes `_FAILED` now, so every `head_object` it issues 404s and it reaps
nothing — while still costing a `list_objects_v2` per pass plus a head per job
prefix, against a bill where LIST count is the dominant term (see
[`cost.md`](cost.md)). Failed cloud staging is therefore reclaimed by the
bucket's `jobs/` lifecycle rule on the `staging_retention_days` clock, not on
the 1 h clock this row advertises. The shape rule is right; its writer is gone,
which is the failure mode a shape-based sweeper has and an age-based one does
not.

Typical local-dist staging is **~2.3 GB per file** (mezzanine, per-chunk
encodes, variants, packaged output). The source itself is no longer staged, so
that is one transfer rather than three.

Manual controls: `make minio-usage` / `make minio-clean`,
`GET /api/dist/staging`, `POST /api/dist/staging/gc`, and the AWS-tab clear
paths — every one of which must call `invalidateS3Prefixes` and
`MarkGoneUnderPrefix`, because deleting staging silently invalidates outputs
that point at it.

## What is unmeasured

- **Whether 30 days is the right archive window.** Chosen as "how long might I
  want to compare against the old one", not measured against actual use.
- **How much the 24 h staging window actually costs** on a busy day. It is a
  disk high-water mark nobody has recorded.
- **Whether the orphan case is common.** Ten instances were found in one audit;
  no trend exists.
