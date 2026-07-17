variable "ecr_repo_url" {
  description = "ECR repo URI from the ecr module (without the :tag suffix)."
  type        = string
}

variable "image_tag" {
  description = "Image tag pinned by each job definition."
  type        = string
}

variable "task_role_arn" {
  description = "Role the running container assumes (S3 rw)."
  type        = string
}

variable "execution_role_arn" {
  description = "Role ECS uses to pull the image + ship logs."
  type        = string
}

variable "s3_bucket" {
  description = "S3 bucket for job I/O — passed into every container as S3_BUCKET env."
  type        = string
}

variable "region" {
  description = "AWS region (used for AWS_REGION env + Logs endpoint)."
  type        = string
}
