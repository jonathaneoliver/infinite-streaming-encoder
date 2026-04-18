#!/usr/bin/env python3
"""Cloud encode entry point (Python replacement for cloud_encode.sh).

Flow:
  1. Preflight: AWS credentials OK, GHCR_PAT set, AMI resolved
  2. Upload inputs (size-idempotent) to s3://$S3_BUCKET/jobs/$JOB_ID/input/
  3. Render user-data bash script with safely-quoted args
  4. Launch spot EC2 instance with AZ + instance-type fallback
  5. Poll S3 for _DONE / _FAILED marker
  6. Sync outputs back to local directory
  7. Optional: clean up S3 staging
  8. Cleanup trap: terminate instance on exit (unless --keep-instance)
"""
from __future__ import annotations

import argparse
import atexit
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from encoder.cloud.aws import AuthError, check_credentials, resolve_al2023_ami, region
from encoder.cloud.launch import LaunchError, LaunchSpec, launch, terminate
from encoder.cloud.poll import poll_until_done
from encoder.cloud.sync import (
    download_outputs, download_user_data_log, remove_staging, upload_inputs,
)
from encoder.cloud.userdata import UserDataSpec, render as render_user_data


def _env(key: str, fallback: str = "") -> str:
    return os.environ.get(key, fallback)


def _default_job_id() -> str:
    # Matches bash: YYYYMMDDTHHMMSSZ-$$ but use pid for tiebreak.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{os.getpid()}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="encoder.cli_cloud",
        description="Encode one or more clips on an AWS EC2 spot instance.",
    )
    p.add_argument("--input", type=Path, action="append", default=[],
                   help="local input file (may be repeated for batch encodes)")
    p.add_argument("--output-dir", type=Path, default=None, dest="output_dir",
                   help="local directory to sync results into")
    p.add_argument("--keep-instance", action="store_true", dest="keep_instance",
                   help="don't terminate the EC2 instance on exit")
    p.add_argument("--keep-s3", action="store_true", dest="keep_s3",
                   help="don't delete S3 staging after successful download")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.add_argument("--job-id", default=None, dest="job_id",
                   help="reuse an existing job id (e.g. to re-download)")

    # Remaining args get forwarded verbatim to create_abr_ladder.sh on the remote.
    return p


def _require(var: str) -> str:
    v = os.environ.get(var, "")
    if not v or v.startswith("CHANGE-ME"):
        raise SystemExit(f"error: {var} env var is required (set in .env)")
    return v


def _reject_duplicate_basenames(inputs: list[Path]) -> None:
    seen: dict[str, Path] = {}
    for p in inputs:
        bn = p.name
        if bn in seen:
            raise SystemExit(
                f"error: two --input files share the basename '{bn}' "
                f"({seen[bn]} and {p}). Rename one locally before uploading."
            )
        seen[bn] = p


