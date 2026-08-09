# AWS Batch infrastructure for encoder

Terraform that provisions the Batch + Step Functions pipeline described in [issue #14](https://github.com/jonathaneoliver/infinite-streaming-encoder/issues/14).

**What each service is for** — Step Functions decides *what runs when* (the dependency graph: mezzanine → audio ‖ variants → package per codec, and the nested `Map` states that turn one encode into hundreds of chunk jobs); Batch decides *what machine it runs on* (queueing, spot fleet scale-up, packing by each job definition's vCPU reservation, and retrying a reclaimed chunk). This README is the operational half — deploy, troubleshoot, tear down. The design half, including the seams where the two layers break and which phases now run on the host instead, is in the repo-root `CLAUDE.md` under "Step Functions vs Batch: which one does what".

## First-run checklist

End to end, the first deploy is:

1. **Prereqs** — create the Terraform-state bucket + lock table and the job-I/O bucket ([One-time prerequisites](#one-time-prerequisites)).
2. **Deploy** — `terraform init` (with backend config) then `apply` ([Deploy](#deploy)).
3. **Push the image to ECR** — build **arm64** (the compute env is Graviton) and push to the repo the stack created ([Push the encoder image into ECR](#push-the-encoder-image-into-ecr)). ⚠️ The `make push` / `make cloud-push` targets push to **GHCR, not ECR** — this step is separate and easy to forget.
4. **Wire the server** — `STATE_MACHINE_ARN=$(terraform output -raw state_machine_arn)` into the repo-root `.env`, then `make restart`.
5. **Run** — drop a clip in `SOURCE_DIR`, open the web UI (http://localhost:8080), pick the **cloud-batch** target (+ Two-pass if wanted), Start. Watch the per-chunk grid live in the web UI, or the Step Functions console for the Map fan-out.

If a job hangs or fails, see [Troubleshooting the first run](#troubleshooting-the-first-run).

## Layout

```
infra/terraform/
├── main.tf                 # module wiring
├── variables.tf            # top-level inputs
├── outputs.tf              # ECR URL, queue ARN, state machine ARN
├── versions.tf             # provider pin + S3 backend
└── modules/
    ├── network/            # VPC, public subnets, IGW, free S3 gateway endpoint
    ├── ecr/                # repo + Docker Hub pull-through cache
    ├── iam/                # instance / task / execution / workflow roles
    ├── compute/            # Batch compute env (spot, Graviton) + queue
    ├── jobs/               # 7 job definitions (phases + concat-variant)
    └── workflow/           # Step Functions state machine (chunked DAG)
```

## One-time prerequisites

1. An S3 bucket for Terraform state, and a DynamoDB table for state locking. Both must exist before `make infra-init`.

   Pick your own bucket name — S3 names are globally unique, so the one below is an example, not a default you can use. Substitute it throughout and put the result in the repo-root `.env` as `TFSTATE_BUCKET`.

   ```sh
   BUCKET=your-tfstate-bucket        # must be globally unique; you own it
   REGION=us-west-2

   aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
     --create-bucket-configuration LocationConstraint="$REGION"

   # Versioning is THE recovery mechanism if state is deleted or corrupted.
   # Not optional — without it, rebuilding state means `tofu import` × 55.
   aws s3api put-bucket-versioning --bucket "$BUCKET" \
     --versioning-configuration Status=Enabled

   aws s3api put-public-access-block --bucket "$BUCKET" \
     --public-access-block-configuration \
     BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

   aws s3api put-bucket-encryption --bucket "$BUCKET" \
     --server-side-encryption-configuration \
     '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

   aws dynamodb create-table \
     --table-name terraform-lock \
     --attribute-definitions AttributeName=LockID,AttributeType=S \
     --key-schema AttributeName=LockID,KeyType=HASH \
     --billing-mode PAY_PER_REQUEST \
     --region "$REGION"
   ```

   A **separate** bucket from the job-I/O one, deliberately. Job-I/O cleanup only sweeps `jobs/` and `mezz/`, so state stored alongside it would survive — but "safe by coincidence" is not how the file describing all your infrastructure should be protected.

2. An S3 bucket for job I/O (the `s3_bucket` variable, `S3_BUCKET` in `.env`).

## Deploy

```sh
make infra-init      # tofu init against the S3 backend (reads TFSTATE_* from .env)
make infra-plan      # writes tf.plan — review it
make infra-apply     # applies the saved plan
```

`infra-init` supplies the four `-backend-config` flags from `TFSTATE_BUCKET` / `TFSTATE_KEY` / `TFSTATE_TABLE` / `AWS_REGION`, so there is nothing to remember per checkout. It fails loudly if `TFSTATE_BUCKET` is unset.

First apply creates all resources. Subsequent applies only touch what changed.

### Working from a git worktree

State is shared in S3, so **any** checkout can run infra commands — worktrees included. The one per-checkout step is `make infra-init`, because `.terraform/` (the backend handshake) is local and gitignored. Run it once in a new worktree and `infra-plan` / `infra-apply` / `cloud-dev-up` all work there.

The DynamoDB lock means two checkouts applying at once is safe: the second blocks until the first releases, rather than both writing over each other.

### Migrating an existing local state into S3

Only relevant if you deployed before the S3 backend existed, i.e. you have a `backend_override.tf` and a `terraform.tfstate` in this directory. Do it with **no encode running**.

```sh
cd infra/terraform
cp terraform.tfstate ~/tfstate-premigration-backup.json   # the escape hatch — take it
rm backend_override.tf
tofu init -migrate-state \
  -backend-config="bucket=$TFSTATE_BUCKET" \
  -backend-config="key=encoder/batch.tfstate" \
  -backend-config="region=us-west-2" \
  -backend-config="dynamodb_table=terraform-lock"
```

Answer `yes` when it offers to copy the existing state to the new backend. Verify with `tofu state list` (should list every resource it did before) and `tofu plan` (should report no changes). If anything looks wrong, putting `backend_override.tf` and the copied state file back returns you exactly where you started.

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
  "s3_input": "s3://<your-bucket>/jobs/<job-id>/input/clip.mp4",
  "s3_prefix": "s3://<your-bucket>/jobs/<job-id>",
  "two_pass": "false",
  "chunk_indices": [0, 1, 2, 3],
  "variants": [
    { "codec": "h264", "tier": "360p" },
    { "codec": "hevc", "tier": "1080p" }
  ]
}
```

- `chunk_indices` drives the per-variant chunk fan-out: each variant runs one
  encode job per index, then a `concat-variant` job joins them. Use
  `[0, 1, …, N-1]` where **N = ceil(duration_seconds / 30)** (30s chunks). A
  single-chunk clip is just `[0]` — chunk 0 is the whole clip and concat is a
  passthrough, so there is no special case.
- `two_pass` is a **string** (`"true"`/`"false"`) — it's injected verbatim as
  the `TWO_PASS` container env var.

Start an execution:

```sh
aws stepfunctions start-execution \
  --state-machine-arn $(terraform output -raw state_machine_arn) \
  --input file://example-input.json \
  --region us-west-2
```

Normally you don't hand-write this — the Go server's `cloud-batch` target
probes the source duration, computes `chunk_indices`, and submits the execution
for you. Set `STATE_MACHINE_ARN` and pick the **cloud-batch** target in the UI.

## Troubleshooting the first run

- **`aws_iam_service_linked_role.batch` fails on apply** — the account already
  has `AWSServiceRoleForBatch` (only one is allowed). Import it once:
  ```sh
  terraform import module.compute.aws_iam_service_linked_role.batch \
    arn:aws:iam::<acct>:role/aws-service-role/batch.amazonaws.com/AWSServiceRoleForBatch
  ```
- **Jobs stuck in `RUNNABLE`** — the compute environment can't place instances:
  spot capacity for `c7g`/`c6g`, or the instance role/profile. Check the compute
  env is `VALID`/`ENABLED` and read the Batch console's status reason.
- **Jobs fail pulling the image** — wrong tag (must match `image_tag`, default
  `latest`), **wrong architecture** (an amd64 image won't run on Graviton — build
  `linux/arm64`), or the image was never pushed to ECR (see the note above about
  `make push` targeting GHCR, not ECR).
- **Compute env `INVALID`** — usually IAM (instance profile / spot-fleet role) or
  the subnet / security group.
- **A chunk job errors "chunk-index out of range", or concat reports missing
  chunks** — the control plane's chunk count (from the source duration)
  disagreed with a phase's (from the mezzanine duration). They match as long as
  the mezzanine isn't trimmed or padded; suspect any `--time` trimming.
- **Watching it run** — the web UI shows a live per-chunk grid; the Step
  Functions console renders the `Map` fan-out; CloudWatch has `/aws/batch/infinite-streaming-encoder`
  (per job) and `/aws/states/infinite-streaming-encoder` (state machine).

## Cost & cleanup

Designed to cost ~nothing when idle:

| Resource | Idle cost | Notes |
| --- | --- | --- |
| Batch compute env | **$0** | Spot, `min/desired_vcpus = 0` — scales to zero; no EC2 runs unless a job is active |
| EC2 workers | pay-per-use | Spot only (`SPOT_CAPACITY_OPTIMIZED`), Graviton; terminate when the queue drains |
| S3 staging | bounded | `jobs/` auto-expires after `staging_retention_days` (default **7d**); incomplete multipart uploads swept after 3d |
| ECR | bounded | Lifecycle keeps the last 10 images |
| CloudWatch Logs | bounded | 14-day retention on both log groups |
| **VPC networking** | **$0** | Public subnets + internet gateway + a free S3 **Gateway** endpoint. No NAT gateway, no interface endpoints. |

**Idle cost is effectively $0.** Workers run in **public subnets** with
auto-assigned public IPs and reach ECR/Logs over the internet gateway (S3 via
the free gateway endpoint), so there are no always-on interface endpoints
(~$22/mo) and no NAT gateway (~$32/mo). The trade-off is that spot workers sit
on public subnets — but they're ephemeral, behind an **egress-only** security
group (no inbound), so exposure is minimal. If your security policy forbids
public-subnet compute, switch the network module back to private subnets +
interface endpoints (and accept the ~$22/mo standing cost), or `terraform
destroy` between campaigns.

**S3 hygiene:** every job stages inputs, mezzanine, per-chunk intermediates,
joined variants and packaged output under `s3://<s3_bucket>/jobs/<id>/`. With
chunking a long clip can leave thousands of chunk objects. Outputs are synced
back to the local `OUTPUT_DIR`, so S3 is staging only. The lifecycle rule bounds
this automatically even if a job crashes and orphans chunks. (An eager
delete-on-success in the app would tighten it further — a possible follow-up.)

## Destroy

```sh
terraform destroy
```

Won't delete:
- The state S3 bucket / DynamoDB table (pre-existing)
- The job-I/O bucket (managed outside this stack)
- CloudWatch log groups older than their retention (Terraform drops them on destroy, retention was 14d)
- The service-linked role `AWSServiceRoleForBatch` (AWS refuses to delete; harmless to leave)
