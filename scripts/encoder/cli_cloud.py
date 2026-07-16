#!/usr/bin/env python3
"""[DEPRECATED] Legacy cloud encode — one EC2 spot instance per job
via boto3 run_instances.

Superseded by the AWS Batch + Step Functions path in cli_batch.py +
infra/terraform/. This module stays until that path is verified
end-to-end in a live AWS account (tracked in the follow-up "delete
legacy cloud target" issue); after that it'll be removed along with
cloud/launch.py, cloud/userdata.py, cloud/arch.py, and the awswatch
EC2 inventory plumbing.

Flow (legacy):
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
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from encoder.cloud.arch import ARCH_PROFILES, DEFAULT_ARCH, profile_for
from encoder.cloud.aws import (
    AuthError, check_credentials, resolve_al2023_ami, region, s3_client,
)
from encoder.cloud.cleanup import terminate_job
from encoder.cloud.launch import LaunchError, LaunchSpec, launch
from encoder.cloud.poll import poll_until_done, read_failure_reason
from encoder.cloud.sync import (
    download_outputs, download_user_data_log, remove_staging, upload_inputs,
)
from encoder.cloud.userdata import UserDataSpec, render as render_user_data
from encoder.progress import Stage, emit_plan, emit_stage


# Cloud encode has a fixed set of lifecycle stages. Local side drives
# launch/upload/download/cleanup. Remote side (user-data bash) drives
# the `remote:*` stages via marker emits so the user can see where
# time is going — crucial for deciding whether to parallelise multiple
# encodes (e.g. "is pull dominating wall-clock?" → pre-warm the image).
#
# Launch comes before upload so a capacity / permissions failure never
# wastes an upload cycle. The remote's boot + dnf install + docker
# pull takes 60-120s, giving the local uploader plenty of runway to
# finish before the EC2 instance tries to fetch inputs.
_CLOUD_STAGES = [
    Stage(key="cloud:launch",        label="launch EC2 instance"),
    Stage(key="cloud:upload",        label="upload inputs (local → S3)"),
    Stage(key="remote:install",      label="install docker on EC2"),
    Stage(key="remote:login",        label="registry login"),
    Stage(key="remote:pull",         label="docker pull encoder image"),
    Stage(key="remote:fetch-inputs", label="fetch inputs (S3 → EC2)"),
    Stage(key="cloud:encode-remote", label="encode loop (remote)"),
    Stage(key="remote:sync-outputs", label="sync outputs (EC2 → S3)"),
    Stage(key="cloud:download",      label="download outputs (S3 → local)"),
    Stage(key="cloud:cleanup",       label="cleanup S3 staging"),
]


def _env(key: str, fallback: str = "") -> str:
    """Like os.environ.get(), but treats empty string as unset.

    The Makefile interpolates `-e INSTANCE_TYPE=$(INSTANCE_TYPE)` literally,
    so variables missing from .env land in the container as `KEY=""`. A
    plain dict lookup returns the empty string, bypassing the fallback —
    which is almost never what we want, since the fallback IS the desired
    default for an unset value.
    """
    v = os.environ.get(key, "")
    return v if v else fallback


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
    p.add_argument("--cpu-arch", default=None, dest="cpu_arch",
                   choices=list(ARCH_PROFILES.keys()),
                   help="CPU family for the EC2 worker (default: intel). "
                        "Picks instance type + AMI arch together; the "
                        "encoder image is multi-arch so both x86 and "
                        "Graviton work.")
    # Mirrors USE_SPOT env var. --no-spot is the only override — spot
    # is the default because it's ~2.6× cheaper. On-demand is for when
    # you can't afford a 2-min spot interruption mid-encode.
    p.add_argument("--no-spot", action="store_true", dest="no_spot",
                   help="use on-demand EC2 instead of spot (guaranteed "
                        "uptime; ~2.6× cost)")
    # Resume a prior failed job's work. cli_cloud.py assigns this run
    # a fresh job id for its own tagging/cleanup, but user-data pulls
    # inputs/mezzanines/completed-variants from the prior prefix.
    p.add_argument("--resume-from-job-id", default=None,
                   dest="resume_from_job_id",
                   help="pre-warm /work from a prior failed job's S3 "
                        "staging (skips upload + re-encodes only the "
                        "variants that didn't finish before)")
    # Test-only: inject a fake spot interruption after N seconds so we
    # can exercise the Retry flow without waiting for a real AWS
    # reclaim. Identical code path to the IMDSv2 watcher; writes
    # "SPOT INTERRUPTION:" to _FAILED, rsyncs /work/tmp + /work/output.
    p.add_argument("--simulate-interrupt-after", type=int, default=0,
                   dest="simulate_interrupt_after",
                   help="simulate a spot interrupt on the remote after "
                        "N seconds (test-only; 0 disables)")

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

    # When resuming, reuse the prior job's S3 prefix verbatim — prior
    # inputs, mezzanines, and any completed variants are already at
    # s3://bucket/jobs/<prior>/. The retry adds to that same prefix
    # (new variants land in output/ with .done sidecars), so a chain
    # of retries accumulates completed work in one place instead of
    # copying between prefixes. Clean exit of the retry deletes the
    # whole prefix; abnormal exit keeps it for the next retry; the
    # 1h awswatch GC catches anything abandoned.
    if args.resume_from_job_id:
        job_id = args.resume_from_job_id
    else:
        job_id = args.job_id or _default_job_id()
    s3_bucket = _require("S3_BUCKET")
    subnet_id = _require("SUBNET_ID")
    security_group_id = _require("SECURITY_GROUP_ID")

    aws_region = region()

    # Worker image. Default to the ECR image the Makefile passes through
    # (DOCKER_IMAGE=<ecr-repo>:<tag>), which the Batch target also runs — so
    # this legacy path is PAT-free and an apples-to-apples comparison. Only a
    # ghcr.io image still needs GHCR_PAT; ECR authenticates via the instance
    # role on the remote.
    docker_image = _env("DOCKER_IMAGE", "ghcr.io/jonathaneoliver/encoder:latest")
    ghcr_pat = _require("GHCR_PAT") if "ghcr.io" in docker_image else _env("GHCR_PAT", "")
    s3_prefix = f"s3://{s3_bucket}/jobs/{job_id}"
    local_output_dir = args.output_dir or Path(f"./cloud_output_{job_id}")

    try:
        check_credentials()
    except AuthError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # CPU-architecture profile: picks the instance family + AMI arch
    # together. Explicit INSTANCE_TYPE / INSTANCE_TYPE_FALLBACKS env
    # vars still override the profile — useful for one-off test types
    # or NUMA experiments.
    arch_profile = profile_for(args.cpu_arch or _env("CPU_ARCH", DEFAULT_ARCH))

    ami_id = resolve_al2023_ami(_env("AMI_ID") or None, ami_arch=arch_profile.ami_arch)
    instance_type = _env("INSTANCE_TYPE", arch_profile.primary)
    instance_type_fallbacks = [
        t for t in _env("INSTANCE_TYPE_FALLBACKS", arch_profile.fallbacks).split(",")
        if t.strip()
    ]
    # CLI flag wins over env: --no-spot forces on-demand regardless of
    # USE_SPOT. Otherwise fall through to the env default.
    use_spot = _env("USE_SPOT", "true").lower() == "true"
    if args.no_spot:
        use_spot = False

    print("=== cloud_encode plan ===")
    print(f"  job_id:         {job_id}")
    print(f"  inputs ({len(args.input)}):")
    for p in args.input:
        print(f"    - {p}")
    print(f"  s3 prefix:      {s3_prefix}")
    print(f"  output dir:     {local_output_dir}")
    print(f"  region:         {aws_region}")
    print(f"  cpu arch:       {arch_profile.label} ({arch_profile.ami_arch})")
    print(f"  instance type:  {instance_type} ({'spot' if use_spot else 'on-demand'})")
    print(f"  fallbacks:      {','.join(instance_type_fallbacks) or '(none)'}")
    print(f"  ami:            {ami_id}")
    print(f"  image:          {docker_image}")
    print(f"  encode args:    {' '.join(passthrough) if passthrough else '<none>'}")

    if args.dry_run:
        print("(dry-run; exiting)")
        return 0

    # Announce the lifecycle plan so the UI's stages table populates
    # immediately. Each stage flips to running/done below.
    emit_plan(_CLOUD_STAGES)

    # Register the teardown BEFORE any AWS state is created, so even a
    # crash in upload_inputs can't leave staging behind.
    #
    # --keep-instance / --keep-s3 opt the user out of automatic cleanup:
    # a clean exit respects those flags; an abnormal exit (crash /
    # signal) IGNORES them for EC2 (no billing leaks) but KEEPS ALL
    # S3 staging so the UI's Retry action can resume from prior work:
    # prior inputs, prior mezzanines (under tmp/), and any variants
    # the remote managed to finish (under output/) are all still
    # there. A background GC in the Go server deletes failed-job
    # prefixes whose _FAILED marker is >1h old, so cost isn't
    # unbounded — just deferred enough for retries to land.
    state: dict = {"cleaned": False, "exit_abnormal": True}

    def _cleanup(reason: str) -> None:
        if state["cleaned"]:
            return
        state["cleaned"] = True
        force = state["exit_abnormal"]
        if args.keep_instance and not force:
            print(f">>> leaving instance up (--keep-instance), reason={reason}",
                  flush=True)
            return
        # Pick the S3 cleanup mode:
        #   - normal exit: full delete (unless --keep-s3)
        #   - abnormal exit: keep everything (enables retry; the
        #     background GC will reap it after 1h)
        if not force:
            mode = "all" if args.keep_s3 else "none"
        else:
            mode = "all"
        note = {
            "all": " (S3 staging preserved — retry-ready)" if force else
                   " (S3 staging preserved — --keep-s3)",
            "none": "",
        }[mode]
        print(f">>> cleanup ({reason}) for job {job_id}{note}", flush=True)
        report = terminate_job(job_id, keep_s3=mode)
        for action in report.actions:
            print(f"    {action.action:<11s} {action.kind:<13s} {action.id}",
                  flush=True)

    atexit.register(_cleanup, "atexit")

    def _signal_handler(sig, _frame):
        _cleanup(f"signal {sig}")
        sys.exit(128 + sig)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # On resume, clear the prior run's flip-once markers so the local
    # poller doesn't immediately see a stale _FAILED / _DONE from the
    # previous attempt and decide this one's already over. Also wipe
    # the prior user-data.log — if we leave it, the local _LogTailer
    # starts tailing from byte 0 of the old run's log and replays
    # every STAGE marker from that run into this one's UI, making the
    # retry look like it resurrected the prior state. The remote will
    # rewrite the log from scratch anyway.
    if args.resume_from_job_id:
        print(f"  resuming from:  s3://{s3_bucket}/jobs/{job_id}")
        s3 = s3_client()
        for key in ("_FAILED", "_DONE", "_SIMULATE_INTERRUPT",
                    "logs/user-data.log"):
            try:
                s3.delete_object(Bucket=s3_bucket, Key=f"jobs/{job_id}/{key}")
            except Exception:
                pass

    # Render user-data before launch — it's local-only (no network)
    # and the InstanceId we're about to launch needs the full
    # script baked in via user-data at RunInstances time.
    user_data = render_user_data(UserDataSpec(
        s3_prefix=s3_prefix,
        aws_region=aws_region,
        ghcr_username=_env("GHCR_USERNAME", "jonathaneoliver"),
        ghcr_pat=ghcr_pat,
        docker_image=docker_image,
        input_basenames=[p.name for p in args.input],
        encode_args=passthrough,
        simulate_interrupt_after_s=args.simulate_interrupt_after,
    ))

    # Launch FIRST. Capacity failure / permissions / bad AMI all surface
    # here; if launch bails we exit before uploading anything, so
    # nothing hits S3 on a doomed job. The remote instance takes
    # 60-120s to boot + dnf install + docker pull before it tries to
    # fetch inputs from S3, which is comfortably more runway than an
    # input upload needs on typical residential links.
    emit_stage("cloud:launch", "running", 0.0)
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
        emit_stage("cloud:launch", "failed", 0.0)
        print(f"!!! {e}", file=sys.stderr)
        print(
            "    Spot capacity is tight region-wide. Try:\n"
            "      USE_SPOT=false  (on-demand; ~2x cost, near-guaranteed)\n"
            "    or INSTANCE_TYPE_FALLBACKS='c7i.4xlarge,c7a.4xlarge'  (smaller pools)",
            file=sys.stderr,
        )
        return 1
    emit_stage("cloud:launch", "done", 100.0)

    print(f"    instance: {result.instance_id} "
          f"({result.instance_type} in {result.subnet_id})", flush=True)
    # The atexit handler registered above already knows how to tear
    # down by job_id; it will find this instance via the JobId tag.

    # Upload inputs now that we have a live instance. Runs in parallel
    # (wall-clock wise) with the remote's boot + dnf + docker pull,
    # so on typical residential uplinks this finishes well before the
    # remote attempts to `aws s3 cp` the inputs into /work/input.
    #
    # Skipped on resume: user-data pulls inputs from the prior job's
    # S3 prefix instead, so no upload needed.
    emit_stage("cloud:upload", "running", 0.0)
    if args.resume_from_job_id:
        print(f">>> Skipping upload (resuming from {s3_prefix})", flush=True)
        emit_stage("cloud:upload", "skipped", 100.0)
    else:
        upload_inputs(args.input, s3_prefix, stage_key="cloud:upload")
        emit_stage("cloud:upload", "done", 100.0)

    # Poll for completion
    poll_timeout_per_clip = int(_env("POLL_TIMEOUT_PER_CLIP", "3600"))
    poll_timeout = int(_env("POLL_TIMEOUT") or (len(args.input) * poll_timeout_per_clip))
    poll_interval = int(_env("POLL_INTERVAL", "20"))

    print(f">>> Waiting for completion marker at {s3_prefix}/_DONE "
          f"(timeout {poll_timeout}s for {len(args.input)} clip(s))", flush=True)
    emit_stage("cloud:encode-remote", "running", 0.0)
    status = poll_until_done(s3_prefix, timeout_s=poll_timeout, interval_s=poll_interval)

    if status != "done":
        emit_stage("cloud:encode-remote", "failed", 0.0)
        reason = None
        if status == "failed":
            reason = read_failure_reason(s3_prefix)
        # Surface the reason in the log so it's visible on the job row
        # without opening user-data.log. For spot interruptions the
        # body starts with "SPOT INTERRUPTION:" which the UI can match
        # on to show distinct styling if we want later.
        if reason:
            print(f"!!! Job did not complete (status={status}): {reason}",
                  file=sys.stderr)
        else:
            print(f"!!! Job did not complete (status={status}). "
                  f"Fetching user-data log.", file=sys.stderr)
        # Record whether the local log grab succeeded so _cleanup can
        # delete S3 fully (local copy is safe) vs keep logs/ only.
        state["local_log_saved"] = download_user_data_log(
            s3_prefix, local_output_dir)
        return 2
    emit_stage("cloud:encode-remote", "done", 100.0)

    # Download outputs — same book-ending pattern as upload.
    emit_stage("cloud:download", "running", 0.0)
    print(f">>> Syncing outputs to {local_output_dir}", flush=True)
    count = download_outputs(s3_prefix, local_output_dir, stage_key="cloud:download")
    print(f"    downloaded {count} files", flush=True)
    state["local_log_saved"] = download_user_data_log(s3_prefix, local_output_dir)
    emit_stage("cloud:download", "done", 100.0)

    # Maybe clean up S3 (atexit will pick up anything we leave behind
    # on an abnormal exit, but clean-exit S3 lifecycle is controlled
    # by --keep-s3 and the verified-download check).
    emit_stage("cloud:cleanup", "running", 0.0)
    if args.keep_s3:
        print(f">>> Leaving S3 staging at {s3_prefix} (--keep-s3 set)", flush=True)
    elif count < 1:
        print(f"!!! Local output is empty; leaving S3 staging at {s3_prefix} for inspection",
              file=sys.stderr)
    else:
        print(f">>> Cleaning up S3 staging at {s3_prefix} ({count} local files verified)",
              flush=True)
        remove_staging(s3_prefix)
    emit_stage("cloud:cleanup", "done", 100.0)

    # We reached the end cleanly — flag this so the atexit cleanup
    # respects --keep-instance / --keep-s3 instead of forcing teardown.
    state["exit_abnormal"] = False
    print(f">>> Done. Outputs in {local_output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
