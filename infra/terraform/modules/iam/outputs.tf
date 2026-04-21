output "instance_role_arn" {
  value = aws_iam_role.instance.arn
}

output "instance_profile_arn" {
  value = aws_iam_instance_profile.instance.arn
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

output "workflow_role_arn" {
  value = aws_iam_role.workflow.arn
}
