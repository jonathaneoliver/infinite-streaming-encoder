"""Poll S3 for job completion markers.

The remote instance writes one of two zero-byte keys under the job
prefix when it finishes:

  <prefix>/_DONE     — success
  <prefix>/_FAILED   — failure (body contains an error message)

We poll both with HeadObject (cheap, single request, no list), sleep
between checks, and respect an overall timeout. Returns a small
string: "done", "failed", or "timeout".
"""
from __future__ import annotations

import time
from botocore.exceptions import ClientError

from encoder.cloud.aws import s3_client
from encoder.cloud.sync import parse_s3_uri


def _exists(bucket: str, key: str) -> bool:
    try:
        s3_client().head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return True


def poll_until_done(
    s3_prefix: str,
    *,
    timeout_s: int,
    interval_s: int = 20,
) -> str:
    """Block until _DONE or _FAILED appears, or `timeout_s` elapses.

    Returns:
      "done"    — _DONE was found
      "failed"  — _FAILED was found
      "timeout" — neither marker appeared within timeout_s seconds
    """
    bucket, base_key = parse_s3_uri(s3_prefix.rstrip("/"))
    done_key = f"{base_key}/_DONE"
    failed_key = f"{base_key}/_FAILED"

    elapsed = 0
    while elapsed < timeout_s:
        if _exists(bucket, done_key):
            return "done"
        if _exists(bucket, failed_key):
            return "failed"
        time.sleep(interval_s)
        elapsed += interval_s
        print(f"    [{elapsed:4d}s] still encoding...", flush=True)

    return "timeout"
