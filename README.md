# infinite-streaming-encoder

A Go HTTP server + single-page UI that drives adaptive-bitrate (ABR) video
encoding. The Go code is a thin **control plane**; the actual encoding —
ffmpeg, Shaka Packager, LL-HLS/DASH packaging — lives in a Python package and
runs chunked, fanned out across workers.

One Docker image plays three roles: the **server** (Go control plane, default
`CMD`), the **worker** (Python pipeline, via entrypoint override), and the
**UI** (`static/`, baked in and served by the server).

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

## Notes

- No test suite exists in this repo — there is no test command.
- Direct pushes to `main` are blocked by a git hook; use a PR (`make setup-hooks`).
