output "compute_environment_arn" {
  value = aws_batch_compute_environment.spot_graviton.arn
}

output "job_queue_arn" {
  value = aws_batch_job_queue.main.arn
}

output "job_queue_name" {
  value = aws_batch_job_queue.main.name
}
