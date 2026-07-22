#!/usr/bin/env python3
"""Distributed local encode orchestrator — the local twin of cli_cloud.py.

Runs the full ABR DAG but fans phase containers out across a worker pool
(the local Docker daemon + zero or more `ssh://user@host` daemons) with MinIO
(S3-compatible) as the shared store, instead of AWS Batch + S3. The Go server
launches this as one detached worker exactly like it launches cli_local /
cli_cloud; the phase containers it spawns are ordinary `cli_phase` workers
(unchanged) reading and writing MinIO.

Resilience (the whole point): every chunk is an idempotent unit keyed by its
output object in MinIO. A chunk whose container exits non-zero, or whose host
becomes unreachable mid-encode, is re-queued onto another reachable worker; a
chunk whose output already exists in MinIO is skipped. With no remote worker
reachable the pool collapses to {local} and the run is identical to single-node
A1 — so the system works with and without the extra box and adapts if it
vanishes mid-encode.

DAG:
  1. upload source -> MinIO
  2. mezzanine (local), audio (local)
  3. per (codec, rung): plan chunks; dispatch each chunk as a `variant` phase
     container across the pool, with failover; -> chunk mp4s in MinIO
  4. package-all + fragments + hls per codec (local) -> output_<codec>/ in MinIO
  5. download output_<codec>/ -> <output-dir>/<stem>_<codec>/ for the Go server
     to move into OUTPUT_DIR.

Chunk-plan agreement: the worker (`cli_phase variant`) derives the chunk plan
from the mezzanine's own duration; we set COALESCE_RUNT_TAIL=1 so both it and
this orchestrator fold a sub-frame tail chunk the same way, and package-all
globs whatever chunk files land — so all three agree without passing an
explicit count.
"""
from __future__ import annotations

import argparse
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from encoder.chunking import plan_chunks
from encoder.encode_variants import _coalesce_runt_tail, variant_stage_key
from encoder.ffprobe import ProbeError, probe
from encoder.ladder import (
    Rung, get_ladder, parse_bitrate_override, select_rungs,
)
from encoder.progress import Stage, emit_plan, emit_stage

_SEGMENT_DURATION_S = 6.0
# Fold a final chunk shorter than this into its predecessor (mirrors
# encode_variants._MIN_TAIL_CHUNK_S; the worker does the same via
# COALESCE_RUNT_TAIL, so the dispatched count matches what it encodes).
_MAX_RETRIES_PER_CHUNK = 3


# In-flight phase containers for the POOL backend, so a cancel can `docker kill`
# them on every worker instead of orphaning remote encodes. The Go control plane
# cancels a job by `docker stop`-ing THIS orchestrator (SIGTERM + 30s grace);
# without this, killing the orchestrator leaves the remote `docker run` chunks
# running on their daemons. (The Temporal backend cancels the durable workflow
# instead — see run_temporal.)
_POOL_CONTAINERS: set = set()          # {(host_args_tuple, container_name)}
_POOL_LOCK = threading.Lock()


