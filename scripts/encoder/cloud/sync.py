"""S3 upload + download for cloud encode staging.

Inputs are uploaded with a size-comparison skip so reruns of the same
job don't re-upload matching files. Outputs are synced recursively from
S3 to a local directory at the end of the job.

We deliberately use boto3 + a manual walk for the download instead of
the AWS CLI's `s3 sync`. The motive is parity with bash's `--region`
arg already plumbed through; walking is simple enough (hundreds of
small .m4s files) and keeps dependencies minimal.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from botocore.exceptions import ClientError

from encoder.cloud.aws import s3_client


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split "s3://bucket/key/path" into ("bucket", "key/path")."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"not an s3 uri: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def upload_inputs(inputs: list[Path], s3_prefix: str) -> None:
    """Upload each input file to <s3_prefix>/input/<basename>.

    Skips files whose existing S3 object has the same byte count.
    Mirrors bash's `aws s3 ls | awk '{print $3}'` size comparison.
    """
    bucket, base_key = parse_s3_uri(s3_prefix.rstrip("/"))
    s3 = s3_client()

    for src in inputs:
        bn = src.name
        local_size = src.stat().st_size
        key = f"{base_key}/input/{bn}"
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            if head["ContentLength"] == local_size:
                print(f">>> Skipping {bn} — already in S3 ({local_size} bytes)",
                      flush=True)
                continue
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
                raise

        print(f">>> Uploading {bn} to s3://{bucket}/{key}", flush=True)
        s3.upload_file(str(src), bucket, key)


def download_outputs(s3_prefix: str, local_dir: Path) -> int:
    """Copy every object under <s3_prefix>/output/ into local_dir.

    Returns the number of objects downloaded. Directory structure is
    preserved (bucket key → local path relative to local_dir).
    """
    bucket, base_key = parse_s3_uri(s3_prefix.rstrip("/"))
    prefix = f"{base_key}/output/"

    s3 = s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    local_dir.mkdir(parents=True, exist_ok=True)

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(prefix):]
            dst = local_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(dst))
            count += 1
    return count


def download_user_data_log(s3_prefix: str, local_dir: Path) -> None:
    """Fetch the remote instance's user-data.log (best-effort)."""
    bucket, base_key = parse_s3_uri(s3_prefix.rstrip("/"))
    key = f"{base_key}/logs/user-data.log"
    local_dir.mkdir(parents=True, exist_ok=True)
    try:
        s3_client().download_file(bucket, key, str(local_dir / "user-data.log"))
    except ClientError:
        pass


def remove_staging(s3_prefix: str) -> None:
    """Delete every object under <s3_prefix>/ (used after verified download)."""
    bucket, base_key = parse_s3_uri(s3_prefix.rstrip("/"))
    s3 = s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{base_key}/"):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if not keys:
            continue
        s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
