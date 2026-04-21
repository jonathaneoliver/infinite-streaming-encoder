# Private repo for the encoder image. Batch tasks run in private
# subnets and reach ECR via the Interface endpoints from the network
# module — no internet egress.

resource "aws_ecr_repository" "encoder_worker" {
  name                 = "encoder-worker"
  image_tag_mutability = "MUTABLE"   # we overwrite :latest on every push

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

# Pull-through cache so images pulled under the `dockerhub/` prefix
# resolve to Docker Hub transparently. First pull mirrors into ECR,
# subsequent pulls serve from cache via the VPC endpoint. Only
# actually useful if a future base image lives on Docker Hub; our
# own image builds FROM python:3.12-slim already hit Docker Hub at
# `docker build` time on the host, not inside Batch.
resource "aws_ecr_pull_through_cache_rule" "dockerhub" {
  ecr_repository_prefix = "dockerhub"
  upstream_registry_url = "registry-1.docker.io"
}
