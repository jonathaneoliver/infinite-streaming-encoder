# Private repo for the encoder image. Batch tasks run in private
# subnets and reach ECR via the Interface endpoints from the network
# module — no internet egress.

resource "aws_ecr_repository" "encoder_worker" {
  name                 = "encoder-worker"
  image_tag_mutability = "MUTABLE" # we overwrite :latest on every push

  # Let `tofu destroy` remove the repo even when it still holds images.
  # Without this, teardown fails with RepositoryNotEmptyException and you
  # have to hand-delete every image tag first.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "encoder-worker"
  }
}

# Retain only the last 10 images to keep storage bounded. Job
# definitions pin to a specific tag (short-sha) so old images keep
# existing rollback targets; anything past 10 rolls off.
resource "aws_ecr_lifecycle_policy" "encoder_worker" {
  repository = aws_ecr_repository.encoder_worker.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep last 10 image versions"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# (Removed) A Docker Hub pull-through cache rule used to live here, but
# AWS now requires a Secrets Manager credential for the Docker Hub upstream
# ("UnsupportedUpstreamRegistryException: requires authentication"). It was
# never on the critical path — Batch workers pull encoder-worker:<tag>
# directly, and our image builds FROM python:3.12-slim on the host at
# `docker build` time, not inside Batch. Re-add with a credential secret if
# a future base image needs to be mirrored.
