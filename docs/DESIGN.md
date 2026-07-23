# Design: Encoder (system architecture)

Status: living document · Whole-system view · Complements the narrower
sub-designs ([chunked-encode](chunked-encode-design.md),
[apple-ladder](apple-ladder-design.md))

> A rendered version of the diagram below was produced as a Claude artifact;
> this doc is the source of truth for the topology in text.

## 1. Shape of the system

Two planes and one artifact:

- **Control plane** — a Go HTTP server that owns job state, scheduling, and the
  UI. Runs (containerised) on the operator's machine. Never does encoding work
  itself.
- **Execution plane** — the Python encoder, run as containers: local sibling
  containers, or AWS Batch jobs. Does all the ffmpeg / packaging work.
- **One image** — a single `Dockerfile` produces one image that is the server
  (default `CMD`), the worker (entrypoint override to `cli_local.py`), and the
  UI host (`static/` baked in). Built once; pushed to GHCR and ECR.

```
                        ┌──────────────────────────────────────────┐
   SOURCE_DIR  ──watch──▶            Go control plane (Mac)          │
                        │  Manager/Job · scheduler · SSE · awswatch  │
                        └───────┬───────────┬──────────────┬────────┘
                                │ local      │ cloudbatch   │ cloud (legacy)
                                ▼            ▼              ▼
                     sibling containers   Step Functions   one spot EC2/job
                     (docker.sock)        → AWS Batch       (deprecated)
                                          → chunk fan-out
                                                │
                                          S3 staging ──promote──▶ OUTPUT_DIR
```

## 2. Components

### 2.1 Control plane (Go)

- **`cmd/server/main.go`** — wires config (env → flags override), constructs the
  `encode.Manager`, optionally starts the `watcher` and `awswatch` loops, and
  serves `Server.Mux` on `LISTEN_ADDR` (`:8080`).
- **`internal/encode`** — the core. `Manager` owns the authoritative slice of
  `*Job`, a semaphore gating local concurrency, and the SSE subscriber list.
  Job lifecycle, the three targets, scheduling, and output placement all live
  here. Key files: `job.go` (state machine, `buildRunArgs`, `buildSFNInput`),
  `ladder.go` / `ladder_store.go` (the ABR ladder), `promote.go` (staged →
  live rsync), `speed.go` (learned per-variant encode speed → priority).
- **`internal/api/handlers.go`** — REST + SSE surface. `/api/jobs/stream` emits
  the full job list then streams updates. Static servers for `/content/`
  (output, for HLS.js), `/sources/`, `/logs/`, `/` (the SPA). `mediaFileServer`
  fixes MIME types Go doesn't know (`.m3u8`, `.mpd`, `.m4s`, `.ts`).
- **`internal/watcher`** — polls `SourceDir`; auto-submits stable, not-yet-
  encoded files. First scan only seeds state.
- **`internal/awswatch`** — polls cloud inventory for leaks (auto-terminate)
  and drives Batch keep-warm (sets min-vCPUs during an active run).
- **`internal/imageinfo`** — reads OCI labels from GHCR to show the cloud
  image's version/commit in the UI.

### 2.2 Execution plane (Python — `scripts/encoder`)

Entry points:

| CLI | Role | Invoked by |
| --- | --- | --- |
| `cli_local.py` | full pipeline for one input; also the `phase` sub-command dispatcher | server (local worker entrypoint); Batch job defs (`... phase <name>`) |
| `cli_batch.py` | `submit` starts a Step Functions execution; `poll` blocks on its history | server's `cloudbatch` target |
| `cli_cloud.py` | **deprecated** one-box spot EC2 per job | server's `cloud` target |
| `cli_phase.py` | per-phase worker logic used by Batch | imported by the phase dispatch |

