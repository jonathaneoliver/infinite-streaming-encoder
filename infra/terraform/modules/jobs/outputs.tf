output "job_def_arns" {
  description = "Map of phase name -> job definition ARN, consumed by the workflow module."
  value = {
    mezzanine   = aws_batch_job_definition.mezzanine.arn
    variant     = aws_batch_job_definition.variant.arn
    audio       = aws_batch_job_definition.audio.arn
    package     = aws_batch_job_definition.package.arn
    package_all = aws_batch_job_definition.package_all.arn
    hls         = aws_batch_job_definition.hls.arn
    byteranges  = aws_batch_job_definition.byteranges.arn
  }
}
