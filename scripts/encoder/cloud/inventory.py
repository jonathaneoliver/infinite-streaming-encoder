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
from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import ClientError

from encoder.cloud.aws import (
    APP_TAG_KEY, APP_TAG_VALUE, app_tag_filter, batch_client, cloudwatch_client,
    ec2_client, ecs_client, region, s3_client, sfn_client,
)


# vCPU by instance-size suffix (family-independent for the c/m/r/g families we
# use). Lets us compute each box's capacity from its type without an API call.
_VCPU_BY_SIZE = {
    "medium": 1, "large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16,
    "8xlarge": 32, "12xlarge": 48, "16xlarge": 64, "24xlarge": 96,
    "48xlarge": 192, "metal": 64,
}


def _vcpus_for_type(itype: str | None) -> int:
    if not itype or "." not in itype:
        return 0
    return _VCPU_BY_SIZE.get(itype.split(".", 1)[1], 0)


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
    """Per-prefix S3 sizes under s3://<bucket>/jobs/ (per-job staging) AND
    s3://<bucket>/mezz/ (the source-keyed mezzanine cache) — so the cached
    mezzanines are visible in the S3 Staging view and can be deleted there."""
    if not bucket:
        return []
    s3 = s3_client()
    paginator = s3.get_paginator("list_objects_v2")

    by_prefix: dict[str, dict[str, int]] = {}
    for root in ("jobs/", "mezz/"):
        try:
            for page in paginator.paginate(Bucket=bucket, Prefix=root):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    # keys look like jobs/<job_id>/input/clip.mp4 or
                    # mezz/<source_key>/mezzanine.mp4 — group by the first two.
                    parts = key.split("/", 2)
                    if len(parts) < 2:
                        continue
                    prefix = f"{parts[0]}/{parts[1]}/"
                    bucket_stats = by_prefix.setdefault(prefix,
                                                       {"count": 0, "bytes": 0})
                    bucket_stats["count"] += 1
                    bucket_stats["bytes"] += obj.get("Size", 0)
        except ClientError:
            continue  # one root failing shouldn't hide the other

    out: list[dict[str, Any]] = []
    for prefix, stats in sorted(by_prefix.items()):
        parts = prefix.strip("/").split("/")
        job_id = parts[1] if len(parts) >= 2 else None
        out.append({
            "prefix": f"s3://{bucket}/{prefix}",
            "object_count": stats["count"],
            "size_bytes": stats["bytes"],
            "job_id": job_id,
            "kind": parts[0],  # "jobs" | "mezz" — UI labels the cache distinctly
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


# Sort order for the active-jobs list: actively-working statuses first, then
# by most-recent state change — so running/just-changed encodes surface at the
# top and the list reads as "what's happening now".
_BATCH_STATUS_RANK = {"RUNNING": 0, "STARTING": 1, "RUNNABLE": 2, "PENDING": 3, "SUBMITTED": 4}


def _batch_jobs() -> list[dict[str, Any]]:
    """Active jobs on the encoder Batch queue (any non-terminal status),
    ordered most-recently-changed first within status."""
    queue = os.environ.get("BATCH_JOB_QUEUE", "encoder-queue")
    batch = batch_client()
    out: list[dict[str, Any]] = []
    try:
        for status in _ACTIVE_BATCH_STATUSES:
            for j in batch.list_jobs(jobQueue=queue, jobStatus=status).get("jobSummaryList", []):
                created = j.get("createdAt")  # epoch millis
                started = j.get("startedAt")
                stopped = j.get("stoppedAt")
                # Latest known state-transition time (createdAt for a queued job,
                # startedAt once running, stoppedAt if it just terminated).
                changed = max([t for t in (created, started, stopped) if t] or [0])
                st = j.get("status", status)
                out.append({
                    "id": j["jobId"],
                    "name": j.get("jobName", ""),
                    "status": st,
                    "created_at": created,
                    "started_at": started,
                    "changed_at": changed,
                    "age_seconds": (
                        (datetime.now(timezone.utc).timestamp() - created / 1000.0)
                        if created else 0.0
                    ),
                })
    except ClientError as e:
        return [{"error": str(e)}]
    # Active statuses first, then most-recent change at the top.
    out.sort(key=lambda j: (_BATCH_STATUS_RANK.get(j["status"], 9), -(j.get("changed_at") or 0)))
    return out


# ---------------------------------------------------------------------------
# Fleet packing (job -> instance) + 24h spend tracking
# ---------------------------------------------------------------------------

def _enrich_fleet(batch_jobs: list[dict], instances: list[dict]) -> dict[str, Any]:
    """Resolve each active job's vCPU + the EC2 instance it runs on, annotate
    the instances with busy/idle vCPU + their job list, and return a fleet
    summary. Best-effort: any AWS error just omits the packing detail (the
    fleet gauge still works from running-job vCPU).

    Mapping: job -> ECS container instance (batch.describe_jobs) -> EC2
    instance (ecs.describe_container_instances).
    """
    active = [j for j in batch_jobs if not j.get("error")]
    ids = [j["id"] for j in active]
    running = [i for i in instances if i.get("state") == "running"]
    for inst in instances:
        inst["vcpus"] = _vcpus_for_type(inst.get("type"))
    if not ids:
        cap = sum(i["vcpus"] for i in running)
        return {"total_vcpus": cap, "used_vcpus": 0, "idle_vcpus": cap,
                "utilization": 0.0, "instance_count": len(running),
                "running_jobs": 0, "queued_jobs": 0}

    by_id = {j["id"]: j for j in active}
    arns_by_cluster: dict[str, set] = {}
    try:
        batch = batch_client()
        for i in range(0, len(ids), 100):
            for jd in batch.describe_jobs(jobs=ids[i:i + 100]).get("jobs", []):
                jm = by_id.get(jd.get("jobId"))
                if not jm:
                    continue
                cont = jd.get("container", {}) or {}
                vcpu = 0
                for rr in cont.get("resourceRequirements", []) or []:
                    if rr.get("type") == "VCPU":
                        try:
                            vcpu = int(float(rr.get("value", 0)))
                        except (TypeError, ValueError):
                            vcpu = 0
                if not vcpu:
                    try:
                        vcpu = int(cont.get("vcpus") or 0)
                    except (TypeError, ValueError):
                        vcpu = 0
                jm["vcpu"] = vcpu
                arn = cont.get("containerInstanceArn")
                if arn and arn.count("/") >= 2:
                    cluster = arn.split("/")[-2]
                    arns_by_cluster.setdefault(cluster, set()).add(arn)
                    jm["_ci_arn"] = arn
    except ClientError:
        pass

    ci_to_ec2: dict[str, str] = {}
    try:
        ecs = ecs_client()
        for cluster, arns in arns_by_cluster.items():
            al = list(arns)
            for i in range(0, len(al), 100):
                for ci in ecs.describe_container_instances(
                        cluster=cluster, containerInstances=al[i:i + 100]
                ).get("containerInstances", []):
                    ci_to_ec2[ci.get("containerInstanceArn")] = ci.get("ec2InstanceId")
    except ClientError:
        pass

    used_by_inst: dict[str, int] = {}
    jobs_by_inst: dict[str, list] = {}
    for jm in active:
        arn = jm.pop("_ci_arn", None)
        iid = ci_to_ec2.get(arn) if arn else None
        if iid:
            jm["instance_id"] = iid
            used_by_inst[iid] = used_by_inst.get(iid, 0) + (jm.get("vcpu") or 0)
            jobs_by_inst.setdefault(iid, []).append(
                {"name": jm.get("name"), "vcpu": jm.get("vcpu"), "status": jm.get("status")})

    for inst in running:
        cap = inst["vcpus"]
        used = used_by_inst.get(inst["id"], 0)
        inst["used_vcpus"] = min(used, cap) if cap else used
        inst["idle_vcpus"] = max(0, cap - used)
        inst["jobs"] = sorted(jobs_by_inst.get(inst["id"], []),
                              key=lambda j: -(j.get("vcpu") or 0))

    cap_total = sum(i["vcpus"] for i in running)
    # Fleet gauge uses running-job vCPU directly (robust even if the ECS
    # mapping is unavailable), capped at capacity.
    running_vcpu = sum((j.get("vcpu") or 0) for j in active if j.get("status") == "RUNNING")
    used_total = min(running_vcpu, cap_total) if cap_total else running_vcpu
    return {
        "total_vcpus": cap_total,
        "used_vcpus": used_total,
        "idle_vcpus": max(0, cap_total - used_total),
        "utilization": round(used_total / cap_total, 3) if cap_total else 0.0,
        "instance_count": len(running),
        "running_jobs": sum(1 for j in active if j.get("status") == "RUNNING"),
        "queued_jobs": sum(1 for j in active if j.get("status") in ("SUBMITTED", "PENDING", "RUNNABLE")),
    }


def _ecs_cluster_name() -> str | None:
    """Container-Insights ClusterName for the encoder Batch compute env."""
    try:
        from encoder.cloud.compute_env import _encoder_ce
        ce = _encoder_ce()
        if not ce:
            return None
        ces = batch_client().describe_compute_environments(
            computeEnvironments=[ce]).get("computeEnvironments", [])
        arn = ces[0].get("ecsClusterArn") if ces else None
        return arn.rsplit("/", 1)[-1] if arn else None
    except ClientError:
        return None


def _fleet_cw_series(cluster_name: str) -> dict:
    """2h of ACTUAL fleet CPU (cores) + memory (GiB) from Container Insights, as
    sparkline series — the real usage that complements the self-tracked
    allocated-vCPU history (shows the allocated-vs-used gap live). Needs
    Container Insights enabled on the cluster; best-effort, empty on any error.
    CpuUtilized is in CPU units (1024 = 1 vCPU); MemoryUtilized is MiB."""
    cw = cloudwatch_client()
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=2)
    out: dict[str, list[float]] = {"cpu_cores": [], "mem_gib": []}
    for metric, key, div in (("CpuUtilized", "cpu_cores", 1024.0),
                             ("MemoryUtilized", "mem_gib", 1024.0)):
        try:
            dp = cw.get_metric_statistics(
                Namespace="ECS/ContainerInsights", MetricName=metric,
                Dimensions=[{"Name": "ClusterName", "Value": cluster_name}],
                StartTime=start, EndTime=now, Period=300, Statistics=["Average"],
            ).get("Datapoints", [])
            dp.sort(key=lambda p: p["Timestamp"])
            out[key] = [round(p["Average"] / div, 1) for p in dp]
        except ClientError:
            pass
    return out


def _annotate_instance_cpu(instances: list[dict]) -> None:
    """Attach a 2h CPU% sparkline series (cw_cpu) to each running instance from
    EC2 CloudWatch, all in one batched GetMetricData call. Best-effort — no
    datapoints (fresh box / basic-monitoring lag) just leaves it unset."""
    running = [i for i in instances if i.get("state") == "running"]
    if not running:
        return
    # Enable detailed (1-min) monitoring so a fresh box's CPU sparkline appears
    # in ~2 min instead of ~10 (basic monitoring is 5-min). Idempotent + cheap
    # (~$0.003/instance-hour, dies with the spot box).
    try:
        ec2_client().monitor_instances(InstanceIds=[i["id"] for i in running])
    except ClientError:
        pass
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=2)
    by_qid = {}
    queries = []
    for n, inst in enumerate(running):
        qid = f"c{n}"
        by_qid[qid] = inst
        queries.append({
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
                    "Dimensions": [{"Name": "InstanceId", "Value": inst["id"]}],
                },
                "Period": 300, "Stat": "Average",
            },
            "ReturnData": True,
        })
    try:
        cw = cloudwatch_client()
        for i in range(0, len(queries), 500):  # GetMetricData caps at 500/call
            resp = cw.get_metric_data(
                MetricDataQueries=queries[i:i + 500], StartTime=start,
                EndTime=now, ScanBy="TimestampAscending")
            for r in resp.get("MetricDataResults", []):
                inst = by_qid.get(r["Id"])
                if inst is not None and r.get("Values"):
                    inst["cw_cpu"] = [round(v) for v in r["Values"]]
    except ClientError:
        pass


