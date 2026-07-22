#!/usr/bin/env bash
# Run a distributed-local encode worker on THIS box. The worker is a container
# from the encoder image running `encoder.temporal_worker`; it polls the Temporal
# server (outbound only) and runs cli_phase encodes against MinIO. --restart
# unless-stopped means it comes back on Docker/host start — boots on power-on.
#
# One worker per box; each contributes its cores. Configure via env or an env
# file passed as $1 (see worker.env.example):
#   TEMPORAL_ADDRESS   host:7233 of the Temporal frontend (the master box's LAN IP)
#   S3_ENDPOINT_URL    http://<master-LAN-IP>:9000  (MinIO)
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION   MinIO creds
#   ENCODE_SLOTS       max concurrent encodes here (default: cores/2)
#   ENCODER_IMAGE      image to run (default: encoder:latest)
#   CODE_MOUNT         optional host path bind-mounted over /app/scripts/encoder
#                      (only needed if ENCODER_IMAGE is stale — e.g. ubuntu's
#                      pulled image before a current one is published)
#   WORKER_NAME        container name (default: encode-worker)
set -euo pipefail
[ -n "${1:-}" ] && set -a && . "$1" && set +a

: "${TEMPORAL_ADDRESS:?set TEMPORAL_ADDRESS (host:7233)}"
: "${S3_ENDPOINT_URL:?set S3_ENDPOINT_URL (http://host:9000)}"
: "${AWS_ACCESS_KEY_ID:?set AWS_ACCESS_KEY_ID}"
: "${AWS_SECRET_ACCESS_KEY:?set AWS_SECRET_ACCESS_KEY}"
ENCODER_IMAGE="${ENCODER_IMAGE:-encoder:latest}"
WORKER_NAME="${WORKER_NAME:-encode-worker}"
AWS_REGION="${AWS_REGION:-us-east-1}"

mount_args=()
[ -n "${CODE_MOUNT:-}" ] && mount_args=(-v "${CODE_MOUNT}:/app/scripts/encoder:ro")
slots_args=()
[ -n "${ENCODE_SLOTS:-}" ] && slots_args=(-e "ENCODE_SLOTS=${ENCODE_SLOTS}")

docker rm -f "$WORKER_NAME" >/dev/null 2>&1 || true
docker run -d --name "$WORKER_NAME" --restart unless-stopped \
  -e "TEMPORAL_ADDRESS=${TEMPORAL_ADDRESS}" \
  -e "S3_ENDPOINT_URL=${S3_ENDPOINT_URL}" \
  -e "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}" \
  -e "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}" \
  -e "AWS_REGION=${AWS_REGION}" \
  "${slots_args[@]}" "${mount_args[@]}" \
  --entrypoint python3 "$ENCODER_IMAGE" -m encoder.temporal_worker

echo "worker '$WORKER_NAME' started (image=$ENCODER_IMAGE, temporal=$TEMPORAL_ADDRESS)"
sleep 3
docker logs "$WORKER_NAME" 2>&1 | tail -2
