"""Read-only inventory of every AWS resource this tool has tagged.

Answers "what's running in AWS that will cost me money right now?"
The Go server calls this via `python3 -m encoder.cloud.inventory --json`
and surfaces the result in the AWS tab.

All queries scope to `Application=encoder-app` — user-owned resources
are never included.

Output shape (JSON):

    {
      "fetched_at": "2026-04-20T16:48:42Z",
      "region": "us-west-2",
      "instances": [{id, type, state, job_id, launched_at, age_seconds,
                     estimated_hourly_usd, subnet, availability_zone}],
      "spot_requests": [{id, state, instance_id, job_id, launched_at}],
      "volumes":  [{id, state, size_gib, job_id, attached_to,
                    is_orphan, created_at}],
      "s3_prefixes": [{prefix, object_count, size_bytes, job_id}],
      "summary": {running_instances, orphan_volumes, total_s3_bytes,
                  estimated_hourly_usd}
    }

Costs are approximate. Spot pricing is noisy per-AZ; we use the most
recent spot price when available and fall back to a hardcoded
on-demand table otherwise. Good enough for "am I leaking money?",
not the AWS billing console.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

from encoder.cloud.aws import (
    APP_TAG_KEY, APP_TAG_VALUE, app_tag_filter, batch_client, ec2_client,
    region, s3_client, sfn_client,
)


# Rough on-demand hourly rates for c-family compute-optimised, us-west-2.
# Same table the bash script used; approximate (±15%) but fine for the
# "is this leaking?" readout. Unknown types fall through to 0.
_OD_HOURLY_USD: dict[str, float] = {
    "c7i.large":    0.0892,
    "c7i.xlarge":   0.1785,
    "c7i.2xlarge":  0.3570,
    "c7i.4xlarge":  0.7140,
    "c7i.8xlarge":  1.4280,
    "c7i.12xlarge": 2.1420,
    "c7i.16xlarge": 2.8560,
    "c7i.24xlarge": 4.2840,
    "c7a.xlarge":   0.1530,
    "c7a.4xlarge":  0.6120,
    "c7a.8xlarge":  1.2240,
    "c6i.4xlarge":  0.6800,
    "c6i.8xlarge":  1.3600,
}


def _tag_value(tags: list[dict], key: str) -> str | None:
    for t in tags or []:
        if t.get("Key") == key:
            return t.get("Value")
    return None


def _age_seconds(iso_or_datetime) -> float:
    if isinstance(iso_or_datetime, datetime):
        dt = iso_or_datetime
    else:
        try:
            dt = datetime.fromisoformat(str(iso_or_datetime).replace("Z", "+00:00"))
        except Exception:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _spot_price(ec2, instance_type: str, az: str | None) -> float | None:
    """Most recent spot price for an (instance_type, az) combo. None on failure."""
    try:
        resp = ec2.describe_spot_price_history(
            InstanceTypes=[instance_type],
            ProductDescriptions=["Linux/UNIX"],
            AvailabilityZone=az or "",
            MaxResults=1,
        )
    except ClientError:
        return None
    history = resp.get("SpotPriceHistory", [])
    if not history:
        return None
    try:
        return float(history[0]["SpotPrice"])
    except (KeyError, ValueError):
        return None


def _estimate_hourly(ec2, instance: dict) -> float:
    instance_type = instance.get("InstanceType", "")
    is_spot = instance.get("InstanceLifecycle") == "spot"
    az = instance.get("Placement", {}).get("AvailabilityZone")
    if is_spot:
        sp = _spot_price(ec2, instance_type, az)
        if sp is not None:
            return sp
    return _OD_HOURLY_USD.get(instance_type, 0.0)


# ---------------------------------------------------------------------------
# EC2 / EBS / Spot
# ---------------------------------------------------------------------------

def _describe_app_instances(ec2) -> list[dict]:
    filters = list(app_tag_filter())
    # Include terminated too so the UI can show a recent history,
    # but the summary only counts live ones.
    resp = ec2.describe_instances(Filters=filters)
    out: list[dict] = []
    for r in resp.get("Reservations", []):
        out.extend(r.get("Instances", []))
    return out


def _describe_app_volumes(ec2) -> list[dict]:
    resp = ec2.describe_volumes(Filters=list(app_tag_filter()))
    return resp.get("Volumes", [])


def _describe_app_spot_requests(ec2) -> list[dict]:
    resp = ec2.describe_spot_instance_requests(Filters=list(app_tag_filter()))
    return resp.get("SpotInstanceRequests", [])


def _instance_view(ec2, instance: dict) -> dict[str, Any]:
    state = instance.get("State", {}).get("Name", "unknown")
    is_live = state in ("pending", "running", "stopping", "stopped")
    launched = instance.get("LaunchTime")
    age = _age_seconds(launched) if is_live else 0.0
    hourly = _estimate_hourly(ec2, instance) if is_live else 0.0
    return {
        "id": instance["InstanceId"],
        "type": instance.get("InstanceType"),
        "state": state,
        "lifecycle": instance.get("InstanceLifecycle", "on-demand"),
        "job_id": _tag_value(instance.get("Tags", []), "JobId"),
        "launched_at": launched.isoformat() if launched else None,
        "age_seconds": age,
        "availability_zone": instance.get("Placement", {}).get("AvailabilityZone"),
        "subnet": instance.get("SubnetId"),
        "estimated_hourly_usd": round(hourly, 4),
    }


def _volume_view(volume: dict) -> dict[str, Any]:
    attachments = volume.get("Attachments", []) or []
    attached_to = attachments[0].get("InstanceId") if attachments else None
    return {
        "id": volume["VolumeId"],
        "state": volume.get("State"),
        "size_gib": volume.get("Size"),
        "job_id": _tag_value(volume.get("Tags", []), "JobId"),
        "attached_to": attached_to,
        # "available" means not attached to anything — that's our leak case
        "is_orphan": volume.get("State") == "available",
        "created_at": volume.get("CreateTime").isoformat() if volume.get("CreateTime") else None,
    }


def _spot_view(req: dict) -> dict[str, Any]:
    return {
        "id": req["SpotInstanceRequestId"],
        "state": req.get("State"),
        "status": req.get("Status", {}).get("Code"),
        "instance_id": req.get("InstanceId"),
        "job_id": _tag_value(req.get("Tags", []), "JobId"),
        "create_time": req.get("CreateTime").isoformat() if req.get("CreateTime") else None,
    }


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

def _s3_prefix_inventory(bucket: str | None) -> list[dict[str, Any]]:
    """Per-job S3 prefix sizes under s3://<bucket>/jobs/."""
    if not bucket:
        return []
    s3 = s3_client()
    paginator = s3.get_paginator("list_objects_v2")

    by_prefix: dict[str, dict[str, int]] = {}
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix="jobs/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # keys look like jobs/<job_id>/input/clip.mp4 etc.
                parts = key.split("/", 2)
                if len(parts) < 2:
                    continue
                prefix = f"{parts[0]}/{parts[1]}/"
                bucket_stats = by_prefix.setdefault(prefix,
                                                   {"count": 0, "bytes": 0})
                bucket_stats["count"] += 1
                bucket_stats["bytes"] += obj.get("Size", 0)
    except ClientError as e:
        return [{"prefix": f"s3://{bucket}/jobs/", "error": str(e)}]

    out: list[dict[str, Any]] = []
    for prefix, stats in sorted(by_prefix.items()):
        parts = prefix.strip("/").split("/")
        job_id = parts[1] if len(parts) >= 2 else None
        out.append({
            "prefix": f"s3://{bucket}/{prefix}",
            "object_count": stats["count"],
            "size_bytes": stats["bytes"],
            "job_id": job_id,
        })
    return out


