# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Go HTTP server + single-page UI that drives video encoding. The Go code is a thin control plane — the actual encoding work lives in the Python package under `scripts/infinite_streaming_encoder/`, each run in its own **detached sibling Docker container** (so encodes survive a restart of the server's own container). The server submits encode jobs, tails their stdout via `docker logs -f` into a log buffer, and exposes a web UI for browsing sources, kicking off jobs, and playing back the resulting HLS output.

Testing is thin and deliberately targeted — there is no broad unit-test suite, so
do not assume one and do not invent commands beyond these three:

- `make check` — the static gate: gofmt/vet/build, `go test ./...`, tofu fmt,
  the Step Functions checks, `ruff` F821, python compile, page JS.
- `go test ./...` — currently only `internal/encode/chunkplan_test.go`, which
  pins the Go chunk planner to golden vectors generated from the Python one.
  Both paths must cut a clip in the same places or local and cloud encodes stop
  being comparable.
- `make smoke` — a REAL short encode end to end (synthetic 30s clip, chunked),
  asserting the job reaches `done` AND produced playlists. `TARGET=cloud` for
  the cloud path, `WHOLE=1` for the whole-variant path.

**`make check` passing is not evidence the code runs.** #176 passed every static
check and still broke both encode paths: a Batch job definition gained a `Ref::`
its whole-variant caller never supplied, and a log line referenced a list the
worker no longer built. Both were in the seams between orchestrator, worker and
job definition. Run `make smoke` before merging anything that touches the
chunk/dispatch contract — and `make smoke TARGET=cloud` too when the state
machine or job definitions change, since the cloud submission path has no local
equivalent.

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
3. On success, `moveTmpToOutput` renames each top-level subdir into `OutputDir`. If a dir already exists there: with `ForceReencode=true` it's moved to `OutputDir/.archive/<name>_<timestamp>`; otherwise it's deleted.
4. If rename fails (cross-device), falls back to `copyDir`.
5. Per-job log written (ANSI stripped) and `history.md` appended regardless of outcome. State file removed.

Two key naming/skip conventions in this file:
- `JobConfig.OutputStem(filename)` — `<stem>_p<partialMs>[_padblack|_padpink]`. The encoding script then appends `_<codec>` to produce the final output dir name (e.g. `myclip_p200_h264`). The watcher and `parseOutputMeta` both rely on this layout.
- `Manager.resolveCodec` — before encoding, checks which `<stem>_h264` / `_hevc` / `_av1` dirs already exist in `OutputDir` and narrows the codec flag to only the missing ones. Returns `""` → skip this file entirely. Bypassed when `ForceReencode=true`.

The worker container's entrypoint is overridden to `scripts/infinite_streaming_encoder/cli_local.py` (local) or `scripts/infinite_streaming_encoder/cli_cloud.py` (cloud). Cloud workers additionally receive `HOST_AWS_DIR` as `/root/.aws:ro` and the AWS/GHCR env vars listed in `cloudEnvPassthrough`. Stdout is line-scanned into `job.logLines` (capped at 1000 lines, trimmed to last 500) and the latest line (ANSI-stripped) becomes `job.Progress`, which the SSE stream surfaces live.

### MinIO staging lifecycle (local-dist)

Every local-dist file stages through `s3://$DIST_S3_BUCKET/jobs/<jobID>-<base>/`
(source upload, mezzanine, per-chunk encodes, variants, packaged output — ~2.3 GB
for a typical clip). `encode.DistJobPrefix` is the single definition of that key,
shared by the orchestrator's `--job-prefix` argument and the GC's keep-list.

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
not final — the state machine's Retry resubmits a new Batch job, so
`failed → running` is real. Guarding at call sites was tried twice and failed
twice: each time a different source was still speaking unguarded.