def _annotate_init_states(instances: list[dict]) -> None:
    """Tag each running EC2 box with its ECS lifecycle state so the fleet view
    shows what a box is doing *before* a job lands on it, not just "idle":
      booting  — EC2 up but not yet a connected ECS member (OS boot + agent
                 register + first image pull happen here)
      pulling  — a task is placed and PENDING (image pull / container start)
      running  — has a running task
      idle     — registered, connected, no tasks (a scale-down candidate)
    Covers boxes with no jobs yet by listing the cluster's container instances
    directly (the job->instance map in _enrich_fleet only sees busy boxes).
    Best-effort: any AWS/permission error just leaves init_state unset."""
    running = [i for i in instances if i.get("state") == "running"]
    if not running:
        return
    try:
        from encoder.cloud.compute_env import _encoder_ce
        ce = _encoder_ce()
        cluster = None
        if ce:
            ces = batch_client().describe_compute_environments(
                computeEnvironments=[ce]).get("computeEnvironments", [])
            if ces:
                cluster = ces[0].get("ecsClusterArn")
        if not cluster:
            return
        ecs = ecs_client()
        arns: list[str] = []
        for page in ecs.get_paginator("list_container_instances").paginate(cluster=cluster):
            arns.extend(page.get("containerInstanceArns", []))
        by_ec2: dict[str, dict] = {}
        for i in range(0, len(arns), 100):
            for ci in ecs.describe_container_instances(
                    cluster=cluster, containerInstances=arns[i:i + 100]
            ).get("containerInstances", []):
                by_ec2[ci.get("ec2InstanceId")] = ci
    except ClientError:
        return

    for inst in running:
        ci = by_ec2.get(inst["id"])
        if ci is None:
            inst["init_state"] = "booting"
        elif not ci.get("agentConnected") or ci.get("status") == "REGISTERING":
            inst["init_state"] = "booting"
        elif ci.get("pendingTasksCount", 0) > 0:
            inst["init_state"] = "pulling"
        elif ci.get("runningTasksCount", 0) > 0 or inst.get("jobs"):
            inst["init_state"] = "running"
        else:
            inst["init_state"] = "idle"


