# Batch compute environment on Graviton spot. Spot-aware scheduling
# ("SPOT_CAPACITY_OPTIMIZED" allocation strategy) is the whole reason
# we moved to Batch — AWS picks the pool with the most available
# capacity instead of our client-side heuristics. Instance families are
# c8g (Graviton4) and c7g (Graviton3). c6g (Graviton2) was dropped: it runs
# x264 ~1.6-1.8x slower (Graviton3 doubled SIMD width + added SVE, and x264 is
# NEON/SIMD-bound), so landing a long-pole variant on c6g nearly doubled its
# wall time — not worth the deeper/cheaper spot pools. No GPU, no memory-heavy
# r-families. Re-add "c6g" here if c8g/c7g spot capacity ever gets too tight.

# Spot fleet service role — Batch/EC2 uses this to manage the
# underlying Spot Fleet request.
data "aws_iam_policy_document" "spot_fleet_assume" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["spotfleet.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "spot_fleet" {
  name               = "infinite-streaming-encoder-batch-spotfleet"
  assume_role_policy = data.aws_iam_policy_document.spot_fleet_assume.json
}

resource "aws_iam_role_policy_attachment" "spot_fleet_tagging" {
  role       = aws_iam_role.spot_fleet.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
}

# Launch template for Batch workers. Two jobs:
#   1. ECS_IMAGE_PULL_BEHAVIOR=prefer-cached — if the exact image tag
#      (an immutable short-sha) is already on the box (baked into a
#      custom AMI, or pulled by an earlier job on the same instance),
#      skip the ~60s ECR pull. A tag that ISN'T cached still pulls
#      normally, so a stale/missing AMI self-corrects: the cache is
#      used only when it matches the deployed SHA. This is why we pin
#      image_tag to a SHA (never `latest`) — prefer-cached + a mutable
#      tag would serve a stale image.
#   2. Optionally boot from a pre-baked AMI (var.worker_ami_id) that
#      already has the image loaded. Empty => Batch's default
#      ECS-optimized Graviton AMI, which pulls on the first job.
resource "aws_launch_template" "worker" {
  name = "infinite-streaming-encoder-batch-worker"

  # NOTE: no image_id here — managed Batch ignores a launch-template AMI.
  # The custom AMI goes through compute_resources.ec2_configuration
  # (image_id_override) below. This LT exists only for the prefer-cached
  # ECS agent config in user_data.
  user_data = base64encode(<<-EOT
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="==BOUNDARY=="

    --==BOUNDARY==
    Content-Type: text/x-shellscript; charset="us-ascii"

    #!/bin/bash
    echo "ECS_IMAGE_PULL_BEHAVIOR=prefer-cached" >> /etc/ecs/ecs.config

    --==BOUNDARY==--
  EOT
  )

  # Application=infinite-streaming-encoder-app is what the app's AWS page filters EC2 on
  # (aws.py app_tag_filter), so tagging workers here makes Batch spot
  # instances show up in the EC2 panel alongside jobs/executions. Tag
  # volumes too so any orphaned EBS is discoverable the same way.
  tag_specifications {
    resource_type = "instance"
    tags = {
      Name        = "infinite-streaming-encoder-batch-worker"
      Application = "infinite-streaming-encoder-app"
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      Name        = "infinite-streaming-encoder-batch-worker"
      Application = "infinite-streaming-encoder-app"
    }
  }
}

