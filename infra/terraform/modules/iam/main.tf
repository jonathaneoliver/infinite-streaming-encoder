locals {
  bucket_arn         = "arn:aws:s3:::${var.s3_bucket}"
  bucket_objects_arn = "${local.bucket_arn}/*"
}

# S3 policy doc reused across task_role + instance_role. Boto3's
# credential chain inside the container picks up whichever is
# reachable — IMDS for the instance role, or the task role via
# the standard ECS-task-role-arn env var.
data "aws_iam_policy_document" "s3_rw" {
  statement {
    sid    = "S3Objects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
    ]
    resources = [local.bucket_objects_arn]
  }

  statement {
    sid       = "S3List"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]
  }
}

# ---------------------------------------------------------------
# Batch instance role — the EC2 host Batch spins up. Needs ECS
# agent permissions so the host registers with the cluster, plus
# SSM so we can exec into it for debugging without SSH.
# ---------------------------------------------------------------
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "instance" {
  name               = "infinite-streaming-encoder-batch-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "instance_ecs" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_role_policy_attachment" "instance_ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "instance_s3" {
  role   = aws_iam_role.instance.id
  name   = "S3Rw"
  policy = data.aws_iam_policy_document.s3_rw.json
}

# Batch uses an instance profile; bare roles can't be attached to EC2.
resource "aws_iam_instance_profile" "instance" {
  name = "infinite-streaming-encoder-batch-instance"
  role = aws_iam_role.instance.name
}

# ---------------------------------------------------------------
# Task role — the identity the CONTAINER process sees. Cleaner
# boundary than the instance role: if we ever add a second container
# that shouldn't touch S3, it gets a different task role without
# changing the host.
# ---------------------------------------------------------------
data "aws_iam_policy_document" "ecs_task_assume" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "task" {
  name               = "infinite-streaming-encoder-batch-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json
}

resource "aws_iam_role_policy" "task_s3" {
  role   = aws_iam_role.task.id
  name   = "S3Rw"
  policy = data.aws_iam_policy_document.s3_rw.json
}

# Telemetry: workers publish their [[ENCODER-…]] markers to a per-execution SQS
# queue that the orchestrator creates, drains and deletes. Send-only, and only
# on queues named for this system — a worker must not be able to read (and so
# delete) the telemetry of the run it is part of, nor touch any other queue in
# the account.
#
# Wildcarded because the queue name carries the execution name and so is not
# known until submit time. GetQueueUrl is needed because the worker is given the
# execution name, not a URL: the URL embeds the account id, which the workflow
# definition has no clean way to interpolate.
data "aws_iam_policy_document" "task_sqs" {
  statement {
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:SendMessageBatch",
      "sqs:GetQueueUrl",
    ]
    resources = ["arn:aws:sqs:*:*:encoder-telemetry-*"]
  }
}

resource "aws_iam_role_policy" "task_sqs" {
  role   = aws_iam_role.task.id
  name   = "TelemetrySend"
  policy = data.aws_iam_policy_document.task_sqs.json
}

# ---------------------------------------------------------------
# Execution role — ECS uses this to pull the image from ECR and
# ship container stdout/stderr to CloudWatch Logs.
# ---------------------------------------------------------------
resource "aws_iam_role" "execution" {
  name               = "infinite-streaming-encoder-batch-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ---------------------------------------------------------------
# Workflow (Step Functions) role — submits Batch jobs and follows
# their lifecycle via EventBridge.
# ---------------------------------------------------------------
data "aws_iam_policy_document" "sfn_assume" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_iam_policy_document" "sfn_batch" {
  statement {
    sid    = "BatchSubmit"
    effect = "Allow"
    actions = [
      "batch:SubmitJob",
      "batch:DescribeJobs",
      "batch:TerminateJob",
    ]
    # AWS docs say "*" here is the recommended pattern for SFN +
    # Batch; SubmitJob can't be scoped to a job definition at IAM
    # resource level.
    resources = ["*"]
  }

  # Step Functions uses an EventBridge rule to observe Batch job
  # state changes for .sync invocation mode.
  statement {
    sid    = "EventBridgeForBatchJobs"
    effect = "Allow"
    actions = [
      "events:PutTargets",
      "events:PutRule",
      "events:DescribeRule",
    ]
    resources = [
      "arn:aws:events:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventsForBatchJobsRule",
    ]
  }
}

resource "aws_iam_role" "workflow" {
  name               = "infinite-streaming-encoder-workflow"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

resource "aws_iam_role_policy" "workflow_batch" {
  role   = aws_iam_role.workflow.id
  name   = "BatchSubmit"
  policy = data.aws_iam_policy_document.sfn_batch.json
}
