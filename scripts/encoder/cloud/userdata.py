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
    # Test hook: if > 0, the remote schedules a simulated spot
    # interruption after this many seconds. Identical code path to
    # a real interrupt (writes "SPOT INTERRUPTION:" to _FAILED, rsyncs
    # /work/tmp and /work/output, exits). Lets us exercise the Retry
    # flow deterministically without waiting for a real AWS interrupt.
    simulate_interrupt_after_s: int = 0


def render(spec: UserDataSpec) -> str:
    basenames = " ".join(shlex.quote(b) for b in spec.input_basenames)
    encode_args = " ".join(shlex.quote(a) for a in spec.encode_args)

    s3 = shlex.quote(spec.s3_prefix)
    region = shlex.quote(spec.aws_region)
    pat = shlex.quote(spec.ghcr_pat)
    user = shlex.quote(spec.ghcr_username)
    image = shlex.quote(spec.docker_image)
    simulate_s = int(spec.simulate_interrupt_after_s)

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

# Shared interrupt path — called by the real spot-interruption watcher
# and by the test-simulation timer. Writes the distinctive _FAILED
# body, rsyncs /work/tmp + /work/output to S3, flushes the log, kills
# the encode and exits. The remote bash's main process is almost
# certainly killed by AWS's terminate signal before we finish — so
# we race, but even partial recovery beats none.
trigger_interrupt() {{
    ACTION_BODY="$1"
    echo "!!! SPOT INTERRUPTION detected at $(date -u +%FT%TZ): $ACTION_BODY"
    aws s3 sync /work/output {s3}/output/ \\
        --exclude '*_tmp/*' --exclude '*/abr_ladder_*/*' \\
        --region {region} 2>/dev/null || true
    aws s3 sync /work/tmp {s3}/tmp/ \\
        --exclude '*.log' --exclude '*/abr_ladder_*/*' \\
        --region {region} 2>/dev/null || true
    printf 'SPOT INTERRUPTION: %s\\n' "$ACTION_BODY" \\
        | aws s3 cp - {s3}/_FAILED --region {region} || true
    aws s3 cp /var/log/cloud-encode.log {s3}/logs/user-data.log --region {region} 2>/dev/null || true
    kill $LOG_UPLOADER_PID 2>/dev/null || true
    exit 0
}}

# Spot-interruption watcher: AWS publishes a 2-minute warning to the
# instance-metadata service before reclaiming a spot instance.
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
            trigger_interrupt "$(cat /tmp/spot-action.json)"
        fi
    done
) &
SPOT_WATCHER_PID=$!

# Test-mode simulated interrupt. Runs entirely alongside the real
# watcher — if AWS reclaims first, the real watcher fires; otherwise
# this one fires after SIMULATE_AFTER_S seconds with a synthetic
# action body that's still prefixed "SPOT INTERRUPTION:" so the local
# UI surfaces the amber badge identically.
SIMULATE_AFTER_S={simulate_s}
if [ "$SIMULATE_AFTER_S" -gt 0 ]; then
    (
        sleep "$SIMULATE_AFTER_S"
        trigger_interrupt '{{"action":"terminate","time":"simulated","source":"test"}}'
    ) &
    SIMULATED_INTERRUPT_PID=$!
fi

# On-demand simulated interrupt via S3 sentinel. The local UI writes
# an empty object at <s3>/_SIMULATE_INTERRUPT when the user clicks
# "Simulate interrupt" on a running job; we poll for it every 5s and
# trigger the same interrupt path. Lets testing happen at any point
# in the encode, not just a preset delay.
(
    while true; do
        sleep 5
        [ -d /work ] || continue
        if aws s3 ls {s3}/_SIMULATE_INTERRUPT --region {region} 2>/dev/null \\
                | grep -q '_SIMULATE_INTERRUPT'; then
            aws s3 rm {s3}/_SIMULATE_INTERRUPT --region {region} 2>/dev/null || true
            trigger_interrupt '{{"action":"terminate","time":"simulated","source":"ui-button"}}'
        fi
    done
) &
UI_INTERRUPT_WATCHER_PID=$!

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

# Fetch inputs from our own prefix. On a fresh job the local side
# uploaded them before launch; on a retry the prior run's inputs
# are still there (same prefix). Then opportunistically sync tmp/
# (mezzanines rescued by a prior spot interrupt) and output/ (any
# completed variants with their .done sidecars). Both are no-ops on
# a fresh job — the prefixes simply don't exist yet.
stage remote:fetch-inputs running
for bn in {basenames}; do
    aws s3 cp {s3}/input/${{bn}} /work/input/${{bn}} --region {region}
done
aws s3 sync {s3}/tmp/    /work/tmp/    --region {region} 2>/dev/null || true
aws s3 sync {s3}/output/ /work/output/ --region {region} 2>/dev/null || true
stage remote:fetch-inputs done 100

# Total clip count — used for the ENCODER-FILE marker so the UI can
# render "File N of M: <name>" and reset its stage bars between clips.
TOTAL_CLIPS=0
for _bn in {basenames}; do TOTAL_CLIPS=$((TOTAL_CLIPS + 1)); done
CLIP_IDX=0

for bn in {basenames}; do
    CURRENT_CLIP="${{bn}}"
    CLIP_IDX=$((CLIP_IDX + 1))
    stem="${{bn%.*}}"
    base="${{stem}}_p200"

    # Per-clip boundary marker — local Go scanner archives the previous
    # clip's stages and resets the UI bars. The per-clip docker run of
    # cli_local.py below emits a fresh ENCODER-PLAN that populates them.
    printf '[[ENCODER-FILE index=%d total=%d name=%s]]\\n' "$CLIP_IDX" "$TOTAL_CLIPS" "$bn"

    echo ">>> Encoding ${{bn}} -> /work/output/${{base}}_{{h264,hevc}}/"
    # The image's default ENTRYPOINT is the Go server (for local use);
    # we override to Python here to run the same pipeline the local
    # worker containers use. TMPDIR is pinned on the same filesystem
    # as output_dir so Shaka Packager's temp→rename step doesn't
    # cross a Docker overlay boundary (EXDEV).
    # The container's own stdout carries the per-variant ENCODER-STAGE
    # markers from cli_local.py → they flow through docker → our
    # /var/log/cloud-encode.log → S3 → local poll tail.
    # cli_local.py's encode_all() checks for a matching `.done` sidecar
    # on each expected variant output file and skips re-encoding the
    # complete ones. On a fresh job nothing's there; on a retry any
    # variant that was complete on the prior instance (rescued via
    # spot interrupt sync to S3, then re-downloaded here) gets reused.
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
