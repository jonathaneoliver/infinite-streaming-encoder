output "job_def_arns" {
  description = "Map of phase name -> job definition ARN, consumed by the workflow module."
  value = {
    mezzanine  = aws_batch_job_definition.mezzanine.arn
    variant    = aws_batch_job_definition.variant.arn
    concat     = aws_batch_job_definition.concat.arn
    audio      = aws_batch_job_definition.audio.arn
    package    = aws_batch_job_definition.package.arn
    hls        = aws_batch_job_definition.hls.arn
    byteranges = aws_batch_job_definition.byteranges.arn
  }
}
