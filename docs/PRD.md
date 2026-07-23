# PRD: Encoder

Status: living document · Reflects the system as of branch `dynamic-chunking` (v0.1.0)

## 1. Summary

Encoder is a self-hosted control plane for producing adaptive-bitrate (ABR)
streaming assets from source video. A single operator drops a master file in
and gets back player-ready LL-HLS + DASH output — encoded across multiple
codecs and resolutions, with burned-in QA overlays — either on their own
machine or fanned out cheaply across AWS spot capacity. The system is designed
so that neither a server restart nor a spot reclaim loses meaningful work.

## 2. Problem

Producing a full ABR ladder for a library of source videos is:

- **Slow** — a single 4K encode across three codecs and six rungs is hours of
  CPU. Doing it serially, one box at a time, does not scale to a library.
- **Expensive if done naively** — on-demand cloud compute for hours of
  encoding is costly; spot compute is cheap but interruptible.
- **Fragile to interruption** — a whole-clip encode that dies at minute 38 of
  40 (spot reclaim, or the operator's server restarting) loses all 38 minutes.
- **Operationally fiddly** — stitching ffmpeg, a packager, HLS/DASH manifest
  generation, S3 upload, and instance lifecycle into one reliable flow is a lot
  of moving parts to babysit by hand.

## 3. Goals

1. **One-drop operation.** Put a file in the source directory; get correct ABR
   output out, with no per-file manual steps.
2. **Cheap cloud encoding.** Exploit spot capacity (Graviton) without paying
   the interruption tax.
3. **Resumable everywhere.** A spot reclaim loses at most one small chunk of
   work; a server restart loses nothing — in-flight work is reattached, not
   restarted.
4. **Same pipeline local and cloud.** Identical Python encoder runs on the
   operator's machine and in the cloud, so behaviour matches and there's one
   thing to reason about.
5. **Observable.** Live per-phase, per-chunk progress in the UI, attributed to
   the machine that ran each unit.
6. **Correct, standards-clean output.** LL-HLS (fMP4 with EXT-X-PART byte
   ranges) and DASH that real players accept.

## 4. Non-goals

- **A multi-tenant SaaS.** This is a single-operator control plane, not a
  hosted product with accounts, quotas, or isolation between users.
- **Live / real-time transcoding.** The system encodes files, not live streams.
- **Hardware (VideoToolbox) encoding.** Intentionally dropped — it only works
  on macOS hosts, not the Linux workers the cloud path depends on.
- **A test/CI suite.** Not in scope for this repo today.
- **Managing the S3 bucket lifecycle from zero.** The bucket is a prerequisite;
  Terraform manages what runs *on* it, not the bucket's existence.

## 5. Users

- **Primary: the operator** (the repo owner). Runs the server on their Mac,
  drives the UI, decides local vs. cloud, watches jobs, plays back output.
- **Implicit: downstream players.** The output must satisfy HLS.js / native
  HLS / DASH players — they are the real consumers of the artifact.

## 6. Requirements

### 6.1 Functional

- **F1 — Ingest.** Watch a source directory; auto-submit a file once it is
  stable (size unchanged between scans) and not already encoded. Manual submit
  from the UI is also supported.
- **F2 — Encode ladder.** Produce an ABR ladder per selected codec
  (H.264 / HEVC / AV1) and resolution tier, with closed GOPs and burned-in
  overlays (timecode, rate, codec+res+fps, watermark, padding label).
- **F3 — Package.** Produce LL-HLS (fMP4 + EXT-X-PART byte ranges) and DASH
  (fMP4 SegmentList) per codec. Optional TS-HLS variant.
- **F4 — Targets.** Route a job to `local`, `cloudbatch`, or (legacy) `cloud`.
- **F5 — Skip / narrow.** Before encoding, detect which codec outputs already
  exist and encode only the missing ones; skip a file entirely if complete.
  Bypassed by force-reencode.
- **F6 — Live progress.** Stream per-job, per-phase, per-chunk status to the UI
  over SSE, attributed to the executing machine.
- **F7 — Playback.** Serve output for in-browser HLS/DASH playback.
- **F8 — Idempotent output placement.** Partial output never appears in the
  output directory; only a successful job's result is moved into place.

### 6.2 Reliability

- **R1 — Server-restart resilience.** A running encode survives a restart of
  the server's own container; the server reattaches to the live worker rather
  than restarting the encode.
- **R2 — Spot-reclaim resilience.** In the cloud path, a reclaim loses at most
  one 30 s chunk, re-run idempotently on fresh capacity. Batch jobs retry on
  spot-reclaim exit conditions.
- **R3 — Crash-consistent state.** Per-job state is persisted before each file
  and reconciled on startup; state is removed only on a terminal outcome.

### 6.3 Cost & performance

- **C1 — Spot-first cloud compute** (SPOT_CAPACITY_OPTIMIZED, Graviton c8g/c7g)
  with AZ / instance-type fallback on insufficient capacity.
- **C2 — Parallel fan-out.** Encode `(codec × tier × chunk)` concurrently
  across the fleet, bounded by compute-env max vCPUs and a Map concurrency cap.
- **C3 — Scale to zero.** Compute env min/desired 0 when idle; kept warm only
  during an active run, and leaks auto-terminated.
- **C4 — Cache the shared input.** The mezzanine (stream-copy) is source-keyed
  and cached in S3 so phases and re-runs don't recompute it.

### 6.4 Scheduling & fairness

- **S1 — Earlier job wins.** An earlier-submitted job outranks a later one via
  a per-job priority band, so it isn't starved by a later job that happened to
  fan out first.
- **S2 — Atomic fan-out.** Launch→fan-out is serialised so a job floods the
  queue with all its work before the next execution starts.
- **S3 — Smart within-job ordering.** Variant priority is derived from learned
  encode speed so the schedule reflects real cost.

## 7. Success criteria

- A dropped source file becomes complete, player-valid ABR output with no
  manual per-file steps.
- Restarting the server mid-encode does not restart or corrupt the encode.
- A spot reclaim during a cloud encode costs seconds of redo, not minutes.
- The operator can see, at a glance, which machine ran each phase/chunk and
  where every job stands.

## 8. Open questions / future work

- Retiring the legacy one-box EC2 path (`cli_cloud.py`) once Batch fully
  covers its use.
- Ladder expansion (`apple` / `apple-uniq`) — see
  [`apple-ladder-design.md`](apple-ladder-design.md).
- Whether chunk size (currently 30 s) should adapt to source length / codec.
