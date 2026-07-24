output "state_machine_arn" {
  value = aws_sfn_state_machine.infinite_streaming_encoder.arn
}

output "state_machine_name" {
  value = aws_sfn_state_machine.infinite_streaming_encoder.name
}
