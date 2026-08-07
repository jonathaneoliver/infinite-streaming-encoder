#!/usr/bin/env bash
# Deploy current encoder code to a REMOTE worker box and (re)start its worker.
# Run from the master (Mac) repo root. Idempotent — safe to re-run.
#
#   deploy-worker.sh <ssh_target> <label> [remote_code_dir]
#
# Auto-picks how the box gets its base image:
#   same arch as the master  -> TRANSFER the master's baked encoder:latest
#       (docker save | ssh docker load) — no GHCR auth, no cross-arch emulation.
#       Great for extra Apple-Silicon boxes (Mac Mini). Transferred once; later
#       deploys skip it (set FORCE_IMAGE=1 to re-send after a base/deps rebuild).
#   different arch           -> BUILD from the GHCR base (fast; just adds
#       temporalio). This is the Linux/amd64 path (ubuntu). With DEV_BUILD=1 it
#       instead builds the WORKING-TREE Dockerfile NATIVELY on the box — slower
#       first time, but bakes your uncommitted deps for that arch (no QEMU, no
#       push). Use it when you've changed requirements.txt / the Dockerfile.
# Either way current code is rsync'd and bind-mounted, so routine code deploys
# only move the small scripts/infinite_streaming_encoder dir, never the whole image.
#
# Env: MASTER_IP (LAN IP of the master, default 192.168.1.10),
#      MINIO_ROOT_USER / MINIO_ROOT_PASSWORD, FORCE_IMAGE=1 to re-transfer a
#      same-arch image (e.g. after a dep change), DEV_BUILD=1 to native-build on
#      a cross-arch box.
set -euo pipefail

# Fail fast when a box goes away MID-DEPLOY rather than blocking on a dead TCP
# connection until the OS gives up (~2h). Same options, same reason, as
# deploy-worker-ghcr.sh — this path ships a ~900MB image over the wire, so it
# sits in the vulnerable state for even longer.
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8
          -o ServerAliveInterval=10 -o ServerAliveCountMax=3)

SSH_TARGET="${1:?usage: deploy-worker.sh <ssh_target> <label> [remote_code_dir]}"
LABEL="${2:?label (e.g. macmini) required}"
REMOTE_CODE="${3:-/tmp/encoder-src/encoder}"
MASTER_IP="${MASTER_IP:-192.168.1.10}"
# Derived from GHCR_ORG, never a personal namespace (#149). The Makefile
# exports GHCR_ORG from .env; standalone callers can pass BASE_IMAGE directly.
BASE_IMAGE="${BASE_IMAGE:-${GHCR_ORG:?GHCR_ORG is not set — set it in .env (e.g. ghcr.io/yourname) or pass BASE_IMAGE}/infinite-streaming-encoder:latest}"
MASTER_IMAGE="${MASTER_IMAGE:-encoder:latest}"

master_arch="$(docker image inspect "$MASTER_IMAGE" --format '{{.Architecture}}' 2>/dev/null || true)"
remote_arch="$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" 'docker version --format "{{.Server.Arch}}"' 2>/dev/null || true)"

if [ -n "$master_arch" ] && [ "$master_arch" = "$remote_arch" ]; then
    image="$MASTER_IMAGE"
    have="$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "docker image inspect $image >/dev/null 2>&1 && echo yes || echo no")"
    if [ "$have" != "yes" ] || [ "${FORCE_IMAGE:-}" = "1" ]; then
        echo ">>> [$LABEL] $SSH_TARGET — same arch ($remote_arch): transferring $image (~900MB)"
        docker save "$image" | ssh "${SSH_OPTS[@]}" "$SSH_TARGET" 'docker load'
    else
        echo ">>> [$LABEL] $SSH_TARGET — same arch ($remote_arch): $image already present (FORCE_IMAGE=1 to re-send)"
    fi
elif [ "${DEV_BUILD:-}" = "1" ]; then
    echo ">>> [$LABEL] $SSH_TARGET — arch $remote_arch vs master $master_arch: DEV_BUILD — building the working-tree image natively on the box"
    image="encoder-dev:cur"
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "mkdir -p /tmp/encoder-build"
    # Ship just the Dockerfile build context (mirrors the Dockerfile's COPY set),
    # then build natively on the box — bakes uncommitted deps for its arch.
    rsync -a --delete Dockerfile requirements.txt go.mod cmd internal scripts static \
        "$SSH_TARGET:/tmp/encoder-build/"
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "cd /tmp/encoder-build && docker build -q -t $image . >/dev/null"
else
    echo ">>> [$LABEL] $SSH_TARGET — arch $remote_arch vs master $master_arch: build from GHCR base (DEV_BUILD=1 to native-build uncommitted deps)"
    image="encoder-temporal:cur"
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
        docker pull -q $BASE_IMAGE >/dev/null 2>&1 || true
        docker tag $BASE_IMAGE encoder:cur
        printf 'FROM encoder:cur\nRUN pip install --no-cache-dir temporalio\n' > /tmp/Dockerfile.temporal
        docker build -q -f /tmp/Dockerfile.temporal -t $image /tmp >/dev/null"
fi

echo ">>> [$LABEL] syncing code + (re)starting worker"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "mkdir -p $REMOTE_CODE"
rsync -a --delete scripts/infinite_streaming_encoder/ "$SSH_TARGET:$REMOTE_CODE/"
scp -q "${SSH_OPTS[@]}" infra/local-cluster/run-worker.sh "$SSH_TARGET:/tmp/run-worker.sh"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "cat > /tmp/worker.env" <<EOF
TEMPORAL_ADDRESS=$MASTER_IP:7233
S3_ENDPOINT_URL=http://$MASTER_IP:9000
AWS_ACCESS_KEY_ID=${MINIO_ROOT_USER:-encoder}
AWS_SECRET_ACCESS_KEY=${MINIO_ROOT_PASSWORD:-encoder-secret}
ENCODER_IMAGE=$image
CODE_MOUNT=$REMOTE_CODE
WORKER_LABEL=$LABEL
WORKER_NAME=encode-worker
EOF
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "bash /tmp/run-worker.sh /tmp/worker.env"
