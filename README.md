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
- For `local`: nothing else — `make dist-up` brings up Temporal + MinIO in
  containers on the master.
- For `cloud`: an AWS account, an existing S3 bucket, and the Terraform
  stack under `infra/terraform` applied (`make infra-setup`).

The image bakes in ffmpeg, Shaka Packager, the Docker CLI, and the AWS CLI.

## Quickstart

```bash
cp .env.example .env      # set SOURCE_DIR / OUTPUT_DIR / TMP_DIR (see below)
make run                  # build the image, start the server + UI at :8080
```

Open `http://localhost:8080`, drop a file in `SOURCE_DIR`, and submit a job.

To run the server **without building** (pull the published image from GHCR):

```bash
make run-remote
```

### A single-machine distributed encode (`local`)

```bash
make dist-up              # Temporal + Temporal-UI + Postgres + MinIO (containers)
make dist-worker          # a worker on this box
make run                  # server + UI
```
Submit with target **Local (all machines)**; watch the Temporal UI at `:8233`.

### A multi-machine farm

One command brings the whole farm up from this machine as master, pulling every
image from GHCR (run `make push` first so GHCR has your code):

```bash
# .env: MASTER_IP=<this box's LAN IP>
#       DIST_WORKERS=box2=me@box2.local box3=me@box3.local
make farm
```

`make farm` = `dist-up` → a worker here → a worker on each `DIST_WORKERS` box →
server/UI. See [`docs/PRD.md`](docs/PRD.md) and
[`infra/local-cluster/README.md`](infra/local-cluster/README.md).

### Cloud encoding (`cloud`)

```bash
make infra-setup          # one-time: tofu apply + ecr-push + bake-ami + wire
```
Then submit with target **Cloud (AWS Batch)**.

## Developing across the farm

`make farm-dev` runs the **whole farm from your working tree — nothing committed
or pushed**:

- `make build` compiles your working-tree Go + deps into the local image (the
  server runs it).
- Your working-tree `scripts/encoder` is bind-mounted into every worker and the
  orchestrator, so uncommitted **Python** runs everywhere, any arch.
- Remote boxes go through the arch-aware `deploy-worker.sh` (transfer to
  same-arch boxes, native build on cross-arch ones).

```bash
make farm-dev                 # edit code → re-run to propagate
make farm-dev DEV_BUILD=1     # also native-build uncommitted deps on cross-arch boxes
```

The inner loop is rsync-diffs + restart; only the Go server rebuilds (fast, cached).

## Configuration

`.env` at the repo root is auto-loaded by the Makefile. Only the host paths are
required; everything else has a working default. See
[`.env.example`](.env.example). Key groups:

- **Host paths (required):** `SOURCE_DIR`, `OUTPUT_DIR`, `TMP_DIR`.
- **Server:** `AUTO_WATCH`, `DEFAULT_TARGET` (`local` | `cloud`),
  `DEFAULT_CODEC`, `DEFAULT_MAX_RES`, `MAX_CONCURRENT`.
- **Distributed-local:** `MASTER_IP`, `DIST_WORKERS`, plus Temporal/MinIO vars
  (defaults match `make dist-up`).
- **Cloud:** `AWS_REGION`, `S3_BUCKET`, `STATE_MACHINE_ARN`, `WARM_MIN_VCPUS`.
- **Image/registry:** `GHCR_PAT`, `DOCKER_IMAGE`.

## Images & registries

One `Dockerfile`, published/used four ways:

| Built by | Where | For |
| --- | --- | --- |
| `make build` | local daemon (`encoder`) | server + local/same-arch workers |
| `make push` | GHCR (multi-arch) | cross-arch workers + version display + `make run-remote` |
| `make ecr-push` | ECR (arm64) | AWS Batch workers |
| `make bake-ami` | AWS AMI | pre-pull the ECR image onto spot boxes |

## Repository layout

```
cmd/server/          Go entrypoint
internal/encode/     control plane: Job/Manager, targets, scheduling, promote
internal/api/        HTTP + SSE, dist worker toggle, static file servers
internal/watcher/    source-directory auto-submit
internal/awswatch/   cloud inventory watchdog + Batch keep-warm
scripts/encoder/     the Python encoder package
  cli_local_dist.py    distributed-local orchestrator (Temporal backend)
  temporal_worker.py   the per-box worker (activities + workflow DAG)
  cli_batch.py         AWS Batch: Step Functions submit/poll
  cli_local.py         the phase entry point Batch job-defs invoke
  cloud/               boto3 helpers (Batch admin, inventory, sync, …)
infra/local-cluster/ Temporal + MinIO compose, worker + deploy scripts
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
