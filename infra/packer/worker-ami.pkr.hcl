# Bakes a Batch worker AMI with the encoder image already pulled, so a
# cold spot instance skips the ~60s ECR pull. This is an OPT-IN cache:
# `make infra-apply` uses it only when an AMI is tagged with the current
# image SHA, and falls back to pull-on-boot otherwise. Tear it down with
# `make unbake-ami` when you're done encoding so the EBS snapshot stops
# billing (~$1.50/mo while it exists).
#
#   make bake-ami       # build it (a few cents, ~5-10 min)
#   make infra-apply    # resolves + wires the AMI by SHA
#   make unbake-ami     # deregister + delete snapshot -> $0 standing cost
#
# Start FROM the ECS-optimized Graviton AMI so the result is still a
# valid Batch worker (keeps ecs-init / ecs agent / docker); we only add
# the pre-pulled image layer on top.

packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = ">= 1.2.0"
    }
  }
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "ecr_repo" {
  type        = string
  description = "Full ECR repo URL, e.g. 123456789012.dkr.ecr.us-west-2.amazonaws.com/encoder-worker"
}

variable "image_tag" {
  type        = string
  description = "Immutable image tag to pre-pull (the git short-sha)."
}

variable "instance_type" {
  type    = string
  default = "c7g.large"
}

variable "instance_profile" {
  type        = string
  description = "Existing IAM instance profile for the build instance (needs ECR pull). The Batch worker profile already has it."
  default     = "encoder-batch-instance"
}

# Latest ECS-optimized Amazon Linux 2023 Graviton (arm64) AMI. Same
# family Batch launches by default, so the baked AMI stays Batch-valid.
data "amazon-ami" "ecs_arm" {
  filters = {
    name                = "al2023-ami-ecs-hvm-*-arm64"
    virtualization-type = "hvm"
    root-device-type    = "ebs"
  }
  owners      = ["amazon"]
  most_recent = true
  region      = var.region
}

source "amazon-ebs" "worker" {
  region        = var.region
  instance_type = var.instance_type
  source_ami    = data.amazon-ami.ecs_arm.id
  ssh_username  = "ec2-user"

  # AMI name carries the SHA; the image_tag tag is what `make infra-apply`
  # and `make unbake-ami` key off of.
  ami_name = "encoder-worker-${var.image_tag}"

  # Reuse the Batch instance profile (encoder-batch-instance) for the
  # build. It already carries AmazonEC2ContainerServiceforEC2Role, which
  # grants ECR pull — exactly what the provisioner needs. Reusing an
  # existing profile also avoids the temporary-instance-profile path,
  # whose IAM eventual-consistency wait was mis-reporting a hard
  # RunInstances error as "timed out waiting for IAM to propagate".
  iam_instance_profile = var.instance_profile

  tags = {
    Name      = "encoder-worker"
    image_tag = var.image_tag
    ManagedBy = "packer"
  }
}

build {
  name    = "encoder-worker"
  sources = ["source.amazon-ebs.worker"]

  # Log in to ECR and pull the exact SHA-tagged image so it's resident
  # in the AMI's docker image cache. prefer-cached on the worker then
  # serves it without an ECR round-trip.
  provisioner "shell" {
    inline = [
      "set -euo pipefail",
      "REGISTRY=$(echo ${var.ecr_repo} | cut -d/ -f1)",
      "aws ecr get-login-password --region ${var.region} | sudo docker login --username AWS --password-stdin $REGISTRY",
      "sudo docker pull ${var.ecr_repo}:${var.image_tag}",
      "sudo docker images",
    ]
  }
}