def _install_pool_cancel_handler() -> None:
    """On SIGTERM/SIGINT, `docker kill` every in-flight chunk container across all
    workers, then exit — so a cancelled distributed encode stops now, everywhere."""
    def _handler(signum, frame):  # noqa: ARG001
        print("[dist] cancel (SIGTERM) — killing in-flight chunk containers "
              "across workers", flush=True)
        with _POOL_LOCK:
            items = list(_POOL_CONTAINERS)
        for host_args, name in items:
            try:
                subprocess.run(["docker", *host_args, "kill", name], timeout=20,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:  # noqa: BLE001
                pass
        raise SystemExit(130)
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


# ---------------------------------------------------------------------------
# Worker pool
# ---------------------------------------------------------------------------

@dataclass
class Worker:
    """One Docker daemon we can run phase containers on.

    `spec` is "local" (the mounted docker.sock) or "ssh://user@host". `slots`
    caps how many chunk containers run on it at once — sized to its cores so a
    box packs ~2 threads/encode like the cloud. `code_mount` optionally bind-
    mounts current encoder code over the image's (for a stale remote image).
    """
    spec: str
    image: str
    slots: int
    code_mount: str | None = None
    healthy: bool = True

    @property
    def is_local(self) -> bool:
        return self.spec == "local"

    @property
    def host_args(self) -> list[str]:
        return [] if self.is_local else ["-H", self.spec]

    def label(self) -> str:
        return "local" if self.is_local else self.spec.split("@")[-1]


def _docker(host_args: list[str], *args: str, timeout: float | None = None,
            capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *host_args, *args],
        text=True, timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def _probe_worker(w: Worker) -> bool:
    """Is this daemon reachable right now? Cheap `docker version` ping."""
    try:
        r = _docker(w.host_args, "version", "--format", "{{.Server.Version}}",
                    timeout=15, capture=True)
        return r.returncode == 0
    except Exception:
        return False


def _remote_cores(w: Worker) -> int:
    """CPU count on the worker (for slot sizing). Falls back to 4."""
    try:
        r = _docker(w.host_args, "run", "--rm", "--entrypoint", "nproc",
                    w.image, timeout=60, capture=True)
        if r.returncode == 0:
            return max(1, int((r.stdout or "").strip()))
    except Exception:
        pass
    return 4


def build_pool(specs: list[str], image_local: str, image_remote: str,
               threads_per_encode: int, code_mount_remote: str | None) -> list[Worker]:
    """Resolve worker specs into a live pool, dropping unreachable hosts.

    `local` is always included (the master is self-sufficient). Remote specs
    are health-checked; unreachable ones are logged and skipped, so a downed
    ubuntu box just means a smaller pool.
    """
    pool: list[Worker] = []
    for spec in specs:
        is_local = spec == "local"
        image = image_local if is_local else image_remote
        w = Worker(spec=spec, image=image, slots=1,
                   code_mount=None if is_local else code_mount_remote)
        if not is_local and not _probe_worker(w):
            print(f"[dist] worker {spec} unreachable — skipping", flush=True)
            continue
        cores = os.cpu_count() or 4 if is_local else _remote_cores(w)
        w.slots = max(1, cores // max(1, threads_per_encode))
        pool.append(w)
        print(f"[dist] worker {w.label()}: {w.slots} slot(s) "
              f"({cores} cores ÷ {threads_per_encode} threads)", flush=True)
    if not pool:
        raise SystemExit("[dist] no workers available (not even local?)")
    return pool


# ---------------------------------------------------------------------------
# MinIO (host-side boto3, endpoint-aware — mirrors cli_phase._s3)
# ---------------------------------------------------------------------------

def _s3():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )


def _progress_cb(stage_key: str, total_bytes: int):
    """A boto3 transfer Callback that emits throttled ENCODER-STAGE progress
    (every ~2%) for a byte transfer — so the UI shows a moving bar for the
    otherwise-dark source upload / output download."""
    total = max(1, total_bytes)
    sent = [0]
    last = [-5.0]

    def cb(n: int) -> None:
        sent[0] += n
        pct = min(99.0, sent[0] / total * 100.0)
        if pct - last[0] >= 2.0:
            last[0] = pct
            emit_stage(stage_key, "running", pct)

    return cb


def _upload_source(local: Path, bucket: str, key: str) -> None:
    total = local.stat().st_size
    print(f"[dist] uploading source ({total / 1e6:.0f} MB) -> s3://{bucket}/{key}",
          flush=True)
    emit_stage("upload:source", "running", 0.0)
    _s3().upload_file(str(local), bucket, key,
                      Callback=_progress_cb("upload:source", total))
    emit_stage("upload:source", "done", 100.0)


def _object_exists(bucket: str, key: str) -> bool:
    try:
        _s3().head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def _download_prefix(bucket: str, prefix: str, dest: Path) -> int:
    """Download every object under `prefix` into `dest`, preserving the tail
    path after `prefix`. Emits throttled download:outputs progress by bytes.
    Returns the file count."""
    s3 = _s3()
    dest.mkdir(parents=True, exist_ok=True)
    objs = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        objs.extend(page.get("Contents", []))
    total = sum(o.get("Size", 0) for o in objs)
    emit_stage("download:outputs", "running", 0.0)
    cb = _progress_cb("download:outputs", total)
    n = 0
    for obj in objs:
        rel = obj["Key"][len(prefix):].lstrip("/")
        if not rel:
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, obj["Key"], str(out), Callback=cb)
        n += 1
    emit_stage("download:outputs", "done", 100.0)
    return n


# ---------------------------------------------------------------------------
# Phase container execution
# ---------------------------------------------------------------------------

