"""Shared AWS client factory + credential preflight.

Centralizes the `region_name` wiring so individual modules can just do
`ec2_client()` / `s3_client()` / `ssm_client()` without repeating
region plumbing.
"""
from __future__ import annotations

import os

import boto3


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


class AuthError(RuntimeError):
    """STS preflight couldn't confirm authenticated credentials."""


def check_credentials() -> None:
    """Mirrors bash's `aws sts get-caller-identity >/dev/null` preflight."""
    try:
        sts_client().get_caller_identity()
    except Exception as e:
        raise AuthError(f"AWS not authenticated in region {region()}: {e}") from e


def resolve_al2023_ami(ami_id: str | None = None) -> str:
    """Return the configured AMI, or auto-resolve the AL2023 latest via SSM."""
    if ami_id:
        return ami_id
    name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
    resp = ssm_client().get_parameter(Name=name)
    return resp["Parameter"]["Value"]
