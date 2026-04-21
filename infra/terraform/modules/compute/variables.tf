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

variable "instance_role_arn" {
  description = "IAM role ARN for the EC2 instances (wrapped in an instance profile)."
  type        = string
}

variable "max_vcpus" {
  description = "Max total vCPUs the compute env will scale out to."
  type        = number
}
