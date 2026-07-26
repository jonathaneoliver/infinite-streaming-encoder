"""MinIO staging admin for the local-dist encode path (#93).

The local-dist pipeline stages EVERYTHING through MinIO: the source
upload, the mezzanine, every per-chunk encode, the concatenated
variants, and the packaged HLS output — all under
`s3://<bucket>/jobs/<jobID>-<base>/`. Nothing used to remove that
prefix once the outputs had been pulled down to OUTPUT_DIR, so the
master box's MinIO grew without bound (44.7 GB / 19 jobs when this was
found). This module is the local analogue of `cloud/cleanup.py`'s S3
reclaim, pointed at MinIO instead of AWS.

Three reclaim paths, mirroring the cloud side:

- `delete_prefix()` — one job's staging. Called by the orchestrator
  (`cli_local_dist`) the moment `download:outputs` has landed every
  file on disk, which is when the staging becomes dead weight.
- `gc()` — age-based sweep for everything the happy path missed:
  failed, cancelled, and crashed-orchestrator jobs. The Go control
  plane runs this on an interval (`internal/diststage`) and passes the
  in-flight prefixes as `keep`. Also aborts stale multipart uploads,
  which hold real space but are invisible to an object listing.
- `ensure_lifecycle()` — a MinIO bucket lifecycle expiry on `jobs/`,
  the backstop-of-the-backstop for anything that outlives even the GC
  (e.g. the server never running again).

**Endpoint safety.** Every entry point requires an explicit MinIO
endpoint (`S3_ENDPOINT_URL`, as the orchestrator/worker containers get
it, or `MINIO_ENDPOINT`, as the server container gets it). Without one
boto3 would happily resolve to real AWS S3 and this module would start
deleting the *cloud* bucket's staging — so a missing endpoint is a hard
error, never a fallback. For the same reason the MinIO credentials are
passed explicitly rather than left to the default chain, which on the
server would find the real AWS credentials from the mounted ~/.aws.

Reports use the same `{scope, actions[], summary{}}` JSON shape as
`cloud.cleanup`, so the Go callers and the UI parse both identically.
The dataclasses are redeclared here rather than imported from there:
`cloud.cleanup` pulls in botocore at import time, and this module —
like every other top-level module in the package — must stay
stdlib-only until it actually talks to MinIO (boto3 is imported inside
`_s3()`), so `--help` and an unconfigured environment never explode.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class ResourceAction:
    kind: str                # "s3_prefix" | "lifecycle"
    id: str                  # s3://bucket/prefix
    job_id: str | None       # the <jobID>-<base> segment, when the id is a job prefix
    action: str              # "deleted" | "would-delete" | "skipped" | "configured" | "failed"
    detail: str = ""


@dataclass
class CleanupReport:
    scope: str
    actions: list[ResourceAction] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for a in self.actions:
            counts[a.action] = counts.get(a.action, 0) + 1
        return counts

    def as_json(self) -> str:
        return json.dumps({"scope": self.scope,
                           "actions": [asdict(a) for a in self.actions],
                           "summary": self.summary()}, indent=2)


# The bucket the Go control plane hands to cli_local_dist (`DIST_S3_BUCKET`).
DEFAULT_BUCKET = "encoder-local"

# Every per-job staging prefix lives under here. Nothing outside it is
# ever touched — same containment contract as cloud.cleanup.delete_prefix.
JOBS_PREFIX = "jobs/"

# The cross-job, content-addressed mezzanine cache (cli_local_dist keys it by
# source size+mtime+name). It deliberately lives OUTSIDE jobs/, so per-job
# delete_prefix never reclaims it — one source's mezzanine is reused by every
# job that encodes it. gc() sweeps it on its own idle window: a mezzanine
# unused for this long is evicted; an actively-reused one is kept because the
# orchestrator refreshes its idle clock on every cache hit (see
# cli_local_dist._touch_prefix). A source re-encoded within a day (the audit
# loop) always hits; one untouched for 24h ages out and re-produces on demand.
MEZZ_CACHE_PREFIX = "mezz-cache/"
MEZZ_CACHE_MAX_AGE_S = 24 * 3600  # 24 hours idle

# Lifecycle rule id. Stable, so re-running ensure_lifecycle updates our
# rule in place instead of stacking duplicates (and leaves any rule the
# user added by hand alone).
LIFECYCLE_RULE_ID = "encoder-local-dist-jobs-expiry"

# S3 caps a single delete_objects call at 1000 keys.
_DELETE_BATCH = 1000


class StagingError(RuntimeError):
    """MinIO staging is not addressable (no endpoint configured)."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def endpoint_url() -> str:
    """The MinIO endpoint, from either the worker-side or server-side env.

    `S3_ENDPOINT_URL` is what the orchestrator + worker containers get
    (see buildRunArgs / docker-compose); `MINIO_ENDPOINT` is what the
    server container gets, kept under a distinct name there so it can't
    clobber the real AWS wiring the cloud path uses. No default: see the
    endpoint-safety note in the module docstring.
    """
    url = os.environ.get("S3_ENDPOINT_URL") or os.environ.get("MINIO_ENDPOINT")
    if not url:
        raise StagingError(
            "no MinIO endpoint configured — set S3_ENDPOINT_URL or MINIO_ENDPOINT "
            "(refusing to fall back to AWS S3)")
    return url