def _phase_env(extra: dict[str, str]) -> list[str]:
    """-e flags: MinIO endpoint + creds (+ any per-phase extras). We forward
    the orchestrator's own AWS_* / S3_ENDPOINT_URL so remote workers reach the
    same MinIO on the master's LAN address."""
    env = {
        "S3_ENDPOINT_URL": os.environ["S3_ENDPOINT_URL"],
        "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", ""),
        "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        "AWS_REGION": os.environ.get("AWS_REGION", "us-east-1"),
        "COALESCE_RUNT_TAIL": "1",
    }
    env.update(extra)
    out: list[str] = []
    for k, v in env.items():
        out += ["-e", f"{k}={v}"]
    return out


def run_phase(w: Worker, phase_args: list[str], *, env: dict[str, str],
              log_prefix: str) -> int:
    """Run one `cli_phase <phase_args>` container on worker `w`, streaming its
    output live (each line tagged with the host so the UI can colour by
    machine). Returns the container exit code; non-zero = the caller re-queues.
    """
    # Name the container so the cancel handler can `docker kill` it by name on the
    # right daemon; register it for the duration of the run.
    cname = f"encdist-{uuid.uuid4().hex[:12]}"
    cmd = ["docker", *w.host_args, "run", "--rm", "--name", cname,
           *_phase_env(env)]
    if w.code_mount:
        cmd += ["-v", f"{w.code_mount}:/app/scripts/encoder:ro"]
    cmd += ["--entrypoint", "python3", w.image,
            "-m", "encoder.cli_phase", *phase_args]
    key = (tuple(w.host_args), cname)
    with _POOL_LOCK:
        _POOL_CONTAINERS.add(key)
    try:
        proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except Exception as e:
        with _POOL_LOCK:
            _POOL_CONTAINERS.discard(key)
        print(f"[{log_prefix}@{w.label()}] launch failed: {e}", flush=True)
        return 1
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            # Pass ENCODER-STAGE / ENCODER-SPEED markers through verbatim so the Go
            # log scanner still sees them; tag everything else with the host.
            if line.startswith("[[ENCODER"):
                print(line, flush=True)
            else:
                print(f"[{w.label()}] {line}", flush=True)
        return proc.wait()
    finally:
        with _POOL_LOCK:
            _POOL_CONTAINERS.discard(key)


# ---------------------------------------------------------------------------
# Chunk fan-out with failover
# ---------------------------------------------------------------------------

@dataclass
class ChunkTask:
    codec: str
    rung: Rung
    index: int
    two_pass: bool
    attempts: int = 0


@dataclass
class _Shared:
    q: "queue.Queue[ChunkTask]"
    bucket: str
    work_prefix: str          # s3 key prefix holding mezzanine + chunk mp4s
    chunk_duration_s: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    failed: list[str] = field(default_factory=list)
    remaining: int = 0


def _chunk_key(work_prefix: str, codec: str, label: str, index: int) -> str:
    return f"{work_prefix}/{codec}_{label}_chunk{index:03d}.mp4"


def _worker_loop(w: Worker, sh: _Shared, s3_mezz: str, s3_out: str) -> None:
    """One slot on one worker: pull chunk tasks until the queue drains. On
    failure, re-queue (unless retries exhausted or the host went unreachable)."""
    while True:
        try:
            task = sh.q.get_nowait()
        except queue.Empty:
            return
        key = _chunk_key(sh.work_prefix, task.codec, task.rung.label, task.index)
        stage = variant_stage_key(task.codec, task.rung.label, task.index)
        # Resume/idempotency: already produced (e.g. before a restart) -> skip.
        if _object_exists(sh.bucket, key):
            emit_stage(stage, "done", 100.0)
            with sh.lock:
                sh.remaining -= 1
            sh.q.task_done()
            continue
        task.attempts += 1
        r = task.rung
        args = ["variant", "--codec", task.codec, "--label", r.label,
                "--width", str(r.width), "--height", str(r.height),
                "--bitrate", str(r.bitrate), "--chunk-index", str(task.index),
                "--s3-mezz", s3_mezz, "--s3-out", s3_out]
        if task.two_pass:
            args.append("--two-pass")
        rc = run_phase(w, args,
                       env={"CHUNK_DURATION_S": f"{sh.chunk_duration_s:g}",
                            "TWO_PASS": "1" if task.two_pass else "0"},
                       log_prefix=stage)
        if rc == 0 and _object_exists(sh.bucket, key):
            with sh.lock:
                sh.remaining -= 1
            sh.q.task_done()
            continue
        # Failure. If the host itself is gone, drop this worker's slots by
        # marking it unhealthy (its other in-flight tasks will also fail and
        # re-queue). Re-queue this task for another worker unless exhausted.
        healthy = _probe_worker(w)
        w.healthy = healthy
        emit_stage(stage, "running", 0.0)  # back to pending in the UI
        if task.attempts >= _MAX_RETRIES_PER_CHUNK:
            emit_stage(stage, "failed", 0.0)
            with sh.lock:
                sh.failed.append(f"{task.codec} {r.label} chunk{task.index}")
                sh.remaining -= 1
            sh.q.task_done()
            print(f"[dist] {stage}: giving up after {task.attempts} attempts",
                  flush=True)
            continue
        print(f"[dist] {stage}: rc={rc} on {w.label()} "
              f"(host {'up' if healthy else 'DOWN'}) — re-queueing "
              f"(attempt {task.attempts}/{_MAX_RETRIES_PER_CHUNK})", flush=True)
        sh.q.put(task)
        sh.q.task_done()
        if not healthy:
            return  # this slot's host is gone; stop pulling


