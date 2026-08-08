# Private repo for the encoder image. Batch workers pull it over the
# INTERNET GATEWAY: they run in the network module's PUBLIC subnets with
# auto-assigned public IPs, behind an egress-only security group.
#
# There are no ecr.api / ecr.dkr interface endpoints to pull through. That
# layout was considered and rejected on cost — interface endpoints bill
# ~$22/mo standing whether or not anything encodes, and this fleet is idle
# most of the time. Only the free S3 Gateway endpoint is kept. See the
# header of modules/network/main.tf, which is authoritative for the
# topology.
#
# Said explicitly because the previous comment here claimed the opposite
# ("private subnets… via the Interface endpoints… no internet egress"),
# and #188 spent time reasoning about worker reachability against it.
# Workers DO have egress; what they lack is anything to reach the server
# ON, since it sits behind home NAT with no public address.

resource "aws_ecr_repository" "encoder_worker" {
  name                 = "infinite-streaming-encoder-worker"
  image_tag_mutability = "MUTABLE" # we overwrite :latest on every push

  # Let `tofu destroy` remove the repo even when it still holds images.
  # Without this, teardown fails with RepositoryNotEmptyException and you
  # have to hand-delete every image tag first.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "infinite-streaming-encoder-worker"
  }
}

# Keep storage bounded WITHOUT letting throwaway pushes evict releases.
#
# The previous single rule (tagStatus "any", keep last 10) counted every
# manifest equally, so dev churn competed with released images for the same ten
# slots. Two consequences, both observed: pushing a tag does not delete the
# image it replaced — it only UNTAGS it, and the repo had accumulated 12
# untagged images holding 1.8 GB of 2.4 GB; and because tagged images were
# eligible too, a burst of pushes could expire an image a queued Batch job was
# about to pull, surfacing as a pull failure that reads like a bad tag.
#
# Rules are evaluated in priority order and an image is expired by the first
# that matches. A tagStatus "any" rule must come last — AWS rejects the policy
# otherwise — which is why the keep-last-N backstop is priority 3.
resource "aws_ecr_lifecycle_policy" "encoder_worker" {
  repository = aws_ecr_repository.encoder_worker.name

  policy = jsonencode({
    rules = [
      # The actual reclaim. An untagged manifest is one a later push replaced;
      # nothing can reference it by name, so it is pure residue. A day's grace
      # keeps it recoverable by digest if a push is rolled back.
      {
        rulePriority = 1
        description  = "expire untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      # Testing-lane images (cloud-dev-up publishes dev-<branch>-<sha>). Bounded
      # by age, not count, so a busy week of testing cannot roll a release off.
      {
        rulePriority = 2
        description  = "expire dev-* test images after 7 days"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["dev-"]
          countType     = "sinceImagePushed"
          countUnit     = "days"
          countNumber   = 7
        }
        action = { type = "expire" }
      },
      # Backstop for released images only, now that churn is handled above.
      # Raised from 10: these are rollback targets and job definitions pin by
      # tag, so depth here is what makes a rollback possible.
      {
        rulePriority = 3
        description  = "keep last 20 remaining images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 20
        }
        action = { type = "expire" }
      },
    ]
  })
}

# (Removed) A Docker Hub pull-through cache rule used to live here, but
# AWS now requires a Secrets Manager credential for the Docker Hub upstream
# ("UnsupportedUpstreamRegistryException: requires authentication"). It was
# never on the critical path — Batch workers pull infinite-streaming-encoder-worker:<tag>
# directly, and our image builds FROM python:3.12-slim on the host at
# `docker build` time, not inside Batch. Re-add with a credential secret if
# a future base image needs to be mirrored.