Pipeline modules (stdlib-only except `cloud/*` which uses boto3): `ffprobe`,
`mezzanine`, `audio`, `encode_variants`, `packager`, `hls`, `ladder`, `gop`,
`padding`, `burnin`, `vmaf`, `manifests`, `fragments`, `resume`, `chunking`,
and the `cloud/` subpackage (`aws`, `launch`, `userdata`, `poll`, `sync`,
`compute_env`, `batch_admin`, `inventory`, `arch`).

## 3. Execution lanes

### 3.1 Local (`TargetLocal`)

`buildRunArgs` issues `docker run -dt --name encoder_job_<id>_f<idx>
--entrypoint <cli_local.py>` against the **host** daemon (via the mounted
`docker.sock`), bind-mounting the **host-side** source/output/tmp paths. These
are *sibling* containers, not children — which is what makes them outlive a
restart of the server container.

`runFileContainer` is idempotent: if the named container already exists it
skips `docker run` and reattaches via `docker logs -f`. On a still-running
worker that streams live; on an exited one it drains history then returns; in
both cases `containerExitCode` reads the final status before the worker is
`docker rm`'d. Stdout is line-scanned into a capped log buffer, the latest line
becomes `job.Progress`, and `ENCODER-STAGE` / `ENCODER-HOST` markers drive the
UI.

### 3.2 Cloud — AWS Batch + Step Functions (`TargetCloudBatch`, current)

`cli_batch.py submit` starts a **Step Functions** execution and prints its ARN;
the Go manager persists the ARN so it can re-poll after a restart (submit and
poll are split for exactly this reason). The state machine:

```
MezzCheck → Mezzanine
          → FanOut( Audio  ‖  Variants Map[maxConcurrency 40] )
          → PerCodec( h264 ‖ hevc ‖ av1 : PackageAll )
          → Success
```

The Variants Map fans out over `(codec, tier)`, and **each variant fans out
again over its chunk indices** (nested Map) — this is the dynamic chunking that
names the branch. Whole-variant runs use `chunk_index = -1` and skip the inner
fan-out. Chunks are joined **inline** by the package-all phase (`concat_chunks`)
— there is no separate concat job.

Compute is AWS Batch: a managed **SPOT** Graviton compute env
(`SPOT_CAPACITY_OPTIMIZED`, c8g/c7g, min/desired 0), a fair-share job queue,
and **7 job definitions** (`mezzanine`, `variant`, `audio`, `package`,
`package_all`, `hls`, `byteranges`), each invoking
`python3 -m encoder.cli_local phase <name> --s3-… URI`. Retries cover spot
reclaim. See `infra/terraform`.

### 3.3 Cloud — legacy one-box EC2 (`TargetCloud`, deprecated)

`cli_cloud.py` runs inside a worker with `~/.aws` mounted; it `run_instances`
one spot EC2 box per job (AZ/type fallback on insufficient capacity), the box
pulls the GHCR image and loops every clip, and the server polls S3
`_DONE`/`_FAILED` markers, then syncs output back. Superseded by the Batch path.

## 4. Data flow & shared state

- **Local scratch** — encodes land in `$TMP_DIR/<job_id>/`; only on success is
  each top-level subdir moved into `OUTPUT_DIR` (rename, `copyDir` fallback
  cross-device). A pre-existing dir is archived (force) or deleted.
- **S3 (cloud)** — `s3://<bucket>/jobs/<id>/` holds chunks, the source-keyed
  mezzanine cache, and staged output; lifecycle-expired. The bucket is a
  **prerequisite**, not created by Terraform. `promote.go` rsyncs staged output
  to the live location.
- **Job state** — `$TMP_DIR/jobs/<id>.json` (`{id, config, started_at,
  current_file_idx}`, plus the SFN ARN for cloud) is persisted before each file
  and reconciled by `Manager.Reconcile` on startup. Removed only on a terminal
  outcome.

## 5. Scheduling model

Real parallelism is capped by compute-env max vCPUs; everything else sits
RUNNABLE, ordered by priority. Three mechanisms keep that order fair:

