"""S3 upload + download for cloud encode staging.

Inputs are uploaded with a size-comparison skip so reruns of the same
job don't re-upload matching files. Outputs are synced recursively from
S3 to a local directory at the end of the job.

Both phases emit ENCODER-STAGE progress ticks as bytes transfer, so
the UI's stages table reflects upload/download progress the same way
it reflects per-variant ffmpeg progress. boto3's transfer API accepts
a Callback(bytes_transferred) that fires every ~8 KiB chunk; we
aggregate those across files and throttle emission to avoid drowning
the log stream.

We deliberately use boto3 + a manual walk for the download instead of
the AWS CLI's `s3 sync`. The motive is parity with bash's `--region`
arg already plumbed through; walking is simple enough (hundreds of
small .m4s files) and keeps dependencies minimal.
"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

from botocore.exceptions import ClientError

from encoder.cloud.aws import s3_client
from encoder.progress import emit_stage


# Don't emit ENCODER-STAGE updates more often than this during byte
# transfer. boto3's callback fires every 8 KiB or so — for a 500 MB
# upload that's ~65k callbacks, way too much log traffic.
_STAGE_EMIT_INTERVAL_S = 0.5


class _ByteProgress:
    """Aggregate byte counter across multiple boto3 transfers.

    Emits ENCODER-STAGE percent ticks, throttled to roughly
    `_STAGE_EMIT_INTERVAL_S`. Thread-safe isn't needed — boto3's
    default TransferConfig uses one thread per file, and we call
    upload/download sequentially here.
    """

    def __init__(self, total_bytes: int, stage_key: str):
        self.total = max(1, total_bytes)  # guard /0 on empty batches
        self.stage_key = stage_key
        self.sent = 0
        self.last_emit = 0.0

    def tick(self, chunk_bytes: int) -> None:
        self.sent += chunk_bytes
        now = time.monotonic()
        if now - self.last_emit < _STAGE_EMIT_INTERVAL_S:
            return
        self.last_emit = now
        pct = min(99.9, (self.sent / self.total) * 100.0)
        emit_stage(self.stage_key, "running", pct)

    def callback(self):
        """Returns the boto3 Callback — a plain fn(bytes_transferred)."""
        return self.tick


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split "s3://bucket/key/path" into ("bucket", "key/path")."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"not an s3 uri: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def upload_inputs(inputs: list[Path], s3_prefix: str,
                  stage_key: str | None = None) -> None:
    """Upload each input file to <s3_prefix>/input/<basename>.

    Skips files whose existing S3 object has the same byte count
    (mirrors bash's `aws s3 ls | awk '{print $3}'` size comparison).
    When `stage_key` is set, ENCODER-STAGE ticks reflect cumulative
    bytes sent across every file that actually needed uploading.
    """
    bucket, base_key = parse_s3_uri(s3_prefix.rstrip("/"))
    s3 = s3_client()

    # Two-pass: decide what we're actually going to send, so the
    # percent calc is against the real upload workload (skipped files
    # don't count).
    to_upload: list[tuple[Path, str, int]] = []
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
        to_upload.append((src, key, local_size))

    if not to_upload:
        return

    total = sum(sz for _, _, sz in to_upload)
    progress = _ByteProgress(total, stage_key) if stage_key else None

    for src, key, _sz in to_upload:
        print(f">>> Uploading {src.name} to s3://{bucket}/{key}", flush=True)
        if progress is not None:
            s3.upload_file(str(src), bucket, key, Callback=progress.callback())
        else:
            s3.upload_file(str(src), bucket, key)


def _sum_prefix_size(bucket: str, prefix: str) -> tuple[int, int]:
    """Return (object_count, total_bytes) for everything under prefix."""
    s3 = s3_client()
    total_bytes = 0
    total_objs = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            total_objs += 1
            total_bytes += obj.get("Size", 0)
    return total_objs, total_bytes


def download_outputs(s3_prefix: str, local_dir: Path,
                     stage_key: str | None = None) -> int:
    """Copy every object under <s3_prefix>/output/ into local_dir.

    Returns the number of objects downloaded. Directory structure is
    preserved (bucket key → local path relative to local_dir).
    When `stage_key` is set, emits running-percent ticks as bytes
    arrive.
    """
    bucket, base_key = parse_s3_uri(s3_prefix.rstrip("/"))
    prefix = f"{base_key}/output/"

    _obj_count, total_bytes = _sum_prefix_size(bucket, prefix)
    progress = _ByteProgress(total_bytes, stage_key) if stage_key else None

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
            if progress is not None:
                s3.download_file(bucket, key, str(dst),
                                 Callback=progress.callback())
            else:
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
