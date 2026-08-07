#!/usr/bin/env bash
# Deploy a distributed-local worker to a REMOTE box that PULLS the image from
# GHCR — no image transfer, no per-box build. The published image already carries
# both the encoder code and temporalio, so the box just pulls it and runs the
# worker. Works for any arch (GHCR is multi-arch). Run from the master (repo
# root). Idempotent — safe to re-run.
#
# This is the published/committed path (used by `make dist-deploy-ghcr` and
# `make farm`). For iterating on UNCOMMITTED changes, use `make farm-dev`, which
# builds locally and goes through deploy-worker.sh (image transfer + code mount).
#
#   deploy-worker-ghcr.sh <ssh_target> <label>
#
# Env:
#   MASTER_IP            LAN IP the worker dials for Temporal + MinIO (default 192.168.1.10)
#   IMAGE                GHCR ref to pull (default $GHCR_ORG/infinite-streaming-encoder:latest)
#   GHCR_PAT             set only if the package is private (piped over ssh stdin)
#   GHCR_USERNAME        GHCR user for the login (required; from .env)
#   MINIO_ROOT_USER / MINIO_ROOT_PASSWORD   MinIO creds (default encoder / encoder-secret)
set -euo pipefail

# Fail fast when a box goes away MID-DEPLOY. `docker pull` of a ~900MB image is
# the longest step and the likeliest moment for a laptop-class worker to sleep;
# with no keepalive, ssh then blocks on a dead TCP connection until the OS gives
# up (~2h by default). Observed: a sleeping box held a whole deploy for 22
# minutes with zero output before anyone noticed, and it would have kept going.
# ConnectTimeout covers "never answered", ServerAlive* covers "answered, then
# vanished" — the second is the one that actually bit.
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8
          -o ServerAliveInterval=10 -o ServerAliveCountMax=3)

SSH_TARGET="${1:?usage: deploy-worker-ghcr.sh <ssh_target> <label>}"
LABEL="${2:?label (e.g. ubuntu) required}"
MASTER_IP="${MASTER_IP:-192.168.1.10}"
# Both derive from .env rather than a personal namespace (#149) — a fork that
# inherited these would pull/login against an account it does not own.
IMAGE="${IMAGE:-${GHCR_ORG:?GHCR_ORG is not set — set it in .env (e.g. ghcr.io/yourname) or pass IMAGE}/infinite-streaming-encoder:latest}"
GHCR_USERNAME="${GHCR_USERNAME:?GHCR_USERNAME is not set — the GitHub account to docker-login with (see .env.example)}"

echo ">>> [$LABEL] $SSH_TARGET — pull $IMAGE from GHCR"
# Log in only when a PAT is provided (private package); public needs none. Pipe
# the PAT over ssh stdin so it never appears in the remote process arg list.
if [ -n "${GHCR_PAT:-}" ]; then
    printf '%s' "$GHCR_PAT" | ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
        "docker login ghcr.io -u '$GHCR_USERNAME' --password-stdin"
fi
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "docker pull -q '$IMAGE'"

echo ">>> [$LABEL] $SSH_TARGET — (re)starting worker"
scp -q "${SSH_OPTS[@]}" infra/local-cluster/run-worker.sh "$SSH_TARGET:/tmp/run-worker.sh"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "cat > /tmp/worker.env" <<EOF
TEMPORAL_ADDRESS=$MASTER_IP:7233
S3_ENDPOINT_URL=http://$MASTER_IP:9000
AWS_ACCESS_KEY_ID=${MINIO_ROOT_USER:-encoder}
AWS_SECRET_ACCESS_KEY=${MINIO_ROOT_PASSWORD:-encoder-secret}
ENCODER_IMAGE=$IMAGE
WORKER_LABEL=$LABEL
WORKER_NAME=encode-worker
EOF
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "bash /tmp/run-worker.sh /tmp/worker.env"