def encode_chunks_distributed(pool: list[Worker], tasks: list[ChunkTask],
                              sh: _Shared, s3_mezz: str, s3_out: str) -> None:
    """Run all chunk tasks across the pool with failover. Blocks until the
    queue drains (every task done, skipped, or failed)."""
    for t in tasks:
        sh.q.put(t)
    sh.remaining = len(tasks)

    threads: list[threading.Thread] = []
    for w in pool:
        for slot in range(w.slots):
            t = threading.Thread(target=_worker_loop, args=(w, sh, s3_mezz, s3_out),
                                 name=f"{w.label()}#{slot}", daemon=True)
            t.start()
            threads.append(t)

    # If every worker thread exits while tasks remain (e.g. all remote hosts
    # died and local slots also drained a re-queue burst), respawn local slots
    # so the master always finishes the job itself.
    while True:
        alive = [t for t in threads if t.is_alive()]
        with sh.lock:
            remaining = sh.remaining
        if remaining <= 0:
            break
        if not alive:
            local = next((w for w in pool if w.is_local), None)
            if local is None:
                break
            print("[dist] all workers exited with tasks left — "
                  "respawning local slots to finish", flush=True)
            threads = []
            for slot in range(local.slots):
                t = threading.Thread(target=_worker_loop,
                                     args=(local, sh, s3_mezz, s3_out),
                                     name=f"local-refill#{slot}", daemon=True)
                t.start()
                threads.append(t)
        time.sleep(1.0)


# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------

def _emit_plan(rungs_by_codec: dict[str, list[Rung]], chunks_by_variant: dict,
               has_audio: bool) -> None:
    """Announce upload + mezzanine + every chunk + audio + package/fragments/hls
    + download, so the UI lays out the full grid up front (like the cloud plan).
    The upload/download rows animate from the orchestrator's own S3 transfers."""
    stages: list[Stage] = [
        Stage(key="upload:source", label="upload source"),
        Stage(key="mezzanine", label="mezzanine"),
    ]
    for codec, rungs in rungs_by_codec.items():
        for rung in rungs:
            n = chunks_by_variant[(codec, rung.label)]
            for i in range(n):
                stages.append(Stage(
                    key=variant_stage_key(codec, rung.label, i),
                    label=f"encode {codec} {rung.label} chunk{i}",
                ))
    if has_audio:
        stages.append(Stage(key="audio", label="audio"))
    for codec in rungs_by_codec:
        stages.append(Stage(key=f"package:{codec}", label=f"package {codec}"))
        stages.append(Stage(key=f"fragments:{codec}", label=f"fragments {codec}"))
        stages.append(Stage(key=f"hls:{codec}", label=f"HLS {codec}"))
    stages.append(Stage(key="download:outputs", label="download outputs"))
    emit_plan(stages)


def _emit_commercial_cost(rungs_by_codec, info, input_path) -> None:
    """Print an ENCODER-COMMERCIAL marker: what this job's ladder would have cost
    on a commercial cloud encoder. Source bitrate is approximated from file size /
    duration for the high-bitrate multiplier. Best-effort."""
    try:
        from encoder.commercial_cloud import estimate_usd, mediaconvert_usd
        try:
            src_mbps = input_path.stat().st_size * 8 / (info.duration_s or 1) / 1e6
        except OSError:
            src_mbps = 0.0
        usd = estimate_usd(rungs_by_codec, info.duration_s, fps=float(info.fps),
                           has_audio=info.has_audio, src_mbps=src_mbps)
        mc = mediaconvert_usd(rungs_by_codec, info.duration_s, fps=float(info.fps))
        print(f"[[ENCODER-COMMERCIAL commercial={usd:.4f} mediaconvert={mc:.4f}]]",
              flush=True)
    except Exception:  # noqa: BLE001 — cost estimate is cosmetic, never fail a run
        pass