def bucket_name(explicit: str | None = None) -> str:
    return explicit or os.environ.get("DIST_S3_BUCKET") or DEFAULT_BUCKET


def _s3():
    # Resolve the endpoint BEFORE importing boto3, so a misconfigured
    # environment fails with the actionable StagingError rather than
    # whatever boto3 raises first.
    url = endpoint_url()

    import boto3
    from botocore.config import Config

    # MINIO_* wins over AWS_*: on the server both are present and only the
    # MINIO_* pair is valid against MinIO. In the orchestrator/worker
    # containers only AWS_* exists (the creds are handed over under those
    # names for boto3), so that's the fallback.
    key = os.environ.get("MINIO_ACCESS_KEY") or os.environ.get("AWS_ACCESS_KEY_ID")
    secret = os.environ.get("MINIO_SECRET_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    return boto3.client(
        "s3",
        endpoint_url=url,
        # NOT AWS_REGION — on the server that's the real cloud region
        # (us-west-2), which would sign requests MinIO may reject.
        region_name=os.environ.get("MINIO_REGION", "us-east-1"),
        aws_access_key_id=key or None,
        aws_secret_access_key=secret or None,
        config=Config(s3={"addressing_style": "path"}),
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def _normalize_prefix(prefix: str) -> str:
    """`s3://bucket/jobs/x/` | `jobs/x` | `/jobs/x/` -> `jobs/x/`."""
    p = prefix.strip()
    if p.startswith("s3://"):
        p = p.split("/", 3)[3] if p.count("/") >= 3 else ""
    return p.strip("/") + "/" if p.strip("/") else ""


def scan(bucket: str | None = None, top: str = JOBS_PREFIX) -> dict[str, dict]:
    """Group every object under `top` (default `jobs/`) by its two-segment
    prefix (`jobs/<id>/`, or `mezz-cache/<key>/` for the shared mezzanine
    cache).

    Returns {prefix: {"objects": n, "bytes": n, "idle_s": seconds since
    the NEWEST object was written}}. Idle age (not oldest-object age) is
    what the GC keys on: a running job writes chunks continuously, so its
    staging always looks fresh even when the job started hours ago.
    """
    b = bucket_name(bucket)
    s3 = _s3()
    now = datetime.now(timezone.utc)
    groups: dict[str, dict] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=b, Prefix=top):
        for obj in page.get("Contents", []):
            parts = obj["Key"].split("/", 2)
            if len(parts) < 3 or not parts[1]:
                continue  # a stray object directly under jobs/ — not a job prefix
            prefix = f"{parts[0]}/{parts[1]}/"
            g = groups.setdefault(prefix, {"objects": 0, "bytes": 0, "idle_s": None})
            g["objects"] += 1
            g["bytes"] += int(obj.get("Size", 0) or 0)
            age = (now - obj["LastModified"]).total_seconds()
            if g["idle_s"] is None or age < g["idle_s"]:
                g["idle_s"] = age
    for g in groups.values():
        g["idle_s"] = int(g["idle_s"] or 0)
    return groups


