variable "vpc_id" {
  description = "VPC that holds the compute environment's subnets."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets for Batch to launch spot instances into."
  type        = list(string)
}

variable "security_group_id" {
  description = "SG attached to every compute instance."
  type        = string
}

variable "instance_profile_arn" {
  description = "IAM instance profile ARN for the EC2 instances. Batch's instance_role field wants the profile ARN, not the bare role ARN."
  type        = string
}

variable "max_vcpus" {
  description = "Max total vCPUs the compute env will scale out to."
  type        = number
}

variable "worker_ami_id" {
  description = <<EOT
Optional pre-baked worker AMI (built by `make bake-ami`) that already has the
encoder image loaded, so a cold instance skips the ~60s ECR pull. Empty string
=> Batch's default ECS-optimized Graviton AMI (pulls on first job). The AMI is
purely a cache: `make infra-apply` resolves it from the current image SHA, and
a stale or missing AMI simply falls back to pull-on-boot — nothing breaks.
EOT
  type        = string
  default     = ""
}
