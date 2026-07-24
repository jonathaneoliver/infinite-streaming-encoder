"""Where-did-the-time-go report for a cloud-batch execution.

For every Batch job in the execution it combines two sources:

  from Batch job metadata:
    startup = startedAt - createdAt    (queue + instance bringup + image pull)
    runtime = stoppedAt - startedAt    (the container: fetch + encode + upload)

  from the container's [[ENCODER-TIMING]] line in CloudWatch:
    fetch / probe / encode / upload

Jobs are grouped by the EC2 instance they ran on, and the first job on each
instance is flagged "cold" (it paid the image pull) vs "warm" — so you can
see how much of the wall-clock is machine bringup / image load vs real encode.

  python3 -m infinite_streaming_encoder.cloud.timing --execution-arn <arn> [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from botocore.exceptions import ClientError

from infinite_streaming_encoder.cloud.aws import batch_client, region

try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore

_BATCH_LOG_GROUP = "/aws/batch/encoder"
_TIMING_RE = re.compile(r"\[\[ENCODER-TIMING (.+?)\]\]")


def _logs():
    return boto3.client("logs", region_name=region())


def _exec_name(execution_arn: str) -> str:
    return execution_arn.rsplit(":", 1)[-1]


def _jobs_for_execution(exec_name: str) -> list[dict]:
    """Every Batch job whose name carries this execution's name. After a run
    they're mostly SUCCEEDED, some FAILED."""
    batch = batch_client()
    queue = os.environ.get("BATCH_JOB_QUEUE", "encoder-queue")
    ids: list[str] = []
    for status in ("SUCCEEDED", "FAILED"):
        for j in batch.list_jobs(jobQueue=queue, jobStatus=status).get("jobSummaryList", []):
            if exec_name in j.get("jobName", ""):
                ids.append(j["jobId"])
    out: list[dict] = []
    # describe_jobs takes up to 100 ids per call.
    for i in range(0, len(ids), 100):
        for j in batch.describe_jobs(jobs=ids[i:i + 100]).get("jobs", []):
            c = j.get("container", {})
            out.append({
                "name": j.get("jobName", ""),
                "created": j.get("createdAt"),   # epoch millis
                "started": j.get("startedAt"),
                "stopped": j.get("stoppedAt"),
                "instance": c.get("containerInstanceArn", ""),
                "log_stream": c.get("logStreamName"),
                "exit": c.get("exitCode"),
            })
    return out


def _container_timing(log_stream: str | None) -> dict[str, float]:
    """Parse the [[ENCODER-TIMING …]] line (fetch/encode/upload/…) from the
    container's CloudWatch stream, if present."""
    if not log_stream:
        return {}
    try:
        events = _logs().get_log_events(
            logGroupName=_BATCH_LOG_GROUP, logStreamName=log_stream,
            limit=200, startFromHead=False,
        ).get("events", [])
    except ClientError:
        return {}
    for e in reversed(events):
        m = _TIMING_RE.search(e.get("message", ""))
        if m:
            out = {}
            for tok in m.group(1).split():
                if tok.endswith("_s") is False and "_s=" in tok:
                    k, v = tok.split("=", 1)
                    if k.endswith("_s"):
                        try:
                            out[k[:-2]] = float(v)
                        except ValueError:
                            pass
            return out
    return {}


def _secs(a, b) -> float | None:
    if a is None or b is None:
        return None
    return (b - a) / 1000.0


def collect(execution_arn: str) -> dict:
    jobs = _jobs_for_execution(_exec_name(execution_arn))
    jobs.sort(key=lambda j: (j["instance"], j.get("started") or 0))

    # First job on each instance paid the image pull → "cold".
    first_on_instance: set[str] = set()
    seen_instances: set[str] = set()
    for j in jobs:
        inst = j["instance"]
        if inst and inst not in seen_instances:
            seen_instances.add(inst)
            first_on_instance.add(j["name"])

    rows = []
    for j in jobs:
        startup = _secs(j["created"], j["started"])
        runtime = _secs(j["started"], j["stopped"])
        ct = _container_timing(j["log_stream"])
        rows.append({
            "name": j["name"],
            "cold": j["name"] in first_on_instance,
            "startup_s": round(startup, 1) if startup is not None else None,
            "runtime_s": round(runtime, 1) if runtime is not None else None,
            "fetch_s": ct.get("fetch"),
            "encode_s": ct.get("encode"),
            "upload_s": ct.get("upload"),
            "exit": j["exit"],
        })

    def _avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    cold = [r for r in rows if r["cold"]]
    warm = [r for r in rows if not r["cold"]]
    return {
        "region": region(),
        "instances_used": len(seen_instances),
        "job_count": len(rows),
        "jobs": rows,
        "avg": {
            "startup_cold_s": _avg([r["startup_s"] for r in cold]),
            "startup_warm_s": _avg([r["startup_s"] for r in warm]),
            "fetch_s": _avg([r["fetch_s"] for r in rows]),
            "encode_s": _avg([r["encode_s"] for r in rows]),
            "upload_s": _avg([r["upload_s"] for r in rows]),
        },
    }


def _print_human(d: dict) -> None:
    print(f"Timing report ({d['region']}) — {d['job_count']} jobs on "
          f"{d['instances_used']} instance(s)\n")
    hdr = f"{'job':<34} {'cold':<5} {'startup':>8} {'fetch':>6} {'encode':>7} {'upload':>7}"
    print(hdr)
    print("-" * len(hdr))
    for r in d["jobs"]:
        def f(v):
            return f"{v:.1f}" if isinstance(v, (int, float)) else "-"
        print(f"{r['name'][:34]:<34} {'yes' if r['cold'] else '':<5} "
              f"{f(r['startup_s']):>8} {f(r['fetch_s']):>6} "
              f"{f(r['encode_s']):>7} {f(r['upload_s']):>7}")
    a = d["avg"]
    print("\nAverages (startup = queue-wait + instance bringup + image pull):")
    print(f"  startup, cold (first job on an instance): {a['startup_cold_s']}s")
    print(f"  startup, warm (image already cached):     {a['startup_warm_s']}s")
    print(f"  mezzanine fetch:                          {a['fetch_s']}s")
    print(f"  encode (incl. both passes):               {a['encode_s']}s")
    print(f"  upload:                                   {a['upload_s']}s")
    cold_s, warm_s = a["startup_cold_s"], a["startup_warm_s"]
    if cold_s and warm_s:
        # If cold ~= warm, the image pull isn't the driver — the wait is
        # queue-time (limited concurrency). If cold >> warm, the pull dominates.
        pull_est = max(0.0, cold_s - warm_s)
        print(f"\n  cold - warm ~= {pull_est:.0f}s → image-pull's share of startup; "
              f"the rest is queue-wait + instance bringup.")
        if pull_est < 15:
            print("  (cold ~= warm ⇒ pull is NOT the bottleneck; it's queue-wait "
                  "from limited concurrency — raise max vCPUs or right-size jobs.)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="infinite_streaming_encoder.cloud.timing")
    p.add_argument("--execution-arn", required=True, dest="execution_arn")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        data = collect(args.execution_arn)
    except ClientError as e:
        print(json.dumps({"error": str(e)}) if args.json else f"error: {e}",
              file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        _print_human(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
