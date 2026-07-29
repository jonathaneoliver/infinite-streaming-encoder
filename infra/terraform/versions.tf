terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.56"
    }
  }

  # State lives in S3, locked via DynamoDB. Deliberately unconfigured here: the
  # bucket name is per-deployment (S3 names are globally unique, so no default
  # could work for anyone but its owner). Initialize via `make infra-init`,
  # which supplies bucket/key/region/table from TFSTATE_* in .env.
  #
  # The bucket + table must exist before first init. See README.
  backend "s3" {}
}
