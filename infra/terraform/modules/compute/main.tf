# Batch compute environment on Graviton spot. Spot-aware scheduling
# ("SPOT_CAPACITY_OPTIMIZED" allocation strategy) is the whole reason
# we moved to Batch — AWS picks the pool with the most available
# capacity instead of our client-side heuristics. Instance families
# are restricted to c7g / c6g so we stay on ARM64 and on
# general-purpose compute (no GPU, no memory-heavy r-families).

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
  name               = "encoder-batch-spotfleet"
  assume_role_policy = data.aws_iam_policy_document.spot_fleet_assume.json
}

resource "aws_iam_role_policy_attachment" "spot_fleet_tagging" {
  role       = aws_iam_role.spot_fleet.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
}

# Instance profile wraps the instance_role_arn we got from the iam
# module — Batch wants the profile name, not the bare role.
data "aws_iam_role" "instance" {
  name = reverse(split("/", var.instance_role_arn))[0]
}

resource "aws_batch_compute_environment" "spot_graviton" {
  compute_environment_name = "encoder-spot-graviton"
  type                     = "MANAGED"
  state                    = "ENABLED"
  service_role             = aws_iam_service_linked_role.batch.arn

  compute_resources {
    type                = "SPOT"
    allocation_strategy = "SPOT_CAPACITY_OPTIMIZED"

    min_vcpus     = 0
    desired_vcpus = 0
    max_vcpus     = var.max_vcpus

    instance_type = [
      "c7g",
      "c6g",
    ]

    subnets            = var.subnet_ids
    security_group_ids = [var.security_group_id]

    instance_role      = data.aws_iam_role.instance.arn
    spot_iam_fleet_role = aws_iam_role.spot_fleet.arn

    # Bid at on-demand price — spot is always cheaper; this is a
    # safety ceiling, not the actual price we pay.
    bid_percentage = 100

    tags = {
      Name = "encoder-batch-worker"
    }
  }

  # Tear down compute env last (has to go after queue detaches).
  depends_on = [aws_iam_role_policy_attachment.spot_fleet_tagging]
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

resource "aws_batch_job_queue" "main" {
  name     = "encoder-queue"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.spot_graviton.arn
  }
}
