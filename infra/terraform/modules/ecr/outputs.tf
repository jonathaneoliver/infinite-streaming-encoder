output "repo_url" {
  description = "ECR repo URI — tag and push encoder images here"
  value       = aws_ecr_repository.encoder_worker.repository_url
}

output "repo_arn" {
  value = aws_ecr_repository.encoder_worker.arn
}
