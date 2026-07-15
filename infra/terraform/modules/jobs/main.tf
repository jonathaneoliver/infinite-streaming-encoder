# Six phase-specific job definitions. Each points at the SAME image
# (encoder-worker:<tag> in ECR) but overrides command + resources so
# the scheduler can pack cheaper phases onto smaller CPU slices and
# only the variant encodes chew through real cores.
#
# Retry strategy on every definition:
#   - max 3 attempts
#   - SPOT_INSTANCE_TERMINATED exits (HostTerminated) always retry
#   - non-zero container exits retry once (handles transient ffmpeg
#     issues); persistent failures fall through to the state machine's
#     Catch block
#
# Log config: CloudWatch Logs group per definition. Default stream
# prefix = the job-def name; each job run gets its own stream keyed
# by Batch's internal job id, so re-running the same clip never
# overwrites a prior log.

locals {
  image_uri = "${var.ecr_repo_url}:${var.image_tag}"

  # All phases share the same env. Individual phases also see
  # phase-specific vars injected by the Step Function at submit time
  # (via containerOverrides.environment), e.g. S3 source URIs.
  common_env = [
    { name = "S3_BUCKET", value = var.s3_bucket },
    { name = "AWS_REGION", value = var.region },
    { name = "PYTHONUNBUFFERED", value = "1" },
  ]

  log_group_name = "/aws/batch/encoder"
}

resource "aws_cloudwatch_log_group" "batch" {
  name              = local.log_group_name
  retention_in_days = 14
}

# The definitions all share this retry-strategy block; keep it as a
# local so changes apply uniformly.
locals {
  retry_strategy = {
    attempts = 3
    evaluate_on_exit = [
      {
        # HostTerminated = spot reclaim. Batch schedules the retry on
        # fresh capacity; if nothing else changed, the job picks up
        # at the start of its phase, not mid-encode.
        action           = "RETRY"
        on_reason        = "Host EC2*"   # matches "Host EC2 instance was terminated"
      },
      {
        action    = "RETRY"
        on_exit_code = "1"   # generic ffmpeg transient
      },
      {
        # Catch-all: everything else exits without retry. AWS requires
        # every evaluate_on_exit to carry at least one condition, so the
        # "*" glob stands in for "any exit code".
        action    = "EXIT"
        on_exit_code = "*"
      },
    ]
  }

  timeout_seconds = 3600  # 1h is a generous ceiling; any phase over this is a bug
}

# Helper local to DRY up the ContainerProperties blob.
locals {
  base_container = {
    image            = local.image_uri
    jobRoleArn       = var.task_role_arn
    executionRoleArn = var.execution_role_arn
    environment      = local.common_env

    # Logs → the shared group above; Batch appends the job id to the
    # stream prefix automatically.
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = local.log_group_name
        "awslogs-stream-prefix" = "batch"
      }
    }

    # Linux parameters: enable init so Python zombies get reaped. No
    # shared-memory bump needed — ffmpeg/packager don't use it.
    linuxParameters = {
      initProcessEnabled = true
    }
  }
}

# ------------------------------------------------------------------
# encoder-mezzanine — stream-copy the input into a fragmented MP4.
# CPU-light; 2 vCPU + 4 GiB is plenty.
# ------------------------------------------------------------------
resource "aws_batch_job_definition" "mezzanine" {
  name = "encoder-mezzanine"
  type = "container"

  container_properties = jsonencode(merge(local.base_container, {
    resourceRequirements = [
      { type = "VCPU", value = "2" },
      { type = "MEMORY", value = "4096" },
    ]
    command = [
      "python3", "-m", "encoder.cli_local", "phase", "mezzanine",
      "Ref::s3_in", "Ref::s3_out",
    ]
  }))

  retry_strategy {
    attempts = local.retry_strategy.attempts

    dynamic "evaluate_on_exit" {
      for_each = local.retry_strategy.evaluate_on_exit
      content {
        action       = evaluate_on_exit.value.action
        on_reason    = try(evaluate_on_exit.value.on_reason, null)
        on_exit_code = try(evaluate_on_exit.value.on_exit_code, null)
      }
    }
  }

  timeout {
    attempt_duration_seconds = local.timeout_seconds
  }

  platform_capabilities = ["EC2"]
}