def main() -> int:
    parser = build_parser()
    args, passthrough = parser.parse_known_args()

    if not args.input:
        parser.error("at least one --input is required")
    for p in args.input:
        if not p.is_file():
            parser.error(f"input file not found: {p}")
    _reject_duplicate_basenames(args.input)

    job_id = args.job_id or _default_job_id()
    s3_bucket = _require("S3_BUCKET")
    subnet_id = _require("SUBNET_ID")
    security_group_id = _require("SECURITY_GROUP_ID")
    ghcr_pat = _require("GHCR_PAT")

    aws_region = region()
    s3_prefix = f"s3://{s3_bucket}/jobs/{job_id}"
    local_output_dir = args.output_dir or Path(f"./cloud_output_{job_id}")

    try:
        check_credentials()
    except AuthError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    ami_id = resolve_al2023_ami(_env("AMI_ID") or None)
    instance_type = _env("INSTANCE_TYPE", "c7i.8xlarge")
    instance_type_fallbacks = [
        t for t in _env("INSTANCE_TYPE_FALLBACKS", "c7a.8xlarge,c6i.8xlarge").split(",")
        if t.strip()
    ]
    use_spot = _env("USE_SPOT", "true").lower() == "true"

    print("=== cloud_encode plan ===")
    print(f"  job_id:         {job_id}")
    print(f"  inputs ({len(args.input)}):")
    for p in args.input:
        print(f"    - {p}")
    print(f"  s3 prefix:      {s3_prefix}")
    print(f"  output dir:     {local_output_dir}")
    print(f"  region:         {aws_region}")
    print(f"  instance type:  {instance_type} ({'spot' if use_spot else 'on-demand'})")
    print(f"  ami:            {ami_id}")
    docker_image = _env(
        "DOCKER_IMAGE", "ghcr.io/jonathaneoliver/infinite-streaming:latest",
    )
    print(f"  image:          {docker_image}")
    print(f"  encode args:    {' '.join(passthrough) if passthrough else '<none>'}")

    if args.dry_run:
        print("(dry-run; exiting)")
        return 0

    # Upload inputs
    upload_inputs(args.input, s3_prefix)

    # Render user-data
    user_data = render_user_data(UserDataSpec(
        s3_prefix=s3_prefix,
        aws_region=aws_region,
        ghcr_username=_env("GHCR_USERNAME", "jonathaneoliver"),
        ghcr_pat=ghcr_pat,
        docker_image=docker_image,
        input_basenames=[p.name for p in args.input],
        encode_args=passthrough,
    ))

    # Launch
    try:
        result = launch(LaunchSpec(
            ami_id=ami_id,
            instance_type=instance_type,
            instance_type_fallbacks=instance_type_fallbacks,
            primary_subnet_id=subnet_id,
            security_group_id=security_group_id,
            instance_profile=_env("INSTANCE_PROFILE", "encode-worker"),
            user_data=user_data,
            job_id=job_id,
            use_spot=use_spot,
            keep_instance=args.keep_instance,
        ))
    except LaunchError as e:
        print(f"!!! {e}", file=sys.stderr)
        print(
            "    Spot capacity is tight region-wide. Try:\n"
            "      USE_SPOT=false  (on-demand; ~2x cost, near-guaranteed)\n"
            "    or INSTANCE_TYPE_FALLBACKS='c7i.4xlarge,c7a.4xlarge'  (smaller pools)",
            file=sys.stderr,
        )
        return 1

    print(f"    instance: {result.instance_id} "
          f"({result.instance_type} in {result.subnet_id})", flush=True)

    # Cleanup trap — always terminate unless --keep-instance.
    if not args.keep_instance:
        atexit.register(
            lambda: (print(f">>> Terminating {result.instance_id} (safety net)",
                           flush=True), terminate(result.instance_id))
        )

    # Poll for completion
    poll_timeout_per_clip = int(_env("POLL_TIMEOUT_PER_CLIP", "3600"))
    poll_timeout = int(_env("POLL_TIMEOUT") or (len(args.input) * poll_timeout_per_clip))
    poll_interval = int(_env("POLL_INTERVAL", "20"))

    print(f">>> Waiting for completion marker at {s3_prefix}/_DONE "
          f"(timeout {poll_timeout}s for {len(args.input)} clip(s))", flush=True)
    status = poll_until_done(s3_prefix, timeout_s=poll_timeout, interval_s=poll_interval)

    if status != "done":
        print(f"!!! Job did not complete (status={status}). Fetching user-data log.",
              file=sys.stderr)
        download_user_data_log(s3_prefix, local_output_dir)
        return 2

    # Download outputs
    print(f">>> Syncing outputs to {local_output_dir}", flush=True)
    count = download_outputs(s3_prefix, local_output_dir)
    print(f"    downloaded {count} files", flush=True)
    download_user_data_log(s3_prefix, local_output_dir)

    # Maybe clean up S3
    if args.keep_s3:
        print(f">>> Leaving S3 staging at {s3_prefix} (--keep-s3 set)", flush=True)
    elif count < 1:
        print(f"!!! Local output is empty; leaving S3 staging at {s3_prefix} for inspection",
              file=sys.stderr)
    else:
        print(f">>> Cleaning up S3 staging at {s3_prefix} ({count} local files verified)",
              flush=True)
        remove_staging(s3_prefix)

    print(f">>> Done. Outputs in {local_output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