- **Priority bands** (`jobPriorityBase`) — each job gets a band so an earlier
  job outranks a later one. Within-job variant priority (0–999, from learned
  encode speed via `speed.go`) rides inside the band, passed to Batch as
  `SchedulingPriorityOverride` with fair-share `ShareIdentifier=encode`.
- **Launch gate** — a size-1 channel serialises launch→fan-out so a job floods
  the queue with *all* its jobs (atomic fan-out) before the next execution
  starts; otherwise a later job that fanned out first would hold vCPUs
  regardless of priority.
- **Go/Python parity** — Go computes the chunk count (`chunkCountForDuration`)
  the same way `chunking.plan_chunks` does, so the two never disagree.

## 6. Encoding pipeline (inside a worker)

1. **probe** — ffprobe; require a video stream ≥ 640×360.
2. **mezzanine** — stream-copy fragmented MP4 (shared input; S3-cached on cloud).
3. **ladder select** — pick tiers by source width + `--max-res`; plan
   segment-boundary padding (LCM of segment/partial/gop).
4. **variants** — encode each `(codec, tier)`: closed GOPs, drawtext burn-ins.
5. **audio** — extract / transcode if present.
6. **package** — Shaka Packager per codec → DASH + fMP4 SegmentList.
7. **byteranges** — fMP4 fragment byterange sidecars for EXT-X-PART.
8. **hls** — LL-HLS fMP4 playlists (pure Python); optional TS HLS.

On Batch, phases 6–8 collapse into one **package-all** job per codec.

## 7. Naming contracts

Output-directory naming is a contract shared by several places — change one,
change all:

- `JobConfig.OutputStem(filename)` → `<stem>_p<partialMs>[_padblack|_padpink]`;
  the encode script appends `_<codec>` (e.g. `myclip_p200_h264`).
- `parseOutputMeta` — the inverse; infers codec / resolutions / HLS format /
  partial / padding for the UI.
- `Manager.resolveCodec` — narrows the codec flag to only missing outputs
  (`""` → skip the file). Bypassed by `ForceReencode`.
- `watcher.alreadyEncoded` — matches any `<stem>_*` dir.

The worker container name (`encoder_job_<id>_f<idx>`) is a contract between
`runFileContainer` and `Reconcile`; changing it drops the ability to reattach
to workers started by older server versions.

## 8. Images & registries

One `Dockerfile` (multi-stage: Go builder → Python runtime with ffmpeg, Shaka
Packager, Docker CLI, AWS CLI). Pushed to:

- **GHCR** (`ghcr.io/jonathaneoliver/encoder`) — pulled by legacy EC2 workers;
  read for version display in the UI. The server's `DOCKER_IMAGE` default.
- **ECR** (`encoder-worker:<short-sha>`, arm64/Graviton) — pulled by AWS Batch
  via VPC endpoints (ECS prefer-cached).

The local server and its sibling workers use the locally-built `ENCODER_IMAGE`
(`encoder:latest`) with no registry pull.

## 9. Key decisions & rationale

- **Sibling, not child, worker containers** — so encodes survive a restart of
  the server's own container; the server reattaches via `docker logs -f`.
- **Host-side mount paths given to the server** — a `-v` from inside a container
  is resolved by the host daemon against the host filesystem, so workers must
  mount host paths at the same in-container paths the server uses; script args
  then stay identical in both contexts.
- **30 s encode chunk vs. 6 s delivery segment** — decoupled granularities: the
  chunk is the parallel + resumable unit; the segment is what the player
  fetches. A spot reclaim costs one chunk. See
  [chunked-encode-design.md](chunked-encode-design.md).
- **One image, three roles** — local and cloud share the exact same pipeline,
  so behaviour matches and there's a single thing to reason about.
- **Split submit/poll for the cloud path** — lets the Go manager persist the SFN
  ARN and resume polling after a restart.
- **Batch supersedes one-box EC2** — chunked fan-out gives cheaper, more
  parallel, more resumable encoding than looping every clip on a single box.
