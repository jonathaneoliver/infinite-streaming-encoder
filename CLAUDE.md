# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Go HTTP server + single-page UI that drives video encoding. The Go code is a thin control plane — the actual encoding work lives in the Python package under `scripts/infinite_streaming_encoder/`, each run in its own **detached sibling Docker container** (so encodes survive a restart of the server's own container). The server submits encode jobs, tails their stdout via `docker logs -f` into a log buffer, and exposes a web UI for browsing sources, kicking off jobs, and playing back the resulting HLS output.

Testing is targeted rather than broad — each test exists because something
specific broke — but it is no longer thin: 127 Go tests plus ten Python test
scripts. Do not invent commands beyond these four:

- `make check` — the static gate: gofmt/vet/build, `go test -race ./...`,
  `staticcheck -checks all`, govulncheck, tofu fmt, the Step Functions checks,
  `ruff --select F`,
  python compile, page JS. staticcheck/govulncheck **skip when not installed**
  (like tofu and ruff) so the pre-push hook stays fast and offline; CI runs both
  unconditionally via `go run …@latest`, so CI is the authority. Install locally
  with `go install honnef.co/go/tools/cmd/staticcheck@latest` and
  `go install golang.org/x/vuln/cmd/govulncheck@latest`.
  **One step is the exception to "CI is the authority": `sfn schema`.** It asks
  the real Step Functions API whether it would accept the ASL
  (`check_sfn_definition.py`), and CI has no AWS credentials, so there it skips
  *permanently*. It runs only where credentials exist — which is the machine
  that deploys, and that is why the same script also gates `make deploy` and
  `make infra-plan` via `require-valid-sfn`, where a skip is a refusal. See
  "Two Step Functions checks" below.
- `go test -race ./...` — 27 files, 127 tests across `internal/api`,
  `internal/awswatch`, `internal/encode`, `internal/tmpstage`. `-race` is not
  optional: #196 added it after proving a real SSE data race
  (`json.Marshal(job)` walking `j.Stages` while `upsertStage` appended).
  `chunkplan_test.go` is still the load-bearing one — it pins the Go chunk
  planner to golden vectors generated from the Python one, and both paths must
  cut a clip in the same places or local and cloud encodes stop being comparable.
