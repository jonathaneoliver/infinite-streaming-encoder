# Distributed-local encoding (Temporal + MinIO)

Runs one encode across several machines on your LAN — chunked like the cloud
path, but with **no AWS**. Every piece is a container that comes back on
power-on; the only per-box requirement is Docker.

> **Bring-up moved.** The whole farm now comes up from the **unified
> `docker-compose.yml` at the repo root** via two profiles (`master` /
> `worker`) — see "Bring it up" below. The old split (a cluster compose here +
> `run-worker.sh` + the Makefile's docker-run block) has been folded into it.
> `run-worker.sh` survives only for the SSH remote-deploy scripts in this dir.

## Architecture

```
master box (e.g. the Mac) — docker compose --profile master, all restart: unless-stopped
  ├─ temporal        durable workflow server (owns the DAG; reschedules lost work)
  ├─ temporal-ui     dashboard at http://<master>:8233
  ├─ postgresql      Temporal's datastore (state survives restarts)
  ├─ minio           shared blob store at :9000 (mezzanine, chunks, output) — MASTER ONLY
  ├─ server          encoder control plane + UI at http://<master>:8080
  └─ encode-worker   the master's own worker (contributes its cores)

each EXTRA worker box — docker compose --profile worker (worker only, no cluster)
  └─ encode-worker   polls <master>:7233 (outbound only), runs cli_phase encodes
                     against <master>:9000 MinIO. N concurrent = ENCODE_SLOTS (~cores/2).
```

**Why this shape:** workers *pull* work (only outbound connections), so no
inbound firewall/NAT setup and no Docker-Desktop networking pain. Temporal makes
the whole encode **durable** — kill a worker (or its whole box) mid-run and its
in-flight chunks are rescheduled onto another worker; the workflow finishes.
The message queue only carries pointers (S3 URIs + params); MinIO carries the
video. See `../../scripts/infinite_streaming_encoder/temporal_worker.py` (activities + the
`EncodeWorkflow` DAG) and `cli_local_dist.py --backend temporal` (the trigger).

## Bring it up

On the **master box** — one command brings up cluster + server + a local worker
(cloud stays configured-but-idle):

```
make farm-up           # GHCR image (run `make push` first); or
make farm-dev-up       # build from your working tree, live-mount the Python code; or
make farm-test-up      # publish the working tree under a throwaway dev-<branch>-<sha>
                       # tag and run the farm on that published image (TAG= to override)
```

Prefer raw compose? `docker compose --profile master up -d` (set `SOURCE_DIR` /
`OUTPUT_DIR` / `TMP_DIR` in `.env` first). Cluster-only or worker-only pieces:
`make dist-up` (cluster) / `make dist-worker` (this box's worker).

On **each EXTRA box** — Docker + the published image, nothing else. Point it at
the master's LAN IP and start only the worker profile:

```
TEMPORAL_ADDRESS=<master-ip>:7233 \
S3_ENDPOINT_URL=http://<master-ip>:9000 \
MINIO_ACCESS_KEY=encoder MINIO_SECRET_KEY=encoder-secret \
ENCODER_IMAGE=ghcr.io/jonathaneoliver/infinite-streaming-encoder:latest \
docker compose --profile worker up -d
```

Or let the master push it over SSH: set `DIST_WORKERS=label=ssh_target` in `.env`
and `make farm-up` deploys each box (still via `run-worker.sh`).

## Run an encode

From anywhere that can reach the master (the Go control plane will do this):

```
python3 -m infinite_streaming_encoder.cli_local_dist --backend temporal \
  --temporal-address <master>:7233 \
  --input clip.mkv --output clip --output-dir ./out \
  --codec hevc --max-res 1080p --chunk-duration 12 \
  --s3-bucket encoder-local --job-prefix jobs/clip
```

Watch it in the Temporal UI at `http://<master>:8233` (workflows, per-activity
retries, which worker ran each chunk).

## Notes

- First bring-up runs Temporal `auto-setup` (schema + `default` namespace) —
  the server waits on Postgres via a compose healthcheck, then Temporal comes
  up (~30s). MinIO starts empty; the encoder creates the `encoder-local` bucket
  keys as it uploads.
- `make dist-down` (alias for `docker compose --profile master down`) stops the
  whole stack (volumes persist: Temporal history + MinIO blobs). Add `ARGS=-v`
  to wipe.
- Slot sizing: x265 (HEVC) only ~half-fills a box per encode, so ~cores/2
  concurrent chunks saturate it. H264 scales further; tune `ENCODE_SLOTS`.
