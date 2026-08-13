# PRD: Encoder

Status: living document · Reflects `main` (distributed-local + AWS Batch; the
legacy single-box `local` and one-box EC2 `cloud` paths are retired)

> This document says what the product is **for** and what it must achieve.
> For what the system observably **does** — the rules, states and trades, at a
> level you can hold the code to — see the behavioural spec set in
> [`spec/`](spec/README.md). Where the two disagree, the spec is derived from
> the implementation and this is derived from intent; that difference is
> usually the interesting part.

## 1. Summary

Encoder is a self-hosted control plane for producing adaptive-bitrate (ABR)
streaming assets from source video. A single operator drops a master file in
and gets back player-ready LL-HLS + DASH output. Every encode is **chunked and
fanned out** — across the operator's own LAN machines (`local`, via
Temporal + MinIO, no AWS) or across cheap AWS Batch spot capacity
(`cloud`). Neither a server restart nor a lost worker/spot reclaim loses
meaningful work, and the operator can build and test changes across the whole
fleet without committing or pushing anything.

## 2. Problem

Producing a full ABR ladder for a library of source videos is:

- **Slow** — a 4K encode across three codecs and six rungs is hours of CPU;
  doing it serially, one machine at a time, does not scale.
- **Wasteful of idle hardware** — an operator often has several machines on a
  LAN (a Mac, a Linux box) sitting idle that could share the load, but wiring
  them together (networking, shared storage, failover) is fiddly.
- **Expensive if naive in the cloud** — on-demand compute for hours of encoding
  is costly; spot is cheap but interruptible.
- **Fragile to interruption** — a whole-clip encode that dies near the end
  loses everything.
- **Painful to iterate on** — testing an encoder change normally means a
  rebuild/redeploy cycle, made worse across a mix of CPU architectures.

## 3. Goals

1. **One-drop operation.** Put a file in the source directory; get correct ABR
   output out, no per-file manual steps.
2. **Use the machines you have.** Fan one encode across several LAN machines
   with no AWS — one command to stand the farm up.
3. **Cheap cloud when you want it.** Exploit spot Graviton capacity without
   paying the interruption tax.
4. **Resumable everywhere.** A lost worker or reclaimed spot instance costs one
   ~30 s chunk; a server restart loses nothing.
5. **Same pipeline everywhere.** The identical Python encoder runs locally and
   in the cloud, so behaviour matches.
6. **Fast, commit-free dev loop.** Build and test *all* changes — Go, Python,
   deps — across every machine straight from the working tree, no commit/push.
7. **Observable.** Live per-phase, per-chunk progress attributed to the machine
   that ran each unit.

## 4. Non-goals

- **A multi-tenant SaaS.** Single-operator control plane, not a hosted product.
- **Live / real-time transcoding.** Files, not live streams.
- **A test/CI suite.** Not in scope for this repo today.
- **Managing the S3 bucket from zero.** The bucket is a prerequisite for the
  cloud path; Terraform manages what runs on it, not its existence.
- **Auto-discovery of worker machines.** The worker set is static config
  (`DIST_WORKERS`); workers otherwise self-register by polling Temporal.

## 5. Users

- **Primary: the operator.** Runs the server on their Mac (master), optionally
  adds LAN worker boxes, decides local vs. cloud, watches jobs, plays back output.
- **Implicit: downstream players.** The output must satisfy HLS.js / native
  HLS / DASH players.

## 6. Requirements

### 6.1 Functional
- **F1 — Ingest.** Watch a source dir; auto-submit a stable, not-yet-encoded
  file. Manual submit + drag-drop upload also supported.
- **F2 — Encode ladder.** Per selected codec (H.264 / HEVC / AV1) and tier,
  closed GOPs, burned-in overlays.
- **F3 — Package.** LL-HLS (fMP4 + EXT-X-PART) and DASH per codec; optional TS-HLS.
- **F4 — Targets.** Route a job to `local` or `cloud`.
- **F5 — Skip / narrow.** Encode only the missing codec outputs; skip a
  complete file. Bypassed by force-reencode.
- **F6 — Live progress.** Stream per-job, per-phase, per-chunk status over SSE,
  attributed to the executing machine.
- **F7 — Playback.** Serve output for in-browser HLS/DASH.

### 6.2 Distributed-local (`local`)
- **D1 — LAN fan-out.** Chunks of one encode run across multiple machines via a
  durable Temporal workflow; MinIO is the shared chunk/blob store.
- **D2 — Pull-based workers.** Each worker box makes outbound-only connections
  (no inbound firewall/NAT setup) and contributes its cores.
- **D3 — One-command farm.** `make farm-up` brings up cluster + this box's worker +
  every `DIST_WORKERS` box + server/UI. Machines can be toggled mid-run.
- **D4 — Mixed architectures.** A farm of arm + x86 boxes works: code is
  bind-mounted (arch-independent), images are transferred (same-arch) or built
  natively (cross-arch).

### 6.3 Reliability
- **R1 — Server-restart resilience.** A running encode survives a restart of
  the server; state is persisted and reconciled.
- **R2 — Worker / spot resilience.** A lost `local` worker's chunks are
  rescheduled by Temporal; a reclaimed Batch chunk is retried. At most one
  ~30 s chunk of work is lost.
- **R3 — Crash-consistent output.** Partial output never appears in
  `OUTPUT_DIR`; only a successful job's result is moved/promoted into place.

### 6.4 Cost & performance
- **C1 — Spot-first cloud** (SPOT_CAPACITY_OPTIMIZED, Graviton) with AZ /
  instance-type fallback.
- **C2 — Parallel fan-out** across `(codec × tier × chunk)`, bounded by fleet
  size / a concurrency cap.
- **C3 — Scale to zero** when idle; kept warm only during an active run.
- **C4 — Cache the shared input** (mezzanine) so phases and re-runs don't
  recompute it.

### 6.5 Developer ergonomics
- **E1 — Commit-free testing.** `make farm-dev-up` runs the entire farm from the
  working tree: local build (uncommitted Go + deps) on the master, working-tree
  Python bind-mounted into every worker and the orchestrator.
- **E2 — Fast propagation.** Re-running the dev loop rsyncs only diffs and
  restarts workers — no image rebuild for code changes.
- **E3 — Cross-arch deps.** `DEV_BUILD=1` native-builds uncommitted deps on
  cross-arch boxes (no QEMU, no push).

## 7. Success criteria

- A dropped file becomes complete, player-valid ABR output with no manual
  per-file steps.
- One command turns a pile of LAN machines into a working encode farm.
- Restarting the server, losing a worker, or a spot reclaim costs seconds of
  redo, not the job.
- An operator can change any layer of the encoder and see it running across the
  whole farm without committing or pushing.
- The operator can see, at a glance, which machine ran each chunk and where
  every job stands.

## 8. Open questions / future work

- Auto-discovery / health of worker machines beyond static `DIST_WORKERS`.
- Ladder expansion (`apple` / `apple-uniq`) — see
  [`apple-ladder-design.md`](apple-ladder-design.md).
- Adaptive chunk sizing by source length / codec.
- Whether to publish a public multi-arch GHCR image so `make farm-up` / `run-remote`
  onboarding needs no PAT.
