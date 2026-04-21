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
  subnet_ids        = module.network.private_subnet_ids
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
