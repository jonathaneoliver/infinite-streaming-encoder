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

# SSH_OPTS, remote_stage and remote_ensure_dir.
# shellcheck source=infra/local-cluster/remote-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/remote-lib.sh"

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
# Per-user staging, not /tmp (#297). This path writes no code dir — the image
# carries it — but run-worker.sh and worker.env are just as wedgeable: a
# root-owned /tmp/worker.env fails the deploy exactly the same way, and being
# plainly named makes it likelier to be created by something else, not less.
REMOTE_STAGE="$(remote_stage "$SSH_TARGET")"
scp -q "${SSH_OPTS[@]}" infra/local-cluster/run-worker.sh "$SSH_TARGET:$REMOTE_STAGE/run-worker.sh"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "cat > '$REMOTE_STAGE/worker.env'" <<EOF
TEMPORAL_ADDRESS=$MASTER_IP:7233
S3_ENDPOINT_URL=http://$MASTER_IP:9000
AWS_ACCESS_KEY_ID=${MINIO_ROOT_USER:-encoder}
AWS_SECRET_ACCESS_KEY=${MINIO_ROOT_PASSWORD:-encoder-secret}
ENCODER_IMAGE=$IMAGE
WORKER_LABEL=$LABEL
WORKER_NAME=encode-worker
EOF
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "bash '$REMOTE_STAGE/run-worker.sh' '$REMOTE_STAGE/worker.env'"
