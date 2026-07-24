terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.56"
    }
  }

  # State lives in S3 + DynamoDB lock. Initialize with:
  #   terraform init \
  #     -backend-config="bucket=infinitestream-tfstate" \
  #     -backend-config="key=encoder/batch.tfstate" \
  #     -backend-config="region=us-west-2" \
  #     -backend-config="dynamodb_table=terraform-lock"
  #
  # The bucket + table must exist before first init. See README.
  backend "s3" {}
}