**`internal/api/handlers.go`** — the HTTP surface. Routes defined in `NewServer`:
- JSON API: `GET /api/sources`, `GET/POST` under `/api/encode`, `/api/jobs`, `/api/jobs/{id}/logs`, `/api/outputs`, `/api/outputs/{name}`, `/api/outputs/{name}/playlists`, `/api/outputs/{name}/logs`.
- SSE: `GET /api/jobs/stream` — emits the full current job list immediately, then streams updates from `Manager.Subscribe()`.
- Static file servers: `/logs/` (from `TmpDir/logs`), `/content/` (from `OutputDir`, for HLS.js playback), `/sources/` (from `SourceDir`, direct playback), `/` (from `./static`, which maps to `/app/static` inside the container).
- `mediaFileServer` wraps `http.FileServer` to set the correct Content-Type for `.m3u8`, `.mpd`, `.m4s`, `.ts` — Go's MIME db doesn't know these and browsers/HLS.js won't play without them.

`parseOutputMeta` infers codec / resolutions / HLS format / partial duration / padding from directory naming and contents for the UI — it's the inverse of `OutputStem` + the script's codec suffix.

**`internal/watcher/watcher.go`** — polls `SourceDir` on `watch-interval` (default 30s). A file is auto-submitted when: (1) it's been seen before, (2) its size hasn't changed since last scan (ensures write is complete), (3) `alreadyEncoded` returns false. `alreadyEncoded` checks both in-memory jobs and `OutputDir` for any dir matching `<stem>_*` — so deleting an output dir triggers re-encode on next scan without needing a restart. First scan after startup only seeds `seen` and does not submit.

**`scripts/infinite_streaming_encoder/`** — the real encoder, as a Python package. Edits here affect encode behavior without touching Go. Orchestrators:
- `cli_local.py` — local pipeline entry. Runs input probe, mezzanine stream-copy, variant encodes (per codec × resolution), audio extract, Shaka Packager DASH, fragment byteranges, LL-HLS playlists, and optional TS HLS.
- `cli_cloud.py` — cloud pipeline entry. Uploads inputs to S3, launches a spot EC2 instance (with AZ + instance-type fallback on InsufficientInstanceCapacity), polls for `_DONE`/`_FAILED` markers, syncs outputs back. Uses boto3.

Supporting modules (all stdlib-only except `cloud/*` which uses boto3):
- `ffprobe.py` — structured ffprobe wrapper; fps as `Fraction` for exact GOP math.
- `mezzanine.py`, `audio.py`, `encode_variants.py`, `packager.py`, `hls.py` — one per phase; each exposes a pure builder for its ffmpeg/packager argv plus a function that runs the subprocess.
- `ladder.py` — 6-tier bitrate table × 3 codecs + `--bitrate-override-*` parsing.
- `gop.py` — `KEYINT = round(fps × gop_s)`, min 1, on Fraction fps.
- `padding.py` — LCM-based segment-boundary padding; 0.5s skip-threshold.
- `burnin.py` — 5-layer drawtext filter (timecode, rate, codec+res+fps, encoder, watermark) + optional PADDING label on padded frames only.
- `vmaf.py` — CSV lookup with linear interpolation; no-op when CSV absent.
- `manifests.py` — folds the former `convert_to_segmentlist.py` (DASH SegmentTemplate → SegmentList) and an HLS master/variant generator.
- `fragments.py` — fMP4 box walker producing `.byteranges` sidecars for EXT-X-PART. Reimplementation of the external `parse_fmp4_fragments.py` (not in the original repo) using stdlib `struct` only.
- `resume.py` — scans a temp dir for `{codec}_{res}.mp4` files, used by `--resume-package-from` to skip phases 1-4.
- `cloud/` — boto3-backed subpackage: `aws.py` (client factory + STS preflight), `launch.py` (spot run-instances + fallback), `userdata.py` (remote bash template, shlex-quoted), `poll.py` (S3 marker polling), `sync.py` (idempotent upload + paginated download).

Hardware encoding (VideoToolbox `--force-hardware`) was intentionally dropped in the rewrite — it only works on macOS hosts, not Linux workers.

The EC2 user-data itself is still bash (it runs on the remote instance) — `cloud/userdata.py` renders it as a template string. The remote pulls the same `ghcr.io/jonathaneoliver/infinite-streaming-encoder:latest` image the local server builds from `Dockerfile`, so local and cloud encodes share the Python pipeline end-to-end. The image's default entrypoint (`python3 -m infinite_streaming_encoder.cli_local`) accepts the CLI surface the user-data passes, so no `--entrypoint` override is needed.

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
else), and the HTTP handler computes it on demand and reuses it for
`s3PrefixTTL`. `null` s3_prefixes means "not measured" — never cache it as
"nothing staged", and every user-facing path that deletes staging must call
`invalidateS3Prefixes`.