def usage(bucket: str | None = None) -> dict:
    """Per-prefix staging usage — drives `make minio-usage` and the API."""
    b = bucket_name(bucket)
    groups = scan(b)
    prefixes = [
        {"prefix": p, "objects": g["objects"], "bytes": g["bytes"], "idle_s": g["idle_s"]}
        for p, g in sorted(groups.items(), key=lambda kv: -kv[1]["bytes"])
    ]
    return {
        "bucket": b,
        "endpoint": endpoint_url(),
        "prefixes": prefixes,
        "total_objects": sum(p["objects"] for p in prefixes),
        "total_bytes": sum(p["bytes"] for p in prefixes),
    }


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def _delete_one_prefix(s3, bucket: str, prefix: str, report: CleanupReport,
                       dry_run: bool, detail_suffix: str = "") -> None:
    """Delete every object under one already-normalized prefix."""
    job_id = prefix.rstrip("/").split("/")[-1]
    total = 0
    total_bytes = 0
    batch: list[dict] = []
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                total += 1
                total_bytes += int(obj.get("Size", 0) or 0)
                if dry_run:
                    continue
                batch.append({"Key": obj["Key"]})
                if len(batch) >= _DELETE_BATCH:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
                    batch = []
        if batch:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
    except Exception as e:  # noqa: BLE001 — botocore raises several shapes
        report.actions.append(ResourceAction(
            kind="s3_prefix", id=f"s3://{bucket}/{prefix}", job_id=job_id,
            action="failed", detail=str(e)))
        return
    if total == 0:
        return  # already gone — stay quiet, this path is idempotent
    detail = f"{total} object(s), {total_bytes / 1e9:.2f} GB{detail_suffix}"
    report.actions.append(ResourceAction(
        kind="s3_prefix", id=f"s3://{bucket}/{prefix}", job_id=job_id,
        action="would-delete" if dry_run else "deleted", detail=detail))


def delete_prefix(prefix: str, bucket: str | None = None,
                  dry_run: bool = False) -> CleanupReport:
    """Delete one job's staging. Restricted to `jobs/<something>/`.

    Idempotent: deleting an already-reclaimed (or never-created) prefix
    is a silent no-op, so the orchestrator's success-path call and the
    GC's sweep can both fire for the same job without conflict.
    """
    p = _normalize_prefix(prefix)
    report = CleanupReport(scope=f"dist:prefix:{p}")
    # Exactly `jobs/<id>/` — one whole job's staging, nothing else. Bare
    # "jobs/" is refused because a whole-bucket wipe must go through gc()
    # (which honours the keep-list and the idle-age floor) so a running
    # encode can't be reclaimed out from under itself; anything deeper or
    # oddly-shaped is refused because there's no use for it and a caller
    # asking for one is confused about what it's deleting.
    segments = p.strip("/").split("/")
    if len(segments) != 2 or segments[0] != JOBS_PREFIX.strip("/") \
            or segments[1] in ("", ".", ".."):
        report.actions.append(ResourceAction(
            kind="s3_prefix", id=prefix, job_id=None, action="skipped",
            detail="refused: only a jobs/<id>/ prefix may be deleted"))
        return report
    _delete_one_prefix(_s3(), bucket_name(bucket), p, report, dry_run)
    return report


