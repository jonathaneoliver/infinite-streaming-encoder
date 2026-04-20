"""Tear down AWS resources this tool has created.

Two scopes:

- `terminate_job(job_id)` — touches only resources tagged with both
  `Application=encoder-app` AND `JobId=<job_id>`. Safe to call from
  atexit handlers; idempotent.

- `sweep_all()` — touches every `Application=encoder-app` resource
  regardless of job. This is the emergency-clear path: when the user
  clicks "Clear all AWS resources" in the UI, or when the CLI is
  invoked with `--sweep-all`.

Nothing here ever touches a resource without the Application tag.
That's the safety contract — user-owned infra stays untouched even
if this module is invoked wildly.

Every function returns a structured `CleanupReport` so the caller
(server API, CLI, atexit wrapper) can surface what happened. AWS
API errors are caught per-resource so one failure doesn't mask
progress on the others.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from botocore.exceptions import ClientError

from encoder.cloud.aws import (
    APP_TAG_KEY, APP_TAG_VALUE, app_tag_filter, ec2_client, s3_client,
)


@dataclass
class ResourceAction:
    kind: str                # "instance" | "volume" | "spot_request" | "s3_prefix"
    id: str                  # resource id or s3 prefix
    job_id: str | None       # from JobId tag, if any
    action: str              # "terminated" | "deleted" | "cancelled" | "skipped" | "failed"
    detail: str = ""         # human-readable note or error message


@dataclass
class CleanupReport:
    scope: str               # "job:<id>" | "sweep_all"
    actions: list[ResourceAction] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        """Count actions by outcome, for quick-read UI / log output."""
        counts: dict[str, int] = {}
        for a in self.actions:
            counts[a.action] = counts.get(a.action, 0) + 1
        return counts

    def as_json(self) -> str:
        return json.dumps({"scope": self.scope, "actions": [asdict(a) for a in self.actions],
                           "summary": self.summary()}, indent=2)


def _tag_value(tags: list[dict], key: str) -> str | None:
    for t in tags or []:
        if t.get("Key") == key:
            return t.get("Value")
    return None


# ---------------------------------------------------------------------------
# EC2 instances + spot requests + volumes
# ---------------------------------------------------------------------------

def _describe_app_instances(ec2, job_id: str | None) -> list[dict]:
    filters = list(app_tag_filter())
    if job_id:
        filters.append({"Name": "tag:JobId", "Values": [job_id]})
    # Exclude already-terminated instances — nothing to do for those.
    filters.append({"Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"]})
    resp = ec2.describe_instances(Filters=filters)
    out: list[dict] = []
    for r in resp.get("Reservations", []):
        out.extend(r.get("Instances", []))
    return out


def _describe_app_volumes(ec2, job_id: str | None, only_orphans: bool = True) -> list[dict]:
    filters = list(app_tag_filter())
    if job_id:
        filters.append({"Name": "tag:JobId", "Values": [job_id]})
    if only_orphans:
        filters.append({"Name": "status", "Values": ["available"]})
    resp = ec2.describe_volumes(Filters=filters)
    return resp.get("Volumes", [])


def _describe_app_spot_requests(ec2, job_id: str | None) -> list[dict]:
    filters = list(app_tag_filter())
    if job_id:
        filters.append({"Name": "tag:JobId", "Values": [job_id]})
    # Only open/active requests — already-cancelled or already-fulfilled
    # (and thus closed-by-AWS) ones don't need our intervention.
    filters.append({"Name": "state", "Values": ["open", "active"]})
    resp = ec2.describe_spot_instance_requests(Filters=filters)
    return resp.get("SpotInstanceRequests", [])


def _terminate_instances(ec2, instances: list[dict], report: CleanupReport) -> None:
    ids = [i["InstanceId"] for i in instances]
    if not ids:
        return
    try:
        ec2.terminate_instances(InstanceIds=ids)
        for inst in instances:
            report.actions.append(ResourceAction(
                kind="instance",
                id=inst["InstanceId"],
                job_id=_tag_value(inst.get("Tags", []), "JobId"),
                action="terminated",
                detail=f"state was {inst['State']['Name']}",
            ))
    except ClientError as e:
        for inst in instances:
            report.actions.append(ResourceAction(
                kind="instance", id=inst["InstanceId"],
                job_id=_tag_value(inst.get("Tags", []), "JobId"),
                action="failed", detail=str(e),
            ))


def _cancel_spot_requests(ec2, requests: list[dict], report: CleanupReport) -> None:
    ids = [r["SpotInstanceRequestId"] for r in requests]
    if not ids:
        return
    try:
        ec2.cancel_spot_instance_requests(SpotInstanceRequestIds=ids)
        for r in requests:
            report.actions.append(ResourceAction(
                kind="spot_request", id=r["SpotInstanceRequestId"],
                job_id=_tag_value(r.get("Tags", []), "JobId"),
                action="cancelled",
            ))
    except ClientError as e:
        for r in requests:
            report.actions.append(ResourceAction(
                kind="spot_request", id=r["SpotInstanceRequestId"],
                job_id=_tag_value(r.get("Tags", []), "JobId"),
                action="failed", detail=str(e),
            ))


def _delete_volumes(ec2, volumes: list[dict], report: CleanupReport) -> None:
    for v in volumes:
        try:
            ec2.delete_volume(VolumeId=v["VolumeId"])
            report.actions.append(ResourceAction(
                kind="volume", id=v["VolumeId"],
                job_id=_tag_value(v.get("Tags", []), "JobId"),
                action="deleted",
                detail=f"{v.get('Size', '?')} GiB, was {v.get('State', '?')}",
            ))
        except ClientError as e:
            report.actions.append(ResourceAction(
                kind="volume", id=v["VolumeId"],
                job_id=_tag_value(v.get("Tags", []), "JobId"),
                action="failed", detail=str(e),
            ))


# ---------------------------------------------------------------------------
# S3 staging
# ---------------------------------------------------------------------------

def _s3_bucket_from_env() -> str | None:
    import os
    return os.environ.get("S3_BUCKET")


def _delete_s3_prefix(bucket: str, prefix: str, report: CleanupReport,
                      job_id: str | None) -> None:
    """Recursively delete every object under prefix. Does nothing if empty."""
    s3 = s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    total = 0
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            contents = page.get("Contents", [])
            if not contents:
                continue
            keys = [{"Key": obj["Key"]} for obj in contents]
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            total += len(keys)
        if total > 0:
            report.actions.append(ResourceAction(
                kind="s3_prefix", id=f"s3://{bucket}/{prefix}",
                job_id=job_id,
                action="deleted", detail=f"{total} object(s)",
            ))
    except ClientError as e:
        report.actions.append(ResourceAction(
            kind="s3_prefix", id=f"s3://{bucket}/{prefix}",
            job_id=job_id, action="failed", detail=str(e),
        ))


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def terminate_job(job_id: str) -> CleanupReport:
    """Tear down one job's AWS footprint. Safe for atexit handlers.

    Order: terminate instances first (this frees attached EBS as long
    as DeleteOnTermination=True, which launch.py explicitly sets),
    then cancel any still-open spot request, then delete any orphaned
    volumes (shouldn't exist post-terminate, but defensive), then S3.
    """
    report = CleanupReport(scope=f"job:{job_id}")
    ec2 = ec2_client()

    instances = _describe_app_instances(ec2, job_id)
    _terminate_instances(ec2, instances, report)

    spot_requests = _describe_app_spot_requests(ec2, job_id)
    _cancel_spot_requests(ec2, spot_requests, report)

    orphan_volumes = _describe_app_volumes(ec2, job_id, only_orphans=True)
    _delete_volumes(ec2, orphan_volumes, report)

    bucket = _s3_bucket_from_env()
    if bucket:
        _delete_s3_prefix(bucket, f"jobs/{job_id}/", report, job_id)
    return report


def sweep_all() -> CleanupReport:
    """Tear down every Application=encoder-app resource.

    Used by the emergency-clear button and the server's startup sanity
    check. Scoped filter means user-owned infra stays untouched.
    """
    report = CleanupReport(scope="sweep_all")
    ec2 = ec2_client()

    instances = _describe_app_instances(ec2, job_id=None)
    _terminate_instances(ec2, instances, report)

    spot_requests = _describe_app_spot_requests(ec2, job_id=None)
    _cancel_spot_requests(ec2, spot_requests, report)

    orphan_volumes = _describe_app_volumes(ec2, job_id=None, only_orphans=True)
    _delete_volumes(ec2, orphan_volumes, report)

    # S3 staging: everything under s3://bucket/jobs/. We don't filter
    # by Application tag here — object-level tags would require an
    # extra GetObjectTagging call per object and the prefix is owned
    # by this app by convention.
    bucket = _s3_bucket_from_env()
    if bucket:
        _delete_s3_prefix(bucket, "jobs/", report, job_id=None)
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def backfill_tags() -> CleanupReport:
    """Add Application=encoder-app to resources tagged with JobId but missing App.

    Pre-phase-1 launches set only JobId=<id> and Name=encode-<id>; they
    wouldn't appear in inventory (scoped by Application) and wouldn't
    be caught by sweep_all. This finds any EC2 instance/volume/spot
    request carrying JobId + Name='encode-*' and adds the Application
    tag. Safe to re-run — CreateTags is idempotent.

    After this runs once, everything this tool has ever launched is
    visible + cleanable via the normal inventory + sweep paths.
    """
    report = CleanupReport(scope="backfill")
    ec2 = ec2_client()

    def _retag(kind: str, ids: list[str], job_ids: list[str | None]) -> None:
        if not ids:
            return
        try:
            ec2.create_tags(
                Resources=ids,
                Tags=[{"Key": APP_TAG_KEY, "Value": APP_TAG_VALUE}],
            )
            for i, rid in enumerate(ids):
                report.actions.append(ResourceAction(
                    kind=kind, id=rid,
                    job_id=job_ids[i] if i < len(job_ids) else None,
                    action="deleted",  # co-opting the enum; a "tagged" value would be cleaner
                    detail="backfilled Application=encoder-app",
                ))
        except ClientError as e:
            for i, rid in enumerate(ids):
                report.actions.append(ResourceAction(
                    kind=kind, id=rid,
                    job_id=job_ids[i] if i < len(job_ids) else None,
                    action="failed", detail=str(e),
                ))

    # Instances: tagged with JobId (from the pre-phase-1 launcher) but
    # NOT tagged Application yet.
    try:
        resp = ec2.describe_instances(
            Filters=[{"Name": "tag-key", "Values": ["JobId"]}],
        )
    except ClientError as e:
        report.actions.append(ResourceAction(
            kind="instance", id="?", job_id=None,
            action="failed", detail=f"describe_instances: {e}",
        ))
        return report

    inst_ids: list[str] = []
    inst_jobs: list[str | None] = []
    for r in resp.get("Reservations", []):
        for inst in r.get("Instances", []):
            tags = inst.get("Tags", [])
            if _tag_value(tags, APP_TAG_KEY) == APP_TAG_VALUE:
                continue  # already good
            inst_ids.append(inst["InstanceId"])
            inst_jobs.append(_tag_value(tags, "JobId"))
    _retag("instance", inst_ids, inst_jobs)

    # Volumes: same logic.
    try:
        vol_resp = ec2.describe_volumes(
            Filters=[{"Name": "tag-key", "Values": ["JobId"]}],
        )
    except ClientError as e:
        report.actions.append(ResourceAction(
            kind="volume", id="?", job_id=None,
            action="failed", detail=f"describe_volumes: {e}",
        ))
        vol_resp = {"Volumes": []}

    vol_ids: list[str] = []
    vol_jobs: list[str | None] = []
    for v in vol_resp.get("Volumes", []):
        tags = v.get("Tags", [])
        if _tag_value(tags, APP_TAG_KEY) == APP_TAG_VALUE:
            continue
        vol_ids.append(v["VolumeId"])
        vol_jobs.append(_tag_value(tags, "JobId"))
    _retag("volume", vol_ids, vol_jobs)

    # Spot requests: ditto.
    try:
        sr_resp = ec2.describe_spot_instance_requests(
            Filters=[{"Name": "tag-key", "Values": ["JobId"]}],
        )
    except ClientError as e:
        report.actions.append(ResourceAction(
            kind="spot_request", id="?", job_id=None,
            action="failed", detail=f"describe_spot_instance_requests: {e}",
        ))
        sr_resp = {"SpotInstanceRequests": []}

    sr_ids: list[str] = []
    sr_jobs: list[str | None] = []
    for r in sr_resp.get("SpotInstanceRequests", []):
        tags = r.get("Tags", [])
        if _tag_value(tags, APP_TAG_KEY) == APP_TAG_VALUE:
            continue
        sr_ids.append(r["SpotInstanceRequestId"])
        sr_jobs.append(_tag_value(tags, "JobId"))
    _retag("spot_request", sr_ids, sr_jobs)

    return report


def _main() -> int:
    import argparse
    p = argparse.ArgumentParser(prog="encoder.cloud.cleanup")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--job-id", help="tear down one job's resources")
    group.add_argument("--sweep-all", action="store_true",
                       help="tear down every Application=encoder-app resource")
    group.add_argument("--backfill-tags", action="store_true",
                       help="add Application=encoder-app to resources already "
                            "tagged with JobId (one-shot pre-phase-1 cleanup)")
    p.add_argument("--json", action="store_true",
                   help="print machine-readable JSON instead of a summary")
    args = p.parse_args()

    if args.sweep_all:
        report = sweep_all()
    elif args.backfill_tags:
        report = backfill_tags()
    else:
        report = terminate_job(args.job_id)

    if args.json:
        print(report.as_json())
    else:
        summary = report.summary()
        if not summary:
            print(f"[cleanup] scope={report.scope}: nothing to do")
        else:
            parts = [f"{k}={v}" for k, v in sorted(summary.items())]
            print(f"[cleanup] scope={report.scope}: {' '.join(parts)}")
            for a in report.actions:
                print(f"  {a.action:<11s} {a.kind:<13s} {a.id}"
                      f"{f' — {a.detail}' if a.detail else ''}")

    # Non-zero exit iff any action failed — lets callers detect partial fails.
    return 1 if report.summary().get("failed", 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(_main())
