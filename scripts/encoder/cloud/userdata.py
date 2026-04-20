"""User-data bash script that runs on the remote EC2 instance.

This is the one piece of bash that stays in Python — it executes *on
the remote EC2 instance*, not in this container. We compose it as a
string template and shell-quote every interpolated value with
shlex.quote so special characters in filenames can't inject commands.

The remote script:

  1. Installs Docker + AWS CLI from AL2023's dnf repos
  2. Logs in to GHCR with the baked-in PAT
  3. Pulls the configured worker image (default: ghcr.io/jonathaneoliver/encoder:latest —
     the same image this tool builds locally, so local and cloud
     encodes share the Python pipeline + every fix made to it)
  4. Downloads every input clip from S3 to /work/input/
  5. Runs `python3 -m encoder.cli_local` (the image's default
     entrypoint) per clip. That emits ENCODER-PLAN + ENCODER-STAGE
     markers to the remote log, which the local poller tails out of
     the S3 log mirror so the Jobs UI sees per-variant progress.
  6. Incrementally rsyncs /work/output/ to s3://.../output/
  7. Writes _DONE (or _FAILED on trap) and shuts down

Any failure path writes _FAILED to S3 before shutdown so the local
poller sees it instead of hanging until timeout.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class UserDataSpec:
    s3_prefix: str            # "s3://bucket/jobs/JOB_ID"
    aws_region: str
    ghcr_username: str
    ghcr_pat: str
    docker_image: str
    input_basenames: list[str]
    encode_args: list[str]    # passthrough flags for create_abr_ladder.sh


def render(spec: UserDataSpec) -> str:
    basenames = " ".join(shlex.quote(b) for b in spec.input_basenames)
    encode_args = " ".join(shlex.quote(a) for a in spec.encode_args)

    s3 = shlex.quote(spec.s3_prefix)
    region = shlex.quote(spec.aws_region)
    pat = shlex.quote(spec.ghcr_pat)
    user = shlex.quote(spec.ghcr_username)
    image = shlex.quote(spec.docker_image)

    # Triple-brace blocks in f-strings would be awkward; compose plainly.
    return f"""#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/cloud-encode.log | aws s3 cp - {s3}/logs/user-data.log --region {region}) 2>&1

CURRENT_CLIP="<pre-loop>"

mark_failed() {{
    echo "FAILED at clip '${{CURRENT_CLIP}}': $1" | aws s3 cp - {s3}/_FAILED --region {region} || true
    shutdown -h +1 "encode failed: $1"
    exit 1
}}
trap 'mark_failed "trap at line $LINENO"' ERR

dnf install -y docker
systemctl enable --now docker

echo {pat} | docker login ghcr.io -u {user} --password-stdin
docker pull {image}

mkdir -p /work/input /work/output /work/tmp
for bn in {basenames}; do
    aws s3 cp {s3}/input/${{bn}} /work/input/${{bn}} --region {region}
done

for bn in {basenames}; do
    CURRENT_CLIP="${{bn}}"
    stem="${{bn%.*}}"
    base="${{stem}}_p200"

    echo ">>> Encoding ${{bn}} -> /work/output/${{base}}_{{h264,hevc}}/"
    # The image's default ENTRYPOINT is the Go server (for local use);
    # we override to Python here to run the same pipeline the local
    # worker containers use. TMPDIR is pinned on the same filesystem
    # as output_dir so Shaka Packager's temp→rename step doesn't
    # cross a Docker overlay boundary (EXDEV).
    docker run --rm \\
        -v /work:/work \\
        -w /work/output \\
        -e TMPDIR=/work/tmp \\
        -e PYTHONPATH=/app/scripts \\
        --entrypoint python3 \\
        {image} \\
        -m encoder.cli_local \\
        --input "/work/input/${{bn}}" \\
        --output-dir /work/output \\
        --output "${{base}}" \\
        {encode_args}

    aws s3 sync /work/output {s3}/output/ \\
        --exclude '*_tmp/*' --exclude '*/abr_ladder_*/*' \\
        --region {region}
done
CURRENT_CLIP="<post-loop>"

aws s3 sync /work/output {s3}/output/ \\
    --exclude '*_tmp/*' --exclude '*/abr_ladder_*/*' \\
    --region {region}

echo "OK" | aws s3 cp - {s3}/_DONE --region {region}
shutdown -h +1 "encode complete"
"""