def _record_fleet_samples(hourly_usd: float, fleet: dict) -> dict:
    """Append a fleet sample to a persisted log and return the trailing-24h
    spend (integrated burn rate) plus a recent history for sparklines. No Cost
    Explorer / extra IAM — the AWS poller runs this every ~60s so the log
    accrues continuously; gaps > 1h (server down) are not counted.

    Sample record: [ts, hourly_usd, used_vcpus, total_vcpus, queued, running].
    Old 2-field [ts, hourly] records are tolerated for backward compat.
    """
    # Persist to the app's host-mounted TMP_DIR (survives server restarts) —
    # NOT the container's ephemeral /tmp, which every restart/deploy wipes,
    # resetting the trailing-24h integral to "since last restart". TMPDIR is the
    # standard-lib fallback (usually unset here); /tmp is the last resort.
    path = os.environ.get("COST_LOG") or os.path.join(
        os.environ.get("TMP_DIR") or os.environ.get("TMPDIR") or "/tmp",
        "cost_samples.json")
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - 24 * 3600
    samples: list[list] = []
    try:
        with open(path) as f:
            samples = json.load(f)
    except (OSError, ValueError):
        samples = []
    samples = [s for s in samples if isinstance(s, list) and len(s) >= 2 and s[0] >= cutoff - 3600]
    samples.append([round(now, 1), round(float(hourly_usd), 4),
                    fleet.get("used_vcpus", 0), fleet.get("total_vcpus", 0),
                    fleet.get("queued_jobs", 0), fleet.get("running_jobs", 0)])

    spend = 0.0
    for a, b in zip(samples, samples[1:]):
        dt_h = (b[0] - a[0]) / 3600.0
        if 0 < dt_h <= 1.0:  # ignore gaps (server was down)
            spend += (a[1] + b[1]) / 2.0 * dt_h

    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(samples[-5000:], f)
        os.replace(tmp, path)
    except OSError:
        pass

    # Sparkline history: last 2h, downsampled to ~180 points.
    hist = [s for s in samples if s[0] >= now - 2 * 3600]
    if len(hist) > 180:
        stride = len(hist) // 180 + 1
        hist = hist[::stride]
    history = [{
        "t": s[0],
        "util": round((s[2] / s[3]) if len(s) >= 4 and s[3] else 0.0, 3),
        "used": s[2] if len(s) >= 3 else 0,
        "total": s[3] if len(s) >= 4 else 0,
        "queued": s[4] if len(s) >= 5 else 0,
        "running": s[5] if len(s) >= 6 else 0,
        "hourly": s[1],
    } for s in hist]
    return {"spend_24h_usd": round(spend, 4), "history": history}


