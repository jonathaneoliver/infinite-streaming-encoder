# Step Functions state machine that chains the six job definitions
# into the DAG from issue #14:
#
#   mezzanine ─┬─ audio
#              ├─ Map(12 variants in parallel)
#              │
#              └─ Parallel(
#                    h264: package → hls → byteranges,
#                    hevc: package → hls → byteranges,
#                  )
#
# The ASL is rendered from definition.json.tpl — it's much easier to
# hand-edit JSON with comments (in the .tpl via multi-line strings)
# than try to build the same structure via jsonencode() calls.

locals {
  definition = templatefile("${path.module}/definition.json.tpl", {
    job_queue_arn   = var.job_queue_arn
    mezzanine_def   = var.job_def_arns["mezzanine"]
    variant_def     = var.job_def_arns["variant"]
    audio_def       = var.job_def_arns["audio"]
    package_def     = var.job_def_arns["package"]
    package_all_def = var.job_def_arns["package_all"]
    hls_def         = var.job_def_arns["hls"]
    byteranges_def  = var.job_def_arns["byteranges"]
  })
}

resource "aws_cloudwatch_log_group" "state_machine" {
  name = "/aws/states/infinite-streaming-encoder"
  # 7 days: long enough to debug a failed run (the log tail is pulled into the
  # job log at failure time anyway, so the group is a backstop rather than the
  # primary record), short enough that stored bytes stay a rounding error
  # against the 5 GB allowance. Ingestion, not storage, is what would ever cost
  # money here — and that needs ~3,800 executions/month to reach the allowance.
  retention_in_days = 7
}

resource "aws_sfn_state_machine" "encoder" {
  name     = "infinite-streaming-encoder"
  role_arn = var.workflow_role_arn
  type     = "STANDARD"

  definition = local.definition

  # EXPRESS would cost less but caps at 5 minutes — our worst-case
  # encode is ~30 min so STANDARD is required.
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.state_machine.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = true
  }

  # Also attach a permissive log-writing policy so the state machine
  # can actually write to the group above. Without this the logging
  # config errors with "access denied" on first execution.
  depends_on = [aws_iam_role_policy.workflow_logs]
}

# Terraform re-applies to the iam module would rewrite this; keep
# it local here so it stays in sync with the log group.
data "aws_iam_policy_document" "workflow_logs" {
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

# Attach to the workflow_role that the iam module created. We
# reference it by ARN only, so extract the name for the policy
# attachment.
locals {
  workflow_role_name = reverse(split("/", var.workflow_role_arn))[0]
}

resource "aws_iam_role_policy" "workflow_logs" {
  role   = local.workflow_role_name
  name   = "SfnLogs"
  policy = data.aws_iam_policy_document.workflow_logs.json
}

# X-Ray tracing permission for the state machine.
resource "aws_iam_role_policy" "workflow_xray" {
  role = local.workflow_role_name
  name = "SfnXray"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets",
      ]
      Resource = "*"
    }]
  })
}
