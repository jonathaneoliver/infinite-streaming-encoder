# Three private subnets (one per AZ) so Batch can spread spot
# instances across AZs. PRIVATE_ISOLATED = no NAT, no internet
# gateway; traffic reaches S3 + ECR only via the endpoints below.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 3)

  private_cidrs = [
    for i in range(length(local.azs)) : cidrsubnet("10.42.0.0/16", 8, i)
  ]
}

resource "aws_vpc" "this" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "encoder-batch"
  }
}

resource "aws_subnet" "private" {
  for_each = { for i, az in local.azs : az => local.private_cidrs[i] }

  vpc_id            = aws_vpc.this.id
  cidr_block        = each.value
  availability_zone = each.key

  tags = {
    Name = "encoder-batch-private-${each.key}"
    Tier = "private-isolated"
  }
}

# Single route table for all private subnets — S3 + ECR endpoints
# attach here. No default route; traffic stays in-VPC unless an
# endpoint covers the destination.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "encoder-batch-private"
  }
}

resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------
# S3 Gateway endpoint — free, no data-transfer cost for in-region
# traffic. Attached to the private route table above.
# ---------------------------------------------------------------
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = {
    Name = "encoder-batch-s3"
  }
}

# ---------------------------------------------------------------
# Interface endpoints for ECR + Logs. Each costs a few $/month per
# AZ but still far cheaper than a NAT gateway (~$30/month/AZ +
# per-GB). Attached to all three private subnets so endpoint
# resolution doesn't cross AZs.
# ---------------------------------------------------------------
resource "aws_security_group" "endpoints" {
  name        = "encoder-batch-endpoints"
  description = "ingress from private subnets for VPC interface endpoints"
  vpc_id      = aws_vpc.this.id

  # Endpoints listen on 443. Accept from the whole VPC CIDR; the only
  # thing in these subnets is our Batch instances.
  ingress {
    description = "https from VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.this.cidr_block]
  }

  egress {
    description = "all egress"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [for s in aws_subnet.private : s.id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "encoder-batch-ecr-api"
  }
}

resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [for s in aws_subnet.private : s.id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "encoder-batch-ecr-dkr"
  }
}

resource "aws_vpc_endpoint" "logs" {
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [for s in aws_subnet.private : s.id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "encoder-batch-logs"
  }
}

# ---------------------------------------------------------------
# SG for Batch compute instances. No ingress; egress to VPC is
# enough since S3 goes via the Gateway endpoint (no SG rules
# needed on the instance side) and ECR/Logs go through the
# Interface endpoints (which accept VPC CIDR above).
# ---------------------------------------------------------------
resource "aws_security_group" "batch" {
  name        = "encoder-batch-compute"
  description = "Batch compute instances; egress-only"
  vpc_id      = aws_vpc.this.id

  egress {
    description = "all egress (resolves via VPC endpoints)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
