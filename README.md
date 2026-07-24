# infinite-streaming-encoder

[![CI](https://img.shields.io/github/actions/workflow/status/jonathaneoliver/infinite-streaming-encoder/ci.yml?branch=main&label=ci)](https://github.com/jonathaneoliver/infinite-streaming-encoder/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/jonathaneoliver/infinite-streaming-encoder?label=release&color=blue)](https://github.com/jonathaneoliver/infinite-streaming-encoder/releases/latest)
[![License](https://img.shields.io/badge/license-InfiniteStream-lightgrey)](LICENSE)
[![GHCR](https://img.shields.io/badge/ghcr.io-infinite--streaming--encoder-2496ED?logo=docker&logoColor=white)](https://github.com/jonathaneoliver/infinite-streaming-encoder/pkgs/container/infinite-streaming-encoder)
[![Stars](https://img.shields.io/github/stars/jonathaneoliver/infinite-streaming-encoder?style=flat&color=yellow)](https://github.com/jonathaneoliver/infinite-streaming-encoder/stargazers)

<!-- A screenshot of the dashboard (chunk grid + live progress) sells this tool instantly.
     Drop one at docs/screenshots/dashboard.png and uncomment:
![Dashboard](docs/screenshots/dashboard.png)
-->

A Go HTTP server + single-page UI that drives adaptive-bitrate (ABR) video
encoding. The Go code is a thin **control plane**; the actual encoding —
ffmpeg, Shaka Packager, LL-HLS/DASH packaging — lives in a Python package and
runs chunked, fanned out across workers.

One Docker image plays three roles: the **server** (Go control plane, default
`CMD`), the **worker** (Python pipeline, via entrypoint override), and the
**UI** (`static/`, baked in and served by the server).

> **Not** a production encoding service or media origin. It's a hobbyist tool for
> generating test-ready ABR content and experimenting with encode ladders,
> packaging, and spot-resumable fan-out — tuned for reproducibility on a home lab,
> not for scale or an SLA. See [Project scope & roadmap](#project-scope--roadmap).

## Why you might want this

Producing a full ABR ladder (multiple codecs × resolutions, LL-HLS **and** DASH,
with burned-in QA overlays) for testing is fiddly, and doing it *fast* usually
means renting big cloud boxes. The common options each fall short:

- **Hand-rolled ffmpeg + packager scripts** get you one output at a time on one
  machine, with no resumability — a crash or a laptop lid halfway through means
  starting over — and no live progress.
- **Managed services** (AWS MediaConvert, Bitmovin, Mux) are turnkey but a black
  box: you don't control the exact ladder, GOP math, or overlays, you pay per
  minute, and there's no "run it all locally for free" mode.
- **Render-farm frameworks** (Nomad, Kubernetes Jobs) can fan out, but you're
  wiring up the encode pipeline, chunk planning, and packaging yourself.

**What makes this different:**

- **Chunked and spot-resumable.** Every variant is split into chunks and fanned
  out; a lost worker or a reclaimed spot instance costs *one chunk*, not the whole
  encode. Interrupted jobs reattach and resume, not restart.
- **Local *or* cloud from one image.** The same Docker image encodes across your
  LAN (Temporal + MinIO, zero AWS) or across AWS Batch spot Graviton — pick the
  target per job. `$0` at rest between cloud runs.
- **You own the ladder.** A 6-tier bitrate table × 3 codecs, exact
  fps-derived GOP math, per-codec Apple-style ladder, and per-frame burned-in
  overlays (timecode, codec/res/fps, watermark, padding markers) — all in a
  readable, stdlib-first Python package you can edit.
- **Live, per-chunk observability.** The UI streams each phase and chunk as it
  runs, coloured by the machine that encoded it, so a multi-box farm is visible at
  a glance.
- **Both delivery formats, one run.** LL-HLS (fMP4 + `EXT-X-PART`) and DASH come
  out of the same encode, so cross-protocol player testing is apples-to-apples.

Pairs naturally with a player-testing setup — the output is exactly the kind of
deterministic ABR content a tool like
[infinite-streaming](https://github.com/jonathaneoliver/infinite-streaming) serves.

## Two run modes

Every encode is split into chunks and fanned out. You choose where the workers run:

- **`local` — "Local (all machines)."** Chunks fan out across one or more
  machines on your LAN via **Temporal** (durable orchestration) + **MinIO**
  (shared chunk store). No AWS. A worker box only needs Docker and outbound
  reach to the master.
- **`cloud` — "Cloud (AWS Batch)."** Chunks fan out across **AWS Batch**
  spot Graviton instances, driven by Step Functions, staged in S3.

(The older single-box `local` and one-box EC2 `cloud` targets were retired.)

## What it does

- Watches a source directory and auto-submits new files.
- Encodes an ABR ladder (H.264 / HEVC / AV1) with burned-in overlays, packaged
  as LL-HLS (fMP4 + EXT-X-PART) and DASH.
- Chunks each variant so work is **parallel and resumable** — a lost worker or
  reclaimed spot instance costs one chunk, not the whole encode.
- Streams live per-phase, per-chunk progress to the UI, coloured by the machine
  that ran each chunk.
- Serves the output for in-browser HLS/DASH playback.

## How it works

The Go server is a **thin control plane** — it holds no encoding logic itself. It
shells out to `docker`, `ssh`, `aws`, and `python3`; there's no Docker, Temporal,
or AWS SDK compiled in. A job flows like this:

```
                 ┌────────────────────── one Docker image, three roles ──────────────────────┐
  browser  ─▶  Go server (control plane)  ─spawns▶  per-job orchestrator container (Python)
  UI / API      · watches SOURCE_DIR                    │
                · plans the ladder + chunks             ├─ local  ─▶ Temporal + MinIO ─▶ workers (LAN boxes)
                · streams live progress (SSE)           └─ cloud  ─▶ Step Functions ─▶ AWS Batch spot (S3)
                · promotes finished output ─▶ OUTPUT_DIR ─▶ /content (HLS/DASH playback)
```

- The server **watches** `SOURCE_DIR` and auto-submits new files (or you submit via
  the UI / API). It plans the ABR ladder and splits each variant into chunks.
- Each job runs in a **detached sibling container** (via the mounted
  `docker.sock`), so encodes survive a restart of the server's own container and
  reattach on startup.
- Chunks fan out either to **local** workers (Temporal orchestration + MinIO shared
  chunk store, all on your LAN) or to **AWS Batch** spot instances (Step Functions
  + S3). Same Python pipeline both ways.
- Finished output is promoted into `OUTPUT_DIR` and served at `/content` for
  in-browser HLS.js / DASH playback.

**Ports** (all host-published; the master box runs the cluster):

| Port | Service |
| --- | --- |
| `8080` | server + UI + JSON/SSE API |
| `7233` | Temporal (local farm orchestration) |
| `8233` | Temporal UI (watch the workflow / chunk DAG) |
| `9000` | MinIO (shared chunk store) |

For the full design — the output-dir naming contract, worker-container reattach,
codec-skip logic, cloud user-data — see [`CLAUDE.md`](CLAUDE.md) and [`docs/`](docs/).

## Performance: single machine vs local farm vs cloud

Speed comes from one lever — **how many chunks encode at once** — traded against
each target's **startup overhead**. The three modes sit at different points on that
curve:

| | **Single machine** | **Local farm** (multi-box) | **Cloud** (AWS Batch) |
| --- | --- | --- | --- |
| Parallel chunks | this box's slots (`physical-cores ÷ 2`, 2 threads each) | **sum** of every box's slots | scales out to your Batch max-vCPUs |
| Startup overhead | none | none (workers already up) | ~60–90 s spot boot + ECR pull per cold box (an [AMI](#images--registries) removes the pull) |
| Marginal cost | electricity | electricity | spot $/vCPU-hr while running; **$0 at rest** |
| Resumability | reattaches on restart | lost chunk reschedules to another box | reclaimed spot chunk retries |
| Best for | small / quick jobs on one machine | big jobs + idle LAN boxes, no cloud spend | huge jobs, or no local hardware — burst wide, then scale to zero |

**The model in one paragraph.** Each worker runs `physical-cores ÷ 2` chunks
concurrently (2 threads per chunk), so a 10-core box does ~5 at once. Adding boxes
adds their slots, so a local farm scales **roughly linearly — until you run out of
chunks**: a 20-second clip is only a chunk or two, so it can't fill a big farm and
won't speed up much (the coordination just adds latency). Big, long jobs are where
fan-out pays. Cloud trades a fixed cold-start tax (spot boot + image pull) for
near-unlimited width — worth it when encode time far exceeds that tax (large jobs),
not for a tiny clip where the boot dominates. AV1 is the slow codec on every target.

**Don't guess — the app measures it.** Every job card reports the actual
`local_wall_s` (wall time), `cpu_vcpu_h` (CPU-hours), and what the same encode
*would* cost on AWS spot / on-demand / MediaConvert / a commercial encoder. For a
cloud run, dig into where the time and CPU went:

```bash
make timing     EXEC=<execution-arn>   # per-phase where-did-the-time-go
make cpu-report EXEC=<execution-arn>   # per-tier CPU utilization vs reserved vCPU
```

## Requirements

- Docker (with the daemon socket at `/var/run/docker.sock`).
- For `local`: nothing else — `make farm-up` brings up the whole master profile
  (Temporal + MinIO + server + a worker) from one `docker-compose.yml`.
- For `cloud`: an AWS account, an existing S3 bucket, and the Terraform
  stack under `infra/terraform` applied (`make cloud-up`).

The image bakes in ffmpeg, Shaka Packager, the Docker CLI, and the AWS CLI.

## Quickstart

```bash
cp .env.example .env      # set SOURCE_DIR / OUTPUT_DIR / TMP_DIR (see below)
make farm-up                 # pull + bring up the whole master stack, UI at :8080
```

`make farm-up` is one `docker compose --profile master up` — Temporal + Temporal-UI +
Postgres + MinIO + the server + one local worker. Open `http://localhost:8080`,
drop a file in `SOURCE_DIR`, and submit a job.

`make farm-up` pulls the published image from GHCR. To build from your **working
tree** instead, use `make farm-dev-up` (below). `make run` / `make run-remote` bring
up **just the server** (against an already-running cluster), which is what
`make restart` / `make deploy` use to bounce it after an image change.

### From a clean checkout → running: three scenarios

Everything reduces to three, chosen by *where the workers run* and *whose code*:

| Goal | Commands |
| --- | --- |
| **Local farm, your working-tree code** (the dev loop) | `make farm-dev-up` |
| **Local farm, the published image** (multi-box, identical everywhere) | `make publish` → `make farm-up` |
| **Cloud (AWS Batch)** | `make cloud-up` → submit a `cloud` job → `make cloud-clear` |

- **`farm-dev-up`** builds from your working tree and needs no GHCR push; for any
  `DIST_WORKERS` boxes it rsyncs/builds your code to them too.
- **`publish` → `farm-up`** publishes one multi-arch image to GHCR, then every box
  (master + `DIST_WORKERS`) pulls that identical image.
- **`cloud-up`** provisions the AWS stack and pushes the image to ECR (it runs
  `ecr-publish` for you); **`cloud-clear`** zeroes idle cost between sessions.
  Nothing runs on AWS until you submit a `cloud`-target job.

The sections below break each scenario down; teardown is `make farm-down` (local)
and `make cloud-clear` / `make cloud-down` (cloud).

### A single-machine distributed encode (`local`)

`make farm-up` already gives you this on one box — cluster + server + one worker.
Prefer the pieces individually?

```bash
make dist-up              # just the cluster: Temporal + Temporal-UI + Postgres + MinIO
make dist-worker          # just this box's worker
make run                  # just the server + UI
```
Submit with target **Local (all machines)**; watch the Temporal UI at `:8233`.

### A multi-machine farm

`make farm-up` brings this box up as master (cluster + server + worker) in one
compose command, then deploys a worker to each `DIST_WORKERS` box over SSH (run
`make publish` first so GHCR has your code):

```bash
# .env: MASTER_IP=<this box's LAN IP>
#       DIST_WORKERS=box2=me@box2.local box3=me@box3.local
make farm-up
```

Each **extra box** is just the `worker` profile — no cluster, no source dirs, no
MinIO/Temporal locally; it only dials the master's LAN Temporal + MinIO:

```bash
MASTER_IP=<master LAN IP> \
TEMPORAL_ADDRESS=$MASTER_IP:7233 S3_ENDPOINT_URL=http://$MASTER_IP:9000 \
  docker compose --profile worker up -d
```

Tear the whole farm down with `make farm-down` — it stops the local master stack
**and** removes the `encode-worker` on each `DIST_WORKERS` box over SSH (the true
inverse of `make farm-up`; `make down`/`dist-down` only stop the local stack). Add
`ARGS=-v` to also wipe the local Temporal/MinIO volumes.

See [`docs/PRD.md`](docs/PRD.md) and
[`infra/local-cluster/README.md`](infra/local-cluster/README.md).

### Cloud encoding (`cloud`)

```bash
make cloud-up          # new account / after an image change: provision + push + verify
                       #   (USE_AMI=1 also bakes the warm-start AMI)
```
Then submit with target **Cloud (AWS Batch)**.

## Developing across the farm

`make farm-dev-up` runs the **whole farm from your working tree — nothing committed
or pushed**:

- Builds the local image from your working-tree Go + deps (`up --build`).
- The `docker-compose.dev.yml` overlay bind-mounts your working-tree
  `scripts/infinite_streaming_encoder` into the server + worker (and, via
  `HOST_SCRIPTS_DIR`, the orchestrator containers the server spawns), so
  uncommitted **Python** runs everywhere without a rebuild.
- Remote boxes go through the arch-aware `deploy-worker.sh` (transfer to
  same-arch boxes, native build on cross-arch ones).

```bash
make farm-dev-up                 # edit code → re-run to propagate
make farm-dev-up DEV_BUILD=1     # also native-build uncommitted deps on cross-arch boxes
```

The inner loop is a `compose up --build` (fast, layer-cached) plus rsync-diffs to
remote boxes.

## Configuration

`.env` at the repo root is auto-loaded by the Makefile. Only the host paths are
required; everything else has a working default. See
[`.env.example`](.env.example). Key groups:

- **Host paths (required):** `SOURCE_DIR`, `OUTPUT_DIR`, `TMP_DIR`.
- **Server:** `AUTO_WATCH`, `DEFAULT_TARGET` (`local` | `cloud`),
  `DEFAULT_CODEC`, `DEFAULT_MAX_RES`, `MAX_CONCURRENT`.
- **Distributed-local:** `MASTER_IP`, `DIST_WORKERS`, `ENCODE_SLOTS`, plus
  Temporal/MinIO vars (defaults match `make farm-up`; master vs. worker box is a
  `docker compose` profile choice).
- **Cloud:** `AWS_REGION`, `S3_BUCKET`, `STATE_MACHINE_ARN`, `WARM_MIN_VCPUS`.
- **Image/registry:** `GHCR_PAT`, `DOCKER_IMAGE`.

## Programmatic use (HTTP API)

The UI is a thin client over a plain JSON API on `:8080` — everything you can do in
the browser you can script. Submit an encode:

```bash
curl -X POST http://localhost:8080/api/encode \
  -H 'Content-Type: application/json' \
  -d '{"files":["myclip.mp4"],"target":"local","codec":"h264","max_res":"1080p","chunk_duration":"12"}'
```

| Field | Values |
| --- | --- |
| `files` | one or more names under `SOURCE_DIR` |
| `target` | `local` (LAN farm) · `cloud` (AWS Batch) |
| `codec` | `h264` · `hevc` · `av1` · `both` · `all` |
| `max_res` | cap the ladder, e.g. `1080p`; omit / `""` = all rungs |
| `chunk_duration` | seconds per chunk; omit / `""` = whole variant |
| `force_reencode` | `true` to re-encode even if output already exists |

Then watch it run and fetch the result:

```bash
curl localhost:8080/api/jobs                       # list jobs + status
curl -N localhost:8080/api/jobs/stream             # live updates (SSE): full job list, then deltas
curl localhost:8080/api/jobs/<id>/logs             # per-job log
curl localhost:8080/api/outputs                    # finished output dirs
curl localhost:8080/api/outputs/<name>/playlists   # HLS/DASH playlist URLs under /content
```

Selected endpoints:

| Method + path | Purpose |
| --- | --- |
| `GET /api/sources` | list files in `SOURCE_DIR` |
| `POST /api/encode` | submit a job (body above) |
| `GET /api/jobs` · `GET /api/jobs/stream` | poll / live-stream (SSE) job state |
| `POST /api/jobs/{id}/cancel` · `/retry` · `/redo` | control a job |
| `GET /api/outputs` · `/{name}/playlists` · `/{name}/logs` | browse finished output |
| `GET /api/dist/workers` · `POST /api/dist/workers/{machine}` | list / toggle farm workers |
| `GET /api/aws/inventory` · `POST /api/aws/clear` | cloud cost inventory / sweep |

Static file servers: `/content/` (HLS/DASH output for playback), `/sources/`
(direct source playback), `/logs/` (raw job logs).

## Images & registries

One `Dockerfile`, published/used four ways:

| Built by | Where | For |
| --- | --- | --- |
| `make build` | local daemon (`infinite-streaming-encoder`) | server + local/same-arch workers |
| `make publish` | GHCR (multi-arch) | cross-arch workers + version display + `make run-remote` |
| `make ecr-publish` | ECR (arm64) | AWS Batch workers |
| `make ami-up` | AWS AMI | pre-pull the ECR image onto spot boxes |

## Repository layout

```
docker-compose.yml   the unified farm: master / worker profiles (cluster + server + worker)
docker-compose.dev.yml         working-tree code-mount overlay (make farm-dev-up)
docker-compose.promote-*.yml   optional promote overlays (local dir / ssh remote)
cmd/server/          Go entrypoint
internal/encode/     control plane: Job/Manager, targets, scheduling, promote
internal/api/        HTTP + SSE, dist worker toggle, static file servers
internal/watcher/    source-directory auto-submit
internal/awswatch/   cloud inventory watchdog + Batch keep-warm
scripts/infinite_streaming_encoder/     the Python encoder package
  cli_local_dist.py    distributed-local orchestrator (Temporal backend)
  temporal_worker.py   the per-box worker (activities + workflow DAG)
  cli_batch.py         AWS Batch: Step Functions submit/poll
  cli_local.py         the phase entry point Batch job-defs invoke
  cloud/               boto3 helpers (Batch admin, inventory, sync, …)
infra/local-cluster/ worker + SSH deploy scripts (the cluster compose moved to ./docker-compose.yml)
infra/terraform/     AWS Batch + Step Functions + ECR + IAM
static/index.html    the single-file SPA
docs/                design docs
```

## Documentation

- [`docs/PRD.md`](docs/PRD.md) — product requirements: problem, goals, scope.
- [`infra/local-cluster/README.md`](infra/local-cluster/README.md) — the distributed-local cluster.
- [`docs/chunked-encode-design.md`](docs/chunked-encode-design.md) — spot-resumable chunked encoding.
- [`docs/apple-ladder-design.md`](docs/apple-ladder-design.md) — per-codec ABR ladder model.
- [`CLAUDE.md`](CLAUDE.md) — orientation for working in this repo.

## Known limitations

Deliberate non-goals and rough edges — worth knowing before you rely on it:

- **No hardware encoding.** VideoToolbox (`--force-hardware`) was dropped in the
  rewrite — it only worked on macOS hosts, not Linux workers. Everything is
  software (libx264 / libx265 / libaom-av1).
- **No automated test suite.** Correctness is a manual smoke matrix
  ([`docs/TESTING.md`](docs/TESTING.md)); the per-PR CI gate is `gofmt`/`vet`/build
  plus a cold-boot smoke encode.
- **Single control-plane server.** One server process per farm — not HA, no
  horizontal scaling of the control plane itself (the *workers* scale; the
  coordinator doesn't).
- **UI can't cold-provision workers.** The worker on/off toggle can only
  start/stop boxes already provisioned by `make farm-up` — it won't stand up a
  brand-new machine from the UI.
- **AV1 is slow.** `libaom-av1` software encoding is dramatically slower than
  H.264/HEVC; use it selectively.
- **LAN-trust security model.** The server mounts `docker.sock` (root-equivalent),
  `~/.aws`, and `~/.ssh`; run it only on a trusted network. See
  [`SECURITY.md`](SECURITY.md).

## Project scope & roadmap

This is a **side project**, built and run on a home lab (a Mac + a couple of spare
boxes). The UI isn't perf-tuned and the server isn't built for production scale —
it's tuned for reproducible encodes and experimentation, not throughput or uptime.

Rough direction, no dates and no commitments:

1. **Reproducible ABR output, local or cloud.** *Done.* One image, chunked
   spot-resumable fan-out across a LAN farm or AWS Batch, LL-HLS + DASH from the
   same run.
2. **Observability + cost control.** *Done.* Live per-chunk progress, per-job logs,
   cloud cost inventory + one-click sweep (`$0` at rest).
3. **Ladder ergonomics.** *In progress.* Editable per-codec ladders, VMAF-informed
   bitrate selection, saved presets.
4. **Deeper cloud parity + benchmarking.** *Future.* Systematic encode-time / cost
   / quality comparison across codecs, ladders, and local-vs-cloud targets.

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the dev loop, the
smoke-test matrix (there's no unit suite), and PR conventions. By participating you agree to
the [Code of Conduct](CODE_OF_CONDUCT.md). To report a vulnerability, see
[`SECURITY.md`](SECURITY.md).

## License

Licensed under the **InfiniteStream License** (attribution + internal use + no productization
without permission) — see [`LICENSE`](LICENSE). Redistributions must preserve attribution to
Jonathan Oliver (see [`NOTICE`](NOTICE)).

## Notes

- No test suite exists in this repo — there is no test command.
- Direct pushes to `main` are blocked by a git hook; use a PR (`make setup-hooks`).
