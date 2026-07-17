"""Adjust the encoder Batch compute environment's minvCpus at runtime.

Used by the Go awswatch loop to keep a warm box alive during active runs
(so the packaging tail lands on a hot instance instead of cold-starting) and
to drop back to 0 when idle. Live UpdateComputeEnvironment call — no redeploy;
Terraform ignores min_vcpus so it won't fight this.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from encoder.cloud.aws import batch_client


def _encoder_ce() -> str | None:
    """Resolve the compute environment backing the encoder job queue."""
    queue = os.environ.get("BATCH_JOB_QUEUE", "encoder-queue")
    b = batch_client()
    qs = b.describe_job_queues(jobQueues=[queue]).get("jobQueues", [])
    if not qs:
        return None
    order = qs[0].get("computeEnvironmentOrder", [])
    if not order:
        return None
    return order[0].get("computeEnvironment")


def set_min_vcpus(n: int) -> dict:
    ce = _encoder_ce()
    if not ce:
        return {"error": "no compute environment found for the encoder queue"}
    batch_client().update_compute_environment(
        computeEnvironment=ce,
        computeResources={"minvCpus": int(n)},
    )
    return {"compute_environment": ce, "min_vcpus": int(n)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="encoder.cloud.compute_env")
    p.add_argument("--set-min-vcpus", type=int, required=True,
                   help="new minvCpus floor for the managed compute environment")
    args = p.parse_args(argv)
    try:
        print(json.dumps(set_min_vcpus(args.set_min_vcpus)))
        return 0
    except Exception as e:  # noqa: BLE001 - surface any boto/API error as JSON
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
