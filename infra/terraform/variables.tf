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

variable "staging_retention_days" {
  description = <<EOT
Days before staged job data under s3://<s3_bucket>/jobs/ is auto-expired.
The pipeline writes inputs, mezzanine, chunk intermediates, joined variants
and packaged output there; outputs are synced back to the local OUTPUT_DIR,
so S3 is staging only. This lifecycle rule is the safety net that bounds S3
cost even if a job crashes mid-run and orphans chunk files. Give failed jobs
enough runway to debug / resume before cleanup.
EOT
  type    = number
  default = 7
}