def run(args: argparse.Namespace) -> int:
    _install_pool_cancel_handler()
    input_path = Path(args.input)
    if not os.access(input_path, os.R_OK):
        print(f"error: cannot read input: {input_path}", file=sys.stderr)
        return 1
    try:
        info = probe(input_path)
    except ProbeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    bucket = args.s3_bucket
    prefix = args.job_prefix.strip("/")
    src_key = f"{prefix}/input{input_path.suffix}"
    work_prefix = f"{prefix}/work"
    out_prefix = f"{prefix}/out"
    s3_work = f"s3://{bucket}/{work_prefix}"
    s3_out = f"s3://{bucket}/{out_prefix}"

    # Resolve rungs + per-variant chunk plan (coalesced, matching the worker).
    ladder_def = get_ladder(args.ladder)
    overrides = {
        "hevc": parse_bitrate_override(args.bitrate_override_hevc),
        "h264": parse_bitrate_override(args.bitrate_override_h264),
        "av1": {},
    }
    codecs = {"both": ["hevc", "h264"], "all": ["hevc", "h264", "av1"]}.get(
        args.codec, [args.codec])
    rungs_by_codec: dict[str, list[Rung]] = {}
    for codec in codecs:
        rungs = select_rungs(ladder_def, codec, args.max_res, info.width,
                             overrides[codec])
        if rungs:
            rungs_by_codec[codec] = rungs
    if not rungs_by_codec:
        print("error: no ladder rungs fit this source", file=sys.stderr)
        return 1

    chunks = _coalesce_runt_tail(
        plan_chunks(info.duration_s, args.chunk_duration_s, _SEGMENT_DURATION_S))
    n_chunks = len(chunks)
    chunks_by_variant = {
        (codec, r.label): n_chunks
        for codec, rungs in rungs_by_codec.items() for r in rungs
    }
    _emit_plan(rungs_by_codec, chunks_by_variant, info.has_audio)
    _emit_commercial_cost(rungs_by_codec, info, input_path)

    pool = build_pool(
        args.worker or ["local"], args.image_local, args.image_remote,
        args.encode_threads or 2, args.remote_code_mount)

    # 1. source -> MinIO
    _upload_source(input_path, bucket, src_key)

    # 2. mezzanine + audio (local only — one-shot, cheap, needs no fan-out)
    local = next(w for w in pool if w.is_local)
    print("[dist] === mezzanine ===", flush=True)
    if run_phase(local, ["mezzanine", "--s3-in", f"s3://{bucket}/{src_key}",
                         "--s3-out", s3_work], env={}, log_prefix="mezzanine"):
        return 1
    if info.has_audio:
        print("[dist] === audio ===", flush=True)
        if run_phase(local, ["audio", "--s3-mezz", s3_work, "--s3-out", s3_work],
                     env={}, log_prefix="audio"):
            return 1

    # 3. chunk fan-out across the pool
    two_pass_for = {"hevc": not args.hevc_single_pass, "h264": False, "av1": False}
    tasks: list[ChunkTask] = []
    for codec, rungs in rungs_by_codec.items():
        for rung in rungs:
            for i in range(n_chunks):
                tasks.append(ChunkTask(codec, rung, i, two_pass_for[codec]))
    print(f"[dist] === {len(tasks)} chunk task(s) across "
          f"{sum(w.slots for w in pool)} slot(s) ===", flush=True)
    sh = _Shared(q=queue.Queue(), bucket=bucket, work_prefix=work_prefix,
                 chunk_duration_s=args.chunk_duration_s)
    encode_chunks_distributed(pool, tasks, sh, s3_work, s3_work)
    if sh.failed:
        print(f"[dist] FAILED chunks: {', '.join(sh.failed)}", file=sys.stderr)
        return 1

    # 4. package-all + fragments + hls per codec (local)
    for codec in rungs_by_codec:
        print(f"[dist] === package {codec} ===", flush=True)
        if run_phase(local, ["package-all", "--codec", codec,
                             "--s3-variants", s3_work, "--s3-audio", s3_work,
                             "--s3-out", s3_out], env={}, log_prefix=f"package:{codec}"):
            return 1
        for ph in ("byteranges", "hls"):
            if run_phase(local, [ph, "--codec", codec, "--s3-package", s3_out,
                                "--s3-out", s3_out], env={},
                         log_prefix=f"{ph}:{codec}"):
                return 1

    # 5. download output_<codec>/ -> <output-dir>/<stem>_<codec>/
    out_dir = Path(args.output_dir)
    for codec in rungs_by_codec:
        dest = out_dir / f"{args.output}_{codec}"
        n = _download_prefix(bucket, f"{out_prefix}/output_{codec}/", dest)
        print(f"[dist] downloaded {n} file(s) -> {dest}", flush=True)

    print("[dist] done", flush=True)
    return 0


