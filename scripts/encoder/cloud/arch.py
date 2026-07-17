"""CPU-architecture profiles for cloud encode.

Each profile maps a high-level name the user picks ("intel" / "amd" /
"graviton") to the concrete AWS settings needed to run on it:

  - `primary`   — default instance type
  - `fallbacks` — comma-separated INSTANCE_TYPE_FALLBACKS (same arch
    family; don't mix ARM/x86 — different AMI)
  - `ami_arch`  — the SSM parameter suffix used to resolve the latest
    Amazon Linux 2023 AMI for this architecture

Picking Graviton requires that the worker image ship a `linux/arm64`
manifest; `docker buildx ... --platform linux/amd64,linux/arm64` on
the GHCR push covers that.

Users can still override via INSTANCE_TYPE / INSTANCE_TYPE_FALLBACKS /
AMI_ID env vars — the profile just sets the defaults.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArchProfile:
    key: str          # stable identifier: "intel", "amd", "graviton"
    label: str        # display name for UI and log output
    primary: str      # default instance type
    fallbacks: str    # comma-separated fallback types (same arch)
    ami_arch: str     # SSM path suffix: "x86_64" or "arm64"


ARCH_PROFILES: dict[str, ArchProfile] = {
    "intel": ArchProfile(
        key="intel",
        label="Intel Xeon",
        primary="c7i.8xlarge",
        # c7a is AMD — swap out if you're strict about Intel-only. Left
        # in as a capacity fallback: if a c-family c7i AZ is dry but a
        # c7a one isn't, we'd rather encode than fail.
        fallbacks="c7a.8xlarge,c6i.8xlarge",
        ami_arch="x86_64",
    ),
    "amd": ArchProfile(
        key="amd",
        label="AMD EPYC",
        primary="c7a.8xlarge",
        fallbacks="c7i.8xlarge,c6a.8xlarge",
        ami_arch="x86_64",
    ),
    "graviton": ArchProfile(
        key="graviton",
        label="AWS Graviton (ARM)",
        primary="c7g.8xlarge",
        # c8g is newer / not in every region; c6g is a solid backup.
        fallbacks="c8g.8xlarge,c6g.8xlarge",
        ami_arch="arm64",
    ),
}


# Graviton by default: it's the cheapest, and the worker image we push to ECR
# is arm64 — so an x86 default would launch an instance the image can't exec.
# Matches the Batch target (also Graviton).
DEFAULT_ARCH = "graviton"


def profile_for(arch: str | None) -> ArchProfile:
    """Return the profile for `arch`, falling back to intel on empty/unknown."""
    if not arch:
        return ARCH_PROFILES[DEFAULT_ARCH]
    return ARCH_PROFILES.get(arch.lower(), ARCH_PROFILES[DEFAULT_ARCH])


def ssm_ami_parameter(ami_arch: str) -> str:
    """SSM Parameter Store name for the latest AL2023 AMI on this arch."""
    return f"/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{ami_arch}"
