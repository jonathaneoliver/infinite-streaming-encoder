variable "region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-west-2"
}

# No default, deliberately (#149). S3 bucket names are globally unique across
# all of AWS, so any name here would exist in exactly one account and silently
# point every other user at a bucket they cannot write. `make infra-plan` and
# friends supply this from S3_BUCKET in .env and fail loudly when it's unset —
# never rely on a bare `tofu plan`, which would sit waiting on an interactive
# prompt for it instead.
variable "s3_bucket" {
  description = "Existing S3 bucket for job I/O staging. Created outside of this stack; set S3_BUCKET in .env."
  type        = string
}

variable "image_tag" {
  description = <<EOT
Encoder image tag the Batch job definitions pull from ECR. Defaults
to `latest`, but pin to a short-sha in production for reproducible
runs.
EOT
  type        = string
  default     = "latest"
}

variable "worker_ami_id" {
  description = <<EOT
Optional pre-baked worker AMI id (from `make bake-ami`) with the encoder image
already loaded, so cold instances skip the ~60s ECR pull. Empty => Batch's
default ECS-optimized Graviton AMI. `make infra-apply` resolves this from the
AMI tagged with the current image SHA, so it's opt-in and self-correcting:
bake before an encode session, `make unbake-ami` after to drop the standing
EBS-snapshot cost back to $0. A missing/stale AMI just falls back to pull-on-boot.
EOT
  type        = string
  default     = ""
}

variable "compute_max_vcpus" {
  description = <<EOT
Upper bound on the Batch compute environment's total vCPUs — the concurrency
ceiling. Spot instances only launch when jobs need them and scale to zero, so
a higher ceiling costs nothing when idle; it just allows more chunks to run at
once (less queue-wait). With per-tier right-sizing (small tiers = 2 vCPU), 48
runs ~24 small chunks or ~6 4K chunks concurrently — and caps peak spend at
roughly half of what 96 would (~6 packed instances instead of ~12).
EOT
  type        = number
  default     = 48
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
  type        = number
  default     = 7
}

variable "mezz_cache_retention_days" {
  description = <<EOT
Days before a cached mezzanine under s3://<s3_bucket>/mezz/ is auto-expired.
The mezzanine is source-keyed (name+size+mtime) and reused across jobs so a
re-encode of the same source skips the upload + mezzanine job. Keep it long
enough to cover an iterate-on-one-clip session; the TTL bounds the "recent
files only" cache so storage never accumulates.
EOT
  type        = number
  default     = 7
}