## Things to know when editing

- Output dir naming is a contract shared by `OutputStem`, the encode script (appends `_<codec>`), `parseOutputMeta`, `resolveCodec`, and the watcher's `alreadyEncoded`. Changing the format means touching all of them.
- The worker container name format (`encoder_job_<id>_f<idx>`) is a contract between `runFileContainer` and `Reconcile` — changing it means losing the ability to reattach to workers started by older server versions.
- The docker.sock mount is what lets the Go server spawn and talk to worker containers. Without it, `docker run` / `docker logs -f` / `docker inspect` all fail and no encoding happens.
- Host paths: the server needs both container-side paths (`SOURCE_DIR`, `OUTPUT_DIR`, `TMP_DIR` — used for all in-process file I/O) and host-side paths (`HOST_*` — used only for `-v` flags when launching workers). Workers mount host paths at the same paths the Go server uses, so script args don't need translation.
- `move to OutputDir` only happens on success. A failed job leaves nothing in `OutputDir` (the `$TMP_DIR/<job_id>/` is unconditionally removed in `run`'s defer path).
- The MinIO staging key (`encode.DistJobPrefix` → `jobs/<jobID>-<base>/`) is a contract between the orchestrator's `--job-prefix` and the staging GC's keep-list. Deriving it separately in either place is how you get a GC that deletes a running encode's chunks.
- The telemetry queue name (`telemetry.queue_name()` → `encoder-telemetry-<execution>`) is the same shape of contract, between the worker that publishes and the orchestrator that creates/drains/deletes. Derive it separately and you get a worker publishing into one queue while the orchestrator polls another — with **no error on either side**, because both operations succeed. Execution names reach 67 chars against SQS's 80-char limit, so the name is trimmed; the trailing uniqueness suffix must survive the trim or two executions of the same job share a queue.
- Emit new markers via `telemetry.emit()`, and remember `cli_batch` is the queue's consumer — it must keep using `print`.
- **`.remote.json` is the media-is-still-in-S3 flag** (`encode.RemoteSidecar`, written by `_write_remote_sidecar` in `cli_batch.py`, read by `encode.ReadRemote`). Its **presence** is the state — written by a `--no-media` sync-back, deleted by a completed `cli_batch.py fetch` — so the two languages agree on a filename and nothing else. Field names in `encode.RemoteInfo` are a contract with the Python writer. A metadata-only output dir is indistinguishable from a complete one by every other signal: right name, right rung subdirs, manifests present, `parseOutputMeta` happy. Miss the sidecar and the UI offers Play, hls.js loads the playlist, and every segment 404s.
- **The sidecar has three states, not two** (#225). `expires_at` says when the lifecycle rule will remove the media; it does **not** say the media is still there. Everything else that removes objects — a staging clear, a console delete, the lifecycle firing early off each object's own creation time — leaves an output that looks available and fails on the click. So `gone: true` is set (never the file deleted — deleting it reclassifies the output as *complete*, the one wrong answer available) by two paths: `cmd_fetch` when the listing comes back **empty**, exiting `EXIT_STAGING_GONE` (4, mirrored as `encode.exitStagingGone`); and `Manager.MarkGoneUnderPrefix`, called from every cloud-clear handler with the prefixes out of `cleanup.py`'s own report. Ask `RemoteInfo.Fetchable()` rather than re-deriving from `Expired()`. **No S3 call belongs on the `/api/outputs` path** — it already costs ~0.8s over 30 dirs, and a HEAD per remote output every poll would be far worse than the problem.
- Media exclusion is stated as `_MEDIA_SUFFIXES` (`.m4s`, `.byteranges`) — an **exclusion, not an allow-list**, so a new metadata file the packager starts writing ships by default instead of being silently dropped. Measured on a real ladder: metadata is 3.99 MB of 2.64 GB (0.151%).
