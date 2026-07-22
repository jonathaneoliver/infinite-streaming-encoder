#!/usr/bin/env bash
# Deploy current encoder code to a REMOTE worker box and (re)start its worker.
# Run from the master (Mac) repo root. Idempotent — safe to re-run.
#
#   deploy-worker.sh <ssh_target> <label> [remote_code_dir]
#
# Steps: rsync scripts/encoder → the box, ensure a worker image with temporalio,
# then start the worker container (run-worker.sh) pointed at the master's
# Temporal + MinIO. Env: MASTER_IP (LAN IP of the master, default 192.168.0.110),
# MINIO_ROOT_USER / MINIO_ROOT_PASSWORD.
set -euo pipefail
SSH_TARGET="${1:?usage: deploy-worker.sh <ssh_target> <label> [remote_code_dir]}"
LABEL="${2:?label (e.g. ubuntu) required}"
REMOTE_CODE="${3:-/tmp/encoder-src/encoder}"
MASTER_IP="${MASTER_IP:-192.168.0.110}"
BASE_IMAGE="${BASE_IMAGE:-ghcr.io/jonathaneoliver/encoder:latest}"

echo ">>> [$LABEL] $SSH_TARGET — syncing code"
ssh -o BatchMode=yes "$SSH_TARGET" "mkdir -p $REMOTE_CODE"
rsync -a --delete scripts/encoder/ "$SSH_TARGET:$REMOTE_CODE/"

echo ">>> [$LABEL] ensuring worker image (base + temporalio)"
ssh -o BatchMode=yes "$SSH_TARGET" "
  docker pull -q $BASE_IMAGE >/dev/null 2>&1 || true
  docker tag $BASE_IMAGE encoder:cur
  printf 'FROM encoder:cur\nRUN pip install --no-cache-dir temporalio\n' > /tmp/Dockerfile.temporal
  docker build -q -f /tmp/Dockerfile.temporal -t encoder-temporal:cur /tmp >/dev/null"

echo ">>> [$LABEL] (re)starting worker"
scp -q infra/local-cluster/run-worker.sh "$SSH_TARGET:/tmp/run-worker.sh"
ssh -o BatchMode=yes "$SSH_TARGET" "cat > /tmp/worker.env" <<EOF
TEMPORAL_ADDRESS=$MASTER_IP:7233
S3_ENDPOINT_URL=http://$MASTER_IP:9000
AWS_ACCESS_KEY_ID=${MINIO_ROOT_USER:-encoder}
AWS_SECRET_ACCESS_KEY=${MINIO_ROOT_PASSWORD:-encoder-secret}
ENCODER_IMAGE=encoder-temporal:cur
CODE_MOUNT=$REMOTE_CODE
WORKER_LABEL=$LABEL
WORKER_NAME=encode-worker
EOF
ssh -o BatchMode=yes "$SSH_TARGET" "bash /tmp/run-worker.sh /tmp/worker.env"
