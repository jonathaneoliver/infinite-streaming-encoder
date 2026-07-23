# Encoder

A Go HTTP server + single-page UI that drives adaptive-bitrate (ABR) video
encoding. The Go code is a thin **control plane**; the actual encoding —
ffmpeg, Shaka Packager, LL-HLS/DASH packaging — lives in a Python package and
runs in its own containers, either locally or fanned out across AWS Batch spot
instances.

One Docker image plays three roles: the **orchestrator** (Go server, the
default `CMD`), the **worker** (Python pipeline, via entrypoint override), and
the **UI** (`static/`, baked into the image and served by the server).

> Architecture diagram: see [`docs/DESIGN.md`](docs/DESIGN.md) for the full
> topology, or the rendered version linked from that doc.

## What it does

- Watches a source directory and auto-submits new files for encoding.
- Encodes each source into an ABR ladder (H.264 / HEVC / AV1) with burned-in
  overlays, packaged as LL-HLS (fMP4) + DASH.
- Runs encodes **detached and restart-resilient** — a worker survives a
  restart of the server itself.
- Runs the same pipeline **locally** (sibling Docker containers on your Mac)
  or in the **cloud** (AWS Batch, spot Graviton, chunked and resumable).
- Serves a single-page UI to browse sources, launch jobs, watch live progress
  over SSE, and play back the HLS output.

## Requirements

- Docker (with the daemon socket available at `/var/run/docker.sock`).
- For cloud encoding: an AWS account, an existing S3 bucket, and the Terraform
  stack under `infra/terraform` applied. See [`docs/DESIGN.md`](docs/DESIGN.md)
  and `infra/terraform/README.md`.

The image bakes in ffmpeg, Shaka Packager, the Docker CLI, and the AWS CLI —
you don't install those on the host.

## Quickstart (local)

```bash
cp .env.example .env          # then edit — see Configuration below
make build                    # docker build the single image
make run                      # run the server, mounting SOURCE/OUTPUT/TMP + docker.sock
make logs                     # tail server logs
```

Open the UI at `http://localhost:8080`. Drop a file into `SOURCE_DIR` (or use
the UI) and it encodes into `OUTPUT_DIR`.

Other Make targets: `make restart`, `make stop`, `make shell`, `make clean`.

### Go-only dev (outside Docker)

```bash
go build ./cmd/server
go run ./cmd/server           # reads env vars; flags override env
```

Note: the scripts need ffmpeg / Shaka Packager / AWS CLI, so real encodes want
the Docker workflow. The Go-only path is for iterating on the control plane.

## Configuration

`.env` at the repo root is auto-loaded by the Makefile and passed through to
the container. Key variables:

| Variable | Purpose |
| --- | --- |
| `SOURCE_DIR` / `OUTPUT_DIR` / `TMP_DIR` | Host paths for inputs, finished output, and scratch. |
| `AUTO_WATCH` | Enable the source-directory watcher. |
| `DEFAULT_TARGET` | `local` \| `cloud` \| `cloudbatch` — where jobs run by default. |
| `DEFAULT_CODEC` | `h264` \| `hevc` \| `av1` \| `both` \| `all`. |
| `DEFAULT_MAX_RES` | Cap the top ladder rung. |
| `MAX_CONCURRENT` | Local worker concurrency (semaphore size). |
| `S3_BUCKET`, `SUBNET_ID`, `SECURITY_GROUP_ID`, `INSTANCE_PROFILE`, `INSTANCE_TYPE` | Cloud encoding. |
| `GHCR_PAT` | Pull the image on legacy EC2 workers. |
| `STATE_MACHINE_ARN` | Enables the AWS Batch (Step Functions) path. |

The Makefile also exports **host-side** views of the mount paths
(`HOST_SOURCE_DIR`, `HOST_OUTPUT_DIR`, `HOST_TMP_DIR`, `HOST_AWS_DIR`) and
`ENCODER_IMAGE` into the server container. The server is itself containerised
but spawns *sibling* worker containers via the host daemon, so it needs the
host's view of those paths to bind-mount them correctly.

## How it runs work

The server routes each job to one of three lanes:

- **`local`** — a detached sibling container per file
  (`encoder_job_<id>_f<idx>`), streamed via `docker logs -f`. Idempotent:
  reattaches to an existing worker after a restart.
- **`cloudbatch`** *(current cloud path)* — a Step Functions execution fans the
  work out across AWS Batch spot jobs, chunked into 30 s resumable units.
- **`cloud`** *(deprecated)* — one spot EC2 box per job, superseded by Batch.

Output-directory naming is a contract shared across the codebase — see
[`docs/DESIGN.md`](docs/DESIGN.md#naming-contracts) before changing it.

## Repository layout

```
cmd/server/          Go entrypoint — wires config, manager, watcher, HTTP server
internal/encode/     Control plane: Job/Manager state machine, scheduling, targets
internal/api/        HTTP + SSE surface, static file servers
internal/watcher/    Source-directory auto-submit
internal/awswatch/   Cloud inventory watchdog + Batch keep-warm
scripts/encoder/     The Python encoder package (the real work)
  cli_local.py         full pipeline / phase dispatcher
  cli_batch.py         Step Functions submit/poll
  cli_cloud.py         legacy one-box EC2 (deprecated)
  cloud/               boto3 helpers (launch, poll, sync, batch admin)
static/index.html    the single-file SPA
infra/terraform/     AWS Batch + Step Functions + ECR + IAM
docs/                design docs (this system + narrower sub-designs)
```

## Documentation

- [`docs/PRD.md`](docs/PRD.md) — product requirements: the problem, goals, and scope.
- [`docs/DESIGN.md`](docs/DESIGN.md) — system design: architecture, components, key decisions.
- [`docs/chunked-encode-design.md`](docs/chunked-encode-design.md) — spot-resumable chunked encoding.
- [`docs/apple-ladder-design.md`](docs/apple-ladder-design.md) — per-codec ABR ladder model.
- [`CLAUDE.md`](CLAUDE.md) — orientation for working in this repo.

## Notes

- No test suite exists in this repo — there is no test command to run.
- Hardware encoding (VideoToolbox) was intentionally dropped: it only works on
  macOS hosts, not the Linux workers.