def abort_stale_uploads(max_age_s: float, bucket: str, report: CleanupReport,
                        keep: "set[str] | frozenset[str]" = frozenset(),
                        dry_run: bool = False) -> None:
    """Abort multipart uploads under `jobs/` that were never completed.

    boto3's upload_file switches to multipart above 8 MB, so every source
    upload (~500 MB) and every chunk encode is one. An orchestrator killed
    mid-upload leaves the already-transferred parts behind: they consume
    real space but do NOT appear in list_objects_v2, so the prefix sweep
    can't see them and the object-expiry lifecycle rule doesn't cover them
    on MinIO. This is the only thing that reclaims them.
    """
    s3 = _s3()
    now = datetime.now(timezone.utc)
    try:
        # NO Prefix argument: MinIO's ListMultipartUploads returns an EMPTY
        # list when one is passed (it lists correctly without). So list them
        # all and filter on the key here — the bucket is this app's staging
        # anyway, so there's nothing else in it to page through.
        pages = s3.get_paginator("list_multipart_uploads").paginate(Bucket=bucket)
        uploads = [u for page in pages for u in page.get("Uploads", [])]
    except Exception as e:  # noqa: BLE001
        report.actions.append(ResourceAction(
            kind="multipart", id=f"s3://{bucket}/{JOBS_PREFIX}", job_id=None,
            action="failed", detail=str(e)))
        return
    for u in uploads:
        key = u["Key"]
        if not key.startswith(JOBS_PREFIX):
            continue
        segments = key.split("/", 2)
        prefix = f"{segments[0]}/{segments[1]}/" if len(segments) >= 3 else ""
        if prefix in keep:
            continue
        age = (now - u["Initiated"]).total_seconds()
        if age < max_age_s:
            continue
        detail = f"initiated {age / 3600:.1f}h ago"
        if not dry_run:
            try:
                s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=u["UploadId"])
            except Exception as e:  # noqa: BLE001
                report.actions.append(ResourceAction(
                    kind="multipart", id=f"s3://{bucket}/{key}",
                    job_id=prefix.rstrip("/").split("/")[-1] or None,
                    action="failed", detail=str(e)))
                continue
        report.actions.append(ResourceAction(
            kind="multipart", id=f"s3://{bucket}/{key}",
            job_id=prefix.rstrip("/").split("/")[-1] or None,
            action="would-delete" if dry_run else "deleted", detail=detail))


def gc(max_age_s: float, bucket: str | None = None,
       keep: "list[str] | tuple[str, ...]" = (), dry_run: bool = False) -> CleanupReport:
    """Reclaim job prefixes idle longer than `max_age_s`.

    This is the backstop for staging the success path never got to:
    failed jobs, cancelled jobs (the Go control plane `docker stop`s the
    orchestrator, so its cleanup never runs), and crashes. The age gate
    doubles as the debugging window — a failed job's inputs and partial
    variants stay inspectable in MinIO until it expires.

    `keep` is the set of prefixes belonging to queued/running jobs; they
    are skipped regardless of age. That's belt-and-braces on top of the
    idle-age test, which already protects an actively-writing job.
    """
    b = bucket_name(bucket)
    report = CleanupReport(scope=f"dist:gc:{int(max_age_s)}s")
    keep_set = {_normalize_prefix(k) for k in keep}
    s3 = _s3()
    for prefix, g in sorted(scan(b).items()):
        if prefix in keep_set:
            report.actions.append(ResourceAction(
                kind="s3_prefix", id=f"s3://{b}/{prefix}",
                job_id=prefix.rstrip("/").split("/")[-1], action="skipped",
                detail="job still active"))
            continue
        if g["idle_s"] < max_age_s:
            continue
        _delete_one_prefix(s3, b, prefix, report, dry_run,
                           detail_suffix=f", idle {g['idle_s'] / 3600:.1f}h")
    # Shared mezzanine cache — its own, much longer idle window (it's meant to
    # persist for cross-job reuse). `keep` holds job prefixes only, so it never
    # applies here; an actively-reused mezzanine stays fresh because the
    # orchestrator touches it on each hit.
    for prefix, g in sorted(scan(b, top=MEZZ_CACHE_PREFIX).items()):
        if g["idle_s"] < MEZZ_CACHE_MAX_AGE_S:
            continue
        _delete_one_prefix(s3, b, prefix, report, dry_run,
                           detail_suffix=f", mezz idle {g['idle_s'] / 3600:.1f}h")
    abort_stale_uploads(max_age_s, b, report, keep_set, dry_run)
    return report


# ---------------------------------------------------------------------------
# Lifecycle backstop
# ---------------------------------------------------------------------------