# ------------------------------------------------------------------
# encoder-variant — single (codec, tier) ffmpeg encode. The bulk of
# our compute cost. 8 vCPU matches the old c7g.8xlarge × 4 (each
# variant gets a quarter of the old machine; Batch fits 4 variants
# per instance). Memory scales with resolution; 16 GiB covers 4K.
# ------------------------------------------------------------------
resource "aws_batch_job_definition" "variant" {
  name = "encoder-variant"
  type = "container"

  container_properties = jsonencode(merge(local.base_container, {
    resourceRequirements = [
      { type = "VCPU", value = "8" },
      { type = "MEMORY", value = "16384" },
    ]
    command = [
      "python3", "-m", "encoder.cli_local", "phase", "variant",
      "--codec", "Ref::codec",
      "--tier", "Ref::tier",
      "--chunk-index", "Ref::chunk_index",
      "--s3-mezz", "Ref::s3_mezz",
      "--s3-out", "Ref::s3_out",
    ]
  }))

  retry_strategy {
    attempts = local.retry_strategy.attempts

    dynamic "evaluate_on_exit" {
      for_each = local.retry_strategy.evaluate_on_exit
      content {
        action       = evaluate_on_exit.value.action
        on_reason    = try(evaluate_on_exit.value.on_reason, null)
        on_exit_code = try(evaluate_on_exit.value.on_exit_code, null)
      }
    }
  }

  timeout {
    attempt_duration_seconds = local.timeout_seconds
  }

  platform_capabilities = ["EC2"]
}

# ------------------------------------------------------------------
# encoder-concat — join a variant's per-chunk encodes into the whole
# variant via the ffmpeg concat demuxer (stream copy). CPU-light like
# the mezzanine phase; 2 vCPU + 4 GiB. Runs once per (codec, tier)
# after that variant's chunk fan-out completes.
# ------------------------------------------------------------------
resource "aws_batch_job_definition" "concat" {
  name = "encoder-concat"
  type = "container"

  container_properties = jsonencode(merge(local.base_container, {
    resourceRequirements = [
      { type = "VCPU", value = "2" },
      { type = "MEMORY", value = "4096" },
    ]
    command = [
      "python3", "-m", "encoder.cli_local", "phase", "concat-variant",
      "--codec", "Ref::codec",
      "--tier", "Ref::tier",
      "--s3-mezz", "Ref::s3_mezz",
      "--s3-chunks", "Ref::s3_chunks",
      "--s3-out", "Ref::s3_out",
    ]
  }))

  retry_strategy {
    attempts = local.retry_strategy.attempts

    dynamic "evaluate_on_exit" {
      for_each = local.retry_strategy.evaluate_on_exit
      content {
        action       = evaluate_on_exit.value.action
        on_reason    = try(evaluate_on_exit.value.on_reason, null)
        on_exit_code = try(evaluate_on_exit.value.on_exit_code, null)
      }
    }
  }

  timeout {
    attempt_duration_seconds = local.timeout_seconds
  }

  platform_capabilities = ["EC2"]
}

# ------------------------------------------------------------------
# encoder-audio — extract / transcode the audio track.
# ------------------------------------------------------------------
resource "aws_batch_job_definition" "audio" {
  name = "encoder-audio"
  type = "container"

  container_properties = jsonencode(merge(local.base_container, {
    resourceRequirements = [
      { type = "VCPU", value = "2" },
      { type = "MEMORY", value = "4096" },
    ]
    command = [
      "python3", "-m", "encoder.cli_local", "phase", "audio",
      "--s3-mezz", "Ref::s3_mezz",
      "--s3-out", "Ref::s3_out",
    ]
  }))

  retry_strategy {
    attempts = local.retry_strategy.attempts

    dynamic "evaluate_on_exit" {
      for_each = local.retry_strategy.evaluate_on_exit
      content {
        action       = evaluate_on_exit.value.action
        on_reason    = try(evaluate_on_exit.value.on_reason, null)
        on_exit_code = try(evaluate_on_exit.value.on_exit_code, null)
      }
    }
  }

  timeout {
    attempt_duration_seconds = local.timeout_seconds
  }

  platform_capabilities = ["EC2"]
}

