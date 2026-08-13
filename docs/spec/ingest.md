# Ingest: how a file becomes a job

Four ways in — the watcher, the UI, the HTTP API and the CLI — all of which
converge on one `JobConfig` and one `Manager.Submit`. The behaviour worth
specifying is not the four entry points but the two decisions made on the way
through: **is this file ready**, and **what is still missing for it**.

## The core model

**Submission and work are separate decisions.** `Submit` always creates a job;
whether that job *encodes anything* is decided later, per file, by
`resolveCodec`. A submitted job whose outputs all already exist is a real job
that does no work and reaches `done`.

This matters because it is what makes the watcher safe to run continuously and
what makes re-submitting a partly-encoded file cheap. The alternative — deciding
at submit time — would need the same directory scan, would race with a running
encode, and would leave the operator with no record that they asked.

### The two gates, and why they are different

| gate | question | when |
| --- | --- | --- |
| watcher stability | has this file finished being written? | before submit, watcher only |
| `resolveCodec` | which codecs are still missing? | per file, inside the run |

Only the second applies to a manual submit. The operator pressing Encode has
already decided the file is ready; the watcher has to infer it.

## The rules that must hold

1. **The watcher submits a file only on the scan AFTER it first appears at a
   stable size.** A file must have been seen before, and its size must be
   unchanged since the previous scan. A file still being copied in grows
   between scans and is held.
2. **The first scan after startup seeds only.** It records sizes and submits
   nothing, so a restart does not re-submit the entire source directory.
3. **Re-enabling the watcher re-seeds.** Toggling it off and on again does not
   treat every existing file as new.
4. **`alreadyEncoded` consults both the in-memory job list and `OUTPUT_DIR`.**
   Deleting an output directory therefore triggers a re-encode on the next scan
   with no restart needed.
5. **`resolveCodec` returns the still-missing subset, or `""` to skip the file
   entirely.** It checks `<stem>_<codec>[_<tag>]` for each requested codec and
   narrows the codec flag to those absent. A directory counts as present only if
   it contains at least one non-directory entry — an empty directory is not an
   encode.
6. **`ForceReencode` bypasses rule 5 entirely**, and is the only thing that does.
7. **Dot-prefixed entries in `OUTPUT_DIR` are not outputs.** `.archive/` (the
   superseded copies, #332) is skipped by the watcher's already-encoded check,
   by `/api/outputs`, by output metadata and by promote.
8. **A job ID is all digits**, minted from the wall clock in milliseconds and
   guaranteed unique within the process even when two submissions land in the
   same millisecond (#326).

**Enforced by:** rules 5–6 by `resolveCodec` and its tests; rule 8 by
`nextJobIDLocked` and `chunkbudget`/id tests. Rules 1–4 are **not enforced by a
test** — they are watcher behaviour verified by use. Rule 7 is enforced
structurally by the dot prefix rather than by four agreeing checks (#332).

## Blast radius — what does NOT change

The naming contract is untouched by anything here. `resolveCodec` and the
watcher's `alreadyEncoded` are both *readers* of the layout specified in
[`outputs.md`](outputs.md); neither invents a name. A change to ingest that does
not change `OutputStem` cannot change what a finished encode is called.

Submission never writes to `OUTPUT_DIR`. Everything a job produces lands in
`$TMP_DIR/<job_id>/` and moves only on success — see
[`job-lifecycle.md`](job-lifecycle.md).

## The entry points

| entry | route / command | notes |
| --- | --- | --- |
| watcher | — | `AUTO_WATCH`, polls `SOURCE_DIR` on `-watch-interval` (default 30 s); toggleable at runtime |
| UI | `POST /api/encode` | the encode form; also `POST /api/encode/estimate` for the pre-flight figure |
| upload | `POST /api/sources/upload` | drag-and-drop into `SOURCE_DIR`; the watcher then applies rules 1–4 |
| CLI | `make encode ARGS="…"` | full option surface, `--wait` available |

`GET /api/sources` lists what is available to submit.

## The trade

| option | what it costs | what it buys | status |
| --- | --- | --- | --- |
| stability-by-size (current) | one scan interval of latency; a file written in two bursts with a pause can submit early | no filesystem-event dependency, works over network mounts | shipped |
| filesystem events | platform-specific, unreliable over the network volumes this runs on | instant pickup | not built |
| explicit ready-marker file | operator has to place it | unambiguous | not built |

## As it stands

Defaults on the master as configured: `-watch-interval` 30 s, `AUTO_WATCH` from
`.env`, `MAX_CONCURRENT` gating how many jobs run at once (submission is never
gated — only `run`).

Video extensions only; `isVideoExt` is the filter, and the list is hardwired:
`.mp4`, `.mkv`, `.mov`, `.avi`, `.webm`, `.ts`, `.m3u8`. Anything else in
`SOURCE_DIR` is invisible to the watcher and to `GET /api/sources`.

**`.ts` and `.m3u8` carry two hardwired meanings each.** Under `SOURCE_DIR`
they are submittable sources; under `/content/` they are delivery artifacts that
`mediaFileServer` types for playback (see [`outputs.md`](outputs.md)). The two
never meet — different directories, different handlers — but a reader who learns
the extension in one place will read it wrongly in the other.

## What is unmeasured

- **How often the stability heuristic is wrong in practice.** No counter exists
  for "submitted a file that was still being written", so the failure would
  present as a corrupt-source encode failure rather than as an ingest defect.
- **Whether the 30 s interval costs anything on a large `SOURCE_DIR`.** The
  analogous cost on `/api/outputs` was real and measured (#228, 133 dirs /
  108,606 files); the source scan has never been profiled.
- **Auto-discovery of worker machines** remains out of scope (PRD §8); the
  worker set is static `DIST_WORKERS` config.