# ---------------------------------------------------------------------------
# Cloud-batch: Step Functions executions + Batch jobs
#
# These are the controllable units of a cloud-batch run. Stopping an execution
# aborts its jobs, and terminating jobs lets the Batch compute environment
# scale its spot instances back to zero — so surfacing (and releasing) these in
# the AWS tab is what stops a run from quietly burning money.
# ---------------------------------------------------------------------------

# Non-terminal Batch job statuses worth showing (they hold or want capacity).
_ACTIVE_BATCH_STATUSES = ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING")


def _executions() -> list[dict[str, Any]]:
    """Running + recently-finished executions of the encoder state machine.
    Scoped to STATE_MACHINE_ARN; empty when the cloud-batch target isn't
    configured."""
    arn = os.environ.get("STATE_MACHINE_ARN")
    if not arn:
        return []
    sfn = sfn_client()
    out: list[dict[str, Any]] = []
    try:
        # Running first (the ones that cost money), then a few recent for context.
        seen = set()
        for status in ("RUNNING", None):
            kwargs = {"stateMachineArn": arn, "maxResults": 20}
            if status:
                kwargs["statusFilter"] = status
            for ex in sfn.list_executions(**kwargs).get("executions", []):
                if ex["executionArn"] in seen:
                    continue
                seen.add(ex["executionArn"])
                out.append({
                    "arn": ex["executionArn"],
                    "name": ex["name"],
                    "status": ex["status"],
                    "started_at": ex.get("startDate").isoformat() if ex.get("startDate") else None,
                    "age_seconds": _age_seconds(ex.get("startDate")),
                })
    except ClientError as e:
        return [{"error": str(e)}]
    # Running on top, then most-recent.
    out.sort(key=lambda e: (e.get("status") != "RUNNING", e.get("age_seconds", 0)))
    return out[:25]