def _resolve_plan(args, info):
    """Shared prep: resolve rungs_by_codec + coalesced chunk count from the
    source probe. Used by both the pool DAG and the Temporal plan."""
    ladder_def = get_ladder(args.ladder)
    overrides = {"hevc": parse_bitrate_override(args.bitrate_override_hevc),
                 "h264": parse_bitrate_override(args.bitrate_override_h264), "av1": {}}
    codecs = {"both": ["hevc", "h264"], "all": ["hevc", "h264", "av1"]}.get(
        args.codec, [args.codec])
    rungs_by_codec: dict[str, list[Rung]] = {}
    for codec in codecs:
        rr = select_rungs(ladder_def, codec, args.max_res, info.width, overrides[codec])
        if rr:
            rungs_by_codec[codec] = rr
    chunks = _coalesce_runt_tail(
        plan_chunks(info.duration_s, args.chunk_duration_s, _SEGMENT_DURATION_S))
    return rungs_by_codec, len(chunks)


# Map a Temporal activity_id (set in EncodeWorkflow._phase) back to the UI stage
# key, so activity progress drives the same per-chunk grid the pool backend does.
_ENC_ACT_RE = re.compile(r"^enc-(hevc|h264|av1)-(.+)-c(\d+)$")


def _stage_key_for(activity_id: str) -> str | None:
    if activity_id in ("mezzanine", "audio"):
        return activity_id
    m = _ENC_ACT_RE.match(activity_id)
    if m:
        return f"encode:{m.group(1)}:{m.group(2)}:chunk{m.group(3)}"
    if activity_id.startswith("pkg-"):
        return f"package:{activity_id[4:]}"
    if activity_id.startswith("byteranges-"):
        return f"fragments:{activity_id[len('byteranges-'):]}"
    if activity_id.startswith("hls-"):
        return f"hls:{activity_id[4:]}"
    return None


async def _emit_temporal_progress(handle, EventType, emitted: dict) -> None:
    """Read the workflow history and emit an ENCODER-STAGE marker for each
    activity that changed state (scheduled→running, completed→done). Cheap
    enough at our scale (tens of activities); the Go server scans these off the
    orchestrator's stdout exactly like the single-container path."""
    try:
        hist = await handle.fetch_history()
    except Exception:  # noqa: BLE001 — progress is best-effort, never fail the run
        return
    sched: dict[int, str] = {}
    states: dict[str, str] = {}
    hosts: dict[str, str] = {}   # activity_id -> worker identity (which box ran it)
    for e in hist.events:
        et = e.event_type
        if et == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            aid = e.activity_task_scheduled_event_attributes.activity_id
            sched[e.event_id] = aid
            states.setdefault(aid, "running")
        elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            a = e.activity_task_started_event_attributes
            aid = sched.get(a.scheduled_event_id)
            if aid and a.identity:
                hosts[aid] = a.identity
        elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            aid = sched.get(e.activity_task_completed_event_attributes.scheduled_event_id)
            if aid:
                states[aid] = "done"
    for aid, st in states.items():
        key = _stage_key_for(aid)
        if not key:
            continue
        # Which machine ran this stage → colours the UI chunk/phase plot by box.
        host = hosts.get(aid)
        if host and emitted.get(f"host:{aid}") != host:
            emitted[f"host:{aid}"] = host
            print(f"[[ENCODER-HOST key={key} instance={host}]]", flush=True)
        if emitted.get(aid) != st:
            emitted[aid] = st
            emit_stage(key, st, 100.0 if st == "done" else 0.0)


