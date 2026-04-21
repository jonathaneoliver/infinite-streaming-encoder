# AWS Batch infrastructure for encoder

Terraform that provisions the Batch + Step Functions pipeline described in [issue #14](https://github.com/jonathaneoliver/Encoder/issues/14).

## Layout

```
infra/terraform/
├── main.tf                 # module wiring
├── variables.tf            # top-level inputs
├── outputs.tf              # ECR URL, queue ARN, state machine ARN
├── versions.tf             # provider pin + S3 backend
└── modules/
    ├── network/            # VPC, private subnets, S3+ECR+Logs endpoints
    ├── ecr/                # repo + Docker Hub pull-through cache
    ├── iam/                # instance / task / execution / workflow roles
    ├── compute/            # Batch compute env (spot, Graviton) + queue
    ├── jobs/               # 6 job definitions (one per pipeline phase)
    └── workflow/           # Step Functions state machine (DAG)
```

## One-time prerequisites

1. An S3 bucket for Terraform state, and a DynamoDB table for state locking. Both must exist before `terraform init`:

   ```sh
   aws s3 mb s3://infinitestream-tfstate --region us-west-2
   aws dynamodb create-table \
     --table-name terraform-lock \
     --attribute-definitions AttributeName=LockID,AttributeType=S \
     --key-schema AttributeName=LockID,KeyType=HASH \
     --billing-mode PAY_PER_REQUEST \
     --region us-west-2
   ```

2. An S3 bucket for job I/O (the `s3_bucket` variable). Defaults to `infinitestream-encoding-staging` — the bucket the current cloud target already uses.

## Deploy

```sh
cd infra/terraform

terraform init \
  -backend-config="bucket=infinitestream-tfstate" \
  -backend-config="key=encoder/batch.tfstate" \
  -backend-config="region=us-west-2" \
  -backend-config="dynamodb_table=terraform-lock"

terraform plan
terraform apply
```

First apply creates all resources. Subsequent applies only touch what changed.

## Push the encoder image into ECR

Batch job definitions point at `<ecr_repo_url>:latest` (set via the `image_tag` variable). After `terraform apply` the repo exists but is empty; the Batch jobs will fail to start until an image is pushed.

```sh
# Grab the ECR URI from terraform output
export ECR_URI=$(terraform output -raw ecr_repo_url)

# Log docker in against ECR (one-time per session)
aws ecr get-login-password --region us-west-2 \
  | docker login --username AWS --password-stdin "$ECR_URI"

# Build + push. Graviton = arm64; build for that platform specifically.
cd ../..
docker buildx build --platform linux/arm64 \
  --tag "$ECR_URI:latest" --push .
```

For production you'd tag with `$(git rev-parse --short HEAD)` and pin the job definitions to that tag via the `image_tag` Terraform variable.

## Trigger an encode

The state machine expects input JSON shaped like:

```json
{
  "s3_input": "s3://infinitestream-encoding-staging/jobs/<job-id>/input/clip.mp4",
  "s3_prefix": "s3://infinitestream-encoding-staging/jobs/<job-id>",
  "variants": [
    { "codec": "h264", "tier": "360p" },
    { "codec": "h264", "tier": "540p" },
    { "codec": "h264", "tier": "720p" },
    { "codec": "h264", "tier": "1080p" },
    { "codec": "h264", "tier": "1440p" },
    { "codec": "h264", "tier": "2160p" },
    { "codec": "hevc", "tier": "360p" },
    { "codec": "hevc", "tier": "540p" },
    { "codec": "hevc", "tier": "720p" },
    { "codec": "hevc", "tier": "1080p" },
    { "codec": "hevc", "tier": "1440p" },
    { "codec": "hevc", "tier": "2160p" }
  ]
}
```

Start an execution:

```sh
aws stepfunctions start-execution \
  --state-machine-arn $(terraform output -raw state_machine_arn) \
  --input file://example-input.json \
  --region us-west-2
```

The Go server's `cloud-batch` target (Phase 6 of the migration) will build this JSON and submit it for you.

## Destroy

```sh
terraform destroy
```

Won't delete:
- The state S3 bucket / DynamoDB table (pre-existing)
- The job-I/O bucket (managed outside this stack)
- CloudWatch log groups older than their retention (Terraform drops them on destroy, retention was 14d)
- The service-linked role `AWSServiceRoleForBatch` (AWS refuses to delete; harmless to leave)
