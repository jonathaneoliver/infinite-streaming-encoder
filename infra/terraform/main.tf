provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Application = "encoder-app"
      ManagedBy   = "terraform"
      Stack       = "encoder-batch"
    }
  }
}

# Convenience data sources reused across modules.
data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = var.region
}

# Auto-expire staged job data so S3 doesn't accumulate forever. The encoder
# writes everything under jobs/<id>/ (inputs, mezzanine, chunk intermediates,
# joined variants, packaged output); with chunking a long clip can leave
# thousands of chunk objects. Outputs are synced back to the local OUTPUT_DIR,
# so S3 is staging only — safe to expire. This is the cost safety net; the app
# can still delete a prefix eagerly on success on top of this.
#
# NOTE: var.s3_bucket is created outside this stack (a prerequisite) and must
# exist before apply. This manages only its jobs/ lifecycle, nothing else.
resource "aws_s3_bucket_lifecycle_configuration" "staging" {
  bucket = var.s3_bucket

  rule {
    id     = "expire-job-staging"
    status = "Enabled"

    filter {
      prefix = "jobs/"
    }

    expiration {
      days = var.staging_retention_days
    }

    # Failed multipart uploads bill silently until aborted — sweep them.
    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }
}

module "network" {
  source = "./modules/network"
  region = local.region
}

module "ecr" {
  source = "./modules/ecr"
}

module "iam" {
  source    = "./modules/iam"
  s3_bucket = var.s3_bucket
}

module "compute" {
  source            = "./modules/compute"
  vpc_id            = module.network.vpc_id
  subnet_ids        = module.network.subnet_ids
  security_group_id = module.network.batch_sg_id
  instance_role_arn = module.iam.instance_role_arn
  max_vcpus         = var.compute_max_vcpus
}

module "jobs" {
  source            = "./modules/jobs"
  ecr_repo_url      = module.ecr.repo_url
  image_tag         = var.image_tag
  task_role_arn     = module.iam.task_role_arn
  execution_role_arn = module.iam.execution_role_arn
  s3_bucket         = var.s3_bucket
  region            = local.region
}

module "workflow" {
  source              = "./modules/workflow"
  job_queue_arn       = module.compute.job_queue_arn
  job_def_arns        = module.jobs.job_def_arns
  workflow_role_arn   = module.iam.workflow_role_arn
}