def ensure_lifecycle(days: int, bucket: str | None = None) -> CleanupReport:
    """Put a `jobs/` expiry rule on the bucket, preserving other rules.

    MinIO returns NoSuchLifecycleConfiguration on a fresh bucket, which
    is why nothing expired before. This is deliberately slower than the
    GC (days, not hours): it only ever catches staging that outlived the
    server itself.
    """
    b = bucket_name(bucket)
    report = CleanupReport(scope=f"dist:lifecycle:{days}d")
    s3 = _s3()
    # Expiration only. An AbortIncompleteMultipartUpload directive is silently
    # dropped by MinIO when paired with Expiration and rejected outright on its
    # own (InvalidArgument), so dangling multipart uploads are reclaimed by
    # abort_stale_uploads() in the GC instead of by this rule.
    rule = {
        "ID": LIFECYCLE_RULE_ID,
        "Filter": {"Prefix": JOBS_PREFIX},
        "Status": "Enabled",
        "Expiration": {"Days": int(days)},
    }
    try:
        existing = s3.get_bucket_lifecycle_configuration(Bucket=b).get("Rules", [])
    except Exception:  # noqa: BLE001 — NoSuchLifecycleConfiguration on a fresh bucket
        existing = []
    for r in existing:
        if r.get("ID") == LIFECYCLE_RULE_ID:
            if r.get("Expiration", {}).get("Days") == int(days) and r.get("Status") == "Enabled":
                return report  # already correct — stay quiet (this runs every boot)
            break
    rules = [r for r in existing if r.get("ID") != LIFECYCLE_RULE_ID] + [rule]
    try:
        s3.put_bucket_lifecycle_configuration(
            Bucket=b, LifecycleConfiguration={"Rules": rules})
        report.actions.append(ResourceAction(
            kind="lifecycle", id=f"s3://{b}/{JOBS_PREFIX}", job_id=None,
            action="configured", detail=f"expire after {days}d"))
    except Exception as e:  # noqa: BLE001
        report.actions.append(ResourceAction(
            kind="lifecycle", id=f"s3://{b}/{JOBS_PREFIX}", job_id=None,
            action="failed", detail=str(e)))
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report(report: CleanupReport, as_json: bool) -> None:
    if as_json:
        print(report.as_json())
        return
    summary = report.summary()
    if not summary:
        print(f"[dist-staging] scope={report.scope}: nothing to do")
        return
    parts = [f"{k}={v}" for k, v in sorted(summary.items())]
    print(f"[dist-staging] scope={report.scope}: {' '.join(parts)}")
    for a in report.actions:
        print(f"  {a.action:<13s} {a.id}{f' — {a.detail}' if a.detail else ''}")


def _main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="infinite_streaming_encoder.dist_staging")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--usage", action="store_true",
                       help="report per-job-prefix object count / bytes / idle age")
    group.add_argument("--delete-prefix", metavar="PREFIX",
                       help="delete one job's staging (jobs/<id>/ only)")
    group.add_argument("--gc", action="store_true",
                       help="delete job prefixes idle longer than --max-age-s")
    group.add_argument("--ensure-lifecycle", action="store_true",
                       help="put a jobs/ expiry rule on the bucket (--days)")
    p.add_argument("--bucket", default=None,
                   help=f"staging bucket (default $DIST_S3_BUCKET or {DEFAULT_BUCKET})")
    p.add_argument("--max-age-s", type=float, default=86400,
                   help="idle-age threshold for --gc (default 86400 = 24h)")
    p.add_argument("--keep", action="append", default=[],
                   help="repeatable: a prefix --gc must not touch (active jobs)")
    p.add_argument("--days", type=int, default=3,
                   help="expiry for --ensure-lifecycle (default 3)")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be deleted without deleting it")
    p.add_argument("--json", action="store_true",
                   help="print machine-readable JSON instead of a summary")
    args = p.parse_args()

    try:
        if args.usage:
            doc = usage(args.bucket)
            if args.json:
                print(json.dumps(doc, indent=2))
            else:
                print(f"[dist-staging] {doc['bucket']} @ {doc['endpoint']}: "
                      f"{doc['total_objects']} object(s), "
                      f"{doc['total_bytes'] / 1e9:.2f} GB across "
                      f"{len(doc['prefixes'])} job prefix(es)")
                for e in doc["prefixes"]:
                    print(f"  {e['bytes'] / 1e9:8.2f} GB  {e['objects']:6d} obj  "
                          f"idle {e['idle_s'] / 3600:6.1f}h  {e['prefix']}")
            return 0
        if args.delete_prefix:
            report = delete_prefix(args.delete_prefix, args.bucket, args.dry_run)
        elif args.gc:
            report = gc(args.max_age_s, args.bucket, args.keep, args.dry_run)
        else:
            report = ensure_lifecycle(args.days, args.bucket)
    except StagingError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    _print_report(report, args.json)
    return 1 if report.summary().get("failed", 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(_main())
