#!/usr/bin/env python3
"""Temporal activity worker for distributed-local encoding.

Runs inside the encoder image on every box (Mac + ubuntu). Registers the encode
activities and polls the shared task queue; Temporal (durable server on the Mac)
routes activities here and reschedules them elsewhere if this worker vanishes —
that's the "adapt if a box goes away mid-encode" guarantee, for free.

Each activity shells out to the unchanged `cli_phase` (which does its own MinIO
I/O), so the queue only carries pointers (S3 URIs + params), never video. The
activity heartbeats each output line so a dead worker is detected quickly and
its in-flight chunk is retried on another worker.

Env: TEMPORAL_ADDRESS (host:7233), TEMPORAL_TASK_QUEUE (default "encode"),
ENCODE_SLOTS (max concurrent activities here ≈ cores/2), plus the MinIO creds
the activities pass through to cli_phase.
"""
from __future__ import annotations

import asyncio
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import Priority, RetryPolicy
from temporalio.worker import Worker

TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "encode")

# Chunk priority (Temporal priority_key: 1 = highest, dispatched first). The key
# is COMPOSITE — cross-job rank is the dominant term, intra-job cost the minor one:
#     priority_key = job_rank * PRIORITY_BANDS + cost_band     (both start at 0/1)
# so ALL of an older job's chunks (rank 0) outrank ANY younger job's (rank 1+),
# and within a job the heaviest tiers lead. PRIORITY_BANDS = cost bands per job;
# PRIORITY_LEVELS = the server's matching.priorityLevels ceiling (raised to 50 in
# the encode dynamic config) = max jobs (10) × bands (5). Keys are clamped to it.
# See #99. Mirrors the cloud path's jobPriorityBase (1000-wide bands + 0-999 within).
PRIORITY_BANDS = int(os.environ.get("CHUNK_PRIORITY_BANDS", "5"))
PRIORITY_LEVELS = int(os.environ.get("CHUNK_PRIORITY_LEVELS", "50"))

