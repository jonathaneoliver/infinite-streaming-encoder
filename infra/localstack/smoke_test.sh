#!/usr/bin/env bash
# Offline smoke test for the AWS Batch phase pipeline, against LocalStack S3
# (community edition — no Batch/Step Functions, no AWS spend).
#
# It drives the SAME code each Batch job runs — `infinite_streaming_encoder.cli_local phase
# <name>` — by hand in DAG order, wiring S3 in/out at LocalStack. This
# exercises: the two-pass variant encode (--two-pass), every phase's S3
# download/upload, and the .done sidecar completeness invariant.
#
# What it does NOT cover (needs a real Batch/Spot run or LocalStack Pro):
#   - Step Functions orchestration (the Map/ItemSelector/Parallel graph)
#   - spot interruption + the Host EC2* retry path
#
# Prereqs: docker; the encoder image built locally (`make build`, tag
# `encoder`); LocalStack up (`docker compose -f infra/localstack/\
# docker-compose.yml up -d`). ffmpeg + aws-cli come from the encoder image.
#
# Usage:
#   ./infra/localstack/smoke_test.sh                 # generated 8s test clip
#   ./infra/localstack/smoke_test.sh path/to/clip.mp4  # your own source
set -euo pipefail

# ---- config ---------------------------------------------------------------
IMAGE="${ENCODER_IMAGE:-encoder}"
NET="encoder-localstack"
# Endpoint as seen from a container ON the localstack network (not the host).
S3_ENDPOINT="http://encoder-localstack:4566"
BUCKET="${BUCKET:-encoder-smoke}"
JOB_ID="${JOB_ID:-smoke-$$}"
PREFIX="s3://${BUCKET}/jobs/${JOB_ID}"
CODEC="${CODEC:-hevc}"
# Space-separated tier list, overridable: TIERS="360p 1080p 2160p" ./smoke_test.sh
IFS=' ' read -r -a TIERS <<< "${TIERS:-360p 540p}"
REGION="us-east-1"
SRC_ARG="${1:-}"

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

pass=0; fail=0
ok()   { echo "  ✅ $1"; pass=$((pass+1)); }
bad()  { echo "  ❌ $1"; fail=$((fail+1)); }
step() { echo; echo "▶ $1"; }

# Run the encoder image with an arbitrary entrypoint on the localstack net,
# with the S3-testing env wired in. Args after the entrypoint are passed through.
run_img() {
  local entry="$1"; shift
  docker run --rm --network "$NET" \
    -e S3_ENDPOINT_URL="$S3_ENDPOINT" \
    -e AWS_ACCESS_KEY_ID=test -e AWS_SECRET_ACCESS_KEY=test \
    -e AWS_REGION="$REGION" -e AWS_DEFAULT_REGION="$REGION" \
    -v "$SCRATCH:/work" \
    --entrypoint "$entry" "$IMAGE" "$@"
}

# aws-cli against LocalStack (path-style endpoint).
awsls() { run_img aws --endpoint-url "$S3_ENDPOINT" "$@"; }

# Run one pipeline phase. First arg = phase name, rest = phase flags.
phase() {
  echo "  · phase $1"
  run_img python3 -m infinite_streaming_encoder.cli_local phase "$@" >/dev/null
}

# True if an S3 key exists under the job prefix.
s3_has() { awsls s3 ls "${PREFIX}/$1" >/dev/null 2>&1; }

# ---- 0. sanity ------------------------------------------------------------
step "Checking LocalStack + image"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "error: image '$IMAGE' not found — run 'make build' first."; exit 1
fi
if ! curl -sf http://localhost:4566/_localstack/health >/dev/null; then
  echo "error: LocalStack not reachable on :4566 — run:"; \
  echo "  docker compose -f infra/localstack/docker-compose.yml up -d"; exit 1
fi
ok "LocalStack healthy, image '$IMAGE' present"

# ---- 1. bucket + input ----------------------------------------------------
step "Creating bucket + input clip"
awsls s3 mb "s3://${BUCKET}" >/dev/null 2>&1 || true
ok "bucket s3://${BUCKET}"

if [[ -n "$SRC_ARG" ]]; then
  cp "$SRC_ARG" "$SCRATCH/input.mp4"
  ok "using provided source $(basename "$SRC_ARG")"
else
  # 8s 720p test pattern + 440Hz tone so the audio phase has real work.
  run_img ffmpeg -hide_banner -loglevel error \
    -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=8" \
    -f lavfi -i "sine=frequency=440:duration=8" \
    -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest -y /work/input.mp4
  ok "generated 8s test clip"
fi
awsls s3 cp /work/input.mp4 "${PREFIX}/input/input.mp4" >/dev/null
s3_has "input/input.mp4" && ok "uploaded input" || bad "input upload failed"

# ---- 2. run the DAG by hand ----------------------------------------------
step "Phase 1: mezzanine"
phase mezzanine --s3-in "${PREFIX}/input/input.mp4" --s3-out "$PREFIX"
s3_has "mezzanine.mp4.done" && ok "mezzanine.mp4 + .done" || bad "mezzanine missing"

step "Phase 2: audio + variants (--two-pass)"
phase audio --s3-mezz "$PREFIX" --s3-out "$PREFIX"
# audio may legitimately be absent if the source had no track; our clip has one
s3_has "audio.mp4.done" && ok "audio.mp4 + .done" || bad "audio missing"

for t in "${TIERS[@]}"; do
  phase variant --codec "$CODEC" --tier "$t" \
    --s3-mezz "$PREFIX" --s3-out "$PREFIX" --two-pass
  if s3_has "${CODEC}_${t}.mp4.done"; then
    ok "two-pass ${CODEC} ${t} + .done"
  else
    bad "variant ${CODEC} ${t} missing"
  fi
done

step "Phase 3: package → hls → byteranges ($CODEC)"
phase package --codec "$CODEC" --s3-variants "$PREFIX" \
  --s3-audio "$PREFIX" --s3-out "$PREFIX"
phase hls        --codec "$CODEC" --s3-package "$PREFIX" --s3-out "$PREFIX"
phase byteranges --codec "$CODEC" --s3-package "$PREFIX" --s3-out "$PREFIX"

# Packaged output lands under output_<codec>/. Expect DASH + HLS artifacts.
step "Verifying packaged output"
listing="$(awsls s3 ls "${PREFIX}/output_${CODEC}/" --recursive 2>/dev/null || true)"
echo "$listing" | grep -q '\.m3u8'  && ok "HLS playlists (.m3u8)"  || bad "no .m3u8"
echo "$listing" | grep -q '\.mpd'   && ok "DASH manifest (.mpd)"   || bad "no .mpd"
echo "$listing" | grep -q '\.byteranges' && ok "byterange sidecars" || bad "no .byteranges"

# ---- summary --------------------------------------------------------------
echo
echo "──────────────────────────────────────────"
echo "  passed: $pass   failed: $fail   job: ${JOB_ID}"
echo "  inspect: $(printf '%q' aws) --endpoint-url http://localhost:4566 s3 ls ${PREFIX}/ --recursive"
echo "──────────────────────────────────────────"
[[ "$fail" -eq 0 ]]
