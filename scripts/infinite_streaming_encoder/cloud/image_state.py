"""Cloud worker-image + AMI state — read (for the About tab, #80) and heal
(for the awswatch self-heal, #79).

`state()` answers, at a glance: which image tag will Batch actually pull, is it
in ECR, is a matching worker AMI baked, and is that AMI *wired* into the compute
env (warm) vs just baked (cold) vs pull-on-boot — plus flags the two dangerous
divergences (AMI/job-def tag mismatch; a wired AMI that no longer exists).

`heal()` fixes only the one state that BREAKS encodes: a compute env wired to a
deleted AMI. It clears image_id_override -> pull-on-boot, using the same live
UpdateComputeEnvironment call compute_env.py already uses for min/max vCPUs
(Terraform would reach the same null on the next apply, since a deleted AMI
resolves WORKER_AMI to empty — no fight). Every other state already degrades
safely, so heal is a no-op for them.
"""
from __future__ import annotations

import argparse
import json
import sys

from infinite_streaming_encoder.cloud.aws import (
    batch_client,
    ec2_client,
    ecr_client,
)
from infinite_streaming_encoder.cloud.compute_env import _encoder_ce

_REPO = "infinite-streaming-encoder-worker"
_AMI_NAME = "infinite-streaming-encoder-worker"


def _tag_of(image_ref: str) -> str:
    """`…/repo:TAG` -> `TAG` (empty if untagged / digest-pinned)."""
    tail = image_ref.rsplit("/", 1)[-1]
    return tail.split(":", 1)[1] if ":" in tail else ""


def expected_tag() -> str:
    """The tag the Batch job-defs actually pin — the source of truth for what
    cloud runs. Read off the mezzanine job-def (all 7 share the same image)."""
    jds = batch_client().describe_job_definitions(
        jobDefinitionName=f"{_AMI_NAME.replace('-worker', '')}-mezzanine",
        status="ACTIVE",
    ).get("jobDefinitions", [])
    if not jds:
        return ""
    latest = max(jds, key=lambda j: j.get("revision", 0))
    return _tag_of(latest.get("containerProperties", {}).get("image", ""))


def _ecr() -> dict:
    try:
        imgs = ecr_client().describe_images(repositoryName=_REPO).get("imageDetails", [])
    except Exception:  # noqa: BLE001 — best-effort; repo may not exist yet
        return {"reachable": False, "tags": []}
    tags = sorted({t for d in imgs for t in (d.get("imageTags") or [])})
    return {"reachable": True, "tags": tags}


def _available_amis() -> list[dict]:
    resp = ec2_client().describe_images(
        Owners=["self"],
        Filters=[{"Name": "tag:Name", "Values": [_AMI_NAME]}],
    )
    out = []
    for img in resp.get("Images", []):
        tags = {t["Key"]: t["Value"] for t in img.get("Tags", [])}
        out.append({
            "id": img["ImageId"],
            "image_tag": tags.get("image_tag", ""),
            "arch": img.get("Architecture", ""),
            "state": img.get("State", ""),
            "created": img.get("CreationDate", ""),
        })
    return sorted(out, key=lambda a: a["created"], reverse=True)


def _wired(ce: str | None) -> dict:
    """The AMI id the compute env is pinned to (image_id_override), if any."""
    if not ce:
        return {"ami": None}
    envs = batch_client().describe_compute_environments(
        computeEnvironments=[ce]).get("computeEnvironments", [])
    if not envs:
        return {"ami": None}
    ec2cfg = (envs[0].get("computeResources", {}) or {}).get("ec2Configuration") or []
    ami = ec2cfg[0].get("imageIdOverride") if ec2cfg else None
    return {"ami": ami or None}


def state() -> dict:
    ce = _encoder_ce()
    exp = expected_tag()
    ecr = _ecr()
    amis = _available_amis()
    wired_id = _wired(ce)["ami"]

    by_id = {a["id"]: a for a in amis}
    wired = by_id.get(wired_id) if wired_id else None
    wired_exists = wired is not None
    wired_tag = wired["image_tag"] if wired else None

    warnings: list[str] = []
    if wired_id and not wired_exists:
        warnings.append(f"compute env wired to {wired_id} which no longer exists (dangling)")
    if wired_exists and exp and wired_tag != exp:
        warnings.append(f"wired AMI is for image_tag {wired_tag}, but job-defs pin {exp} (mismatch)")
    if exp and ecr["reachable"] and exp not in ecr["tags"]:
        warnings.append(f"expected tag {exp} is NOT in ECR")

    if not wired_id:
        status = "pull-on-boot"
    elif not wired_exists:
        status = "dangling"          # BROKEN unless healed
    elif exp and wired_tag != exp:
        status = "baked-wrong-tag"   # works, cold pull of the right tag
    else:
        status = "warm"              # AMI wired + matches the pinned tag

    return {
        "compute_env": ce,
        "expected_tag": exp,
        "ecr": ecr,
        "wired_ami": wired_id,
        "wired_ami_tag": wired_tag,
        "wired_ami_exists": wired_exists,
        "available_amis": amis,
        "status": status,
        "warnings": warnings,
    }


def heal() -> dict:
    """Clear image_id_override iff the compute env is wired to a missing AMI.
    No-op for every safe state. Same live UpdateComputeEnvironment path as
    compute_env.set_min_vcpus."""
    st = state()
    if st["status"] != "dangling":
        return {"healed": False, "status": st["status"], "reason": "no dangling AMI"}
    ce = st["compute_env"]
    envs = batch_client().describe_compute_environments(
        computeEnvironments=[ce]).get("computeEnvironments", [])
    ec2cfg = (envs[0].get("computeResources", {}) or {}).get("ec2Configuration") or [{}]
    # Re-send ec2Configuration WITHOUT imageIdOverride -> Batch reverts to the
    # default AMI for that imageType (pull-on-boot).
    new_cfg = [{"imageType": ec2cfg[0].get("imageType", "ECS_AL2023")}]
    batch_client().update_compute_environment(
        computeEnvironment=ce, computeResources={"ec2Configuration": new_cfg})
    return {"healed": True, "cleared_ami": st["wired_ami"], "compute_env": ce,
            "now": "pull-on-boot"}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="infinite_streaming_encoder.cloud.image_state")
    p.add_argument("--heal", action="store_true",
                   help="clear a dangling (deleted) wired AMI -> pull-on-boot")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    try:
        print(json.dumps(heal() if args.heal else state()))
        return 0
    except Exception as e:  # noqa: BLE001 — surface as JSON
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
