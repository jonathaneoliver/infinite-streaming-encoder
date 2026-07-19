"""Release controls for the cloud-batch target, surfaced by the AWS tab.

Stopping a Step Functions execution aborts the Batch jobs it manages;
terminating jobs lets the Batch compute environment scale its spot instances
back to zero. The Go server shells out to these commands:

    python3 -m encoder.cloud.batch_admin stop-execution --arn <arn>
    python3 -m encoder.cloud.batch_admin terminate-job --id <jobId>
    python3 -m encoder.cloud.batch_admin stop-all

Each prints a small JSON report to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from botocore.exceptions import ClientError

from encoder.cloud.aws import batch_client, sfn_client

_ACTIVE_BATCH_STATUSES = ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING")
_REASON = "released from encoder AWS tab"


def _stop_execution(arn: str) -> dict:
    sfn_client().stop_execution(executionArn=arn, cause=_REASON)
    return {"stopped_execution": arn}


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
    p = argparse.ArgumentParser(prog="encoder.cloud.batch_admin")
    # Output is always JSON; accept --json as a no-op so the Go server's
    # runPythonCloud helper (which always appends it) works unchanged.
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    se = sub.add_parser("stop-execution"); se.add_argument("--arn", required=True)
    est = sub.add_parser("execution-status"); est.add_argument("--arn", required=True)
    tj = sub.add_parser("terminate-job"); tj.add_argument("--id", required=True, dest="job_id")
    sub.add_parser("stop-all")
    args = p.parse_args(argv)

    try:
        if args.cmd == "stop-execution":
            report = _stop_execution(args.arn)
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
