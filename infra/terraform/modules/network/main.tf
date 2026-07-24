# Three public subnets (one per AZ) so Batch can spread spot instances
# across AZs. Public subnets + auto-assigned public IPs let workers reach
# ECR / CloudWatch Logs / S3 straight over the internet — no NAT gateway
# AND no interface VPC endpoints, so the network costs $0 when idle (the
# whole point of this layout vs. the private-subnet + endpoints design,
# which billed ~$22/mo standing for the ecr.api/ecr.dkr/logs endpoints).
#
# The S3 Gateway endpoint is kept: it's free and keeps S3 traffic off the
# public path (also avoids per-GB internet data-transfer for S3).
#
# Workers are ephemeral spot instances behind an egress-only security
# group (no inbound rules), so the public-subnet exposure is minimal.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 3)

  public_cidrs = [
    for i in range(length(local.azs)) : cidrsubnet("10.42.0.0/16", 8, i)
  ]
}

resource "aws_vpc" "this" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "infinite-streaming-encoder-batch"
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "infinite-streaming-encoder-batch"
  }
}

resource "aws_subnet" "public" {
  for_each = { for i, az in local.azs : az => local.public_cidrs[i] }

  vpc_id                  = aws_vpc.this.id
  cidr_block              = each.value
  availability_zone       = each.key
  map_public_ip_on_launch = true # Batch EC2 workers get a public IP at launch

  tags = {
    Name = "infinite-streaming-encoder-batch-public-${each.key}"
    Tier = "public"
  }
}

# Default route to the internet gateway so workers can reach ECR + Logs.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = {
    Name = "infinite-streaming-encoder-batch-public"
  }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------
# S3 Gateway endpoint — free; keeps S3 traffic off the public path
# and avoids per-GB internet data-transfer for S3. Attached to the
# public route table.
# ---------------------------------------------------------------
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]

  tags = {
    Name = "infinite-streaming-encoder-batch-s3"
  }
}

# ---------------------------------------------------------------
# SG for Batch compute instances: egress-only, no inbound. ECR + Logs
# egress via the internet gateway; S3 via the gateway endpoint above.
# ---------------------------------------------------------------
resource "aws_security_group" "batch" {
  name        = "infinite-streaming-encoder-batch-compute"
  description = "Batch compute instances; egress-only"
  vpc_id      = aws_vpc.this.id

  egress {
    description = "all egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
