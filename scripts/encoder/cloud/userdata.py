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

# Tee everything to a local log. Streaming the pipe to S3 directly
# doesn't work — `aws s3 cp -` from stdin does a multipart upload
# that only becomes visible in S3 after CompleteMultipartUpload
# fires, which means the local log-tail sees nothing until the script
# finishes. Instead: tee to a local file here, and a background
# uploader (started below) syncs that file to S3 every 5s. The local
# tail sees ENCODER-STAGE markers within ~5s of the remote emitting
# them.
exec > /var/log/cloud-encode.log 2>&1

# Start the log uploader in the background; runs until the script
# exits. Copies the whole log each tick — S3 PutObject semantics make
# every tick a complete, visible object. The `2>/dev/null` hides the
# "file not found" at t=0 before tee has written anything.
# 2-second interval: keeps remote → S3 latency under the local tail
# interval (also 2s) so the user sees progress updates in roughly
# 2–4 seconds end-to-end during encode.
(
    while true; do
        aws s3 cp /var/log/cloud-encode.log {s3}/logs/user-data.log --region {region} 2>/dev/null || true
        sleep 2
    done
) &
LOG_UPLOADER_PID=$!

# Also stream to the EC2 console for instance-connect debugging.
exec > >(tee -a /dev/console) 2>&1

CURRENT_CLIP="<pre-loop>"

mark_failed() {{
    echo "FAILED at clip '${{CURRENT_CLIP}}': $1" | aws s3 cp - {s3}/_FAILED --region {region} || true
    # One last log sync so the failure reason is captured.
    aws s3 cp /var/log/cloud-encode.log {s3}/logs/user-data.log --region {region} 2>/dev/null || true
    kill $LOG_UPLOADER_PID 2>/dev/null || true
    kill $SPOT_WATCHER_PID 2>/dev/null || true
    shutdown -h +1 "encode failed: $1"
    exit 1
}}
trap 'mark_failed "trap at line $LINENO"' ERR

# Spot-interruption watcher: AWS publishes a 2-minute warning to the
# instance-metadata service before reclaiming a spot instance. When we
# see it we record a distinctive _FAILED body (so the local side
# surfaces "spot interruption" rather than generic worker-failure),
# rescue whatever's been produced so far with one last s3 sync of
# /work/output, and flush the log. The remote bash's main process is
# almost certainly killed by AWS's terminate signal before we get to
# write this — so we race, but even partial recovery beats none.
# IMDSv2 requires a session token first.
(
    while true; do
        sleep 5
        # Only poll once /work exists (i.e. we're past dnf install).
        [ -d /work ] || continue
        TOKEN=$(curl -s -X PUT --max-time 3 "http://169.254.169.254/latest/api/token" \\
            -H "X-aws-ec2-metadata-token-ttl-seconds: 300" 2>/dev/null) || continue
        BODY=$(curl -s --max-time 3 \\
            -H "X-aws-ec2-metadata-token: $TOKEN" \\
            -o /tmp/spot-action.json -w "%{{http_code}}" \\
            http://169.254.169.254/latest/meta-data/spot/instance-action 2>/dev/null)
        if [ "$BODY" = "200" ]; then
            ACTION=$(cat /tmp/spot-action.json)
            echo "!!! SPOT INTERRUPTION detected at $(date -u +%FT%TZ): $ACTION"
            # Rescue whatever encoded variants we have so far before AWS
            # reclaims. `|| true` in case /work/output is empty or sync
            # loses the race against the hypervisor.
            aws s3 sync /work/output {s3}/output/ \\
                --exclude '*_tmp/*' --exclude '*/abr_ladder_*/*' \\
                --region {region} 2>/dev/null || true
            # Distinctive body so poll.py can classify.
            printf 'SPOT INTERRUPTION: %s\\n' "$ACTION" \\
                | aws s3 cp - {s3}/_FAILED --region {region} || true
            # Final log flush + clean up log uploader before AWS kills us.
            aws s3 cp /var/log/cloud-encode.log {s3}/logs/user-data.log --region {region} 2>/dev/null || true
            kill $LOG_UPLOADER_PID 2>/dev/null || true
            exit 0
        fi
    done
) &
SPOT_WATCHER_PID=$!

# Progress marker helpers — same `[[ENCODER-STAGE ...]]` format the
# Python progress module uses, emitted as plain text so they land in
# the log the local poller is tailing. The local Go server parses them
# and updates Job.Stages.
stage() {{
    # stage <key> <status> [percent]
    printf '[[ENCODER-STAGE key=%s status=%s percent=%s]]\\n' "$1" "$2" "${{3:-0.0}}"
}}

stage remote:install running
dnf install -y docker
systemctl enable --now docker
stage remote:install done 100

stage remote:ghcr-login running
echo {pat} | docker login ghcr.io -u {user} --password-stdin
stage remote:ghcr-login done 100

stage remote:pull running
docker pull {image}
stage remote:pull done 100

mkdir -p /work/input /work/output /work/tmp

stage remote:fetch-inputs running
for bn in {basenames}; do
    aws s3 cp {s3}/input/${{bn}} /work/input/${{bn}} --region {region}
done
stage remote:fetch-inputs done 100

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
    # The container's own stdout carries the per-variant ENCODER-STAGE
    # markers from cli_local.py → they flow through docker → our
    # /var/log/cloud-encode.log → S3 → local poll tail.
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

    stage remote:sync-outputs running
    aws s3 sync /work/output {s3}/output/ \\
        --exclude '*_tmp/*' --exclude '*/abr_ladder_*/*' \\
        --region {region}
    stage remote:sync-outputs done 100
done
CURRENT_CLIP="<post-loop>"

aws s3 sync /work/output {s3}/output/ \\
    --exclude '*_tmp/*' --exclude '*/abr_ladder_*/*' \\
    --region {region}

echo "OK" | aws s3 cp - {s3}/_DONE --region {region}
# Final log flush so the poll's last-tick fetch gets the tail of the
# run (the 2s uploader may have just slept past the _DONE write).
aws s3 cp /var/log/cloud-encode.log {s3}/logs/user-data.log --region {region} 2>/dev/null || true
kill $LOG_UPLOADER_PID 2>/dev/null || true
kill $SPOT_WATCHER_PID 2>/dev/null || true
shutdown -h +1 "encode complete"
"""