resource "aws_batch_compute_environment" "spot_graviton" {
  # name_prefix (not a fixed name) so a replacement can be created
  # alongside the old one — a fixed name would collide. Paired with
  # create_before_destroy below, this lets an attribute that forces
  # replacement (e.g. adding the launch template) roll over cleanly:
  # build the new env, move the queue to it, then delete the old. A
  # fixed name forces delete-first, which AWS refuses while the job
  # queue is still attached ("found existing JobQueue relationship").
  compute_environment_name_prefix = "infinite-streaming-encoder-spot-graviton-"
  type                            = "MANAGED"
  state                           = "ENABLED"
  service_role                    = aws_iam_service_linked_role.batch.arn

  compute_resources {
    type                = "SPOT"
    allocation_strategy = "SPOT_CAPACITY_OPTIMIZED"

    min_vcpus     = 0
    desired_vcpus = 0
    max_vcpus     = var.max_vcpus

    instance_type = [
      "c8g",
      "c7g",
    ]

    subnets            = var.subnet_ids
    security_group_ids = [var.security_group_id]

    instance_role       = var.instance_profile_arn
    spot_iam_fleet_role = aws_iam_role.spot_fleet.arn

    # Launch template carries the prefer-cached ECS agent config (user_data).
    # "$Latest" so a rebuilt LT rolls forward on apply.
    launch_template {
      launch_template_id = aws_launch_template.worker.id
      version            = "$Latest"
    }

    # Custom worker AMI. This is the ONLY way managed Batch honors a custom
    # AMI — a launch-template image_id is silently ignored; Batch picks its
    # own LATEST ECS AMI instead (which is what left the baked AMI unused).
    #
    # The block is ALWAYS present (not a dynamic for_each) so that clearing the
    # AMI reliably resets it: worker_ami_id="" => image_id_override=null =>
    # Batch falls back to its default LATEST ECS AMI. Removing the whole block
    # instead can leave a stale override behind (Batch retains it), which is
    # what would strand a deleted-AMI pointer. Keeping image_type pinned +
    # override null is the clean "no custom AMI" state.
    ec2_configuration {
      image_type        = "ECS_AL2023"
      image_id_override = var.worker_ami_id != "" ? var.worker_ami_id : null
    }

    # Bid at on-demand price — spot is always cheaper; this is a
    # safety ceiling, not the actual price we pay.
    bid_percentage = 100

    tags = {
      Name = "infinite-streaming-encoder-batch-worker"
    }
  }

  # Tear down compute env last (has to go after queue detaches).
  depends_on = [aws_iam_role_policy_attachment.spot_fleet_tagging]

  # Create the replacement before destroying the old env so the job
  # queue can be moved onto it first (see name_prefix note above).
  lifecycle {
    create_before_destroy = true
    # Both of these are Batch-managed at runtime, not by us — ignore them to
    # avoid perpetual diffs: update_policy is auto-populated on in-place
    # updates, and desired_vcpus scales dynamically with the job queue (config
    # keeps it at 0 as a starting point; Batch moves it up/down from there).
    ignore_changes = [
      update_policy,
      compute_resources[0].desired_vcpus,
      # min_vcpus is driven at runtime by the server's awswatch loop (warm a
      # box during a run, drop to 0 when idle) via UpdateComputeEnvironment;
      # don't let apply clobber it back to the config value.
      compute_resources[0].min_vcpus,
      # max_vcpus is toggled at runtime from the AWS panel (current vs 2x) via
      # UpdateComputeEnvironment; ignore so apply doesn't reset the ceiling.
      compute_resources[0].max_vcpus,
    ]
  }
}

# Batch itself needs a service-linked role; terraform can create or
# adopt it. Most accounts already have one from prior Batch use.
resource "aws_iam_service_linked_role" "batch" {
  aws_service_name = "batch.amazonaws.com"
  description      = "Service-linked role for AWS Batch"

  # If the SLR already exists (common on accounts with prior Batch
  # use), terraform would fail to create it — set ignore_changes on
  # anything that can't be imported cleanly. Import once via:
  #   terraform import module.compute.aws_iam_service_linked_role.batch \
  #     arn:aws:iam::<acct>:role/aws-service-role/batch.amazonaws.com/AWSServiceRoleForBatch
  lifecycle {
    ignore_changes = [aws_service_name, description]
  }
}

# Fair-share scheduling policy. Attaching ANY scheduling policy to the queue
# switches it from FIFO-by-submission to priority-ordered: within a share,
# Batch schedules by each job's schedulingPriority (higher first). We use a
# single share ("encode") and set the priority per tier at submit time (see
# the SFN's SchedulingPriorityOverride), so 4K/HEVC variants win the next free
# CPU over small ones — a hard scheduler guarantee, not just submission order.
resource "aws_batch_scheduling_policy" "priority" {
  name = "infinite-streaming-encoder-priority"

  fair_share_policy {
    compute_reservation = 0
    share_decay_seconds = 0

    share_distribution {
      share_identifier = "encode"
      weight_factor    = 1
    }
  }
}

resource "aws_batch_job_queue" "main" {
  name                  = "infinite-streaming-encoder-queue"
  state                 = "ENABLED"
  priority              = 1
  scheduling_policy_arn = aws_batch_scheduling_policy.priority.arn

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.spot_graviton.arn
  }
}
