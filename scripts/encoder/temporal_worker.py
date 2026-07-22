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
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "encode")


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
    cmd = ["python3", "-m", "encoder.cli_phase", *spec["args"]]
    proc = subprocess.Popen(cmd, text=True, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    last = ""
    try:
        for line in proc.stdout:
            last = line.rstrip("\n")
            if last.startswith("[[ENCODER"):
                print(last, flush=True)
            activity.heartbeat(last[:180])
        rc = proc.wait()
    finally:
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

        await self._phase(["mezzanine", "--s3-in", f"s3://{b}/{plan['src_key']}",
                           "--s3-out", s3_work], {}, "mezzanine")
        if plan.get("has_audio"):
            await self._phase(["audio", "--s3-mezz", s3_work, "--s3-out", s3_work],
                              {}, "audio")

        cd = plan["chunk_duration_s"]
        n = plan["n_chunks"]
        chunk_acts = []
        for codec, ci in plan["codecs"].items():
            tp = ci["two_pass"]
            for r in ci["rungs"]:
                for i in range(n):
                    args = ["variant", "--codec", codec, "--label", r["label"],
                            "--width", str(r["width"]), "--height", str(r["height"]),
                            "--bitrate", str(r["bitrate"]), "--chunk-index", str(i),
                            "--s3-mezz", s3_work, "--s3-out", s3_work]
                    if tp:
                        args.append("--two-pass")
                    env = {"CHUNK_DURATION_S": str(cd), "COALESCE_RUNT_TAIL": "1",
                           "TWO_PASS": "1" if tp else "0", "ENCODE_THREADS": "2"}
                    chunk_acts.append(self._phase(
                        args, env, f"enc-{codec}-{r['label']}-c{i}"))
        await asyncio.gather(*chunk_acts)

        for codec in plan["codecs"]:
            await self._phase(["package-all", "--codec", codec, "--s3-variants",
                              s3_work, "--s3-audio", s3_work, "--s3-out", s3_out],
                              {}, f"pkg-{codec}")
            for ph in ("byteranges", "hls"):
                await self._phase([ph, "--codec", codec, "--s3-package", s3_out,
                                  "--s3-out", s3_out], {}, f"{ph}-{codec}")
        return "done"

    async def _phase(self, args, env, act_id):
        return await workflow.execute_activity(
            "EncodePhase", {"args": args, "env": env},
            start_to_close_timeout=timedelta(hours=1),
            heartbeat_timeout=timedelta(seconds=90),
            retry_policy=_RETRY, activity_id=act_id)


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    slots = int(os.environ.get("ENCODE_SLOTS", "0")) or max(1, (os.cpu_count() or 4) // 2)
    client = await Client.connect(address)
    print(f"[temporal-worker] connected {address} queue={TASK_QUEUE} slots={slots}",
          flush=True)
    with ThreadPoolExecutor(max_workers=slots) as pool:
        worker = Worker(
            client, task_queue=TASK_QUEUE,
            activities=[encode_phase],
            workflows=[TestChunkWorkflow, EncodeWorkflow],
            activity_executor=pool, max_concurrent_activities=slots,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
