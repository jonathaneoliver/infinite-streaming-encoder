# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Go HTTP server + single-page UI that wraps ABR (adaptive bitrate) video encoding scripts. The Go code is a thin control plane — the actual encoding work lives in the bash/python scripts under `scripts/`, invoked as subprocesses. The server submits encode jobs, streams their stdout into a log buffer, and exposes a web UI for browsing sources, kicking off jobs, and playing back the resulting HLS output.

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
make run           # build + run, mounts SOURCE_DIR / OUTPUT_DIR / TMP_DIR + ~/.aws + docker.sock
make restart       # stop + run
make logs          # docker logs -f
make shell         # exec into container
make stop / clean
```

`.env` at the repo root is auto-loaded by the Makefile and passed through. Key vars: `SOURCE_DIR`, `OUTPUT_DIR`, `TMP_DIR`, `AUTO_WATCH`, `DEFAULT_TARGET` (cloud|local), `DEFAULT_CODEC` (h264|hevc|both|all), `DEFAULT_MAX_RES`, `MAX_CONCURRENT`, plus AWS vars (`S3_BUCKET`, `SUBNET_ID`, `SECURITY_GROUP_ID`, `INSTANCE_PROFILE`, `INSTANCE_TYPE`, `GHCR_PAT`) for cloud encoding.

## Architecture

Three Go packages plus a scripts directory. The interesting coordination is in `internal/encode`.

**`cmd/server/main.go`** — wires everything together. Creates a single `encode.Manager`, optionally starts a `watcher.Watcher` goroutine, hands the manager to `api.NewServer`, and serves `Server.Mux` on `LISTEN_ADDR`.

**`internal/encode/job.go`** — the core. `Manager` owns:
- A slice of `*Job` (in-memory only; lost on restart, but per-job logs persist under `$TMP_DIR/logs/<id>.log` and history appends to `$TMP_DIR/history.md`).
- A `chan struct{}` semaphore sized to `MAX_CONCURRENT` that gates `run()`.
- A list of SSE subscriber channels used by `/api/jobs/stream`; `notify()` does non-blocking sends (slow subscribers drop updates, never block the encode).

Job lifecycle (`Manager.run`):
1. Encode into `$TMP_DIR/<job_id>/` — partial output never appears in `OutputDir`.
2. On success, `moveTmpToOutput` renames each top-level subdir into `OutputDir`. If a dir already exists there: with `ForceReencode=true` it's moved to `OutputDir/.archive/<name>_<timestamp>`; otherwise it's deleted.
3. If rename fails (cross-device), falls back to `copyDir`.
4. Per-job log written (ANSI stripped) and history.md appended regardless of outcome.

Two key naming/skip conventions in this file:
- `JobConfig.OutputStem(filename)` — `<stem>_p<partialMs>[_padblack|_padpink]`. The encoding script then appends `_<codec>` to produce the final output dir name (e.g. `myclip_p200_h264`). The watcher and `parseOutputMeta` both rely on this layout.
- `Manager.resolveCodec` — before encoding, checks which `<stem>_h264` / `_hevc` / `_av1` dirs already exist in `OutputDir` and narrows the codec flag to only the missing ones. Returns `""` → skip this file entirely. Bypassed when `ForceReencode=true`.

`runLocal` invokes `scripts/create_abr_ladder.sh`; `runCloud` invokes `scripts/cloud_encode.sh`. Both scripts are passed identical CLI args built by `encodeArgsForFile`. Stdout is line-scanned into `job.logLines` (capped at 1000 lines, trimmed to last 500) and the latest line (ANSI-stripped) becomes `job.Progress`, which the SSE stream surfaces live.

**`internal/api/handlers.go`** — the HTTP surface. Routes defined in `NewServer`:
- JSON API: `GET /api/sources`, `GET/POST` under `/api/encode`, `/api/jobs`, `/api/jobs/{id}/logs`, `/api/outputs`, `/api/outputs/{name}`, `/api/outputs/{name}/playlists`, `/api/outputs/{name}/logs`.
- SSE: `GET /api/jobs/stream` — emits the full current job list immediately, then streams updates from `Manager.Subscribe()`.
- Static file servers: `/logs/` (from `TmpDir/logs`), `/content/` (from `OutputDir`, for HLS.js playback), `/sources/` (from `SourceDir`, direct playback), `/` (from `./static`, which maps to `/app/static` inside the container).
- `mediaFileServer` wraps `http.FileServer` to set the correct Content-Type for `.m3u8`, `.mpd`, `.m4s`, `.ts` — Go's MIME db doesn't know these and browsers/HLS.js won't play without them.

`parseOutputMeta` infers codec / resolutions / HLS format / partial duration / padding from directory naming and contents for the UI — it's the inverse of `OutputStem` + the script's codec suffix.

**`internal/watcher/watcher.go`** — polls `SourceDir` on `watch-interval` (default 30s). A file is auto-submitted when: (1) it's been seen before, (2) its size hasn't changed since last scan (ensures write is complete), (3) `alreadyEncoded` returns false. `alreadyEncoded` checks both in-memory jobs and `OutputDir` for any dir matching `<stem>_*` — so deleting an output dir triggers re-encode on next scan without needing a restart. First scan after startup only seeds `seen` and does not submit.

**`scripts/`** — the real encoder. Edits here affect encode behavior without touching Go code. `create_abr_ladder.sh` handles local ffmpeg + Shaka Packager encoding; `cloud_encode.sh` uploads to S3, launches a spot EC2 instance with user-data that runs the same encode inside a container from GHCR, then syncs results back.

**`static/index.html`** — a single self-contained HTML file (vanilla JS, no build step). Served directly by the Go file server. It polls `/api/jobs/stream` for live updates and plays HLS via hls.js pointed at `/content/<dir>/<playlist>.m3u8`.

## Things to know when editing

- Output dir naming is a contract shared by `OutputStem`, the encode script (appends `_<codec>`), `parseOutputMeta`, `resolveCodec`, and the watcher's `alreadyEncoded`. Changing the format means touching all of them.
- Jobs are in-memory. The log file under `$TMP_DIR/logs/` and `history.md` are the only persistent record across restarts — `outputLogs` in the API scans both.
- The container needs `docker.sock` mounted because `create_abr_ladder.sh` may shell out to `docker run` for the encode toolchain. The Go server itself doesn't talk to Docker.
- `move to OutputDir` only happens on success. A failed job leaves nothing in `OutputDir` (the `$TMP_DIR/<job_id>/` is unconditionally removed in `run`'s defer path).
