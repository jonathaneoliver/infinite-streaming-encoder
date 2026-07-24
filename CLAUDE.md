# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Go HTTP server + single-page UI that drives video encoding. The Go code is a thin control plane — the actual encoding work lives in the Python package under `scripts/infinite_streaming_encoder/`, each run in its own **detached sibling Docker container** (so encodes survive a restart of the server's own container). The server submits encode jobs, tails their stdout via `docker logs -f` into a log buffer, and exposes a web UI for browsing sources, kicking off jobs, and playing back the resulting HLS output.

No tests exist in this repo — do not invent a test command.

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
make farm          # master box: GHCR image -> cluster + server + one local worker (+ cloud configured)
make farm-dev      # same, but build from your working tree + live-mount scripts/infinite_streaming_encoder
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

## Things to know when editing

- Output dir naming is a contract shared by `OutputStem`, the encode script (appends `_<codec>`), `parseOutputMeta`, `resolveCodec`, and the watcher's `alreadyEncoded`. Changing the format means touching all of them.
- The worker container name format (`encoder_job_<id>_f<idx>`) is a contract between `runFileContainer` and `Reconcile` — changing it means losing the ability to reattach to workers started by older server versions.
- The docker.sock mount is what lets the Go server spawn and talk to worker containers. Without it, `docker run` / `docker logs -f` / `docker inspect` all fail and no encoding happens.
- Host paths: the server needs both container-side paths (`SOURCE_DIR`, `OUTPUT_DIR`, `TMP_DIR` — used for all in-process file I/O) and host-side paths (`HOST_*` — used only for `-v` flags when launching workers). Workers mount host paths at the same paths the Go server uses, so script args don't need translation.
- `move to OutputDir` only happens on success. A failed job leaves nothing in `OutputDir` (the `$TMP_DIR/<job_id>/` is unconditionally removed in `run`'s defer path).
