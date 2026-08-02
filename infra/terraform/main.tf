provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Application = "infinite-streaming-encoder-app"
      ManagedBy   = "terraform"
      Stack       = "infinite-streaming-encoder-batch"
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

  # This bucket has S3 versioning enabled (or suspended) — created outside this
  # stack. On such a bucket, expiring or deleting the "current" object only lays
  # a delete marker; the prior version becomes noncurrent and keeps billing, and
  # the marker itself lingers. So each prefix needs THREE things: expire the
  # current version, expire noncurrent versions, and reap the leftover delete
  # markers. AWS rejects `expired_object_delete_marker` in the same rule as an
  # `expiration { days }`, so the marker reap is a separate rule per prefix (#212).
  rule {
    id     = "expire-job-staging"
    status = "Enabled"

    filter {
      prefix = "jobs/"
    }

    expiration {
      days = var.staging_retention_days
    }

    # Noncurrent versions (left behind when a current object is overwritten or
    # deleted on a versioned bucket) free no space until expired. 1 day: staging
    # is transient, nothing needs an old version.
    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    # Failed multipart uploads bill silently until aborted — sweep them.
    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }

  # Reap delete markers left with no noncurrent versions under them — otherwise
  # every expiry/clear on the versioned bucket leaves a marker behind forever.
  rule {
    id     = "reap-job-staging-delete-markers"
    status = "Enabled"

    filter {
      prefix = "jobs/"
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  # Source-keyed mezzanine cache (mezz/<key>/mezzanine.mp4): reused across jobs
  # to skip the re-upload + mezzanine job for a source encoded again. Bounded to
  # recent files by its own TTL so it never accumulates cost.
  rule {
    id     = "expire-mezz-cache"
    status = "Enabled"

    filter {
      prefix = "mezz/"
    }

    expiration {
      days = var.mezz_cache_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }

  rule {
    id     = "reap-mezz-cache-delete-markers"
    status = "Enabled"

    filter {
      prefix = "mezz/"
    }

    expiration {
      expired_object_delete_marker = true
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
  source               = "./modules/compute"
  vpc_id               = module.network.vpc_id
  subnet_ids           = module.network.subnet_ids
  security_group_id    = module.network.batch_sg_id
  instance_profile_arn = module.iam.instance_profile_arn
  max_vcpus            = var.compute_max_vcpus
  worker_ami_id        = var.worker_ami_id
}

module "jobs" {
  source             = "./modules/jobs"
  ecr_repo_url       = module.ecr.repo_url
  image_tag          = var.image_tag
  task_role_arn      = module.iam.task_role_arn
  execution_role_arn = module.iam.execution_role_arn
  s3_bucket          = var.s3_bucket
  region             = local.region
}

module "workflow" {
  source            = "./modules/workflow"
  job_queue_arn     = module.compute.job_queue_arn
  job_def_arns      = module.jobs.job_def_arns
  workflow_role_arn = module.iam.workflow_role_arn
}
