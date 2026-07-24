"""Release controls for the cloud-batch target, surfaced by the AWS tab.

Stopping a Step Functions execution does NOT stop its in-flight
batch:submitJob.sync jobs — they run to completion, orphaned — so cancel passes
--terminate-jobs to also terminate the execution's Batch jobs (in-progress
chunks stop now, and the compute environment can scale spot instances back to
zero). The Go server shells out to these commands:

    python3 -m infinite_streaming_encoder.cloud.batch_admin stop-execution --arn <arn> [--terminate-jobs]
    python3 -m infinite_streaming_encoder.cloud.batch_admin terminate-job --id <jobId>
    python3 -m infinite_streaming_encoder.cloud.batch_admin stop-all

Each prints a small JSON report to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from botocore.exceptions import ClientError

from infinite_streaming_encoder.cloud.aws import batch_client, sfn_client

_ACTIVE_BATCH_STATUSES = ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING")
_REASON = "released from encoder AWS tab"


def _stop_execution(arn: str, terminate_jobs: bool = False) -> dict:
    """Stop the execution. With terminate_jobs, ALSO terminate the Batch jobs it
    launched — stopping a Standard-workflow execution does NOT stop its in-flight
    batch:submitJob.sync jobs (they run to completion, orphaned), so a plain
    stop leaves in-progress chunk encodes running and the fleet busy. The jobs
    are matched by JobName, which carries the execution name (var-*-<execName>,
    mezz-<execName>, etc.)."""
    sfn_client().stop_execution(executionArn=arn, cause=_REASON)
    report: dict = {"stopped_execution": arn}
    if terminate_jobs:
        name = arn.rsplit(":", 1)[-1]
        queue = os.environ.get("BATCH_JOB_QUEUE", "encoder-queue")
        batch = batch_client()
        terminated, errors = [], []
        for status in _ACTIVE_BATCH_STATUSES:
            try:
                for j in batch.list_jobs(jobQueue=queue, jobStatus=status).get("jobSummaryList", []):
                    if name not in j.get("jobName", ""):
                        continue
                    try:
                        batch.terminate_job(jobId=j["jobId"], reason=_REASON)
                        terminated.append(j["jobId"])
                    except ClientError as e:
                        errors.append(f"terminate {j['jobId']}: {e}")
            except ClientError as e:
                errors.append(f"list {status}: {e}")
        report["terminated_jobs"] = terminated
        if errors:
            report["errors"] = errors
    return report


def _execution_status(arn: str) -> dict:
    """RUNNING / SUCCEEDED / FAILED / ABORTED / TIMED_OUT, or NOT_FOUND when the
    ARN no longer resolves — lets the Go server decide reattach vs resubmit."""
    try:
        return {"status": sfn_client().describe_execution(executionArn=arn)["status"]}
    except ClientError:
        return {"status": "NOT_FOUND"}


def _terminate_job(job_id: str) -> dict:
    batch_client().terminate_job(jobId=job_id, reason=_REASON)
    return {"terminated_job": job_id}


def _stop_all() -> dict:
    """Stop every running execution and terminate every active Batch job — the
    cloud-batch equivalent of the legacy 'clear all AWS resources' sweep."""
    stopped, terminated, errors = [], [], []

    arn = os.environ.get("STATE_MACHINE_ARN")
    if arn:
        sfn = sfn_client()
        try:
            for ex in sfn.list_executions(stateMachineArn=arn, statusFilter="RUNNING",
                                          maxResults=100).get("executions", []):
                try:
                    sfn.stop_execution(executionArn=ex["executionArn"], cause=_REASON)
                    stopped.append(ex["name"])
                except ClientError as e:
                    errors.append(f"stop {ex['name']}: {e}")
        except ClientError as e:
            errors.append(f"list executions: {e}")

    queue = os.environ.get("BATCH_JOB_QUEUE", "encoder-queue")
    batch = batch_client()
    for status in _ACTIVE_BATCH_STATUSES:
        try:
            for j in batch.list_jobs(jobQueue=queue, jobStatus=status).get("jobSummaryList", []):
                try:
                    batch.terminate_job(jobId=j["jobId"], reason=_REASON)
                    terminated.append(j["jobId"])
                except ClientError as e:
                    errors.append(f"terminate {j['jobId']}: {e}")
        except ClientError as e:
            errors.append(f"list {status}: {e}")

    return {"stopped_executions": stopped, "terminated_jobs": terminated, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="infinite_streaming_encoder.cloud.batch_admin")
    # Output is always JSON; accept --json as a no-op so the Go server's
    # runPythonCloud helper (which always appends it) works unchanged.
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    se = sub.add_parser("stop-execution"); se.add_argument("--arn", required=True)
    se.add_argument("--terminate-jobs", action="store_true", dest="terminate_jobs")
    est = sub.add_parser("execution-status"); est.add_argument("--arn", required=True)
    tj = sub.add_parser("terminate-job"); tj.add_argument("--id", required=True, dest="job_id")
    sub.add_parser("stop-all")
    args = p.parse_args(argv)

    try:
        if args.cmd == "stop-execution":
            report = _stop_execution(args.arn, terminate_jobs=args.terminate_jobs)
        elif args.cmd == "execution-status":
            report = _execution_status(args.arn)
        elif args.cmd == "terminate-job":
            report = _terminate_job(args.job_id)
        else:
            report = _stop_all()
    except ClientError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
