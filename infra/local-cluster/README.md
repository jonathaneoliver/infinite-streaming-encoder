# Distributed-local encoding (Temporal + MinIO)

Runs one encode across several machines on your LAN — chunked like the cloud
path, but with **no AWS**. Every piece is a container that comes back on
power-on; the only per-box requirement is Docker.

## Architecture

```
master box (e.g. the Mac) — docker-compose.yml, all restart: unless-stopped
  ├─ temporal        durable workflow server (owns the DAG; reschedules lost work)
  ├─ temporal-ui     dashboard at http://<master>:8233
  ├─ postgresql      Temporal's datastore (state survives restarts)
  └─ minio           shared blob store at :9000 (mezzanine, chunks, output)

each worker box (master included) — one container (run-worker.sh)
  └─ encode-worker   polls temporal:7233 (outbound only), runs cli_phase encodes
                     against MinIO. N concurrent = ENCODE_SLOTS (~cores/2).
```

**Why this shape:** workers *pull* work (only outbound connections), so no
inbound firewall/NAT setup and no Docker-Desktop networking pain. Temporal makes
the whole encode **durable** — kill a worker (or its whole box) mid-run and its
in-flight chunks are rescheduled onto another worker; the workflow finishes.
The message queue only carries pointers (S3 URIs + params); MinIO carries the
video. See `../../scripts/encoder/temporal_worker.py` (activities + the
`EncodeWorkflow` DAG) and `cli_local_dist.py --backend temporal` (the trigger).

## Bring it up

On the **master box** (sets the LAN IP others dial — update it in the compose
`ports`/worker env if not 192.168.0.110):

```
make dist-up        # postgres + temporal + temporal-ui + minio
make dist-worker    # start a worker on the master too (uses its cores)
```

On **each other box** (needs Docker + a current encoder image, or a synced
`scripts/encoder` checkout + `CODE_MOUNT`):

```
cp infra/local-cluster/worker.env.example worker.env   # edit master IP + slots
infra/local-cluster/run-worker.sh worker.env
```

## Run an encode

From anywhere that can reach the master (the Go control plane will do this):

```
python3 -m encoder.cli_local_dist --backend temporal \
  --temporal-address <master>:7233 \
  --input clip.mkv --output clip --output-dir ./out \
  --codec hevc --max-res 1080p --chunk-duration 12 \
  --s3-bucket encoder-local --job-prefix jobs/clip
```

Watch it in the Temporal UI at `http://<master>:8233` (workflows, per-activity
retries, which worker ran each chunk).

## Notes

- First `make dist-up` runs Temporal `auto-setup` (schema + `default`
  namespace) — give it ~30s. MinIO starts empty; the encoder creates the
  `encoder-local` bucket keys as it uploads.
- `make dist-down` stops the stack (volumes persist: Temporal history + MinIO
  blobs). Add `-v` to wipe.
- Slot sizing: x265 (HEVC) only ~half-fills a box per encode, so ~cores/2
  concurrent chunks saturate it. H264 scales further; tune `ENCODE_SLOTS`.
