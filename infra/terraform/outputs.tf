output "vpc_id" {
  description = "VPC id for the Batch compute environment"
  value       = module.network.vpc_id
}

output "ecr_repo_url" {
  description = "Push encoder images here: <url>:<tag>"
  value       = module.ecr.repo_url
}

output "job_queue_arn" {
  description = "Submit Batch jobs to this queue (also reachable via Step Functions)"
  value       = module.compute.job_queue_arn
}

output "state_machine_arn" {
  description = "Step Functions state machine that orchestrates the full encode"
  value       = module.workflow.state_machine_arn
}