def _current_max_vcpus():
    """The encoder compute env's current maxvCpus, for the AWS panel's max-vCPUs
    radio (current vs 2x). None if it can't be read (don't break inventory)."""
    try:
        from encoder.cloud.compute_env import get_vcpus
        return get_vcpus().get("max_vcpus")
    except Exception:  # noqa: BLE001 — best-effort
        return None


def _spot_and_reclaim_stats() -> dict:
    """Accumulated 'saved by using spot' + trailing-24h reclaim-waste %, read
    from the Go server's spot_samples.json (one entry per finished cloud-batch
    job: ts, lost_s, total_s, spot_usd, ondemand_usd, saved_usd)."""
    path = os.environ.get("SPOT_LOG") or os.path.join(
        os.environ.get("TMP_DIR") or os.environ.get("TMPDIR") or "/tmp",
        "spot_samples.json")
    try:
        with open(path) as f:
            samples = json.load(f)
    except (OSError, ValueError):
        samples = []
    now = datetime.now(timezone.utc).timestamp()
    cut = now - 24 * 3600
    def _sum(field, since=None):
        return sum(s.get(field, 0) for s in samples
                   if (since is None or s.get("ts", 0) >= since))
    lost_24h, total_24h = _sum("lost_s", cut), _sum("total_s", cut)
    return {
        "saved_total_usd": round(_sum("saved_usd"), 2),
        "saved_24h_usd": round(_sum("saved_usd", cut), 2),
        "reclaim_24h_lost_min": round(lost_24h / 60.0, 1),
        "reclaim_24h_pct": round(lost_24h / total_24h * 100.0, 1) if total_24h > 0 else 0.0,
    }


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

    # Fleet packing (busy/idle vCPU per instance + summary) and self-tracked
    # 24h spend. _enrich_fleet annotates `instances` in place.
    fleet = _enrich_fleet(batch_jobs, instances)
    _annotate_init_states(instances)  # booting / pulling / idle per box
    _annotate_instance_cpu(instances)  # per-machine CPU% sparkline series
    fleet["estimated_hourly_usd"] = round(hourly_total, 4)
    _sampled = _record_fleet_samples(hourly_total, fleet)
    fleet["spend_24h_usd"] = _sampled["spend_24h_usd"]
    fleet["history"] = _sampled["history"]
    fleet.update(_spot_and_reclaim_stats())  # saved_total_usd, reclaim_24h_pct, …
    # Actual CPU (cores) + memory (GiB) from CloudWatch Container Insights, for
    # the "real usage vs allocated" sparklines. Only when a box is up (else the
    # cluster metrics are empty and it's a wasted call).
    if any(i.get("state") == "running" for i in instances):
        cluster = _ecs_cluster_name()
        if cluster:
            fleet["cw"] = _fleet_cw_series(cluster)

    return {
        "fleet": fleet,
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
            "spend_24h_usd": fleet["spend_24h_usd"],
            "saved_total_usd": fleet.get("saved_total_usd", 0),
            "saved_24h_usd": fleet.get("saved_24h_usd", 0),
            "reclaim_24h_pct": fleet.get("reclaim_24h_pct", 0),
            "reclaim_24h_lost_min": fleet.get("reclaim_24h_lost_min", 0),
            "max_vcpus": _current_max_vcpus(),
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
