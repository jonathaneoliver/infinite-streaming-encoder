"""Poll S3 for job completion markers + tail the remote log.

The remote instance writes one of two zero-byte keys under the job
prefix when it finishes:

  <prefix>/_DONE     — success
  <prefix>/_FAILED   — failure (body contains an error message)

While polling, we also tail `<prefix>/logs/user-data.log` (which the
EC2 user-data streams via `tee | aws s3 cp -`). Any `[[ENCODER-PLAN]]`
or `[[ENCODER-STAGE]]` lines found in new log bytes are forwarded to
our own stdout unchanged, so the Go server's scanner picks them up
and populates `Job.Stages` the same way it does for local encodes.

That's the one trick that makes cloud jobs render in the UI's stages
table with per-variant progress — no per-job subprocess on the
remote, just a byte offset kept locally.
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


def read_failure_reason(s3_prefix: str) -> str | None:
    """Read the body of <prefix>/_FAILED if it exists.

    The remote user-data writes a human-readable line into the _FAILED
    object — e.g. `SPOT INTERRUPTION: {"action":"terminate",...}` or
    `FAILED at clip 'X': trap at line N`. Returning it lets the local
    UI surface "spot interruption" vs a generic worker failure.
    """
    bucket, base_key = parse_s3_uri(s3_prefix.rstrip("/"))
    try:
        resp = s3_client().get_object(Bucket=bucket, Key=f"{base_key}/_FAILED")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    body = resp["Body"].read()
    return body.decode("utf-8", errors="replace").strip() or None


class _LogTailer:
    """Tracks byte offset into s3://<bucket>/<key> across poll ticks."""

    def __init__(self, bucket: str, key: str):
        self.bucket = bucket
        self.key = key
        self.offset = 0
        self._pending = b""  # partial last line carried to next tick

    def fetch_new(self) -> str:
        """Download any bytes past `self.offset`. Returns decoded text.

        Uses Range requests so each tick only pulls the delta, not the
        whole log. Silently tolerates "log not yet created" (404) and
        "already at end" (416).
        """
        try:
            resp = s3_client().get_object(
                Bucket=self.bucket,
                Key=self.key,
                Range=f"bytes={self.offset}-",
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound", "416",
                        "InvalidRange"):
                return ""
            raise

        body: bytes = resp["Body"].read()
        if not body:
            return ""
        self.offset += len(body)
        # Stitch a partial-line carryover from the previous tick.
        combined = self._pending + body
        # Defer any final partial line (no trailing newline yet) to next tick.
        if combined.endswith(b"\n"):
            complete = combined
            self._pending = b""
        else:
            last_nl = combined.rfind(b"\n")
            if last_nl == -1:
                self._pending = combined
                return ""
            complete = combined[:last_nl + 1]
            self._pending = combined[last_nl + 1:]
        return complete.decode("utf-8", errors="replace")


def _forward_markers(text: str) -> None:
    """Re-emit ENCODER-* marker lines to stdout so the Go scanner sees them."""
    for line in text.splitlines():
        if line.startswith("[[ENCODER-PLAN ") or line.startswith("[[ENCODER-STAGE "):
            # flush=True so the server's docker logs -f sees it without
            # waiting for Python's output buffer to fill.
            print(line, flush=True)


def poll_until_done(
    s3_prefix: str,
    *,
    timeout_s: int,
    interval_s: int = 20,
    log_tail_interval_s: int = 2,
) -> str:
    """Block until _DONE or _FAILED appears, or `timeout_s` elapses.

    Returns:
      "done"    — _DONE was found
      "failed"  — _FAILED was found
      "timeout" — neither marker appeared within timeout_s seconds

    Two independent cadences:

    - `log_tail_interval_s` (default 2s) — how often we fetch new
      bytes of the remote log and forward any ENCODER-PLAN / -STAGE
      markers to stdout. Tight so the UI's stages table updates
      smoothly while the remote is actively encoding.

    - `interval_s` (default 20s) — how often we HEAD the _DONE /
      _FAILED markers. Those only flip once, at job completion, so
      20s is plenty and keeps API-request volume low.
    """
    bucket, base_key = parse_s3_uri(s3_prefix.rstrip("/"))
    done_key = f"{base_key}/_DONE"
    failed_key = f"{base_key}/_FAILED"
    tailer = _LogTailer(bucket, f"{base_key}/logs/user-data.log")

    # Number of log-tail ticks per completion-probe tick.
    tails_per_probe = max(1, interval_s // log_tail_interval_s)

    elapsed = 0
    tail_count = 0
    while elapsed < timeout_s:
        # Forward any new markers first, so the UI sees progress even
        # on the final iteration where _DONE has just appeared.
        new_text = tailer.fetch_new()
        if new_text:
            _forward_markers(new_text)

        tail_count += 1
        # Only check _DONE / _FAILED on every Nth tick — they're
        # flip-once markers and don't need to be checked at 2s rate.
        if tail_count >= tails_per_probe:
            tail_count = 0
            if _exists(bucket, done_key):
                final = tailer.fetch_new()
                if final:
                    _forward_markers(final)
                return "done"
            if _exists(bucket, failed_key):
                return "failed"
            print(f"    [{elapsed:4d}s] still encoding...", flush=True)

        time.sleep(log_tail_interval_s)
        elapsed += log_tail_interval_s

    return "timeout"