# Pulls the live % out of an ENCODER-STAGE marker so it can ride the heartbeat.
_STAGE_PCT_RE = re.compile(r"percent=([0-9.]+)\]\]")


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort stop a phase subprocess AND every child it spawned — the
    ffmpeg it runs for the encode AND the VMAF audit. cli_phase is launched in
    its own session (start_new_session below), so we signal the whole PROCESS
    GROUP: SIGTERM, then SIGKILL after a grace. Signalling only cli_phase's PID
    (proc.terminate/kill) would leave its ffmpeg child orphaned and still
    burning CPU after a cancel — the exact thing cancel is trying to stop.
    No-op if it already exited."""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    except Exception:  # noqa: BLE001
        pass


# --- fleet CPU reporting -----------------------------------------------------
# This box's identity + perf-core target, set in main(). Each activity stamps
# them onto its heartbeat so the orchestrator can surface a per-machine CPU
# sparkline, normalized to the perf-core target (100% = the box's 4/4/8-core
# target fully busy; >100% = SMT/E-core spill).
_MACHINE = ""
_PERF_CORES = 0
_CPU_LOCK = threading.Lock()
_CPU_PREV = {"t": 0.0, "total": 0, "idle": 0, "busy": 0.0}


def _busy_cores() -> float:
    """Logical CPUs currently busy, from /proc/stat deltas. Throttled to ~2s and
    cached between samples (heartbeats fire far more often). 0 if unavailable."""
    now = time.monotonic()
    with _CPU_LOCK:
        try:
            with open("/proc/stat") as f:
                v = [int(x) for x in f.readline().split()[1:]]
            total, idle = sum(v), v[3] + (v[4] if len(v) > 4 else 0)
        except (OSError, ValueError, IndexError):
            return 0.0
        prev_t = _CPU_PREV["t"]
        if prev_t and now - prev_t < 2.0:
            return _CPU_PREV["busy"]
        dt, di = total - _CPU_PREV["total"], idle - _CPU_PREV["idle"]
        _CPU_PREV.update(t=now, total=total, idle=idle)
        if not prev_t or dt <= 0:
            return _CPU_PREV["busy"]
        busy = max(0.0, (1.0 - di / dt) * (os.cpu_count() or 1))
        _CPU_PREV["busy"] = busy
        return busy


@activity.defn(name="EncodePhase")
def encode_phase(spec: dict) -> None:
    """Run one `cli_phase` phase. spec = {"args": [...], "env": {...}}.

    Sync activity (runs in the worker's thread pool). Streams cli_phase output,
    heartbeating each line; raises on non-zero exit so Temporal retries. ENCODER
    markers are echoed to stdout so they show in the worker/Temporal logs.
    """
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    # Isolate the work dir PER activity: a Temporal worker runs many activities
    # concurrently in ONE container, but cli_phase's _prepare_work_dir() rmtree's
    # ENCODER_WORK_DIR at the start of every run — so a shared dir means they nuke
    # each other's mezzanine/chunk files. Each phase exchanges everything via
    # MinIO, so a private scratch dir is correct; clean it up after.
    work_dir = f"/tmp/act-{uuid.uuid4().hex}"
    env["ENCODER_WORK_DIR"] = work_dir
    # Shared (per-worker, NOT per-activity) mezzanine cache, so all this box's
    # chunks download the mezzanine once instead of once each. cli_phase symlinks
    # it into each activity's work dir zero-copy.
    env.setdefault("MEZZ_CACHE_DIR", "/tmp/mezz-cache")
    cmd = ["python3", "-m", "infinite_streaming_encoder.cli_phase", *spec["args"]]
    # start_new_session: run cli_phase as a process-group leader so a cancel can
    # kill it AND its ffmpeg children (encode + VMAF) as one group — see _terminate.
    proc = subprocess.Popen(cmd, text=True, env=env, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    last = ""
    progress = 0.0
    # Pump cli_phase stdout on a helper thread so the loop below wakes at least
    # once a second even when cli_phase is quiet for a long stretch — e.g. a
    # blocking VMAF-audit ffmpeg (subprocess.run) emits nothing to stdout. That
    # lets us HEARTBEAT (which is how cancellation is delivered) and act on a
    # cancel promptly, instead of only between output lines — otherwise a chunk
    # mid-VMAF ignores the cancel until the VMAF pass finishes.
    lines: "queue.Queue[str | None]" = queue.Queue()

    def _pump() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.put(line)
        finally:
            lines.put(None)  # EOF sentinel

    threading.Thread(target=_pump, daemon=True).start()

    def _cancelled() -> bool:
        # Cancellation rides in on the heartbeat response. Kill cli_phase AND its
        # ffmpeg child (process group — see _terminate) so a cancelled chunk stops
        # now instead of running to completion on this worker.
        if activity.is_cancelled():
            print("[temporal-worker] activity cancelled — killing encode + ffmpeg",
                  flush=True)
            _terminate(proc)
            return True
        return False

    rc = None
    try:
        while True:
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                # No output for a second (quiet encode/VMAF pass) — still heartbeat
                # + poll cancellation so we don't sleep through a cancel.
                activity.heartbeat(last[:180], {
                    "machine": _MACHINE, "busy": round(_busy_cores(), 2),
                    "perf": _PERF_CORES, "progress": round(progress, 1)})
                if _cancelled():
                    raise asyncio.CancelledError
                continue
            if line is None:
                break  # cli_phase closed stdout (exited)
            last = line.rstrip("\n")
            if last.startswith("[[ENCODER"):
                print(last, flush=True)
                # cli_phase's run_ffmpeg_with_progress emits ENCODER-STAGE with the
                # live out_time/duration %. Capture it so the orchestrator can show
                # real per-chunk progress instead of a binary 0→100 on completion.
                mp = _STAGE_PCT_RE.search(last)
                if mp:
                    try:
                        progress = float(mp.group(1))
                    except ValueError:
                        pass
            activity.heartbeat(last[:180], {
                "machine": _MACHINE, "busy": round(_busy_cores(), 2),
                "perf": _PERF_CORES, "progress": round(progress, 1)})
            if _cancelled():
                raise asyncio.CancelledError
        rc = proc.wait()
    finally:
        _terminate(proc)  # guards the readline-EOF path; no-op if already exited
        shutil.rmtree(work_dir, ignore_errors=True)
    if rc != 0:
        raise RuntimeError(f"cli_phase {spec['args'][:2]} exit {rc}: {last[:200]}")


# Minimal workflow to validate the Python side end-to-end (one activity).
@workflow.defn(name="TestChunkWorkflow")
class TestChunkWorkflow:
    @workflow.run
    async def run(self, spec: dict) -> str:
        await workflow.execute_activity(
            "EncodePhase", spec,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return "ok"


_RETRY = RetryPolicy(maximum_attempts=4)


@workflow.defn(name="EncodeWorkflow")
class EncodeWorkflow:
    """The distributed-local encode DAG, as durable code.

    Input `plan` (computed by the caller — Go control plane or cli_local_dist):
      bucket, job_prefix, src_key, has_audio, chunk_duration_s, n_chunks,
      burnin (bool, default True — the per-variant text overlay),
      codecs = {codec: {"two_pass": bool, "rungs": [{label,width,height,bitrate}]}}

    mezzanine + audio run first (sequential); then every (codec, rung, chunk) is
    fanned out with asyncio.gather so Temporal spreads them across all workers;
    then package/fragments/hls per codec. Temporal persists this history, so a
    worker (or the whole box) dying mid-run resumes exactly — a lost activity is
    retried on another worker. cli_phase skips a chunk whose output already
    exists (idempotent), so retries are cheap.
    """
    @workflow.run
    async def run(self, plan: dict) -> str:
        b = plan["bucket"]
        pfx = plan["job_prefix"].strip("/")
        s3_work = f"s3://{b}/{pfx}/work"
        s3_out = f"s3://{b}/{pfx}/out"
        # Mezzanine lives in a cross-job, content-addressed cache (mezz-cache/)
        # when the orchestrator supplies it; older plans fall back to per-job
        # work/. The mezzanine phase reuses it if already present, so a repeat
        # source skips the source download + stream-copy (and each worker's
        # /tmp/mezz-cache, keyed off this URI, skips the MinIO fetch too).
        mezz = plan.get("mezz_prefix") or s3_work

        # Cross-job priority spans the WHOLE job lifecycle, not just chunks. Every
        # non-chunk phase (mezzanine, audio, package, fragments, hls) rides the TOP
        # band of this job's range (job_rank*BANDS + 1) so that an older job's
        # setup AND finalization outrank a younger job's encoding — otherwise the
        # default band (~PRIORITY_LEVELS/2) would let a newer job's chunks preempt
        # this job's mezzanine (delaying its start) or its packaging (delaying its
        # finish). Chunks get the finer composite key below.
        job_rank = int(plan.get("job_rank", 0))
        job_top = min(PRIORITY_LEVELS, job_rank * PRIORITY_BANDS + 1)

        # #109: the mezzanine phase stages the pre-scaled VMAF reference when the
        # job has VMAF on, so it needs the same MEASURE_VMAF flag the chunk
        # activities get — otherwise it skips the reference and every chunk falls
        # back to re-downscaling the native mezzanine.
        await self._phase(["mezzanine", "--s3-in", f"s3://{b}/{plan['src_key']}",
                           "--s3-out", mezz],
                          {"MEASURE_VMAF": "1" if plan.get("measure_vmaf") else "0"},
                          "mezzanine", priority_key=job_top)
        if plan.get("has_audio"):
            await self._phase(["audio", "--s3-mezz", mezz, "--s3-out", s3_work],
                              {}, "audio", priority_key=job_top)

        cd = plan["chunk_duration_s"]
        n = plan["n_chunks"]
        # Dispatch HIGHEST-COST chunks first (LPT scheduling): a chunk's expected
        # cost ~ height² × codec-factor × pass-factor, so the slow 4K HEVC 2-pass
        # work starts immediately and the tail is cheap chunks — better makespan
        # (no giant chunk finishing last) and the expensive work is visibly
        # underway from the start. Deterministic (pure plan math) → Temporal-safe.
        codec_cost = {"h264": 1.0, "hevc": 3.5, "av1": 8.0}
        measure_vmaf = bool(plan.get("measure_vmaf"))  # job-global VMAF audit flag
        bn = plan.get("burnin", True)  # job-level text-overlay toggle (default on)
        specs = []
        for codec, ci in plan["codecs"].items():
            tp = ci["two_pass"]
            ea = ci.get("extra_args", "")  # per-codec ladder-profile ffmpeg args
            for r in ci["rungs"]:
                w = (r["height"] * r["height"] * codec_cost.get(codec, 1.0)
                     * (1.8 if tp else 1.0))
                for i in range(n):
                    specs.append((w, codec, r, tp, ea, i))
        specs.sort(key=lambda s: s[0], reverse=True)  # most expensive first
        # Enqueue order alone doesn't hold — Temporal doesn't guarantee FIFO
        # dispatch even on a single partition (#96/#99), so cheap chunks leak
        # ahead of the 4K work. Give each chunk a COMPOSITE priority_key:
        #     job_rank * PRIORITY_BANDS + cost_band
        # (cost_band 1..PRIORITY_BANDS, 1 = heaviest tier). Cross-job rank dominates
        # so an older job's whole ladder outranks a younger job's; cost_band orders
        # tiers within a job. Deterministic (pure plan math over the sorted distinct
        # weights) → Temporal-replay-safe.
        distinct_w = sorted({s[0] for s in specs}, reverse=True)
        _wband = {
            wv: min(PRIORITY_BANDS, 1 + (idx * PRIORITY_BANDS) // max(1, len(distinct_w)))
            for idx, wv in enumerate(distinct_w)
        }
        chunk_acts = []
        for _w, codec, r, tp, ea, i in specs:
            args = ["variant", "--codec", codec, "--label", r["label"],
                    "--width", str(r["width"]), "--height", str(r["height"]),
                    "--bitrate", str(r["bitrate"]), "--chunk-index", str(i),
                    "--s3-mezz", mezz, "--s3-out", s3_work]
            if tp:
                args.append("--two-pass")
            if ea:
                args += ["--extra-args", ea]
            if measure_vmaf:
                args.append("--measure-vmaf")
            if not bn:
                args.append("--no-burnin")
            env = {"CHUNK_DURATION_S": str(cd), "COALESCE_RUNT_TAIL": "1",
                   "TWO_PASS": "1" if tp else "0", "EXTRA_ARGS": ea,
                   "MEASURE_VMAF": "1" if measure_vmaf else "0",
                   "BURNIN": "1" if bn else "0", "ENCODE_THREADS": "2"}
            key = min(PRIORITY_LEVELS, job_rank * PRIORITY_BANDS + _wband[_w])
            chunk_acts.append(self._phase(
                args, env, f"enc-{codec}-{r['label']}-c{i}", priority_key=key))
        await asyncio.gather(*chunk_acts)

        # Finalization (DASH packaging, fragment byteranges, HLS) — one set per
        # codec. All ride job_top too, so an older job's packaging/manifests
        # outrank a younger job's chunk encoding and its outputs land first.
        for codec in plan["codecs"]:
            await self._phase(["package-all", "--codec", codec, "--s3-variants",
                              s3_work, "--s3-audio", s3_work, "--s3-out", s3_out],
                              {}, f"pkg-{codec}", priority_key=job_top)
            for ph in ("byteranges", "hls"):
                await self._phase([ph, "--codec", codec, "--s3-package", s3_out,
                                  "--s3-out", s3_out], {}, f"{ph}-{codec}",
                                  priority_key=job_top)
        return "done"

    async def _phase(self, args, env, act_id, priority_key: int | None = None):
        # priority_key steers the matcher toward the heaviest chunks first (#99).
        # Non-chunk phases (mezzanine/audio/package/…) pass None → Temporal's
        # default band; they run sequentially outside the fan-out anyway.
        return await workflow.execute_activity(
            "EncodePhase", {"args": args, "env": env},
            start_to_close_timeout=timedelta(hours=1),
            heartbeat_timeout=timedelta(seconds=90),
            retry_policy=_RETRY, activity_id=act_id,
            priority=Priority(priority_key=priority_key))


_GB = 1024 ** 3
# A 2-pass 4K (2160p) x265 encode peaks at a few GB; too many at once OOM-kills
# the container (ffmpeg exit -9). Cap concurrency so each concurrent encode has
# ~this much RAM headroom. Conservative — a big-RAM box still packs by cores.
_MEM_PER_ENCODE_BYTES = 3 * _GB


def _container_mem_bytes() -> int:
    """RAM available to this worker — the cgroup limit if set, else /proc/meminfo.
    0 if unknown."""
    for path in ("/sys/fs/cgroup/memory.max",                    # cgroup v2
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):  # cgroup v1
        try:
            v = open(path).read().strip()
            if v not in ("max", ""):
                n = int(v)
                if 0 < n < (1 << 62):  # v1 sentinel for "unlimited" is huge
                    return n
        except (OSError, ValueError):
            pass
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _physical_cores() -> int:
    """Physical cores, NOT logical — so SMT siblings aren't counted as capacity
    (a 2nd HEVC encode on a core's SMT sibling adds ~25%, not 100%). Linux reads
    /sys topology; elsewhere (a Docker-Desktop VM that hides P/E cores) it falls
    back to the logical count — those boxes should set ENCODE_SLOTS explicitly."""
    try:
        import glob
        cores = set()
        for f in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology/core_id"):
            pkg = f.replace("core_id", "physical_package_id")
            pid = open(pkg).read().strip() if os.path.exists(pkg) else "0"
            cores.add((pid, open(f).read().strip()))
        if cores:
            return len(cores)
    except OSError:
        pass
    return os.cpu_count() or 4


def _default_slots() -> int:
    """Concurrent encodes when ENCODE_SLOTS is unset: physical-cores/2 (with the
    workflow's 2 threads each this fills the physical cores and skips SMT),
    capped by RAM/3GB so a small VM doesn't OOM on 4K. A box that can't reveal
    its performance cores (a Mac's Docker VM hides P/E) should set ENCODE_SLOTS."""
    by_cores = max(1, _physical_cores() // 2)
    mem = _container_mem_bytes()
    if mem <= 0:
        return by_cores
    return min(by_cores, max(1, int(mem // _MEM_PER_ENCODE_BYTES)))


async def main() -> None:
    import socket
    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    slots = int(os.environ.get("ENCODE_SLOTS", "0")) or _default_slots()
    # Friendly, stable worker identity (WORKER_LABEL, e.g. "mac"/"ubuntu"): it
    # rides on every ActivityTaskStarted event, so the orchestrator can tell
    # which box ran each chunk and colour the UI plot by machine. Defaults to
    # the hostname.
    identity = os.environ.get("WORKER_LABEL") or socket.gethostname()
    global _MACHINE, _PERF_CORES
    _MACHINE, _PERF_CORES = identity, slots * 2
    client = await Client.connect(address, identity=identity)
    print(f"[temporal-worker] connected {address} queue={TASK_QUEUE} "
          f"slots={slots} identity={identity}", flush=True)
    with ThreadPoolExecutor(max_workers=slots) as pool:
        worker = Worker(
            client, task_queue=TASK_QUEUE,
            activities=[encode_phase],
            workflows=[TestChunkWorkflow, EncodeWorkflow],
            activity_executor=pool, max_concurrent_activities=slots,
            # encode_phase heartbeats every output line (its live % rides the
            # heartbeat), but the SDK throttles the actual heartbeat RPC — default
            # ~30s, max ~60s — so a slow 2160p chunk's progress bar only nudged
            # once a minute. A handful of chunks heartbeating a tiny payload every
            # couple seconds is negligible load; make the UI feel live.
            default_heartbeat_throttle_interval=timedelta(seconds=2),
            max_heartbeat_throttle_interval=timedelta(seconds=3),
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
