#!/usr/bin/env bash
# `make status` — what is actually deployed where, against what the working tree
# says it should be.
#
# Written because answering that took 4-5 ad-hoc aws/docker/curl calls, and
# getting it wrong caused real mistakes: baking an AMI against a tag that was
# never in ECR, applying a state machine against a server that could not satisfy
# it, and promoting from a tag built on a dirty tree. Every line below is a
# question that went wrong at least once.
#
# Read-only. Degrades line by line — no AWS creds, no server, no network each
# print a dash rather than failing, so it is safe to run anywhere.
set -u

if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1m'; D=$'\033[2m'; N=$'\033[0m'
else G=; Y=; R=; B=; D=; N=; fi
row(){ printf '  %-22s %s\n' "$1" "$2"; }
head(){ printf '\n%s%s%s\n' "$B" "$1" "$N"; }
same(){ [ "$1" = "$2" ] && printf '%s' "$G" || printf '%s' "$Y"; }

REGION="${AWS_REGION:-us-west-2}"
PORT="${PORT:-8080}"
GHCR="${GHCR_ORG:-}/infinite-streaming-encoder"

head "Source"
HEAD_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo '-')
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '-')
DIRTY=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
# IMAGE_TAG is content-addressed: only commits touching image inputs bump it, so
# it legitimately lags HEAD after a docs- or Go-only change.
IMAGE_TAG=$(git log -1 --format=%h -- Dockerfile requirements.txt scripts static 2>/dev/null || echo '-')
row "branch / HEAD" "$BRANCH @ $HEAD_SHA$( [ "$DIRTY" != 0 ] && printf ' %s(%s file(s) dirty -> tags get -dirty)%s' "$Y" "$DIRTY" "$N" )"
row "IMAGE_TAG" "$IMAGE_TAG${D}  (last commit touching Dockerfile/requirements/scripts/static)${N}"

head "Local server"
V=$(curl -fsS --max-time 3 "http://localhost:$PORT/api/version" 2>/dev/null || true)
if [ -z "$V" ]; then
  row "state" "${D}not reachable on :$PORT${N}"
else
  SREV=$(printf '%s' "$V" | python3 -c "import json,sys;print(json.load(sys.stdin)['local'].get('revision','-'))" 2>/dev/null || echo '-')
  SMOUNT=$(printf '%s' "$V" | python3 -c "import json,sys;print(json.load(sys.stdin).get('dev_mount','-'))" 2>/dev/null || echo '-')
  # The Go control plane builds the Step Functions input. A server behind the
  # tree can emit input the applied state machine rejects outright.
  row "built from" "$(same "$SREV" "$HEAD_SHA")$SREV${N}$( [ "$SREV" != "$HEAD_SHA" ] && printf ' %s!= HEAD (run make restart)%s' "$Y" "$N" )"
  row "dev_mount" "$SMOUNT${D}  (true = running bind-mounted working-tree Python)${N}"
  JOBS=$(curl -fsS --max-time 3 "http://localhost:$PORT/api/jobs" 2>/dev/null \
    | python3 -c "import json,sys;j=json.load(sys.stdin);print(sum(1 for x in j if x.get('status')=='running'))" 2>/dev/null || echo '-')
  row "local encodes running" "$JOBS"
fi

head "GHCR"
if [ -z "${GHCR_ORG:-}" ]; then row "GHCR_ORG" "${Y}unset — set it in .env${N}"; else
  LATEST=$(docker buildx imagetools inspect "$GHCR:latest" 2>/dev/null | awk '/^Digest:/{print $2;exit}')
  row ":latest" "${LATEST:--}"
  TAGD=$(docker buildx imagetools inspect "$GHCR:$IMAGE_TAG" 2>/dev/null | awk '/^Digest:/{print $2;exit}')
  if [ -n "$TAGD" ]; then
    row ":$IMAGE_TAG" "$(same "$TAGD" "${LATEST:-x}")$TAGD${N}$( [ "$TAGD" = "${LATEST:-x}" ] && printf ' %s= :latest (this tree is released)%s' "$G" "$N" )"
  else
    row ":$IMAGE_TAG" "${Y}not published${N}"
  fi
fi

head "Cloud (AWS Batch)"
JD=$(aws batch describe-job-definitions --region "$REGION" --status ACTIVE --output json 2>/dev/null || true)
if [ -z "$JD" ]; then row "state" "${D}no AWS access${N}"; else
  printf '%s' "$JD" | python3 -c '
import json,sys
d=json.load(sys.stdin).get("jobDefinitions",[])
revs=sorted({j["revision"] for j in d})
print("  %-22s %s" % ("job definitions", "%d defs, rev %s" % (len(d), ",".join(map(str,revs)))))
' 2>/dev/null || row "job definitions" "-"
  # Tag extraction lives in scripts/cloud_payload.sh — `make fleet-check` needs
  # the same value to compare against the farm's payload, and two copies of the
  # parse is how they drift. Piped our already-fetched JSON so this stays one
  # describe call. Note the "Farm workers" rows below are NOT comparable to this:
  # they print each container's image reference (…:latest), which is identical
  # whatever payload the box runs. `make fleet-check` does that comparison (#300).
  row "pinned image tag" "$(printf '%s' "$JD" | bash scripts/cloud_payload.sh - | tr '\n' ',' | sed 's/,$//' || echo '-')"
  EX=$(aws stepfunctions list-executions --region "$REGION" \
    --state-machine-arn "$(cd infra/terraform 2>/dev/null && tofu output -no-color -raw state_machine_arn 2>/dev/null)" \
    --status-filter RUNNING --query 'length(executions)' --output text 2>/dev/null || echo '-')
  row "executions running" "${EX:--}"
  # The AMI is looked up by the CURRENT IMAGE_TAG, so every release orphans a
  # previously baked one — silently, reverting to a ~60s pull-on-boot.
  AMI=$(aws batch describe-compute-environments --region "$REGION" \
    --query "computeEnvironments[?contains(computeEnvironmentName,'infinite-streaming-encoder')].computeResources.ec2Configuration[0].imageIdOverride | [0]" \
    --output text 2>/dev/null || echo None)
  if [ "$AMI" = "None" ] || [ -z "$AMI" ]; then
    row "worker AMI" "${D}none wired (pull-on-boot)${N}"
  else
    AMITAG=$(aws ec2 describe-images --region "$REGION" --image-ids "$AMI" \
      --query 'Images[0].Tags[?Key==`image_tag`].Value | [0]' --output text 2>/dev/null || echo '-')
    row "worker AMI" "$(same "$AMITAG" "$IMAGE_TAG")$AMI (baked for $AMITAG)${N}$( [ "$AMITAG" != "$IMAGE_TAG" ] && printf ' %s!= IMAGE_TAG -> cache miss%s' "$Y" "$N" )"
  fi
fi

head "Farm workers"
for c in "${CONTAINER_NAME:-infinite-streaming-encoder}" "${DIST_WORKER_CONTAINER:-encode-worker}"; do
  IMG=$(docker inspect "$c" --format '{{.Config.Image}}' 2>/dev/null || true)
  row "$c" "${IMG:-${D}not running${N}}"
done
for w in ${DIST_WORKERS:-}; do
  host="${w#*=}"; label="${w%%=*}"
  IMG=$(ssh -o BatchMode=yes -o ConnectTimeout=4 "$host" \
    "docker inspect ${DIST_WORKER_CONTAINER:-encode-worker} --format '{{.Config.Image}}'" 2>/dev/null || true)
  row "$label" "${IMG:-${D}unreachable${N}}${D}  ($host)${N}"
done
echo