# Temporal PendingActivityState.STARTED — an activity actually executing on a
# worker right now (vs SCHEDULED = still queued). Stable enum value.
_PA_STARTED = 2


async def _emit_fleet_cpu(handle, client) -> None:
    """Emit one ENCODER-FLEET marker per machine with its live CPU (busy/perf,
    from the workers' heartbeat details) AND the chunks currently RUNNING on it
    (STARTED pending activities, keyed by last_worker_identity). Both come from a
    single describe_workflow — the Temporal-native backchannel, no ssh, no extra
    channel. Best-effort — never fails the run."""
    try:
        desc = await handle.describe()
    except Exception:  # noqa: BLE001
        return
    raw = getattr(desc, "raw_description", None)
    if raw is None:
        return
    pcv = client.data_converter.payload_converter
    agg: dict = {}   # machine -> {"busy", "perf", "chunks": [activity_id, ...]}
    for pa in getattr(raw, "pending_activities", []):
        machine = getattr(pa, "last_worker_identity", "") or ""
        cpu = None
        hb = getattr(pa, "heartbeat_details", None)
        if hb and hb.payloads:
            try:
                vals = pcv.from_payloads(list(hb.payloads))
                cpu = next((v for v in vals if isinstance(v, dict) and "machine" in v), None)
            except Exception:  # noqa: BLE001
                cpu = None
        if cpu and cpu.get("machine"):
            machine = cpu["machine"]
        if not machine:
            continue
        a = agg.setdefault(machine, {"busy": 0, "perf": 0, "chunks": []})
        if cpu:
            a["busy"], a["perf"] = cpu.get("busy", 0), cpu.get("perf", 0)
        aid = getattr(pa, "activity_id", "") or ""
        if getattr(pa, "state", 0) == _PA_STARTED and aid.startswith("enc-"):
            a["chunks"].append(aid)
            # Real per-chunk progress: the worker rides ffmpeg's out_time/duration
            # % on its heartbeat, so a chunk fills 0→100 as it actually encodes
            # instead of snapping 0→100 on completion.
            key = _stage_key_for(aid)
            p = (cpu or {}).get("progress")
            if key and p is not None and 0 < p < 100:
                emit_stage(key, "running", float(p))
    for m, a in agg.items():
        chunks = "|".join(a["chunks"][:16])
        print(f"[[ENCODER-FLEET machine={m} busy={a['busy']} perf={a['perf']} "
              f"chunks={chunks}]]", flush=True)


