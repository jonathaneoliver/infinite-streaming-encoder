"""Adjust the encoder Batch compute environment's maxvCpus at runtime.

Live UpdateComputeEnvironment call — no redeploy; Terraform ignores max_vcpus
so it won't fight this.

The minvCpus half of this was removed with the keep-warm floor. It existed to
hold a box hot for the packaging tail, and #197 moved packaging off Batch
entirely — so it was paying for a tail that no longer existed. It was also the
wrong shape of call to make often: every UpdateComputeEnvironment puts the CE
into UPDATING, and Batch pauses ALL scale-down while it is, so the change that
dropped the floor to 0 at the idle transition was itself capable of delaying
the drain it was asking for.

Keep that in mind before automating maxvCpus per job for the same reason.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from infinite_streaming_encoder.cloud.aws import batch_client


def _encoder_ce() -> str | None:
    """Resolve the compute environment backing the encoder job queue."""
    queue = os.environ.get("BATCH_JOB_QUEUE", "infinite-streaming-encoder-queue")
    b = batch_client()
    qs = b.describe_job_queues(jobQueues=[queue]).get("jobQueues", [])
    if not qs:
        return None
    order = qs[0].get("computeEnvironmentOrder", [])
    if not order:
        return None
    return order[0].get("computeEnvironment")


def _update(field: str, n: int) -> dict:
    ce = _encoder_ce()
    if not ce:
        return {"error": "no compute environment found for the encoder queue"}
    batch_client().update_compute_environment(
        computeEnvironment=ce, computeResources={field: int(n)})
    return {"compute_environment": ce, field: int(n)}


def set_max_vcpus(n: int) -> dict:
    # Terraform ignores max_vcpus (like min) so this live change isn't reverted.
    return _update("maxvCpus", n)


def get_vcpus() -> dict:
    """Current min/max/desired vCPUs of the encoder compute environment — lets
    the UI show 'current' and offer 2x."""
    ce = _encoder_ce()
    if not ce:
        return {"error": "no compute environment found for the encoder queue"}
    envs = batch_client().describe_compute_environments(
        computeEnvironments=[ce]).get("computeEnvironments", [])
    cr = (envs[0].get("computeResources", {}) if envs else {})
    return {"compute_environment": ce, "min_vcpus": cr.get("minvCpus"),
            "max_vcpus": cr.get("maxvCpus"), "desired_vcpus": cr.get("desiredvCpus")}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="infinite_streaming_encoder.cloud.compute_env")
    p.add_argument("--set-max-vcpus", type=int, help="new maxvCpus ceiling")
    p.add_argument("--get", action="store_true", help="print current min/max/desired")
    # Output is always JSON; accept --json as a no-op so the Go server's
    # runPythonCloud helper (which always appends it) works unchanged.
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    try:
        if args.set_max_vcpus is not None:
            report = set_max_vcpus(args.set_max_vcpus)
        else:
            report = get_vcpus()
        print(json.dumps(report))
        return 0
    except Exception as e:  # noqa: BLE001 - surface any boto/API error as JSON
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
