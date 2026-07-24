# LocalStack offline test harness

Test the AWS Batch **phase pipeline** against a real S3 API locally — no AWS
account, no spend — using LocalStack **community** edition.

## What this covers (and what it doesn't)

Community LocalStack emulates **S3** only. AWS Batch and Step Functions are
Pro-gated, so this harness does **not** run the state machine. Instead
`smoke_test.sh` drives the exact command each Batch job runs —
`python3 -m infinite_streaming_encoder.cli_local phase <name> …` — by hand, in the DAG order
defined in `infra/terraform/modules/workflow/definition.json.tpl`.

| Exercised here | Needs real Batch/Spot (or LocalStack Pro) |
| --- | --- |
| Each phase's real ffmpeg / Shaka work | Step Functions Map / Parallel / ItemSelector graph |
| Two-pass variant encode (`--two-pass`) | Spot interruption + the `Host EC2*` retry path |
| S3 download/upload per phase | Batch queue depth / compute-environment scaling |
| `.done` sidecar completeness invariant | ECR image pull, per-arch job defs |

The S3 wiring is enabled by the `S3_ENDPOINT_URL` env var, which
`cli_phase._s3()` honors (forcing path-style addressing). It is unset in
production, so the hook is a no-op on real AWS.

## Run it

```bash
make build                                                   # tag: encoder
docker compose -f infra/localstack/docker-compose.yml up -d  # start S3
./infra/localstack/smoke_test.sh                             # generated 8s clip
./infra/localstack/smoke_test.sh path/to/your.mp4            # or your own source
docker compose -f infra/localstack/docker-compose.yml down   # stop
```

A green run ends with `failed: 0`. Inspect the produced objects with:

```bash
aws --endpoint-url http://localhost:4566 s3 ls s3://encoder-smoke/jobs/ --recursive
```

Config via env: `ENCODER_IMAGE`, `BUCKET`, `JOB_ID`, `CODEC` (default `hevc`).
Edit `TIERS` in the script to encode more rungs.

## Pro (Batch + Step Functions)

A LocalStack **Pro** token unlocks AWS Batch and Step Functions, so the full
state machine (Map / ItemSelector / Parallel + the `TWO_PASS` override) can run
offline. Set up:

1. Copy `.env.example` → `.env` (gitignored) and paste your token:
   ```
   LOCALSTACK_AUTH_TOKEN=ls-...            # from app.localstack.cloud → Auth Tokens
   LOCALSTACK_IMAGE=localstack/localstack-pro:3
   LOCALSTACK_SERVICES=s3,batch,stepfunctions,iam,logs,ecr
   ```
2. `docker compose -f infra/localstack/docker-compose.yml up -d`

The token lives **only** in `.env` (or your shell env as `LOCALSTACK_AUTH_TOKEN`)
— never in a committed file. With Pro up, the Terraform under
`infra/terraform/` can target LocalStack via `tflocal`, and Batch jobs run as
local Docker containers (the compose file already mounts the docker socket).
Even so, spot interruption + the `Host EC2*` retry path still isn't reproduced
— that remains a real-AWS-only test.