def _batch_jobs() -> list[dict[str, Any]]:
    """Active jobs on the encoder Batch queue (any non-terminal status)."""
    queue = os.environ.get("BATCH_JOB_QUEUE", "encoder-queue")
    batch = batch_client()
    out: list[dict[str, Any]] = []
    try:
        for status in _ACTIVE_BATCH_STATUSES:
            for j in batch.list_jobs(jobQueue=queue, jobStatus=status).get("jobSummaryList", []):
                created = j.get("createdAt")  # epoch millis
                out.append({
                    "id": j["jobId"],
                    "name": j.get("jobName", ""),
                    "status": j.get("status", status),
                    "created_at": created,
                    "age_seconds": (
                        (datetime.now(timezone.utc).timestamp() - created / 1000.0)
                        if created else 0.0
                    ),
                })
    except ClientError as e:
        return [{"error": str(e)}]
    return out


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def collect() -> dict[str, Any]:
    ec2 = ec2_client()

    instances_raw = _describe_app_instances(ec2)
    instances = [_instance_view(ec2, i) for i in instances_raw]

    volumes = [_volume_view(v) for v in _describe_app_volumes(ec2)]
    spot_requests = [_spot_view(r) for r in _describe_app_spot_requests(ec2)]

    bucket = os.environ.get("S3_BUCKET") or None
    s3_prefixes = _s3_prefix_inventory(bucket)

    executions = _executions()
    batch_jobs = _batch_jobs()

    running_instances = [i for i in instances if i["state"] == "running"]
    orphan_volumes = [v for v in volumes if v["is_orphan"]]
    total_s3_bytes = sum(p.get("size_bytes", 0) for p in s3_prefixes)
    hourly_total = sum(i["estimated_hourly_usd"] for i in running_instances)
    running_executions = [e for e in executions if e.get("status") == "RUNNING"]
    active_batch_jobs = [j for j in batch_jobs if not j.get("error")]

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "region": region(),
        "app_tag": {"key": APP_TAG_KEY, "value": APP_TAG_VALUE},
        "s3_bucket": bucket,
        "instances": instances,
        "spot_requests": spot_requests,
        "volumes": volumes,
        "s3_prefixes": s3_prefixes,
        "executions": executions,
        "batch_jobs": batch_jobs,
        "summary": {
            "running_instances": len(running_instances),
            "orphan_volumes": len(orphan_volumes),
            "total_s3_bytes": total_s3_bytes,
            "estimated_hourly_usd": round(hourly_total, 4),
            "running_executions": len(running_executions),
            "active_batch_jobs": len(active_batch_jobs),
        },
    }


def _main() -> int:
    import argparse
    p = argparse.ArgumentParser(prog="encoder.cloud.inventory")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON (default: text summary)")
    args = p.parse_args()

    try:
        data = collect()
    except ClientError as e:
        print(json.dumps({"error": str(e)}) if args.json else f"[inventory] error: {e}",
              file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    # Text summary
    s = data["summary"]
    print(f"[inventory] region={data['region']} fetched_at={data['fetched_at']}")
    print(f"  running instances:  {s['running_instances']}")
    print(f"  orphan volumes:     {s['orphan_volumes']}")
    print(f"  S3 staged:          {s['total_s3_bytes'] / (1024**3):.2f} GiB "
          f"({sum(p.get('object_count', 0) for p in data['s3_prefixes'])} objects)")
    print(f"  est hourly spend:   ${s['estimated_hourly_usd']:.2f}")
    for i in data["instances"]:
        if i["state"] != "running":
            continue
        print(f"    {i['id']} {i['type']:<15s} {i['lifecycle']:<10s} "
              f"{i['age_seconds']/60:5.1f}m job={i['job_id'] or '?'}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
