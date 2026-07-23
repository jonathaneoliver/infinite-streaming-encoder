# Testing: pre-commit smoke matrix

There is no automated test suite. Before any **major** commit (anything touching
chunking, orchestration, packaging, the cloud path, or the farm scripts), run
the four fan-out topologies against one tiny asset with one cheap profile. Tests
1–3 are free (your own machines); only #4 costs money, and only a few cents.

`make smoke` automates test 1 end-to-end. The rest are manual (they need your
hardware / cost money).

## Test asset (small + light)

One ~20 s 720p clip with audio (~1–2 MB). 720p clears the encoder's ≥640×360
probe floor; the sine tone exercises the audio phase. `make smoke` generates
this for you; to make it by hand:

```bash
docker run --rm -v "$SOURCE_DIR:/src" --entrypoint ffmpeg encoder \
  -f lavfi -i testsrc2=size=1280x720:rate=30 \
  -f lavfi -i sine=frequency=440:sample_rate=48000 \
  -t 20 -pix_fmt yuv420p -c:v libx264 -c:a aac -shortest -y /src/smoke.mp4
```

## Cheap profile (use for every test)

- **Codec: H.264 only** — fastest, fewest variants.
- **Max-res: 720p** — caps the ladder to a couple of rungs.
- **Chunk: 12s** for the distributed tests — a 20 s clip → ~2 chunks, enough to
  spread across two boxes. (Cloud: prefer **whole variant** — see #4.)

Submit via the UI (target dropdown + these settings) or the API:

```bash
curl -X POST http://localhost:8080/api/encode -H 'Content-Type: application/json' \
  -d '{"files":["smoke.mp4"],"target":"local-dist","codec":"h264","max_res":"720p","chunk_duration":"12"}'
```

## The four tests

### 1 — Single device (master only) — `make smoke`
```bash
make smoke        # generates the asset, brings up a 1-box farm, encodes, asserts output
```
Or manually: `make farm-dev` with **no** `DIST_WORKERS`, then submit.
**Pass:** job reaches `done`; `OUTPUT_DIR/smoke_p200_h264*/` has `.m3u8` playlists;
plays in the UI; the chunk grid is **one colour** (one machine).

### 2 — Two devices, same arch (e.g. Mac + Mac Mini, both arm64)
```bash
# .env: MASTER_IP=<this box LAN IP>, DIST_WORKERS=mini=you@mini.local
make farm-dev
```
Submit the clip. **Pass:** the chunk grid shows **two colours** (work ran on both
boxes); completes; plays back. `deploy-worker.sh` transfers the master's image to
the same-arch box.

### 3 — Two devices, different arch (Mac arm64 + Ubuntu amd64)
```bash
# .env: DIST_WORKERS=ubuntu=you@ubuntu.local
make farm-dev                 # cross-arch: builds from GHCR base + bind-mounts code
make farm-dev DEV_BUILD=1     # if the commit changed requirements.txt / Dockerfile
```
Submit the clip. **Pass:** the **amd64 box actually runs chunks** (its colour
appears in the grid); completes; plays back. This is the test that catches
arch-handling regressions.

### 4 — Cloud (`cloud-batch`)
Infra must be up (`make infra-apply`). Submit with target **Cloud (AWS Batch)**,
H.264, 720p, and **whole variant** chunking (one Batch job per tier — cheapest
for a pipeline check; use small chunks only to test cloud fan-out).
**Pass:** the Step Functions execution runs → Batch spins a spot box → output
syncs back and promotes → plays in the UI.

**Cost & cautions:**
- The dominant cost is the ~60–90 s spot boot + ECR pull, not the encode — one
  tiny clip ≈ a few cents. A pre-baked AMI (`make bake-ami`) removes the pull.
- **After** the test: confirm the instance scales to zero (AWS tab); `make
  clear-costs` sweeps anything idle.
- **Do not `make deploy` while a cloud job is running** — it deregisters job-def
  revisions and fails the in-flight execution.
- A compute-env infra-apply pauses scale-down until it clears; run cloud tests
  when infra is stable.

## Resilience add-ons (recommended for major commits)

- **Restart resilience:** mid-encode, `make restart` (local) — the job should
  reattach/resume, not restart. For cloud, the server re-polls the SFN ARN.
- **Worker loss (distributed):** during test 2/3, kill a worker
  (`docker rm -f encode-worker` on one box, or toggle it off in the UI) — its
  chunks should reschedule onto the other box and the job still finishes.
- **Skip + force:** re-submit `smoke.mp4` → it should **skip** (already encoded);
  submit again with force-reencode → it re-runs.

## Teardown

```bash
make dist-down        # stop the Temporal + MinIO cluster (add ARGS=-v to wipe volumes)
make stop             # stop the server
# remote workers: docker rm -f encode-worker on each box
```