- `make smoke` — a REAL short encode end to end (synthetic clip, chunked) on the
  **local-dist** path, asserting the job reaches `done` AND produced playlists.
  Builds and brings the master up from your working tree, so it tests uncommitted
  code. It reports the fleet it ran on rather than claiming a single device — see
  `make fleet-check` (#248).
- `make smoke-cloud` — the cloud twin, and a **different kind of test**: it runs
  against the **DEPLOYED** ECR image, state machine and job definitions, so your
  working tree is not under test and it deliberately neither builds nor bounces
  the farm. Run it *after* `make deploy`. It forces the media home
  (`--no-skip-media-download`) and asserts segments on disk with no `.remote.json`,
  because a metadata-only output dir passes every other check (#225). It launches
  spot capacity and transfers media, so **it costs money**; `SMOKE_CLOUD_TIMEOUT`
  (default 1800s) covers spot boot + image pull before the encode even starts.
  There is no `WHOLE=1` — the whole-variant path has no smoke.

**`make check` passing is not evidence the code runs.** #176 passed every static
check and still broke both encode paths: a Batch job definition gained a `Ref::`
its whole-variant caller never supplied, and a log line referenced a list the
worker no longer built. Both were in the seams between orchestrator, worker and
job definition. Run `make smoke` before merging anything that touches the
chunk/dispatch contract — and `make smoke-cloud` when the state machine or job
definitions change, since the cloud submission path has no local equivalent.

That second instruction used to read `make smoke TARGET=cloud`, and **no such
variable ever existed** — make accepts an override nothing reads without
complaint, so the command ran the LOCAL smoke and printed PASS. A confident
green for the path it claimed to be covering, which is worse than no gate at
all. If you add a knob to a target here, add it to the target and not only to
this file.

**`make smoke-cloud` spends real money, and `make deploy` mutates shared
infrastructure — never run either unprompted.** smoke-cloud launches spot
capacity and transfers media; deploy bumps and *deregisters* live Batch job-def
revisions, which fails any execution already in flight. Both are the
maintainer's call on the maintainer's account. Say which one you want run and
why, then wait.

Do not mutate the smoke fixture `$SOURCE_DIR/smoke.mp4` either. `make
smoke-cloud` `touch`es it deliberately to move the mezzanine cache key and force
a real mezzanine run — the key is `name|size|mtime`, so an out-of-band edit
makes that gate silently meaningless in either direction.

## Commands

Build and run (Go, for local dev outside Docker):
```
go build ./cmd/server
go run ./cmd/server                   # reads env vars; flags override env
```

Docker workflow (normal development — scripts need ffmpeg, Shaka Packager, docker-cli, aws-cli baked into the image):
```
make build         # docker build
make run           # bring up JUST the server (compose, --no-deps); mounts SOURCE_DIR / OUTPUT_DIR / TMP_DIR + ~/.aws + ~/.ssh + docker.sock
make restart       # stop + run (bounce the server, e.g. after an image push)
make logs          # docker logs -f
make shell         # exec into container
make stop / clean
```

**"Is it deployed?" is four different questions, and there is a target for
each.** Asked ~15 times across the sessions this file exists to prevent; the
answers were always in the Makefile and never here.
```
make status         # what is deployed where, vs what THIS TREE says it should be
make fleet-check    # which workers are actually connected + their payload version,
                    #   then falls through to cloud-payload
make cloud-payload  # what encoder payload the ACTIVE Batch job definitions are on
make doctor         # preflight: .env, host tools, per-target config
```
Read them in that order, and read `version` as **encoder payload, not build**
(see the `version` note under "Things to know when editing"). A green `make
status` with a Go-only change still means the tag did not move — that is
correct, not a bug, and it is the single most common way this question gets
answered wrongly.

`fleet-check` cannot answer the pre-flight form. The server learns a box's
version from the **chunk heartbeat**, so before any encode has run it reports
"none reported yet". The connect line proves the worker knows its build; the
control plane does not until work flows (#248).

Farm bring-up (distributed-local encoding — Temporal + MinIO, no AWS). The whole
thing lives in the **unified root `docker-compose.yml`** with two profiles:
```
make farm-up          # master box: GHCR image -> cluster + server + one local worker (+ cloud configured)
make farm-dev-up      # same, but build from your working tree + live-mount scripts/infinite_streaming_encoder
make farm-test-up     # publish the working tree under a throwaway dev-<branch>-<sha> tag and run
                      # the farm on that IMAGE (no code mount, :latest untouched) — catches packaging
                      # bugs farm-dev-up hides. TAG=<name> to override.
docker compose --profile worker up -d   # an EXTRA box: worker only (dials master's LAN Temporal/MinIO)
```

**Under `farm-dev-up`, editing a file IS deploying it.** `HOST_SCRIPTS_DIR`
live-mounts `scripts/infinite_streaming_encoder/` and `cli_phase` runs as a
**fresh subprocess per phase** — so an edit to the package lands in the next
phase of a job already in flight, with no bounce, no restart and no log line.
Discovered 2026-08-11 while a four-ladder full-length comparison was mid-run;
the fix was to hold a rebase for two hours rather than produce a comparison set
whose members were built by different code.

The mount boundary stops **at the package directory**. `scripts/test_*.py` is
outside it, so test files are safe to touch mid-run — which is what makes
mutation-testing possible during an encode. To check a hypothesis against the
package itself without mutating it, read the source, edit the string, and `exec`
the result into a throwaway namespace.

**Before any `farm-up` / `farm-dev-up` / `deploy` / `restart`, check for a
non-terminal job** — `curl -s localhost:8080/api/jobs`. A bounce mid-encode does
not fail the run: the encode completes and **PASSES**, having silently lost the
telemetry of every chunk that ran on the replaced worker (2026-08-08).
`make deploy` and `make restart` have `require-idle`, but it fires ONCE at entry
and `publish` then takes minutes before `farm-up` touches a container — anything
submitted in that window is unguarded and the guard still says idle.

**Never bounce the farm without saying so** when another session may own it. A
worktree that is dev-mounted owns the compose stack for every session on the
box, and "the farm is running someone's uncommitted tree" is the first
hypothesis whenever two outputs of the same source disagree structurally.

`master` profile = postgres + temporal + temporal-ui + minio + server + worker;
`worker` profile = worker only (MinIO/Temporal are **master-only**). The server,
worker, cluster, and the old `run-worker.sh` are all folded into this one file;
`make run`/`run-remote` target just the `server` service within it. The worker
container is named `encode-worker` (the `internal/api/dist.go` start/stop toggle
contract). **Networking contract:** every service keeps its host-published port
and addresses the cluster via `TEMPORAL_ADDRESS` / `S3_ENDPOINT_URL`
(`host.docker.internal` on the master, `MASTER_IP` on remote boxes) — because the
server spawns short-lived per-job orchestrator containers that are *not* on the
compose network and reach Temporal/MinIO through `host.docker.internal`. Don't
rewrite services to compose-DNS names (`temporal:7233`). See
`infra/local-cluster/README.md` and `scripts/infinite_streaming_encoder/temporal_worker.py`.

`.env` at the repo root is auto-loaded by the Makefile and passed through. Key vars: `SOURCE_DIR`, `OUTPUT_DIR`, `TMP_DIR` (host paths), `AUTO_WATCH`, `DEFAULT_TARGET` (cloud|local), `DEFAULT_CODEC` (h264|hevc|both|all), `DEFAULT_MAX_RES`, `MAX_CONCURRENT`, plus AWS vars (`S3_BUCKET`, `SUBNET_ID`, `SECURITY_GROUP_ID`, `INSTANCE_PROFILE`, `INSTANCE_TYPE`, `GHCR_PAT`) for cloud encoding. The Makefile additionally exports `HOST_SOURCE_DIR` / `HOST_OUTPUT_DIR` / `HOST_TMP_DIR` / `HOST_AWS_DIR` / `ENCODER_IMAGE` into the server container — the server needs the host-side view of those paths to spawn worker containers (see "Worker containers" below).

**`.env` reaches the server only through the `environment:` allow-list on the
`server` service.** Compose enumerates that block, so a var the Go side reads
but the block omits is silently inert *under the only configuration that
ships* — it works when you `go run ./cmd/server` on the host and does nothing in
the container, which is the hard way round to discover. `TMP_STAGING_MAX_AGE_H`,
`DIST_STAGING_MAX_AGE_H`, `DIST_STAGING_LIFECYCLE_DAYS`, `DEFAULT_LADDER`,
`DEFAULT_MIN_RES` and `AUTO_TERMINATE_STALE` were all inert this way. Adding an
`env()` / `os.Getenv` to `cmd/server` means adding a line to that block too.
`LISTEN_ADDR` is deliberately NOT passed (compose owns the published port, and a
mismatch with the `ports:` mapping would just make the server unreachable), and
`DEV_MOUNT` comes from `docker-compose.dev.yml` by design.

## Architecture

Three Go packages plus a scripts directory. The interesting coordination is in `internal/encode`.

**`cmd/server/main.go`** — wires everything together. Creates a single `encode.Manager`, optionally starts a `watcher.Watcher` goroutine, hands the manager to `api.NewServer`, and serves `Server.Mux` on `LISTEN_ADDR`.

**`internal/encode/job.go`** — the core. `Manager` owns:
- A slice of `*Job` (the authoritative working set; rebuilt from persisted state on startup via `Reconcile`).
- A `chan struct{}` semaphore sized to `MAX_CONCURRENT` that gates `run()`.
- A list of SSE subscriber channels used by `/api/jobs/stream`; `notify()` does non-blocking sends (slow subscribers drop updates, never block the encode).

### Worker containers (restart resilience)

Each file's encode runs in a **detached sibling Docker container** named `encoder_job_<id>_f<fileIdx>`, launched via the `docker.sock` mount. `runFileContainer` is idempotent: if a container with that name already exists, it skips `docker run` and reattaches via `docker logs -f`. This is what makes encodes survive a restart of the Go server's own container — `docker logs -f` on a still-running worker streams live, on an already-exited worker it drains history then returns, and in either case `containerExitCode` reads the final status. The worker is `docker rm`'d only after exit is accounted for.

For this to work, the Go server is given the **host-side** paths to its volumes (`HOST_SOURCE_DIR` / `HOST_OUTPUT_DIR` / `HOST_TMP_DIR` / `HOST_AWS_DIR`). `docker run -v` from inside a container is resolved by the host daemon against the host filesystem, so the in-container paths aren't usable for mounts. The workers mount host paths at the same paths the Go server uses, so all the `--input` / `--output-dir` arguments stay identical in both contexts.

Per-job state is persisted to `$TMP_DIR/jobs/<id>.json` (`{id, config, started_at, current_file_idx}`) while the job is active. On startup, `Manager.Reconcile` reads those files, rebuilds `Job`s, and calls `run(job, currentFileIdx)` — which either reattaches to a running worker for that file or starts a fresh one. State is removed only on terminal outcome (done, failed, or move-failed).

Job lifecycle (`Manager.run`):
1. Encode each file into `$TMP_DIR/<job_id>/` — partial output never appears in `OutputDir`.
2. Before launching each file, persist current index to `$TMP_DIR/jobs/<id>.json`.
3. On success, `moveTmpToOutput` renames each top-level subdir into `OutputDir`. If a dir already exists there it is **always** preserved — moved to `OutputDir/.archive/<name>_<YYYYMMDD>` (`_HHMMSS` too on a same-day repeat), never deleted, and `ForceReencode` does not change that. This line used to describe a `.archive/` the code did not have and a delete it never did; #332 made the first half true and dropped the second.
4. If rename fails (cross-device), falls back to `copyDir`.
5. Per-job log written (ANSI stripped) and `history.md` appended regardless of outcome. State file removed.

Two key naming/skip conventions in this file:
- `JobConfig.OutputStem(filename)` — `<stem>_p<partialMs>[_padblack|_padpink]`. The encoding script then appends `_<codec>` to produce the final output dir name (e.g. `myclip_p200_h264`). The watcher and `parseOutputMeta` both rely on this layout.
- `Manager.resolveCodec` — before encoding, checks which `<stem>_h264` / `_hevc` / `_av1` dirs already exist in `OutputDir` and narrows the codec flag to only the missing ones. Returns `""` → skip this file entirely. Bypassed when `ForceReencode=true`.

The worker container's entrypoint is overridden to `scripts/infinite_streaming_encoder/cli_local.py`. Stdout is line-scanned into `job.logLines` (capped at 1000 lines, trimmed to last 500) and the latest line (ANSI-stripped) becomes `job.Progress`, which the SSE stream surfaces live.

This paragraph used to describe a `cli_cloud.py` worker receiving `HOST_AWS_DIR` and an env allow-list called `cloudEnvPassthrough`. **That path no longer exists** — the single-box EC2 target was retired when cloud encoding moved to Step Functions + Batch, and `cloudEnvPassthrough` sat unreferenced until staticcheck's U1000 found it. `SUBNET_ID` / `INSTANCE_PROFILE` / `GHCR_PAT` and friends are configured on the Batch job definition now. Cloud jobs do not spawn a worker container from the Go server at all; see "Batch state, event-driven (cloud)".

### Which phases run on the HOST (local-dist)

The local twin of the cloud section above, and it moved for the same reason:
bytes were crossing a boundary to reach a machine that did not need to be
involved. Here that boundary is MinIO, and the orchestrator container already
runs on the master with `SOURCE_DIR` / `TMP_DIR` mounted — so it is the box the
bytes were already on.

**The mezzanine is built by the orchestrator** (`_build_mezzanine_on_host`),
shelling out to the same `cli_phase mezzanine` the activity runs with a local
path for `--s3-in`. Staging the source moved a source-sized file into MinIO, a
worker pulled the same bytes back out, and the mezzanine went in again — three
transfers of a file that never left the box; now it is one.

**No workflow change was needed**, for the same reason as cloud: `phase_mezzanine`
short-circuits on the `.done` sidecar, so the mezzanine activity the workflow
still schedules finds this build and returns. That guard was already there.

Two consequences:

- **Nothing stages the source any more**, so the mezzanine activity's `--s3-in`
  points at an object that does not exist and CANNOT serve as a fallback.
  `_build_mezzanine_on_host` refuses to hand off until it has confirmed the
  `.done`, so the failure is stated here rather than surfacing four Temporal
  retries later as a `NoSuchKey` on a key nobody expected to be read.
- **There is no `upload:source` stage row.** A declared stage that never fires is
  a row that sits pending forever — the same rule `_emit_plan(sync_back=…)`
  applies on the cloud path.

The mezzanine's scratch dir lives INSIDE `$TMP_DIR/<jobID>/` because that is the
only scratch here that anything cleans. It must be gone before the run finishes:
`moveTmpToOutput` renames **every** top-level entry of that directory into
`OUTPUT_DIR`, dot-prefixed or not. The one path that skips the cleanup (the
orchestrator killed mid-mezzanine) also fails the job, and a failed job moves
nothing.

**The orchestrator packages too** (`_package_on_host`), for every codec unless
`--no-host-package` is passed. Same mechanism again: the same `cli_phase
package-all` the activity runs, with a local directory for `--s3-out`. What it
removes here is transfer rather than queue latency — a worker-packaged codec was
uploaded to MinIO purely so this process could download it straight back, and if
that worker was a remote box the whole ladder crossed the LAN twice to reach a
disk on the master.

This one DOES need a plan key: `host_package` (a list of codecs) tells the
workflow not to dispatch `pkg-<codec>`. It is read with `plan.get`, so an older
orchestrator's plan reads as "package everything in a worker" — the old
behaviour. The key is a contract between two files that fails silently in both
directions, so `test_dist_stage_state` pins the two spellings together.

Three consequences:

- **The package/fragments/hls rows become LIVE.** `cli_phase`'s per-step markers
  are `CLASS_LIVE` and so deliberately never relayed through an activity result;
  run here, this process's stdout IS the channel to the Go server. On the worker
  path the three rows can only move together, on completion.
- **No retry.** Temporal owned that and this gives it up. The trade is the same
  one #197 made and softer — minutes of local CPU against hours of chunk
  encoding — but the recovery is *worse* than cloud's, because a Retry mints a
  NEW job id and therefore a new staging prefix, so it re-encodes everything.
  Re-running against the **same `--job-prefix`** reuses the staged chunks, and
  that is what the failure message says.
- **`download:outputs` is dropped from the plan** when every codec is packaged
  here, and the reclaim guard counts delivered files rather than downloaded ones
  — an empty result still means the staging is the only copy of the encode.

**`package-all` is the only finalization activity.** It does the DASH packaging,
the fragment-granularity manifest and the LL-HLS playlists from one local copy of
the ladder. The workflow used to run `byteranges` and `hls` after it as separate
activities, each downloading the entire packaged output out of MinIO and pushing
it back — two full-ladder round trips per codec, to arrive at bytes it already
had. Idempotent, so nothing was ever wrong with the output; it was pure transfer,
and the cloud state machine had already dropped them (one `PackageAll` per
codec).

Their UI rows survive because `cli_local_dist._stage_keys_for` maps
`pkg-<codec>` onto all three, exactly as `cli_batch._host_stage_keys` does for a
pkgall job. `byteranges-`/`hls-` are deliberately **not** mapped: a farm
mid-rolling-update can still have a box running the old workflow, and those
activities would re-announce rows the run had already finished.

That last point is the one general hazard on this path: **nothing between
`emit_stage` and the UI stops a cell walking backwards.** `progress.emit_stage`
is a plain print and Go's `upsertStage` is last-writer-wins — there is no
`_STAGE_RANK` chokepoint here like the one `cli_batch` grew for the same class of
race. `_SELF_RUN_STAGES` is the narrow version of that guard: it names the activity
ids this process already drove itself, so the workflow-history reader cannot
re-announce them.

### MinIO staging lifecycle (local-dist)

Every local-dist file stages through `s3://$DIST_S3_BUCKET/jobs/<jobID>-<base>/`
(mezzanine, per-chunk encodes, variants, packaged output — ~2.3 GB for a typical
clip; the source itself is no longer staged, see above).
`encode.DistJobPrefix` is the single definition of that key, shared by the
orchestrator's `--job-prefix` argument and the GC's keep-list.

Reclaim is three layers, all in `scripts/infinite_streaming_encoder/dist_staging.py`:

1. **Success path** — `cli_local_dist` deletes its own prefix immediately after
   `download:outputs` has landed every file on disk (skipped by `--keep-staging`,
   and skipped automatically if nothing downloaded, since an empty download means
   the staging is the only copy of the encode).
2. **`internal/diststage`** — a server goroutine sweeping prefixes idle longer than
   `-dist-staging-max-age` (24h), which is what catches failed, cancelled (the
   control plane `docker stop`s the orchestrator, so its own cleanup never runs)
   and crashed jobs. The idle age doubles as the debugging window. It passes
   `Manager.ActiveDistPrefixes()` as a keep-list, so a running encode can never be
   reclaimed out from under itself. The same pass aborts stale multipart uploads —
   a killed mid-upload leaves parts that hold space but never appear in a
   `list_objects_v2` scan, so nothing else can see them.
3. **Bucket lifecycle** — a `jobs/` object-expiry rule re-asserted on every sweep;
   the backstop if the server never runs again. Expiry only: MinIO silently drops
   an `AbortIncompleteMultipartUpload` directive paired with `Expiration` and
   rejects it on its own, which is why the multipart abort lives in the GC.

`dist_staging` requires an explicit MinIO endpoint (`S3_ENDPOINT_URL` in worker
containers, `MINIO_ENDPOINT` on the server) and never falls back to the default
boto3 chain — without that guard it would resolve to real AWS S3 and start
deleting the *cloud* bucket's staging. Manual controls: `make minio-usage` /
`make minio-clean` and `GET /api/dist/staging` + `POST /api/dist/staging/gc`.

### TMP_DIR staging lifecycle (all targets)

The local-disk twin of the above, and the same failure mode: `Manager.run`'s
finalize path clears `$TMP_DIR/<job_id>/` on every terminal outcome, so only a
job whose process *never reached* that path — server container killed or crashed
mid-encode — leaves one behind. Nothing else could see them: `Reconcile`
enumerates `$TMP_DIR/jobs/*.json`, and the state file dies with the payload, so
the directory is orphaned from the only index that indexes it (#207).

**`internal/tmpstage`** sweeps on `-tmp-staging-interval` (30m) for directories
idle longer than `-tmp-staging-max-age` (24h, the crashed-job debugging window),
with `Manager.ActiveJobIDs()` plus the `jobs/*.json` state files as the
keep-list.

`$TMP_DIR` holds four kinds of thing and only one is garbage, so **eligibility
is the `^[0-9]+$` job-ID directory shape, not age** — the `encode_<stem>/`
mezzanine caches, and (unless `STATE_DIR` / `RECORD_DIR` moved them, below) the
learned state that sizes every chunk plan (`encode_speeds.json`,
`quality-curves.json`), the ladder CONFIGURATION (`ladders.json` —
user-authored, not learned) and the permanent record (`logs/`, `history.md`,
`failed/`) are out of scope structurally rather than by a rule someone can
forget. `MaxAge` of 0 means *disabled* at both the loop and the sweep, because
read literally it puts the cutoff at now and takes every job directory with it.

Idle is measured over the whole tree, not the top-level directory's own mtime,
which an encode writing deep inside it never touches. The walk stops at the
first recent file, so a live directory costs a few stats and only a doomed one
is walked in full — which is where the reclaimed byte count in the log comes
from.

### STATE_DIR / RECORD_DIR: what is NOT temporary (#331)

The rule right above — eligibility is structural, not age — existed *only*
because irreplaceable state shared a directory called `tmp`, and it protected
against exactly one actor. It did nothing about a human running `rm -rf
$TMP_DIR/*` to reclaim space, or `rm -rf $TMP_DIR/encode_*` to clear the
mezzanine caches — **a glob that also matches `encode_speeds.json`** — or the
volume being unplugged.

So the location is a choice now. Both **default to `$TMP_DIR`**, so an install
that sets neither is byte-identical:

| | holds | size |
| --- | --- | --- |
| `STATE_DIR` | `ladders.json`, `quality-curves.json`, `encode_speeds.json`, `spot_samples.json`, `cost_samples.json`, `settings.json` | ~250 KB, **recoverable from nowhere** |
| `RECORD_DIR` | `logs/`, `history.md`, `failed/` | tens of MB, and `failed/` can hold GBs |
| `$TMP_DIR` | `<jobid>/`, `encode_<stem>/`, `jobs/*.json`, the mezzanine lock | GBs, genuinely disposable |

The point is not tidiness: it is that **`$TMP_DIR` becomes disposable**, which
is what every cleanup path already assumed. They are two knobs rather than one
because `failed/` is a different size class from the thing you back up.

- **`MigrateDurableState` runs before `NewManager`**, which loads three of the
  stores in its constructor. It never overwrites a destination and never
  deletes a source it could not copy, so a repeat boot is a no-op and the worst
  case is both copies existing *and being told about it*. A half-migrated state
  is worse than either end state and it is silent — the encoder simply replans
  from a cold model and re-learns.
- **Read paths through `StatePath` / `RecordPath`**, never by joining `TmpDir`.
  Empty resolves to `TmpDir`, which is what every `&Manager{TmpDir: …}` in the
  tests relies on. `test_state_dir.py` greps for the join coming back.
- **Two silent cross-language crossings.** `spot_samples.json` is written by Go
  and read by `inventory.py`; disagree about the directory and the AWS view's
  spot savings read zero, which looks like a quiet fleet. `ladders.json`
  reaches Python as `LADDER_STORE`, and a **separate state dir must also be
  bind-mounted** into spawned worker containers or that path does not exist
  there and the encoder falls back to the built-in ladders — a custom ladder
  then encodes as something else. Hence `HOST_STATE_DIR`, and hence
  `cmd/server` writing its *resolved* `STATE_DIR` back into its own environment
  so the Python helpers cannot disagree with the flag.
- **The compose block passes `STATE_DIR: ${STATE_DIR:+/media/state}`.** The `:+`
  is load-bearing: unset must stay EMPTY, or the server sees `/media/state` ≠
  `/media/tmp` and "migrates" between two mounts of the same host directory.
  The mounts themselves fall back to `${TMP_DIR}` so they always resolve.
- **`internal/tmpstage`'s guard stays.** `STATE_DIR` defaults to `$TMP_DIR`, so
  on a default install it is still the only thing between the sweep and the
  learned model. What changed is that it is no longer the only thing between a
  human with `rm -rf` and the same files.

### OUTPUT_DIR/.archive: superseded outputs (#332)

A re-encode has always preserved the copy it replaced under a dated name. What
it never had was anywhere to put them or anything to remove them: they were
renamed **in place**, as siblings of the live outputs, and reached 168
directories / ~158 GB against a 208 GB `OUTPUT_DIR`. Four call sites already
agreed to ignore them (`/api/outputs`, `writeEncodeMetaForDirs`, `autoPromote`,
the watcher's already-encoded check), which is a good sign they did not belong
in that directory — and ignoring was not free, since `/api/outputs` still walked
and classified every one on every poll.

So `archiveExisting` moves them to `OUTPUT_DIR/.archive/` and
**`internal/outarchive`** sweeps that on `-output-archive-max-age` (30 days,
`OUTPUT_ARCHIVE_MAX_AGE_D`), the third instance of the sweeper shape
`internal/tmpstage` and `internal/diststage` already implement.

- **The dot prefix is the mechanism, not decoration.** Every existing
  `strings.HasPrefix(name, ".")` guard now covers the whole archive, which is
  what turns the four `IsDatedBackup` checks from load-bearing into
  belt-and-braces. Keep them: an older server's in-place backups still exist
  until `MigrateDatedBackups` collects them.
- **The archived directory's mtime is stamped to NOW when it is archived**
  (`stampArchived`), and the date in its NAME is still the encode date.
  Retention here means "how long is a superseded copy kept", and that clock
  starts when it is superseded. Without the stamp, re-encoding a year-old output
  would produce an archive already older than any retention window — swept
  before anyone could compare old against new. The startup migration stamps for
  the same reason, so the window starts at the upgrade rather than handing 168
  directories to the first sweep.
- **Four rules hold a directory, and each one is "this is not a superseded
  copy".** `.remote.json` / `.pending.json` (S3 holds the only copy of part of
  it), `.keep` (a human said so — an A/B pair kept as evidence is not a backup),
  a base name belonging to a queued or running job, and an **orphan**: a dated
  copy whose live output no longer exists is the LAST copy, not a superseded
  one, so it is held unless `-output-archive-sweep-orphans` says otherwise. Ten
  of the master's 168 are orphans, which is also why no keep-N-per-base rule
  would work — it cannot see them.
- **A held directory past its retention is logged**, aggregated by reason. An
  immortal directory nobody can see is how this problem started, and a sidecar
  archive is immortal by design: `MarkGoneUnderPrefix` only walks the top level,
  so nothing will ever mark it gone.
- **`MaxAge` of 0 means disabled** at both the loop and the sweep — the same
  rule, for the same reason, as `tmpstage`.

### Duration limit (the UI's "Duration (s)")

Applied in **exactly one place** — `cli_phase`'s mezzanine step truncates the
mezzanine, and every variant, chunk and the audio are cut from that, so nothing
downstream applies it a second time (#184).

Everything that must agree about *how long the content is* agrees by clamping
one number: `cli_local_dist` clamps the probed `info.duration_s` immediately
after the probe, and `buildSFNInput` clamps `clipDurationS`. The chunk plan, the
ladder's chunk sizing, the cost estimate and the progress totals then describe
the truncated clip without any of them knowing a limit exists. A limit at or
above the clip is **not** a limit, on both paths.

**A limit that reaches the clip length is not a limit** — encode the whole
content, since a limit can never describe more media than exists.
`JobConfig.TimeLimitFor(clipDurationS)` is that rule, and the cloud path probes
the source *before* keying the mezzanine so it applies to the key too: otherwise
a non-binding limit files a FULL mezzanine under a limited prefix, and the next
unlimited run of the same source misses the cache and rebuilds an identical
file. An unknown duration (probe failed, `<= 0`) **keeps** the limit — dropping
it on a number nobody measured would encode the whole clip when a short one was
asked for.

**The limit is snapped to the nearest whole segment** (6s by default; the job's
ladder-resolved `SegmentDuration`). Chunk boundaries must land on segments, so a
limit that isn't a multiple leaves a plan that cannot end where the media does,
plus a runt final segment and a comparison run that isn't comparing equal spans.
`JobConfig.TimeLimitSeconds()` snaps, so the encoder argument, the cache key, the
cloud plan and the history line all get the snapped value automatically;
`cli_local_dist` snaps again, idempotently, against the segment duration its own
`plan_chunks` uses, so running it by hand behaves the same. Both round half
**away from zero** — Python's `round()` is banker's rounding and would disagree
with Go's `math.Round` at exactly half a segment.

Two rules that are easy to get wrong:

- **The limit is part of the mezzanine cache key** (`sourceMezzKey` in Go,
  `_mezz_cache_rel` in Python; the per-worker cache in `cli_phase` keys off the
  resulting URI and follows for free). The mezzanine is only a pure function of
  the source while the *whole* source is copied. Key on the source alone and one
  30s encode serves a 30s mezzanine to every later full encode of that file —
  right name, right manifests, silently short video, until the staging GC evicts
  it. An unset limit hashes exactly as before, so existing cached mezzanines
  still hit.
- **The chunk plan is a deliberate PREFIX of a limited mezzanine, not a match
  for it.** `-t` on a stream copy cuts on packet boundaries, so a 10s limit
  yields ~10.07s of media — past the one-frame tolerance, every time. So
  `cli_phase`'s plan-vs-media check flips under a limit: media *shorter* than
  the plan is still fatal (chunks would reference frames that don't exist), an
  overshoot is expected. Which is why `TIME_LIMIT_S` reaches the variant phases
  too, even though they don't apply it — it is a property of the RUN.

`TIME_LIMIT_S` travels as an environment variable, never a `Ref::` parameter: a
caller that doesn't set it gets the full clip (the old behaviour) instead of a
job definition that fails to launch (#176). On the cloud path `buildSFNInput`
**always** emits `time_limit` (`"0"` when unset) because the ASL reads it with
`Value.$`, and both Maps must project it in their `ItemSelector` — `make check`'s
`sfn scopes` step catches a missed one.

A `history.md` "Time limit" line is written only when a limit was actually
applied. `JobConfig.TimeLimitSeconds()` is the single definition of "is this a
real limit" — the field is free text, so unparseable or non-positive means no
limit. Before #184 that line was written from the raw string while nothing
passed it to an encoder, so full-length encodes recorded truncations that never
happened.

### Four numbers for "how long did this job take"

`EndedAt - StartedAt` is not it, and neither is `total measured`. Both were
already displayed and both are misleading, in opposite directions.

| number | what it is | fails how |
| --- | --- | --- |
| `total measured` | SUM of stage durations | overshoots elapsed by the width of the fan-out — ~8x on a 336-chunk run. It is a MACHINE-HOURS measure. |
| `wall clock` | submit → done | `StartedAt` is set in `Submit`, alongside `Status: queued`, so it counts the wait for a `MAX_CONCURRENT` slot as work |
| **`active`** | **UNION of running-stage intervals** | the one to compare runs by |
| `queued` / `idle` | submit → first stage / gaps inside the run | — |

Measured on four ladders submitted together under `MAX_CONCURRENT=2`, all doing
identical work:

```
        active    queued      idle    wall     sum
xs    1h20m16s        5s     2m 0s  1h22m   9h59m
1s    1h21m27s        5s  1h11m42s  2h33m  10h15m
2s    1h20m50s  2h31m46s        2s  3h53m  10h36m
6s    1h21m51s  3h51m12s        4s  5h13m  10h43m
```

Active spreads 95 seconds; wall spreads 3.8x. The whole apparent "the 6s profile
is slow" result was submission order — it is last in the queue every time.

- **`queued` and `idle` are kept apart deliberately.** Both are contention, but
  `1s` above started immediately and was STARVED mid-run while `2s`/`6s` waited
  in the queue and then ran clean. Same total cost, different cause, different
  fix; one number cannot say which happened.
- **The union removes idle, not contention.** Two jobs sharing the worker pool
  both encode slower, which inflates `active` for both. This fixes the
  accounting; only running them one at a time fixes the measurement.
- **`writeTimingSummary` computes from `rows`, not `Job.Stages`.** `rows` spans
  every file of a multi-file job; `Job.Stages` holds only the file in flight, so
  using it there would report a five-file job's active time as its last file's.
- **Recomputed in `computeProgress`, never accumulated.** A stage row can be
  updated by three sources at three latencies, and an incrementally-maintained
  total would drift from the rows it claims to summarize.
- `active + queued + idle` need not equal wall clock — the tail after the last
  stage ends (sync-back, cleanup) belongs to none of them.
### The chunk grid: GOP | segment | GRID | chunk

Chunk boundaries used to have to be **segment** boundaries and nothing more, so
the same clip was cut in different places on every delivery profile:

```
334s clip, 132s target
  1s ladder -> 112, 111, 111
  2s ladder -> 112, 112, 110
  6s ladder -> 114, 114, 106
```

Every chunk boundary is an encoder-state reset, so four ladders that are supposed
to differ **only** in delivery profile were also differing in where the encoder
restarted. That is `docs/ladders-and-delivery.md`'s warning in the other
direction — a comparison meant to isolate one variable quietly carrying a second.

So the tiling unit is now `chunkGridFor(segment)` = **lcm(segment, 6s)**
(`internal/encode/chunkplan.go`, mirrored in `chunking.chunk_grid_for`). All
three shipped segment durations divide 6, so the grid costs them nothing but
agreement, and the same clip now cuts identically on every profile.

- **Segment alignment is the CORRECTNESS half, the 6s grid is the COMPARABILITY
  half, and the LCM is the only value satisfying both.** The grid sits *between*
  segment and chunk in the chain rather than replacing the segment link. A
  segment that shares no small multiple with 6 falls back to the segment
  duration — the old behaviour — rather than inventing a boundary that is not a
  segment edge.
- **`round(r) >= 1` in the LCM search is load-bearing.** Without it a segment
  longer than the grid matches on the first try: `6/1e9` rounds to 0 and sits
  inside the epsilon, so the search returns a 6s grid that is not a whole number
  of segments.
- **Only the FINAL chunk may be off-grid.** It carries the sub-grid tail, and it
  has to: 334s is not a multiple of 6, and rounding it would mean encoding media
  that is not there.
- **A clip cannot be cut into more pieces than it has grid units.** Asking for
  more used to hand the surplus chunks *zero* duration — 334s at `chunkS=3` on a
  6s ladder planned 111 chunks of which **55 encoded nothing**, each a real Batch
  job with a queue wait and a container start. Python's `_validate` refused
  `chunk < segment` and never reached it; Go has no such guard and the cloud path
  did. Both clamp now.

Pinned from both ends, and the two halves are not redundant.
`internal/encode/chunkplan_test.go` pins Go to Python via
`testdata_chunkplan.txt` — but that file is **generated from Python**
(`scripts/gen_chunkplan_golden.py`), so it cannot catch Python being wrong: break
the planner, regenerate, and Go matches the new wrong answer exactly.
`scripts/test_chunk_grid.py` is what pins Python's own invariants, and it is also
the only coverage of local-dist, which plans its chunks in Python.

### The whole-job chunk budget (cloud only)

The dynamic chunk selector is **per-variant and stateless** —
`dynamicChunkSeconds` sizes one rung from its learned speed and knows nothing
about the rest of the job. The ceiling it lives under is a **whole-job**
property: 25,000 history events per Step Functions execution, shared by every
chunk of every rung of every codec in the run.

Nothing connected the two until #316, and the failure mode was the expensive
one — a 4h HEVC plan sails past submit, launches spot capacity, encodes for
hours, and dies when the history fills. `budgetedChunkTarget`
(`internal/encode/chunkbudget.go`) raises the wall-time target until the whole
plan fits, in a pre-pass before `buildSFNInput`'s per-variant loop. That pre-pass
is the actual structural change: resolving the rungs and sizing them used to be
one loop, which made the total unknowable until it was over.

Five things to keep right:

- **It returns the default target untouched whenever the plan already fits**, so
  every job that works today plans byte-identically. `TestBudgetLeaves-
  FittingJobsExactlyAsTheyWere` pins the sizes themselves, not just the counts.
- **It finds the SMALLEST target that fits** — double until something fits, then
  bisect. Scaling by chunks÷budget and repeating is cheaper and converges in
  two or three rounds, but overshoots badly: quantising every rung to a 12s grid
  drops the count faster than the ratio predicts, and a 4h HEVC ladder lands at
  1,881 chunks against a 2,500 budget. That is a quarter of the run's
  parallelism given back for nothing, and parallelism is the only currency this
  spends.
- **Only when chunking is dynamic.** An explicit `--chunk-duration` is never
  silently grown to fit — that is #312's job, and failing legibly beats quietly
  encoding something other than what was asked for.
- **Scale the TARGET, never the floor.** `dynamicMinChunkSeconds` is both the
  floor and the quantum, and it binds on the SLOWEST rungs — which are already
  the longest chunks in the run. Lifting it grows the atomic long pole to save
  chunks that are not where the count is: measured on a 4h HEVC ladder, the same
  fit costs a 1701s worst chunk against 1361s for target-only.
- **The count must come from the same plan that ships.** `plannedChunkCount`
  calls the same `dynamicChunkSecondsAt` + `planChunks` the build loop calls,
  because the 12s floor, the 12s quantum, segment tiling and the runt-tail fold
  each make an arithmetic `clip/chunkSeconds` estimate wrong — in both
  directions. #312's guard has to use this same join or the two will disagree.
- **Cloud-only by construction, and that is correct.** `cli_local_dist` plans its
  own chunks in Python against Temporal, whose limits (51,200 events / 50 MB) are
  ~3x larger. Do not "fix" the asymmetry.

`sfnEventsPerChunk = 8` is an **estimate** and everything scales by it. Being
wrong high is the safe direction (fewer chunks than allowed); one
`get_execution_history` call against a finished execution turns it into a
measurement, and if the true figure is 6 the budget is 4,166.

This rations against the ceiling rather than lifting it, and the rationing is not
free: `variantResourcesFor` reserves a flat 2 vCPU for every HEVC chunk because
x265 tops out around two cores, so for HEVC **chunk count IS the parallelism**.
Doubling the target roughly halves the peak concurrency the run can use.
`chunkBudgetLine` states that in the job log, because a run that grows
13-minute chunks with no explanation reads as the selector misbehaving. #313
(`Mode: DISTRIBUTED`) is what makes the trade unnecessary.

### Worker telemetry (`[[ENCODER-*]]` markers)

Every worker reports the same way — a stream of `[[ENCODER-…]]` marker lines —
but each deployment moves them differently. `scripts/infinite_streaming_encoder/telemetry.py`
is the one place that knows which:

| path | transport |
| --- | --- |
| single-container local | stdout → `docker logs -f` → Go server |
| local-dist | stdout → `temporal_worker` → activity heartbeat (live %) or activity result (records) → orchestrator |
| cloud (Batch) | stdout → CloudWatch (fallback) **and** → per-execution SQS queue → orchestrator |

Emit through `telemetry.emit()`, never `print()`. A marker added at a bare
`print` travels on whichever transports its author happened to think about —
that is exactly how #141 happened (VMAF computed per chunk, then dropped by a
relay that forwarded a whitelist).

**stdout is always one of the transports**, and that is what makes a sink safe
to add: a sink that fails to initialise degrades to precisely the old
behaviour rather than to silence. On the single-container path stdout is also
load-bearing for restart resilience (see "Worker containers" above), and on
local-dist it *is* the sink.

Scope is one hop: **worker → control plane**. The orchestrators (`cli_batch`,
`cli_local_dist`) also print markers, but that hop is control plane → Go server
over a pipe the server is already attached to; it stays a plain `print`. Giving
`cli_batch` a sink would be actively wrong — it is the queue's *consumer*.

Progress percent is a **gauge**, emitted at `ENCODER_PROGRESS_INTERVAL_S`
(default 2s, matching the local-dist heartbeat throttle) and safe to drop.
TIMING / SPEED / VMAF are **records** — unrecoverable without re-encoding — so
they are emitted unthrottled and flushed before a phase returns.

Every consumer has to answer "may I drop this?", so the answer is classified in
one place — `telemetry.marker_class()` → `CLASS_LIVE` (STAGE: superseded, and
duplicated by a state channel on both distributed paths) / `CLASS_GAUGE` (FLEET:
meaning depends on arrival time, so a replayed sample is a lie) / `CLASS_RECORD`
(everything else). Used by `temporal_worker`'s activity-result relay and by both
of `cli_batch`'s drop decisions, which each used to carry their own literal list.

**An unclassified marker is a RECORD.** That default is the point: write a new
marker and every consumer forwards it already. Getting it wrong that way costs
bandwidth; getting it wrong the other way is #141.

### Step Functions vs Batch: which one does what

Everything in the cloud sections below assumes this split, so it is worth
stating once.

**Step Functions decides what runs when. Batch decides what machine it runs
on.** Neither can be dropped: SFN cannot run a container or manage a fleet, and
Batch has no concept of "these 336 jobs are one encode" or "audio must finish
before packaging" — it is a queue, not a graph.

The state machine (`infra/terraform/modules/workflow/definition.json.tpl`) is
pure coordination and never touches a frame:

```
MezzCheck (Choice)            mezz_cached, or built on the host -> skip
  └ Mezzanine (Task)
FanOut (Parallel)             audio and video are independent
  ├ Audio (Task)
  └ Variants (Map)            one branch per codec x ladder rung
      └ Chunked (Choice)      whole-variant, or...
        └ EncodeChunks (Map)
            └ EncodeChunk (Task)      <- the fan-out; 336 jobs on a full ladder
PerCodec (Parallel)           do_h264 / do_hevc / do_av1 gate each branch
  ├ H264Selected (Choice) -> PackageAllH264 | SkipH264
  ├ HevcSelected (Choice) -> PackageAllHevc | SkipHevc
  └ Av1Selected  (Choice) -> PackageAllAv1  | SkipAv1
```

Read that alongside "Which phases run on the HOST" below: **the graph still
contains Mezzanine and the PackageAll tasks, and their job definitions are still
registered, but a default run today skips both** via those Choice states. The
shape is a superset of what a given execution actually does.

It also supplies the **execution**, which is this system's unit of identity —
the execution name scopes the per-run SQS telemetry queue and the EventBridge
rule, which is why one run's chunk state can never leak into another's.

Batch owns machines. Each `Task` names a **job definition**
(`modules/jobs/main.tf`) — a container image plus a reservation:

| job definition | vCPU | memory |
| --- | --- | --- |
| variant (the chunk encoder) | 8 | 16 GB |
| mezzanine, audio, package, package-all | 2 | 4 GB |
| hls, byteranges | 1 | 2 GB |

Batch queues those, scales the spot compute env up, **packs them onto instances
by that reservation**, and scales back down. The reservation is a packing weight
rather than a hard cap (Batch uses CPU shares), which is why `projectCloudCost`
must price the reservation on both sides of its calibration.

**The job definition is the contract between the two layers**, and it is where
they break. A `Ref::` added on the Batch side that the SFN caller does not
supply fails at submit (#176), and `make deploy` mid-run deregisters job-def
revisions — pulling the contract out from under a live execution.

### Two Step Functions checks, asking different questions

`make check` runs both against `definition.json.tpl`, and neither substitutes
for the other:

| step | script | question |
| --- | --- | --- |
| `sfn scopes` | `check_sfn_scopes.py` | is it *consistent* — does every `$.field` a Map body reads get projected by its ItemSelector, and does every `Ref::` a job definition expects get supplied? |
| `sfn schema` | `check_sfn_definition.py` | is it *valid ASL* — would Step Functions accept it? |

The second exists because a definition AWS rejects passed everything else
cleanly: valid JSON, `tofu validate` happy, scopes green (#283 put a `Comment`
key inside three `ContainerOverrides.Environment` entries, where the Batch API
shape allows only `Name`/`Value`).

**A rejected ASL does not fail the deploy cleanly.** Terraform applies the job
definitions FIRST, so it bumps and *deregisters* the live revisions, then fails
on the state machine and leaves it pinned to revisions that no longer exist —
cloud encoding broken, nothing rolled back, and every retry re-breaking it the
same way (a revision bump re-renders the ASL, so the SFN resource always
updates). Which is why the same script is a hard precondition on `make deploy`
and `make infra-plan` (`require-valid-sfn`, `--require`): validate before
Terraform mutates anything, and this class becomes "nothing changed".

Two traps in the API it uses, both pinned by `scripts/test_sfn_definition.py`:

- **`aws stepfunctions validate-state-machine-definition` exits 0 when the
  definition is INVALID.** The call succeeded; the verdict is `result` in the
  payload. Gate on the exit code and the check passes everything forever.
- **It exits 0 on an auth failure too**, with nothing on stdout. So "no `result`
  in the output" must mean *skipped*, never *passed* — and on the deploy path,
  skipped means refused.

`require-valid-sfn` therefore degrades CLOSED, the opposite of `require-idle`
right above it. Deploying without being able to validate is precisely the
situation that costs a half-applied stack, and you need credentials to deploy
anyway.

**Retries are Batch's, not the state machine's.** SFN's `Retry` covers only
`Batch.AWSBatchException` / `States.Timeout` — submit-time blips — and its own
ASL comment says re-running a genuinely failed job just repeats an unrecoverable
failure. What retries a spot reclaim is the job definition's `retry_strategy`
(`attempts = 3`, `evaluate_on_exit` on `HostTerminated`). Host phases have no
such retry, deliberately; see below.

**The local target does the same two jobs with different tools**: Temporal is
the Step Functions equivalent (workflow, dependency graph, retries) and the
worker pool is the Batch equivalent (which box picks up a chunk). Same shape, no
AWS — which is why a chunk-plan change has to be checked on both paths.

### Which phases run on the HOST (cloud path)

Two of the cloud pipeline's phases no longer run in Batch. Both moved for the
same reason — the Batch job spent most of its life moving bytes to and from a
machine that did not need to be involved — and both work the same way: shell out
to the SAME `cli_phase` subcommand the job ran, with one flag pointing at a local
path instead of S3. Neither reimplements anything.

| phase | flag that goes local | what it removes |
| --- | --- | --- |
| mezzanine (#266) | `--s3-in` | the source upload, and the Mezzanine job |
| package-all (#197) | `--s3-out` | the pkgall job, its queue wait, and `download:outputs` |

**Neither needed a state machine change**, and that is not luck — it is the rule
to follow if a third phase moves. Each reuses a Choice the ASL already had:
`mezz_cached` routes past Mezzanine, and `do_h264` / `do_hevc` / `do_av1` gate
nothing but the per-codec packaging branch, so setting them false skips
`PerCodec` entirely. `buildSFNInput` computes them as `doX && !packageOnHost`
and emits the complementary `host_package` list for the orchestrator. Nothing in
`infra/` changes; rebuild the server and it takes effect.

The consequence is that **`do_h264` means "the STATE MACHINE packages h264"**,
not "h264 was encoded". `cmd_poll` needs the union of the two sets to build the
run plan, or a host-packaged run reports itself as encoding no codecs.

Host packaging is **forced off when the run is leaving its media in S3**
(`skipMediaDownload`). The two features want opposite things: one exists so
segments never come home, the other cannot package without pulling every chunk.
Honouring both would fetch the whole ladder and then upload the packaged result
back — strictly more transfer than either alone.

Two things that fail silently and so are pinned by tests:

- **Egress accounting.** With packaging here, the bytes billed as egress are the
  CHUNKS this pulls, not the packaged output the sync-back no longer fetches.
  `cli_batch` recovers that number by scanning `cli_phase`'s own printed fetch
  measurement as it relays it — a regex against a print. Reword either and the
  run reports zero egress, making host packaging look like a saving rather than
  the trade it is (see CLAUDE.md's rule on the estimate and cost staying on one
  basis). `scripts/test_host_package.py` pins the two together.
- **The run plan.** `download:outputs` is declared up front, so on an all-host
  run it would sit pending forever. `_emit_plan` takes `sync_back` and omits it
  when the state machine packaged nothing.

Each codec gets its own `ENCODER_WORK_DIR` — `cli_phase` rmtree's it on entry, so
a shared scratch would delete a sibling codec's inputs, and only ever on a
multi-codec run. `ENCODER_TELEMETRY_EXEC` is explicitly unset: on the host stdout
IS the channel to the server, and the orchestrator is the telemetry queue's
consumer, so leaving it set would have it drain its own output back.

**No Batch retry.** Named as a risk in #197 and real: a packaging failure fails
the job rather than being resubmitted onto fresh spot capacity. The trade is
deliberate — minutes of local CPU on a machine already up, versus an hour of
spot-reclaimable encoding — and the error says the chunks are still in S3, since
re-running with `PACKAGE_ON_HOST=0` packages them without re-encoding anything.

### Deferred packaging, and the FOUR states an output can be in

`DEFER_PACKAGING` / `defer_packaging` (#272) does not package at encode time at
all. The run is done when the last chunk lands — no package, no fragments, no
hls, no sync-back — and the chunks stay in S3 until someone presses Package.
The post-encode tail does not shrink, it disappears.

It reuses the #197 worker wholesale: `cli_batch package --dir <output>` reads the
sidecar and calls the same `_package_on_host`. That is why this is a scheduling
change rather than a second packaging implementation.

**Deferring supersedes skip-media-download; they are not combinable.** Both keep
bytes in S3 until wanted, and deferring keeps strictly more — the packaged output
is never made, so there is nothing to leave behind. Honouring both would write a
`.remote.json` describing media that does not exist.

An output directory now carries one of four states, and **each is a distinct
file rather than a field**, because #225's finding was that these collapse:

| on disk | means | offer |
| --- | --- | --- |
| (neither sidecar) | complete | Play |
| `.remote.json` | packaged; SEGMENTS in S3 | Download |
| `.pending.json` | encoded, never packaged; CHUNKS in S3 | Package |
| either, `gone: true` / expired | unrecoverable | nothing, and say why |

A pending directory is the **furthest from complete** — no manifests, no rung
subdirs, one JSON file. So `parseOutputMeta` cannot distinguish it from an empty
finished encode, and the UI checks `pending` *before* `remote` and before the
no-badge fallback. Get that order wrong and a deferred run renders as a finished
one with nothing in it.

**The chunks are the ONLY copy.** Under `.remote.json` an expiry costs the media
but the manifests survive; here it costs the run, which must be re-encoded from
source. Two consequences: `staging_retention_days` is now a decision rather than
an inherited default, and `cmd_package` spends one `list_objects_v2` up front so
"this can never work" is reported as `EXIT_STAGING_GONE` rather than as
phase_package_all's accurate but useless "no h264 variants found".

`gone` is SET, never signalled by deleting the sidecar — deleting it reclassifies
the output as complete, which is the one wrong answer available.

Two ordering rules that fail silently:

- **The sidecar is removed LAST**, after the media is moved in. Its absence is
  what reclassifies the output as finished, so removing it first makes the
  directory read as complete for the minutes packaging takes.
- **Packaging stages into a sibling `.packaging-<name>/`**, never in place, for
  the same reason.

`do_h264` / `do_hevc` / `do_av1` and `host_package` are both empty on a deferred
run, so **`encoded_codecs` is carried separately** — it is the only thing left
saying which codecs the run produced, and `cmd_poll` builds the run plan and one
marker directory per entry from it.

### Batch state, event-driven (cloud)

Chunk state comes from **EventBridge → a per-execution SQS queue**, not from
polling. The orchestrator creates the rule + queue at submit (scoped by a
`jobName` suffix pattern so it matches only its own execution), drains it each
poll, and deletes both at the end; `_gc_telemetry_queues` sweeps orphans.

Verified on this account before it was built — `jobName` on every event,
`containerInstanceArn` and `logStreamName` on 100% of STARTING/RUNNING/SUCCEEDED,
`attempts`/`exitCode`/`stoppedAt` on 100% of SUCCEEDED, delivery median 0.6s. The
STARTING result matters: a `describe_jobs` poll races placement and often has no
instance arn, so host colouring needed a later backfill; the event carries it, so
a chunk is coloured by the same message that says it started.

`_sync_stages_from_batch` remains as a **backstop** on `_CENSUS_BACKSTOP_S`
(60s), because EventBridge is at-least-once, not guaranteed-delivery. With no
event channel it reverts to every poll — the old behaviour.

**Every state event is logged** with wall-clock time and delivery lag
(`[state] 13:24:51.123 lag=+0.6s running encode:h264:1080p:chunk7`), including
suppressed ones, so a timing question is answered from the job log rather than
by reproducing it.

**All stage emission goes through `_emit_stage`.** Three sources announce state
(SFN history, the Batch census, worker markers) through channels with different
latencies, so they disagree about the present tense. The chokepoint refuses to
announce anything but `done` over an existing `done`. `failed` is deliberately
not final — a reclaimed chunk is retried and goes back to running, so
`failed → running` is real. (That retry is **Batch's** `evaluate_on_exit`, not
the state machine's `Retry` — see the split above. This line used to credit SFN,
which sends anyone debugging an odd retry to the wrong file.) Guarding at call
sites was tried twice and failed twice: each time a different source was still
speaking unguarded.

**`internal/api/handlers.go`** — the HTTP surface. Routes defined in `NewServer`:
- JSON API: `GET /api/sources`, `GET/POST` under `/api/encode`, `/api/jobs`, `/api/jobs/{id}/logs`, `/api/outputs`, `/api/outputs/{name}`, `/api/outputs/{name}/playlists`, `/api/outputs/{name}/logs`.
- SSE: `GET /api/jobs/stream` — emits the full current job list immediately, then streams updates from `Manager.Subscribe()`.
- Static file servers: `/logs/` (from `TmpDir/logs`), `/content/` (from `OutputDir`, for HLS.js playback), `/sources/` (from `SourceDir`, direct playback), `/` (from `./static`, which maps to `/app/static` inside the container).
- `mediaFileServer` wraps `http.FileServer` to set the correct Content-Type for `.m3u8`, `.mpd`, `.m4s`, `.ts` — Go's MIME db doesn't know these and browsers/HLS.js won't play without them.

`parseOutputMeta` infers codec / resolutions / HLS format / partial duration / padding from directory naming and contents for the UI — it's the inverse of `OutputStem` + the script's codec suffix.

**`internal/watcher/watcher.go`** — polls `SourceDir` on `watch-interval` (default 30s). A file is auto-submitted when: (1) it's been seen before, (2) its size hasn't changed since last scan (ensures write is complete), (3) `alreadyEncoded` returns false. `alreadyEncoded` checks both in-memory jobs and `OutputDir` for any dir matching `<stem>_*` — so deleting an output dir triggers re-encode on next scan without needing a restart. First scan after startup only seeds `seen` and does not submit.

**`scripts/infinite_streaming_encoder/`** — the real encoder, as a Python package. Edits here affect encode behavior without touching Go. Orchestrators:
- `cli_local.py` — single-container local pipeline entry. Runs input probe, mezzanine stream-copy, variant encodes (per codec × resolution), audio extract, Shaka Packager DASH, the fragment-granularity manifest, LL-HLS playlists, and optional TS HLS.
- `cli_batch.py` — cloud orchestrator. Submits the Step Functions execution, creates/drains/deletes the per-execution telemetry queue and EventBridge rule, relays state, packages on the host, syncs back, and prices the run. Also the CLI behind `fetch` / `package` / `gc`.
- `cli_local_dist.py` — farm orchestrator, the Temporal twin of the above. Builds the mezzanine and packages on the host, starts the workflow, relays stage/host/fleet markers from its history and pending activities, and reclaims MinIO staging.
- `cli_phase.py` — ONE phase, in a worker: mezzanine / variant (chunk) / audio / package-all. Both orchestrators shell out to it for the phases they run themselves, with `--s3-in` / `--s3-out` pointing at a local path — so a host phase is the same code as the worker phase, not a reimplementation.
- `temporal_worker.py` — the farm worker: `WORKER_LABEL` identity, slot sizing, heartbeats carrying live % + CPU + build, and the `EncodeWorkflow` graph itself.

There is no `cli_cloud.py`. The single-box EC2 target it drove was retired when
cloud encoding moved to Step Functions + Batch, and it went with
`cloud/launch.py`, `cloud/userdata.py` and `cloud/poll.py` (#15). A few comments
in `internal/encode/job.go` still name it; they describe history, not a path.

Supporting modules (all stdlib-only except `cloud/*` which uses boto3):
- `ffprobe.py` — structured ffprobe wrapper; fps as `Fraction` for exact GOP math.
- `mezzanine.py`, `audio.py`, `encode_variants.py`, `packager.py`, `hls.py` — one per phase; each exposes a pure builder for its ffmpeg/packager argv plus a function that runs the subprocess.
- **`docs/spec/`** — the behavioural spec set: what the system observably DOES,
  given inputs, as a complement to `docs/PRD.md`'s what-and-why. Seven files —
  ingest, job lifecycle, chunk plan, outputs, targets, cost, retention — each in
  the `docs/spec-template.md` shape and each auditable with `/spec`. Shipped
  behaviour only; every claim is meant to be falsifiable against the code. It is
  NOT a substitute for this file: anything needed at EDIT time (a contract
  between two files, an ordering that fails silently) belongs here, because this
  is auto-loaded and `docs/` is not.
- **`docs/ladders-and-delivery.md`** — what a ladder actually contains: rungs
  AND the delivery profile (segment / partial / GOP / VBV) AND the output tag,
  why they are one object, the `peak = maxrate% + bufsize/T` relation that
  makes a tight VBV the price of re-choppability, the three competing drivers
  of GOP, and the tag-derivation rule that gives every pinned-segment ladder
  the SAME empty tag (so two of them overwrite each other).
- `ladder.py` — rung selection + `--bitrate-override-*` parsing. The tables are
  DATA now (`$STATE_DIR/ladders.json`, which defaults to `$TMP_DIR`), not a
  hardcoded 6-tier × 3-codec block.
- `gop.py` — `KEYINT = round(fps × gop_s)`, min 1, on Fraction fps.
- `padding.py` — LCM-based segment-boundary padding; 0.5s skip-threshold.
- `burnin.py` — 5-layer drawtext filter (timecode, rate, codec+res+fps, encoder, watermark) + optional PADDING label on padded frames only.
- `vmaf.py` — CSV lookup with linear interpolation; no-op when CSV absent.
- `manifests.py` — folds the former `convert_to_segmentlist.py` (DASH SegmentTemplate → SegmentList), the fragment-granularity expansion of `manifest.mpd`, and an HLS master/variant generator.
- `fragments.py` — fMP4 box walker yielding each fragment's offset/length/independent. Reimplementation of the external `parse_fmp4_fragments.py` (not in the original repo) using stdlib `struct` only.
- `resume.py` — scans a temp dir for `{codec}_{res}.mp4` files, used by `--resume-package-from` to skip phases 1-4.
- `cloud/` — boto3-backed subpackage, and NOT legacy: everything here serves the
  Batch path or the AWS tab. `aws.py` (client factory + STS preflight),
  `sync.py` (idempotent upload + paginated download), `inventory.py`
  (read-only "what is running and costing money", which the Go server shells
  out to), `cleanup.py` (tear down what this tool created — its report is what
  `MarkGoneUnderPrefix` reads), `batch_admin.py` (cancel: stopping an execution
  does not stop its in-flight jobs), `compute_env.py` (live `maxvCpus` without a
  redeploy), `image_state.py` (which tag Batch will actually pull, + the
  awswatch self-heal), `timing.py` and `cpu_report.py` (per-execution
  where-did-the-time-go, and per-tier CPU from the TIMING markers), and `arch.py`
  (cpu-arch profiles, currently reachable only as data — one compute env means a
  job cannot land on Intel or AMD; see `projectCloudCost`).

Hardware encoding (VideoToolbox `--force-hardware`) was intentionally dropped in the rewrite — it only works on macOS hosts, not Linux workers.

Workers pull the same `ghcr.io/jonathaneoliver/infinite-streaming-encoder` image the local server builds from `Dockerfile`, so local and cloud encodes share the Python pipeline end-to-end. Batch job definitions pin a content-hash `IMAGE_TAG` rather than `:latest` (see the `version` note below); the farm's workers take `:latest` unless `make farm-test-up` gave them a throwaway tag.

**`static/index.html`** — a single self-contained HTML file (vanilla JS, no build step). Served directly by the Go file server. It polls `/api/jobs/stream` for live updates and plays HLS via hls.js pointed at `/content/<dir>/<playlist>.m3u8`.

### Background polling (page + server)

Every periodic fetch the page makes goes through `registerPoller` /
`syncPollers`, and a timer runs only when all three gates agree: the browser tab
is not hidden, the user has not gone idle (`pollingIdle()`, 5min), and the
poller's own `when()` says its view is on screen. Add a bare `setInterval` and
you have re-created #228: `/api/outputs` walks 133 dirs / 108,606 files on an
external volume, and it was doing that every 60s on every tab including ones
nobody had looked at since yesterday.

Returning the Jobs/AWS tabs to Originals is a **separate, slower** clock
(`viewExpired()`) on purpose — stopping a fetch is free and the next click
undoes it, but moving someone's tab is disruptive. It is anchored on the work
rather than the user: 30min after the last encode went terminal, or an hour
while one is still running. Keep both, and keep them apart; collapsing them
means either paying for polls nobody wants or yanking the view out from under
someone who came back to read a finished run (#227, #228).

Server-side, the corresponding rule is that the AWS inventory's S3 staging walk
(`_s3_prefix_inventory`) is the only part whose cost is O(objects held) rather
than O(resources running), and it feeds exactly one display. So `awswatch`
passes `--no-s3-prefixes` (it reads instances/executions/Batch jobs and nothing
else), and **the walk is opt-in per HTTP request (`?s3=1`), never on a timer.**

Gating it to the visible tab was not enough, because the tab someone is
*looking at* polls every 10s and the walk is expensive in a way nothing else
here is: the caller is the Mac, not an in-region worker, so S3 bills every byte
of the listing XML as **internet egress**. Measured over 23 days on the real
account, egress tracks Tier1 (LIST) count at r=0.99 and spot hours at only
r=0.33 — it is nearly independent of whether anything is encoding. On a day
with zero encodes that was 62,865 LIST requests and 20.3 GB out, ~$2/day and
~79% of the whole bill, for a table usually not on screen. And because `jobs/`
has a 7-day lifecycle, every object a run leaves keeps being re-listed for a
week after it ends.

So the walk happens when a human asks: opening the AWS tab (`pollNow('aws-inventory', true)`),
the Refresh button, and any path that just deleted staging. `shouldWalkS3`
floors the rate at `s3PrefixMinInterval`; in between, the last measurement is
spliced in with an `s3_prefixes_at` stamp so the tab says how old it is. The
10s poll passes nothing and pays nothing.

`null` s3_prefixes means "not measured" — never cache it as "nothing staged",
and never render it as `0 B` either (`stagedBytesText` → `—`, and `renderAwsS3`
must be handed the raw value, not `|| []`). Every user-facing path that deletes
staging must call `invalidateS3Prefixes`.

Same shape of rule for `awswatch`'s `gc_failed_staging`: it enforces an
*hours*-scale retention (`FailedStagingMaxAge`), so it runs on its own
`FailedStagingInterval` rather than on the 60s inventory tick. Idempotent is
not free — each pass is a LIST plus a `head_object` per job prefix.

## Things to know when editing

- Output dir naming is a contract shared by `OutputStem`, the encode script (appends `_<codec>`), `parseOutputMeta`, `resolveCodec`, and the watcher's `alreadyEncoded`. Changing the format means touching all of them.
- The worker container name format (`encoder_job_<id>_f<idx>`) is a contract between `runFileContainer` and `Reconcile` — changing it means losing the ability to reattach to workers started by older server versions.
- The all-digits job ID (`Submit` → `time.Now().UnixMilli()`) is a contract with `internal/tmpstage`, which uses that shape to tell a reclaimable job directory from the caches and learned state sharing `$TMP_DIR`. Give IDs a prefix and the sweeper stops seeing them (a silent leak); name anything else in `$TMP_DIR` with digits alone and it becomes eligible.
- The docker.sock mount is what lets the Go server spawn and talk to worker containers. Without it, `docker run` / `docker logs -f` / `docker inspect` all fail and no encoding happens.
- Host paths: the server needs both container-side paths (`SOURCE_DIR`, `OUTPUT_DIR`, `TMP_DIR` — used for all in-process file I/O) and host-side paths (`HOST_*` — used only for `-v` flags when launching workers). Workers mount host paths at the same paths the Go server uses, so script args don't need translation.
- `move to OutputDir` only happens on success. A failed job leaves nothing in `OutputDir` (the `$TMP_DIR/<job_id>/` is unconditionally removed in `run`'s defer path).
- The MinIO staging key (`encode.DistJobPrefix` → `jobs/<jobID>-<base>/`) is a contract between the orchestrator's `--job-prefix` and the staging GC's keep-list. Deriving it separately in either place is how you get a GC that deletes a running encode's chunks.
- The telemetry queue name (`telemetry.queue_name()` → `encoder-telemetry-<execution>`) is the same shape of contract, between the worker that publishes and the orchestrator that creates/drains/deletes. Derive it separately and you get a worker publishing into one queue while the orchestrator polls another — with **no error on either side**, because both operations succeed. Execution names reach 67 chars against SQS's 80-char limit, so the name is trimmed; the trailing uniqueness suffix must survive the trim or two executions of the same job share a queue.
- The sweep that reclaims those queues (`_gc_telemetry_queues`, plus `_gc_state_rules`) has **two triggers, and needs both**: `cmd_submit`, and the server's hourly `cli_batch gc` from `awswatch`. Submit alone meant an orphan waited for the next cloud encode — and waited forever once you stopped encoding (#191). **It must never run unscoped:** `--state-machine-arn` is what fills the keep-list, `_active_execution_cores` returns an *empty* set without it, and a run outliving the 1h message retention sits at zero messages looking exactly like an orphan — so an unscoped sweep can delete a live run's queue. The CLI requires the flag and `maybeGCTelemetryQueues` declines to run without `STATE_MACHINE_ARN`; degrading open here would be worse than the leak.
- Emit new markers via `telemetry.emit()`, and remember `cli_batch` is the queue's consumer — it must keep using `print`.
- **`.remote.json` is the media-is-still-in-S3 flag** (`encode.RemoteSidecar`, written by `_write_remote_sidecar` in `cli_batch.py`, read by `encode.ReadRemote`). Its **presence** is the state — written by a `--no-media` sync-back, deleted by a completed `cli_batch.py fetch` — so the two languages agree on a filename and nothing else. Field names in `encode.RemoteInfo` are a contract with the Python writer. A metadata-only output dir is indistinguishable from a complete one by every other signal: right name, right rung subdirs, manifests present, `parseOutputMeta` happy. Miss the sidecar and the UI offers Play, hls.js loads the playlist, and every segment 404s.
- **The sidecar has three states, not two** (#225). `expires_at` says when the lifecycle rule will remove the media; it does **not** say the media is still there. Everything else that removes objects — a staging clear, a console delete, the lifecycle firing early off each object's own creation time — leaves an output that looks available and fails on the click. So `gone: true` is set (never the file deleted — deleting it reclassifies the output as *complete*, the one wrong answer available) by two paths: `cmd_fetch` when the listing comes back **empty**, exiting `EXIT_STAGING_GONE` (4, mirrored as `encode.exitStagingGone`); and `Manager.MarkGoneUnderPrefix`, called from every cloud-clear handler with the prefixes out of `cleanup.py`'s own report. Ask `RemoteInfo.Fetchable()` rather than re-deriving from `Expired()`. **No S3 call belongs on the `/api/outputs` path** — it already costs ~0.8s over 30 dirs, and a HEAD per remote output every poll would be far worse than the problem.
- Media exclusion is stated as `_MEDIA_SUFFIXES` (`.m4s`, `.byteranges`) — an **exclusion, not an allow-list**, so a new metadata file the packager starts writing ships by default instead of being silently dropped. Measured on a real ladder: metadata is 3.99 MB of 2.64 GB (0.151%). `.byteranges` is retained there for library content only; nothing writes sidecars since #282, but old outputs still have them and every path that reads or classifies them must keep tolerating both shapes.

### One DASH manifest, at fragment granularity (#282)

`manifest.mpd` is written **in place** at fragment granularity: one
`<SegmentURL @media @mediaRange>` per fragment, with a fragment-level
`<SegmentTimeline>`. There is no `manifest_fragmented.mpd` and no
`<segment>.byteranges` sidecar — one file replaces three-plus-728-per-rung.

The fragment form is a strict **superset**: segment membership regroups by
`@media`, and a segment's duration is the sum of its fragments'. `@mediaRange` is
inclusive, so length is `last-first+1`, and the first fragment starts at **432** —
bytes 0-431 are the segment's own `styp`/`sidx` header and belong to no fragment.
go-live relies on that offset, and detects granularity from the presence of
`@mediaRange` rather than from the filename, so old content keeps working and this
needed no flag day.

Three rules hold this together:

- **`write_fragmented_mpd` is idempotent.** `@mediaRange` anywhere means "already
  expanded, stop". Phases retry and resume, so it runs twice on the same directory
  routinely, and re-expanding would split each *fragment* into sub-fragments.
- **`_extract_segments` collapses either granularity** back to whole segments, so
  HLS generation cannot be corrupted by the expansion having run first. That
  replaced an ordering rule (`package → byteranges → hls`) which had already been
  got wrong once.
- **HLS reads the media, not the manifest,** for its parts:  `@mediaRange` carries
  no independence bit, and `#EXT-X-PART` needs `INDEPENDENT`. That is the one
  thing the sidecars held which the manifest cannot — a deliberate, one-way loss
  for the DASH side, where nothing reads it.

`manifests._segment_fragments` prefers a `.byteranges` sidecar when one exists so
a pre-#282 package repackages identically, and otherwise walks the `.m4s`. Both
branches call the same `fragments.parse_segment`, so they cannot disagree.
- **`version` (worker, fleet view, `run.json`) means ENCODER PAYLOAD, not build** (#248). It is `IMAGE_TAG`, which the Makefile derives as `git log -1 --format=%h -- Dockerfile requirements.txt scripts static` — excluding `internal/` and `cmd/`, while the Dockerfile still copies the Go server binary into the image. Two consequences, both deliberate: a **Go-only change does not move the tag**, so it publishes different image content under the same tag and is invisible to `make fleet-check` by construction; and **rollback is asymmetric** — `make infra-plan IMAGE_TAG=<prev>` restores the encoder payload, not the server binary that shipped in that image (nothing expects it to, since the server is not deployed from the pinned tag). This is the right identity for the question being asked — a chunk's encode behaviour lives in `scripts/`, so two chunks agreeing on `version` ran identical encoder code — but "version" invites a stronger reading than it can support, and someone will eventually chase a server-side skew the tag is correctly refusing to show.
- **The learned-speed key is a cross-language contract with no schema, and the PASS COUNT is the part that breaks.** A worker prints `[[ENCODER-SPEED … two_pass=N …]]`; Go builds `speedKey` from its own idea of the pass count; `Speed()` is an exact map lookup with **no cross-pass fallback**. So the two agreeing is the entire mechanism, and when they stopped agreeing both sides went on succeeding and logging nothing (#314). h264 wrote `:1:` forever while the planner read `:2:`, so no h264 encode could move the model however many completed, and 43,021 samples piled onto a curve nothing reads. The cause was five copies of one rule — `codec == "hevc" && !hevcSinglePass` — which was correct only while h264 was single-pass and silently wrong the day the ladder default became 2 for every codec. There is now **one definition per language**: `encode_variants.two_pass_for(ctx, codec)` and `LadderDef.twoPassFor(codec, hevcSinglePass)`. Derive it anywhere else and you get the same silent divergence; a grep for `== "hevc" &&` is how you find the next copy. The join is pinned from both ends — `scripts/test_speed_marker_pass.py` checks the marker's label against the argv the encoder actually builds (and that `cli_phase` still *calls* the shared decision, since the bug was an emitter that never did), and `speed_pass_key_test.go` checks that a marker lands in the key the planner reads. Contaminated keys are removed with `scripts/purge_speed_keys.py`, which refuses to run while the server is up: the store is loaded once at startup and rewritten whole on every finished chunk, so a purge underneath a running server is undone by the next one — silently, and looking exactly like it did not work.
- **A stage's machine comes from `ENCODER-HOST` and from nothing else**, and the machine timeline drops any stage without one (`_laneStages` filters on `s.instance`) — so a phase that fails to say where it ran draws its box as IDLE while it works, and its minutes land in the lane's idle arithmetic (#293). Two emitters, because there are two kinds of phase. A phase in a WORKER is read off Temporal's pending-activity record (`last_worker_identity`) rather than from history: Temporal does not write `ActivityTaskStarted` into history until the activity COMPLETES, so the history reader cannot answer while the answer matters. A phase this process runs itself (the host mezzanine, host packaging) never reaches a worker at all and reports its own box from `WORKER_LABEL` — which is why `buildRunArgs` passes `LOCAL_WORKER_LABEL` into the local-dist orchestrator, and why the two must keep spelling the box the same way or one machine gets two lanes. `_SELF_RUN_STAGES` is excluded from the worker emitter for the same reason it is excluded from the history reader: the workflow still schedules a mezzanine activity that a worker picks up, finds the `.done`, and returns from, and it would otherwise claim a row the master earned. Order matters — the Go handler updates a stage row in place and, unlike `ENCODER-REUSED`, does NOT seed one, so a HOST marker emitted before the plan is dropped in silence.
- **The pre-flight estimate and the finished run's cost must stay on one basis.** AWS bills the INSTANCE — launch to termination, boot, image pull and the scale-down tail — not the vCPU-time jobs allocate. `_emit_cost_summary` prices the rental; `projectCloudCost` predicts allocation and converts with the one term that reconciles them, `machine = allocated / (1 - idle)`, from `fleetIdleFraction`. Before that term existed the same app quoted ~60% of what it then reported (#237). Two rules keep it honest: the allowance is **shown, never folded in** (an invisible 1.7x next to the Encode button is how the gap survived), and `variantResourcesFor`'s **reservation** must stay on both sides — `unallocated_pct` is defined against reserved vCPU, so pricing measured busy-cores instead would break the calibration and restore the undercount.
- `$STATE_DIR/spot_samples.json` (`encode.RunSample`) is a **cross-language contract**: Go writes it at every terminal job, `inventory.py`'s `_spot_and_reclaim_stats` reads it **by field name and ignores what it doesn't know**. Adding fields is safe; renaming one silently zeroes the AWS view's spot savings with no error on either side. `idle_pct` there is a lower bound — boxes still alive at run end have their lifetime measured to now — and samples whose `started_at`/`ended_at` overlap another run are excluded from the allowance, because `_emit_machine_rental` counts a concurrent run's time on a shared instance as this run's idle.
- **Cost figures never subtract free tier.** Every estimate and every reported cost is what the run would cost with the allowance already exhausted. Asked three separate times (2026-07-29, 08-05, 08-07); the reason is that the free tier is a property of the account's month, not of the run, so folding it in makes two identical runs quote different numbers and makes a `$0.00` line read as "this is free" when it means "you have not hit the cap yet". State the allowance separately if it is interesting — the same rule the fleet-idle allowance follows in the bullet above.
- **VMAF uses ONE reference model across a whole ladder, and it is chosen by the ladder's TOP rung, not per-rung.** `vmaf_4k_v0.6.1` when any rung exceeds 1080p. Switching models between rungs makes the scores incomparable, which is the only thing the ladder audit exists to compare — a 4K HEVC rung and a 1080p h264 rung scored under different models cannot be put in the same table. Asked four times (2026-07-25 through 08-11). `quality-curves.json` records `model` and the common resolution per point for exactly this reason; a curve without them is unusable, and the Ladders tab's estimates are keyed by `(codec, height, bitrate, clip)` and ignore VBV and GOP entirely (#288).
- `projectCloudCost` hardcodes `"graviton"` **on purpose**: `infra/terraform/modules/compute/main.tf` runs one compute env, c8g/c7g only, so a cloud-batch job cannot land on Intel or AMD. Honouring `JobConfig.CpuArch` would quote hardware the run can never reach. The form's cpu-arch control is hidden as retired legacy for the same reason. Wire it up when a second compute env exists, not before.
