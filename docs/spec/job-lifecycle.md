# Job lifecycle: five states, and what moves between them

A job is the unit the operator sees, cancels and resumes. This is what each
state means, what transitions exist, and — the part that is easy to get wrong —
which of them touch `OUTPUT_DIR`.

## The core model

**A job is a queue slot plus a list of files, and only the final move is
visible.** Every file encodes into `$TMP_DIR/<job_id>/`; nothing appears in
`OUTPUT_DIR` until the whole job has succeeded. There is no partial delivery.

### Why the move is at the end and not per file

Per-file promotion would let a three-file job leave one finished output and two
absences, which is indistinguishable from a job that was only ever asked for one
file — and `resolveCodec` would then skip the missing two on the next run,
making the truncation permanent and silent. Deferring the move makes a failed
job leave *nothing*, which is unambiguous.

The cost is real: a long multi-file job that fails on the last file discards
work that was genuinely finished. `Resume` is what repays it — see below.

## The states

| state | meaning |
| --- | --- |
| `queued` | submitted, waiting on the `MAX_CONCURRENT` semaphore |
| `running` | holds a slot; encoding, or moving to output |
| `done` | every file encoded and moved to `OUTPUT_DIR` |
| `failed` | the encode failed, **or** it succeeded and the move failed |
| `cancelled` | the operator cancelled it |

There is no separate `move-failed` state: a failed move sets `failed` with the
error prefixed `"encode succeeded but move failed: "`. The distinction survives
in the message rather than in the status, because everything a caller does with
it — offer Resume, keep the tmp dir — is the same.

## The rules that must hold

1. **`OUTPUT_DIR` is written only on the success path.** A `failed` or
   `cancelled` job leaves nothing there.
2. **A failed job's `$TMP_DIR/<job_id>/` is PRESERVED; a successful or cancelled
   one's is removed.** The preserved copy is the diagnostic artifact — partial
   variant MP4s and remote logs — and is what the 24 h staging sweep later
   reclaims (see [`retention.md`](retention.md)).
3. **Per-file state is persisted before each file starts**, to
   `$TMP_DIR/jobs/<id>.json`, and removed on any terminal outcome. `Reconcile`
   reads those files on startup and re-enters `run` at the recorded file index.
4. **A worker container's name is deterministic** — `encoder_job_<id>_f<idx>` —
   and `runFileContainer` reattaches to an existing one rather than starting a
   second. This is what makes an encode survive a restart of the server's own
   container.
5. **A container that EXISTS but never RAN is not reattachable** (#323). Its
   logs are empty and its exit code reads 0, which would be taken as success, so
   it is removed and re-run instead.
6. **`Resume` re-runs a failed or cancelled job IN PLACE** — same `*Job`, same
   ID, one row in the list — and only from `failed` or `cancelled`.
7. **Because the ID is unchanged, so is the staging prefix**, so already-encoded
   chunks are found and skipped. Resume is cheap by construction, not by
   special-casing.
8. **Cancel is idempotent and terminal-safe**: cancelling a `done`/`failed`/
   `cancelled` job succeeds and does nothing.
9. **A job cancelled while still queued never starts.** `run` re-checks the flag
   after acquiring its slot.
10. **`history.md` and a per-job log are written on every terminal outcome**,
    success or not.

**Enforced by:** rules 3–5 by the reconcile/container tests and #323's fixture;
rules 6–7 by `Resume` and the local-dist chunk-reuse path. Rules 1–2 are
**enforced by construction** in `run`'s single terminal block rather than by a
test asserting absence.

## Blast radius — what does NOT change

The job ID's **all-digits shape** is a contract with `internal/tmpstage`, which
uses it to tell a reclaimable job directory from the caches and learned state
sharing `$TMP_DIR`. Giving IDs a prefix silently stops the sweeper seeing them;
naming anything else in `$TMP_DIR` with digits alone makes it eligible. See
[`retention.md`](retention.md).

The container-name format is a contract between `runFileContainer` and
`Reconcile`. Changing it loses the ability to reattach to workers started by an
older server — which is the restart-resilience property, not a cosmetic one.

## The operations

| operation | route | from states | effect |
| --- | --- | --- | --- |
| cancel | `POST /api/jobs/{id}/cancel` | queued, running | marks cancelled, stops the worker container |
| resume / retry | `POST /api/jobs/{id}/retry` | failed, cancelled | same job re-runs, reuses staged chunks |
| redo | `POST /api/jobs/{id}/redo` | done | re-encode; see [`outputs.md`](outputs.md) for what happens to the existing output |
| logs | `GET /api/jobs/{id}/logs` | any | the captured worker output |
| live | `GET /api/jobs/stream` | — | SSE: full list on connect, then updates |

Cancel stops the worker with `docker stop --time 30` rather than `kill`, so a
cloud orchestrator's SIGTERM handler can release its capacity. That grace period
is the reason a cancel is not instant.

## The trade

| option | what it costs | what it buys | status |
| --- | --- | --- | --- |
| move-at-end (current) | a late failure discards finished files | no partial output is ever visible or mistaken for complete | shipped |
| resume in place | the job list shows one row that changes state, so the history of attempts is in the log rather than the list | chunk reuse for free, no new prefix | shipped |
| resume as a new job | a clean audit trail per attempt | new ID → new staging prefix → re-encodes everything | rejected |

## As it stands

`MAX_CONCURRENT` gates concurrent `run`s, not submissions. Progress is a
line-scanned view of worker stdout: the latest line, ANSI-stripped, becomes
`job.Progress`, and the log buffer is capped at 1000 lines and trimmed to the
last 500.

`-race` is not optional on the test suite: #196 added it after proving a real
SSE data race between `json.Marshal(job)` and `upsertStage`.

## What is unmeasured

- **How often Resume is used against a failure versus a cancel**, and therefore
  whether the chunk-reuse payoff is mostly recovering from spot loss or from
  operator interruption.
- **The `require-idle` TOCTOU window.** `make deploy` checks for a running job
  at entry, then `publish` takes minutes before anything is bounced; a job
  submitted in that window is unguarded and the guard still reports idle. Named
  and understood, not closed.
- **Whether a multi-file job is common enough** for the move-at-end trade to
  matter in practice. Most observed jobs are single-file.
