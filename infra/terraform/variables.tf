variable "region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-west-2"
}

variable "s3_bucket" {
  description = "Existing S3 bucket for job I/O staging. Created outside of this stack."
  type        = string
  default     = "infinitestream-encoding-staging"
}

variable "image_tag" {
  description = <<EOT
Encoder image tag the Batch job definitions pull from ECR. Defaults
to `latest`, but pin to a short-sha in production for reproducible
runs.
EOT
  type    = string
  default = "latest"
}

variable "compute_max_vcpus" {
  description = "Upper bound on the Batch compute environment's total vCPUs."
  type        = number
  default     = 32
}