# ------------------------------------------------------------------
# encoder-package — Shaka Packager per codec. Consumes all variants
# for that codec + audio and produces DASH + fMP4 segments.
# ------------------------------------------------------------------
resource "aws_batch_job_definition" "package" {
  name = "encoder-package"
  type = "container"

  container_properties = jsonencode(merge(local.base_container, {
    resourceRequirements = [
      { type = "VCPU", value = "2" },
      { type = "MEMORY", value = "4096" },
    ]
    command = [
      "python3", "-m", "encoder.cli_local", "phase", "package",
      "--codec", "Ref::codec",
      "--s3-variants", "Ref::s3_variants",
      "--s3-audio", "Ref::s3_audio",
      "--s3-out", "Ref::s3_out",
    ]
  }))

  retry_strategy {
    attempts = local.retry_strategy.attempts

    dynamic "evaluate_on_exit" {
      for_each = local.retry_strategy.evaluate_on_exit
      content {
        action       = evaluate_on_exit.value.action
        on_reason    = try(evaluate_on_exit.value.on_reason, null)
        on_exit_code = try(evaluate_on_exit.value.on_exit_code, null)
      }
    }
  }

  timeout {
    attempt_duration_seconds = local.timeout_seconds
  }

  platform_capabilities = ["EC2"]
}

# ------------------------------------------------------------------
# encoder-hls — pure-Python LL-HLS manifest. Fast; 1 vCPU.
# ------------------------------------------------------------------
resource "aws_batch_job_definition" "hls" {
  name = "encoder-hls"
  type = "container"

  container_properties = jsonencode(merge(local.base_container, {
    resourceRequirements = [
      { type = "VCPU", value = "1" },
      { type = "MEMORY", value = "2048" },
    ]
    command = [
      "python3", "-m", "encoder.cli_local", "phase", "hls",
      "--codec", "Ref::codec",
      "--s3-package", "Ref::s3_package",
      "--s3-out", "Ref::s3_out",
    ]
  }))

  retry_strategy {
    attempts = local.retry_strategy.attempts

    dynamic "evaluate_on_exit" {
      for_each = local.retry_strategy.evaluate_on_exit
      content {
        action       = evaluate_on_exit.value.action
        on_reason    = try(evaluate_on_exit.value.on_reason, null)
        on_exit_code = try(evaluate_on_exit.value.on_exit_code, null)
      }
    }
  }

  timeout {
    attempt_duration_seconds = local.timeout_seconds
  }

  platform_capabilities = ["EC2"]
}

# ------------------------------------------------------------------
# encoder-byteranges — fMP4 fragment byterange sidecars for
# EXT-X-PART partials.
# ------------------------------------------------------------------
resource "aws_batch_job_definition" "byteranges" {
  name = "encoder-byteranges"
  type = "container"

  container_properties = jsonencode(merge(local.base_container, {
    resourceRequirements = [
      { type = "VCPU", value = "1" },
      { type = "MEMORY", value = "2048" },
    ]
    command = [
      "python3", "-m", "encoder.cli_local", "phase", "byteranges",
      "--codec", "Ref::codec",
      "--s3-package", "Ref::s3_package",
      "--s3-out", "Ref::s3_out",
    ]
  }))

  retry_strategy {
    attempts = local.retry_strategy.attempts

    dynamic "evaluate_on_exit" {
      for_each = local.retry_strategy.evaluate_on_exit
      content {
        action       = evaluate_on_exit.value.action
        on_reason    = try(evaluate_on_exit.value.on_reason, null)
        on_exit_code = try(evaluate_on_exit.value.on_exit_code, null)
      }
    }
  }

  timeout {
    attempt_duration_seconds = local.timeout_seconds
  }

  platform_capabilities = ["EC2"]
}
