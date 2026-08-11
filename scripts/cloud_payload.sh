#!/usr/bin/env bash
# What encoder payload the CLOUD is on: the image tag pinned by the ACTIVE Batch
# job definitions, one distinct tag per line. Empty output means "could not tell"
# — no AWS access, or nothing applied — never "nothing is deployed".
#
# One definition, two callers, because they answer different halves of the same
# question (#300):
#
#   scripts/status.sh   reports it as a fact, next to GHCR and the AMI
#   make fleet-check    compares it against the FARM's payload and judges
#
# The comparison is the part that was missing. status.sh has printed this tag for
# a while, but its "Farm workers" rows print each container's image REFERENCE
# (ghcr.io/…:latest) — a mutable tag that reads identically whatever payload the
# box is actually running. So the two halves looked comparable and were not, and
# the 2026-08-11 split (farm on 4ab3e12, job definitions on 459251f) still had to
# be found by reading a tofu plan diff.
#
# Fetches for itself, or reads job-definitions JSON on stdin when passed "-", so
# a caller that already has it (status.sh does, for the revision row) pays for
# one describe call rather than two. The mode is an explicit argument rather than
# a `[ -t 0 ]` guess: a Makefile recipe's stdin is neither a terminal nor a pipe
# anyone intended, so guessing silently reads nothing and reports "cannot tell".
#
# ACTIVE only: a deploy leaves deregistered revisions behind, and including them
# would report every tag the stack has ever run.
set -uo pipefail

REGION="${AWS_REGION:-us-west-2}"
REPO="${ECR_REPO:-}"

if [ "${1:-}" = "-" ]; then
    JD=$(cat)
else
    JD=$(aws batch describe-job-definitions --region "$REGION" --status ACTIVE \
         --output json 2>/dev/null || true)
fi
[ -n "$JD" ] || exit 0

# Match job definitions by IMAGE, not by name prefix, so this needs no naming
# convention to stay true — ECR_REPO is the repo this stack deploys to, by
# definition. With no ECR_REPO to match on, report every tag found and let the
# caller say it could not narrow it.
printf '%s' "$JD" | REPO="$REPO" python3 -c '
import json, os, sys
repo = os.environ.get("REPO", "")
defs = json.load(sys.stdin).get("jobDefinitions", [])
imgs = [d.get("containerProperties", {}).get("image", "") for d in defs]
if repo:
    imgs = [i for i in imgs if repo in i]
print("\n".join(sorted({i.rsplit(":", 1)[-1] for i in imgs if ":" in i})))
' 2>/dev/null || true
