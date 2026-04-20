"""Spot instance launch with automatic AZ + instance-type fallback.

Iterates (instance type candidates × subnet candidates). For each
combination, calls RunInstances; on `InsufficientInstanceCapacity`
move to the next subnet and then the next instance type. Any other
error is fatal (permissions, bad AMI, etc.).

The primary `INSTANCE_TYPE` is always tried first. `INSTANCE_TYPE_FALLBACKS`
is a comma-separated string of alternate types (e.g.
"c7a.8xlarge,c6i.8xlarge"). The primary subnet is always tried first;
we then auto-discover every default-for-az subnet in the region for
failover, matching bash behaviour.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

from botocore.exceptions import ClientError

from encoder.cloud.aws import ec2_client, job_tags


@dataclass(frozen=True)
class LaunchSpec:
    ami_id: str
    instance_type: str
    instance_type_fallbacks: list[str]
    primary_subnet_id: str
    security_group_id: str
    instance_profile: str
    user_data: str                  # raw text; gets base64-encoded inside
    job_id: str                     # for Name/JobId tags
    use_spot: bool = True
    keep_instance: bool = False     # False → shutdown=terminate, True → stop


@dataclass(frozen=True)
class LaunchResult:
    instance_id: str
    instance_type: str
    subnet_id: str


class LaunchError(RuntimeError):
    """All (type, subnet) combinations hit InsufficientInstanceCapacity."""


def _other_default_subnets(primary: str) -> list[str]:
    ec2 = ec2_client()
    resp = ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])
    return [
        s["SubnetId"] for s in resp["Subnets"]
        if s["SubnetId"] != primary
    ]


def _build_candidate_types(primary: str, fallbacks: list[str]) -> list[str]:
    out = [primary]
    seen = {primary}
    for t in fallbacks:
        t = t.strip()
        if t and t not in seen:
            out.append(t)
            seen.add(t)
    return out


def launch(spec: LaunchSpec) -> LaunchResult:
    ec2 = ec2_client()

    subnets = [spec.primary_subnet_id] + _other_default_subnets(spec.primary_subnet_id)
    types = _build_candidate_types(spec.instance_type, spec.instance_type_fallbacks)

    shutdown_behavior = "stop" if spec.keep_instance else "terminate"
    user_data_b64 = base64.b64encode(spec.user_data.encode("utf-8")).decode("ascii")

    market_options: dict = {}
    if spec.use_spot:
        market_options = {"InstanceMarketOptions": {"MarketType": "spot"}}

    last_error: Exception | None = None

    for try_type in types:
        any_capacity_error = False
        for subnet in subnets:
            print(f">>> Launching {try_type} in subnet {subnet}", flush=True)
            try:
                tags = job_tags(spec.job_id)
                # Tag every resource type RunInstances creates: the instance
                # itself, its spot request (if any), and its attached EBS
                # volume. Scoped cleanup (issue #5) relies on all three
                # carrying the Application=encoder-app tag.
                tag_specs = [
                    {"ResourceType": "instance", "Tags": tags},
                    {"ResourceType": "volume", "Tags": tags},
                ]
                if spec.use_spot:
                    tag_specs.append(
                        {"ResourceType": "spot-instances-request", "Tags": tags},
                    )
                resp = ec2.run_instances(
                    ImageId=spec.ami_id,
                    InstanceType=try_type,
                    MinCount=1,
                    MaxCount=1,
                    SubnetId=subnet,
                    SecurityGroupIds=[spec.security_group_id],
                    IamInstanceProfile={"Name": spec.instance_profile},
                    InstanceInitiatedShutdownBehavior=shutdown_behavior,
                    UserData=user_data_b64,
                    BlockDeviceMappings=[{
                        "DeviceName": "/dev/xvda",
                        # DeleteOnTermination=True is the EBS default for
                        # RunInstances, but stating it explicitly is worth
                        # the extra bytes — if a future AWS SDK change ever
                        # flipped the default, we'd start leaking volumes
                        # into the "orphan" pile.
                        "Ebs": {
                            "VolumeSize": 100,
                            "VolumeType": "gp3",
                            "DeleteOnTermination": True,
                        },
                    }],
                    TagSpecifications=tag_specs,
                    **market_options,
                )
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code == "InsufficientInstanceCapacity":
                    print(f"    no capacity — trying next AZ", flush=True)
                    any_capacity_error = True
                    last_error = e
                    continue
                # Any other error is fatal — permissions, bad AMI, etc.
                raise

            instance = resp["Instances"][0]
            return LaunchResult(
                instance_id=instance["InstanceId"],
                instance_type=try_type,
                subnet_id=subnet,
            )

        if any_capacity_error:
            print(f"    all AZs exhausted for {try_type} — trying next type", flush=True)

    raise LaunchError(
        f"all candidates exhausted: types={types}, subnets={len(subnets)}. "
        f"Last error: {last_error}"
    )


def terminate(instance_id: str) -> None:
    """Fire-and-forget terminate. Safe to call on an already-terminated id."""
    try:
        ec2_client().terminate_instances(InstanceIds=[instance_id])
    except ClientError:
        pass
