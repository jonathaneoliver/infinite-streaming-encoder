#!/usr/bin/env bash
# Deploy a distributed-local worker to a REMOTE box, pulling the image from GHCR
# (no image transfer, any arch). Run from the master (repo root). Idempotent.
#
#   deploy-worker-ghcr.sh <ssh_target> <label>
#
# Modes:
#   default   Pull $IMAGE and run the worker from the image's baked-in code.
#   DEV=1     Fast dev sync: pull $IMAGE only if the box lacks it, rsync the
#             local scripts/encoder to $REMOTE_CODE and bind-mount it into the
#             worker (CODE_MOUNT), so a restart runs your working-tree code —
#             no rebuild, no re-pull.
#
# Env:
#   MASTER_IP            LAN IP the worker dials for Temporal + MinIO (default 192.168.0.110)
#   IMAGE                GHCR ref (default ghcr.io/jonathaneoliver/encoder:latest)
#   GHCR_PAT             set only if the package is private (piped over ssh stdin)
#   GHCR_USERNAME        GHCR user for the login (default jonathaneoliver)
#   MINIO_ROOT_USER / MINIO_ROOT_PASSWORD   MinIO creds (default encoder / encoder-secret)
#   DEV=1                enable dev sync (code bind-mount)
#   REMOTE_CODE          remote path for the synced code in DEV mode (default /tmp/encoder-src/encoder)
set -euo pipefail

SSH_TARGET="${1:?usage: deploy-worker-ghcr.sh <ssh_target> <label>}"
LABEL="${2:?label (e.g. ubuntu) required}"
MASTER_IP="${MASTER_IP:-192.168.0.110}"
IMAGE="${IMAGE:-ghcr.io/jonathaneoliver/encoder:latest}"
GHCR_USERNAME="${GHCR_USERNAME:-jonathaneoliver}"
REMOTE_CODE="${REMOTE_CODE:-/tmp/encoder-src/encoder}"

# login_pull <always|if-missing> — docker login (only if GHCR_PAT set, piped
# over ssh stdin so the PAT never lands on the remote arg list) then pull.
login_pull() {
    if [ "$1" = if-missing ] && \
       ssh -o BatchMode=yes "$SSH_TARGET" "docker image inspect '$IMAGE' >/dev/null 2>&1"; then
        echo ">>> [$LABEL] $IMAGE already present — skip pull"
        return
    fi
    if [ -n "${GHCR_PAT:-}" ]; then
        printf '%s' "$GHCR_PAT" | ssh -o BatchMode=yes "$SSH_TARGET" \
            "docker login ghcr.io -u '$GHCR_USERNAME' --password-stdin"
    fi
    ssh -o BatchMode=yes "$SSH_TARGET" "docker pull -q '$IMAGE'"
}

CODE_MOUNT_LINE=""
if [ "${DEV:-}" = "1" ]; then
    echo ">>> [$LABEL] $SSH_TARGET — dev sync (rsync code + restart)"
    login_pull if-missing
    ssh -o BatchMode=yes "$SSH_TARGET" "mkdir -p '$REMOTE_CODE'"
    rsync -a --delete scripts/encoder/ "$SSH_TARGET:$REMOTE_CODE/"
    CODE_MOUNT_LINE="CODE_MOUNT=$REMOTE_CODE"
else
    echo ">>> [$LABEL] $SSH_TARGET — pull $IMAGE from GHCR"
    login_pull always
fi

scp -q infra/local-cluster/run-worker.sh "$SSH_TARGET:/tmp/run-worker.sh"
ssh -o BatchMode=yes "$SSH_TARGET" "cat > /tmp/worker.env" <<EOF
TEMPORAL_ADDRESS=$MASTER_IP:7233
S3_ENDPOINT_URL=http://$MASTER_IP:9000
AWS_ACCESS_KEY_ID=${MINIO_ROOT_USER:-encoder}
AWS_SECRET_ACCESS_KEY=${MINIO_ROOT_PASSWORD:-encoder-secret}
ENCODER_IMAGE=$IMAGE
${CODE_MOUNT_LINE}
WORKER_LABEL=$LABEL
WORKER_NAME=encode-worker
EOF
ssh -o BatchMode=yes "$SSH_TARGET" "bash /tmp/run-worker.sh /tmp/worker.env"
