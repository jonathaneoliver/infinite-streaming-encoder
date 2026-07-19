"""Shared AWS client factory + credential preflight.

Centralizes the `region_name` wiring so individual modules can just do
`ec2_client()` / `s3_client()` / `ssm_client()` without repeating
region plumbing.

Also defines the resource-tagging contract this tool relies on for
safe cleanup. Every EC2 instance, EBS volume, spot request, and
(where the API permits) S3 object is tagged `Application=encoder-app`.
The inventory and emergency-clear flows scope themselves strictly to
that tag, so nothing un-tagged ever gets touched.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import boto3


# The only tag we use to identify resources this tool has created.
# Do not change without a migration — the emergency-clear filter keys
# off this exact value, and anything that doesn't match is treated as
# user-owned and never touched.
APP_TAG_KEY = "Application"
APP_TAG_VALUE = "encoder-app"


def app_tag() -> dict[str, str]:
    """{'Key': 'Application', 'Value': 'encoder-app'} — the cleanup key."""
    return {"Key": APP_TAG_KEY, "Value": APP_TAG_VALUE}


def job_tags(job_id: str) -> list[dict[str, str]]:
    """All tags to attach to every resource launched for one job.

    - Application: static marker the cleanup flows filter on
    - JobId: ties resources back to /api/jobs/<id> server-side
    - LaunchedAt: RFC 3339 timestamp, used by the MaxLifetime watchdog
      to kill instances that outlive their budget regardless of state
    """
    return [
        app_tag(),
        {"Key": "JobId", "Value": job_id},
        {"Key": "LaunchedAt", "Value": datetime.now(timezone.utc).isoformat()},
        {"Key": "Name", "Value": f"encode-{job_id}"},
    ]


def app_tag_filter() -> list[dict]:
    """Filter list for ec2.describe_* / s3.list-by-tag queries."""
    return [{"Name": f"tag:{APP_TAG_KEY}", "Values": [APP_TAG_VALUE]}]


def region() -> str:
    """Current region. Honors AWS_REGION env var, then defaults to us-west-2
    (matches the bash script's default)."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def ec2_client():
    return boto3.client("ec2", region_name=region())


def s3_client():
    return boto3.client("s3", region_name=region())


def ssm_client():
    return boto3.client("ssm", region_name=region())


def sts_client():
    return boto3.client("sts", region_name=region())


def sfn_client():
    return boto3.client("stepfunctions", region_name=region())


def batch_client():
    return boto3.client("batch", region_name=region())


def ecs_client():
    return boto3.client("ecs", region_name=region())


def cloudwatch_client():
    return boto3.client("cloudwatch", region_name=region())


class AuthError(RuntimeError):
    """STS preflight couldn't confirm authenticated credentials."""


def check_credentials() -> None:
    """Mirrors bash's `aws sts get-caller-identity >/dev/null` preflight."""
    try:
        sts_client().get_caller_identity()
    except Exception as e:
        raise AuthError(f"AWS not authenticated in region {region()}: {e}") from e


def resolve_al2023_ami(ami_id: str | None = None, ami_arch: str = "x86_64") -> str:
    """Return the configured AMI, or auto-resolve the AL2023 latest via SSM.

    `ami_arch` is "x86_64" for Intel/AMD instance types or "arm64" for
    Graviton. ARM instance types (c7g / c8g / c6g) WILL NOT BOOT on an
    x86_64 AMI — the kernel won't match. Callers that pick a
    non-default CPU architecture must pass the matching `ami_arch`.
    """
    if ami_id:
        return ami_id
    name = f"/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{ami_arch}"
    resp = ssm_client().get_parameter(Name=name)
    return resp["Parameter"]["Value"]


def resolve_worker_ami(image_tag: str, ami_arch: str = "arm64") -> str | None:
    """Find a pre-baked `encoder-worker` AMI whose baked image tag matches
    `image_tag` — the SAME AMI the Batch compute env uses. Booting it means the
    worker image is already resident, so the remote's `docker pull` is a cheap
    manifest check with no layer download (~60s saved). Returns None when no
    current AMI exists (caller falls back to the stock AL2023 AMI + a full pull),
    so a stale/absent AMI never breaks the run — same self-correcting contract
    as the Batch side."""
    try:
        resp = ec2_client().describe_images(
            Owners=["self"],
            Filters=[
                {"Name": "tag:Name", "Values": ["encoder-worker"]},
                {"Name": "tag:image_tag", "Values": [image_tag]},
                {"Name": "architecture", "Values": [ami_arch]},
                {"Name": "state", "Values": ["available"]},
            ],
        )
    except Exception:  # noqa: BLE001 — AMI reuse is best-effort; fall back
        return None
    images = sorted(resp.get("Images", []), key=lambda i: i.get("CreationDate", ""))
    return images[-1]["ImageId"] if images else None
