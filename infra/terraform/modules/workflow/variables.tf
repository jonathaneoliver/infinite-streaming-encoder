variable "job_queue_arn" {
  description = "Batch job queue ARN from the compute module."
  type        = string
}

variable "job_def_arns" {
  description = "Map of phase name -> job definition ARN."
  type        = map(string)
}

variable "workflow_role_arn" {
  description = "IAM role Step Functions assumes to submit Batch jobs."
  type        = string
}
