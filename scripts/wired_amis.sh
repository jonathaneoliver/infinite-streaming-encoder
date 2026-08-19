#!/usr/bin/env bash
# Every AMI id AWS Batch could boot right now, one per line (#370).
#
# There are TWO sources and only one of them is terraform's:
#
#   1. computeResources.ec2Configuration[].imageIdOverride — what terraform sets.
#   2. The ImageId inside the launch template Batch GENERATES for itself when an
#      override is set (Batch-lt-<uuid>-rev-N). Terraform does not own that copy
#      and cannot clear it.
#
# (2) is the one that strands. Deregister an AMI it still names and the compute
# environment goes INVALID and places nothing:
#
#     CLIENT_ERROR - You must use a valid fully-formed launch template.
#     The image id '[ami-...]' does not exist
#
# Both guards in the Makefile used to consult (1) alone, so whenever the
# override was null — which is exactly the state `ami-down` leaves behind, and
# the state a cleared apply produces — they protected nothing.
#
# Prints nothing when Batch could boot no specific AMI (the healthy default:
# Batch picks its own latest ECS image). Silent on AWS errors by design: a
# caller must treat "no output" as "could not prove anything is wired", and the
# callers here refuse rather than delete when they cannot prove it.
set -uo pipefail
REGION="${AWS_REGION:-us-west-2}"

{
  aws batch describe-compute-environments --region "$REGION" \
    --query "computeEnvironments[?contains(computeEnvironmentName,'infinite-streaming-encoder')].computeResources.ec2Configuration[].imageIdOverride" \
    --output text 2>/dev/null

  # Batch names its generated templates Batch-lt-*. Every VERSION is checked,
  # not just the default: the compute environment validates against the revision
  # it recorded, which is not necessarily the newest.
  lts=$(aws ec2 describe-launch-templates --region "$REGION" \
          --filters "Name=launch-template-name,Values=Batch-lt-*" \
          --query 'LaunchTemplates[].LaunchTemplateId' --output text 2>/dev/null)
  for lt in $lts; do
    [ -z "$lt" ] && continue
    [ "$lt" = "None" ] && continue
    aws ec2 describe-launch-template-versions --region "$REGION" \
      --launch-template-id "$lt" \
      --query 'LaunchTemplateVersions[].LaunchTemplateData.ImageId' \
      --output text 2>/dev/null
  done
} | tr '\t' '\n' | grep -E '^ami-' | sort -u