def run_temporal(args: argparse.Namespace) -> int:
    """Temporal backend: prep locally, then hand the DAG to a durable
    EncodeWorkflow that fans activities across the worker pool. The heavy
    lifting + failover live in Temporal; this just plans, uploads, starts the
    workflow, waits, and pulls the output down."""
    import asyncio
    from temporalio.client import Client

    input_path = Path(args.input)
    if not os.access(input_path, os.R_OK):
        print(f"error: cannot read input: {input_path}", file=sys.stderr)
        return 1
    try:
        info = probe(input_path)
    except ProbeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    bucket = args.s3_bucket
    prefix = args.job_prefix.strip("/")
    src_key = f"{prefix}/input{input_path.suffix}"
    rungs_by_codec, n_chunks = _resolve_plan(args, info)
    if not rungs_by_codec:
        print("error: no ladder rungs fit this source", file=sys.stderr)
        return 1

    _emit_plan(rungs_by_codec,
               {(c, r.label): n_chunks for c, rr in rungs_by_codec.items() for r in rr},
               info.has_audio)
    _emit_commercial_cost(rungs_by_codec, info, input_path)
    _upload_source(input_path, bucket, src_key)

    two_pass = {"hevc": not args.hevc_single_pass, "h264": False, "av1": False}
    plan = {
        "bucket": bucket, "job_prefix": prefix, "src_key": src_key,
        "has_audio": info.has_audio, "chunk_duration_s": args.chunk_duration_s,
        "n_chunks": n_chunks,
        "codecs": {c: {"two_pass": two_pass[c],
                       "rungs": [{"label": r.label, "width": r.width,
                                  "height": r.height, "bitrate": r.bitrate} for r in rr]}
                   for c, rr in rungs_by_codec.items()},
    }

    async def go():
        from temporalio.api.enums.v1 import EventType
        client = await Client.connect(args.temporal_address)
        wid = f"encode-{prefix.replace('/', '-')}"
        print(f"[dist] starting workflow {wid} on {args.temporal_address}", flush=True)
        handle = await client.start_workflow(
            "EncodeWorkflow", plan, id=wid, task_queue=args.temporal_task_queue)

        # Cancel plumbing: the Go control plane cancels a job by `docker stop`-ing
        # THIS orchestrator container (SIGTERM + 30s grace). The encode DAG is a
        # durable Temporal workflow that outlives us — if we just exit, it keeps
        # dispatching chunks to the remote workers. So on SIGTERM we cancel the
        # workflow; the Temporal server then delivers cancellation to every running
        # activity (each kills its ffmpeg via activity.is_cancelled()) and stops
        # scheduling new ones — even after we're gone. That's what actually stops
        # the remote chunk encodes.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass

        # Await the result while streaming per-activity progress to stdout as
        # ENCODER-STAGE markers (drives the UI chunk grid live).
        result_task = asyncio.ensure_future(handle.result())
        emitted: dict = {}
        while not result_task.done():
            if stop.is_set():
                print("[dist] cancel requested — cancelling workflow "
                      "(stops remote chunk encodes)", flush=True)
                try:
                    await handle.cancel()
                except Exception as e:  # noqa: BLE001
                    print(f"[dist] workflow cancel failed: {e}", flush=True)
                result_task.cancel()
                return "cancelled"
            await _emit_temporal_progress(handle, EventType, emitted)
            await _emit_fleet_cpu(handle, client)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)  # 1s progress cadence
            except asyncio.TimeoutError:
                pass
        await _emit_temporal_progress(handle, EventType, emitted)
        return await result_task

    try:
        result = asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        print(f"[dist] workflow failed: {e}", file=sys.stderr)
        return 1
    if result == "cancelled":
        print("[dist] cancelled — skipping output download", flush=True)
        return 130
    print(f"[dist] workflow result: {result}", flush=True)

    out_dir = Path(args.output_dir)
    for codec in rungs_by_codec:
        dest = out_dir / f"{args.output}_{codec}"
        n = _download_prefix(bucket, f"{prefix}/out/output_{codec}/", dest)
        print(f"[dist] downloaded {n} file(s) -> {dest}", flush=True)
    print("[dist] done", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli_local_dist")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True, help="output stem")
    p.add_argument("--output-dir", required=True, dest="output_dir")
    p.add_argument("--codec", default="hevc")
    p.add_argument("--ladder", default="apple-uniq-live")
    p.add_argument("--max-res", default=None, dest="max_res")
    p.add_argument("--hevc-single-pass", action="store_true", dest="hevc_single_pass")
    p.add_argument("--bitrate-override-hevc", default=None, dest="bitrate_override_hevc")
    p.add_argument("--bitrate-override-h264", default=None, dest="bitrate_override_h264")
    p.add_argument("--chunk-duration", type=float, default=12.0,
                   dest="chunk_duration_s", help="seconds; multiple of segment (6)")
    p.add_argument("--encode-threads", type=int, default=0, dest="encode_threads",
                   help="threads/encode → slot sizing (0=2)")
    # MinIO
    p.add_argument("--s3-bucket", required=True, dest="s3_bucket")
    p.add_argument("--job-prefix", required=True, dest="job_prefix",
                   help="MinIO key prefix for this job's objects")
    # Pool
    p.add_argument("--worker", action="append", default=None,
                   help="repeatable: 'local' or 'ssh://user@host'")
    p.add_argument("--image-local", default="encoder:latest", dest="image_local")
    p.add_argument("--image-remote", default="ghcr.io/jonathaneoliver/encoder:latest",
                   dest="image_remote")
    p.add_argument("--remote-code-mount", default=None, dest="remote_code_mount",
                   help="host path to mount over /app/scripts/encoder on remote "
                        "workers (for a stale remote image)")
    # Backend: 'pool' = hand-rolled docker/ssh pool; 'temporal' = durable
    # EncodeWorkflow across Temporal workers.
    p.add_argument("--backend", choices=("pool", "temporal"), default="pool")
    p.add_argument("--temporal-address", default="127.0.0.1:7233",
                   dest="temporal_address")
    p.add_argument("--temporal-task-queue", default="encode",
                   dest="temporal_task_queue")
    return p


def main() -> int:
    args = build_parser().parse_args()
    return run_temporal(args) if args.backend == "temporal" else run(args)


if __name__ == "__main__":
    sys.exit(main())
