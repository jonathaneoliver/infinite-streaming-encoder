
-include .env
export

IMAGE_NAME ?= infinite-streaming-encoder
CONTAINER_NAME ?= infinite-streaming-encoder
PORT ?= 8080
# Temporal UI address the server queries for available distributed-local workers.
TEMPORAL_UI_ADDR ?= http://host.docker.internal:8233
# Distributed-local (local-dist target): the server hands these to the
# cli_local_dist orchestrator container. Defaults match the local cluster
# (make dist-up). MINIO_* are passed as AWS_* to the worker so they don't
# clobber the server's real AWS creds used by the cloud path.
TEMPORAL_ADDRESS ?= host.docker.internal:7233
MINIO_ENDPOINT ?= http://host.docker.internal:9000
MINIO_ACCESS_KEY ?= encoder
MINIO_SECRET_KEY ?= encoder-secret
DIST_S3_BUCKET ?= encoder-local
# Label for the master box's own worker + the container name the worker runs as
# (the compose `worker` service's container_name) — used by the server to toggle
# machines on/off (internal/api/dist.go docker start/stop).
LOCAL_WORKER_LABEL ?= mac
DIST_WORKER_CONTAINER ?= encode-worker

# Single source of truth: ./VERSION. Embedded into the Go binary via
# -ldflags and stamped on every image tag we publish to GHCR. The
# short git SHA is stamped too, so the About tab can tell you exactly
# which commit the local binary AND the cloud image were built from
# — critical when VERSION hasn't bumped but you've been iterating on
# cloud code.
VERSION := $(shell cat VERSION 2>/dev/null || echo dev)
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)

# Promote (staging -> live rsync). All optional; no-ops when unset. Wired into
# the server via the docker-compose.promote-{local,ssh}.yml overlays, which the
# COMPOSE_PROMOTE logic below layers in only when the matching var is set.
#  - PROMOTE_LOCAL_DIR: host dir mounted at /media/promote-local (a local dest;
#    reference /media/promote-local in PROMOTE_DESTS).
#  - PROMOTE_SSH_HOST: a *.local remote resolved here via mDNS and --add-host'd
#    into the container (via the ssh overlay), since Docker can't resolve .local
#    names itself. PROMOTE_SSH_IP is exported for that overlay to interpolate.
PROMOTE_SSH_IP := $(if $(PROMOTE_SSH_HOST),$(shell dscacheutil -q host -a name $(PROMOTE_SSH_HOST) 2>/dev/null | awk '/^ip_address:/{print $$2; exit}'),)

# GHCR publishing
GHCR_IMAGE ?= ghcr.io/jonathaneoliver/infinite-streaming-encoder
GHCR_USERNAME ?= jonathaneoliver
PLATFORMS ?= linux/amd64,linux/arm64

# Which image `run` / `run-remote` launch. RUN_IMAGE feeds BOTH the server
# container and the worker containers (ENCODER_IMAGE), so the two targets
# differ only in this one value:
#   run        -> the locally-built $(IMAGE_NAME)
#   run-remote -> the published $(REMOTE_IMAGE), pulled from GHCR (no build)
RUN_IMAGE ?= $(IMAGE_NAME)
REMOTE_IMAGE ?= $(GHCR_IMAGE):latest

# Dev only: host path overlaid onto a spawned orchestrator's /app/scripts/infinite_streaming_encoder
# so it runs current working-tree code without a rebuild. Set by `make farm-dev-up`;
# empty in normal runs (the orchestrator then uses the image's baked scripts).
HOST_SCRIPTS_DIR ?=

# ---- Compose bring-up --------------------------------------------------------
# One unified docker-compose.yml drives the whole farm via profiles (master =
# cluster + server + worker; worker = worker only). Optional promote overlays are
# layered only when the matching .env vars are set. This replaces the old split:
# infra/local-cluster compose (cluster) + run-worker.sh (worker) + the
# ENCODER_DOCKER_RUN docker-run block (server).
COMPOSE ?= docker compose
COMPOSE_PROJECT ?= infinite-streaming-encoder
COMPOSE_PROMOTE :=
ifneq ($(strip $(PROMOTE_LOCAL_DIR)),)
COMPOSE_PROMOTE += -f docker-compose.promote-local.yml
endif
ifneq ($(strip $(PROMOTE_SSH_HOST)),)
COMPOSE_PROMOTE += -f docker-compose.promote-ssh.yml
endif
COMPOSE_BASE := $(COMPOSE) -p $(COMPOSE_PROJECT) -f docker-compose.yml $(COMPOSE_PROMOTE)
COMPOSE_DEV  := $(COMPOSE_BASE) -f docker-compose.dev.yml
# Mac master only: size worker concurrency from HOST performance cores (the
# Docker VM hides P/E cores, so in-container detection over-counts). Empty on
# Linux / non-Mac -> the worker detects physical cores in-container. Passed to
# compose as ENCODE_SLOTS (empty string is safe: compose ${ENCODE_SLOTS:-0}).
FARM_ENCODE_SLOTS := $(shell P=$$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null); if [ -n "$$P" ] && [ "$$P" -gt 1 ]; then echo $$((P/2)); fi)

.PHONY: require-paths build run run-remote down stop restart logs shell status clean publish version setup-hooks

# Point git at the committed hooks (scripts/git-hooks/) so the pre-push guard
# that blocks direct pushes to main is active in this clone. Run once per clone.
setup-hooks:
	git config core.hooksPath scripts/git-hooks
	@echo "git hooks active (scripts/git-hooks). Direct pushes to main are now blocked — use a PR."

require-paths:
	@: $${SOURCE_DIR:?SOURCE_DIR is not set — create a .env (see .env.example)}
	@: $${OUTPUT_DIR:?OUTPUT_DIR is not set — create a .env (see .env.example)}
	@: $${TMP_DIR:?TMP_DIR is not set — create a .env (see .env.example)}

build:
	docker build \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_SHA=$(GIT_SHA) \
		--build-arg IMAGE_TAG=$(IMAGE_TAG) \
		-t $(IMAGE_NAME) .

version:
	@echo $(VERSION) $(GIT_SHA)

.PHONY: doctor
doctor:               ## preflight: check .env / host tools / per-target config, report clearly
	@bash scripts/doctor.sh

# Server-only lifecycle. `run` brings up JUST the encoder server (no cluster /
# worker) via compose — this is what `restart`/`deploy` use to bounce the server
# after an image push. `--no-deps` keeps it from dragging the cluster up; for the
# whole master profile (cluster + server + worker) use `make farm-up` / `farm-dev-up`.
# ENCODER_IMAGE feeds BOTH this service's image and the image it spawns workers
# from, so `run` (local build) and `run-remote` (GHCR) differ only in that value.
run: require-paths
	ENCODER_IMAGE=$(RUN_IMAGE) $(COMPOSE_BASE) up -d --build --no-deps server
	@echo "Encoder running at http://localhost:$(PORT)"

# Fire up the server from the published GHCR image instead of a local build.
# Logs into GHCR first only if GHCR_PAT is set (needed when the package is
# private). Pairs with `make farm-up` for a fully no-local-build bring-up.
run-remote: RUN_IMAGE = $(REMOTE_IMAGE)
run-remote: require-paths
	@if [ -n "$$GHCR_PAT" ]; then \
		echo "$$GHCR_PAT" | docker login ghcr.io -u $(GHCR_USERNAME) --password-stdin; \
	fi
	ENCODER_IMAGE=$(REMOTE_IMAGE) $(COMPOSE_BASE) pull server
	ENCODER_IMAGE=$(REMOTE_IMAGE) $(COMPOSE_BASE) up -d --no-build --no-deps server
	@echo "Encoder running at http://localhost:$(PORT)"

# Bring the whole master stack down (cluster + server + worker). ARGS=-v wipes
# the Temporal/MinIO volumes.
down:
	$(COMPOSE_BASE) --profile master down $(ARGS)

# Stop just the server (leaves the cluster + worker running). `restart` bounces
# it via `run` so a fresh image is picked up.
stop:
	$(COMPOSE_BASE) stop server 2>/dev/null || docker stop $(CONTAINER_NAME) 2>/dev/null || true

restart: stop run

logs:
	docker logs -f $(CONTAINER_NAME)

shell:
	docker exec -it $(CONTAINER_NAME) /bin/sh

status:
	@docker ps --filter name=$(CONTAINER_NAME) --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || echo "not running"

clean: stop
	docker rmi $(IMAGE_NAME) 2>/dev/null || true

# Unified publish (#55): ONE multi-arch build, BOTH provenance stamps baked
# (GIT_SHA=real HEAD, IMAGE_TAG=content hash — see the IMAGE_TAG comment above).
# GHCR is ALWAYS pushed (the farm image, AWS-free). ECR is ALSO pushed when a
# cloud stack is configured (ECR_REPO resolves to a real *.dkr.ecr.* URL), so a
# cloud deploy can never leave ECR + GHCR out of sync. Self-contained: logs into
# GHCR (+ ECR when used) and ensures the docker-container buildx builder exists.
# Requires GHCR_PAT (write:packages). Supersedes the old GHCR-only publish and
# the ECR-only ecr-publish (now a thin alias).
publish:              ## build once (multi-arch) → GHCR always, ECR when cloud is configured
	@: $${GHCR_PAT:?GHCR_PAT is not set — create a classic PAT with write:packages scope}
	@echo "$$GHCR_PAT" | docker login ghcr.io -u $(GHCR_USERNAME) --password-stdin
	@docker buildx inspect encoder-builder >/dev/null 2>&1 || \
		docker buildx create --name encoder-builder --driver docker-container >/dev/null
	@set -e; ecr_tags=""; ecr_note=""; \
	if echo "$(ECR_REPO)" | grep -qE '\.dkr\.ecr\.'; then \
	  echo ">>> cloud configured — pushing ECR ($(ECR_REPO)) in sync"; \
	  aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $(ECR_REGISTRY); \
	  ecr_tags="--tag $(ECR_REPO):latest --tag $(ECR_REPO):$(IMAGE_TAG)"; \
	  ecr_note=" + ECR ($(ECR_REPO)) :latest :$(IMAGE_TAG)"; \
	elif [ -n "$$(printf '%s' '$(ECR_REPO)' | tr -d '[:space:]')" ]; then \
	  echo ">>> WARNING: ECR_REPO set but not a valid *.dkr.ecr.* URL — skipping ECR (GHCR only). Value: '$(ECR_REPO)'"; \
	else \
	  echo ">>> no cloud stack (ECR_REPO empty) — GHCR-only publish"; \
	fi; \
	docker buildx build --builder encoder-builder --platform $(PLATFORMS) \
		--build-arg VERSION=$(VERSION) --build-arg GIT_SHA=$(GIT_SHA) --build-arg IMAGE_TAG=$(IMAGE_TAG) \
		--tag $(GHCR_IMAGE):latest --tag $(GHCR_IMAGE):$(VERSION) \
		--tag $(GHCR_IMAGE):$(GIT_SHA) --tag $(GHCR_IMAGE):$(IMAGE_TAG) \
		$$ecr_tags --push . ; \
	echo "Published GHCR ($(GHCR_IMAGE)) :latest :$(VERSION) :$(GIT_SHA) :$(IMAGE_TAG)$$ecr_note [$(PLATFORMS)]"

# ---------------------------------------------------------------------------
# Cloud-batch deploy (AWS Batch + Step Functions). Replaces the manual
# "build → ECR push → tofu plan/apply → restart" dance we were doing by hand.
# AWS_REGION / S3_BUCKET / STATE_MACHINE_ARN come from .env like everything else.
# ---------------------------------------------------------------------------
AWS_REGION ?= us-west-2
TF_DIR := infra/terraform
# ECR repo URL for the Batch worker image — resolved from tofu state, or set
# in .env to override. ECR_REGISTRY is the host part (for docker login).
# -no-color so a colorized tofu warning can never leak ANSI escapes into the
# value (which then reach `docker login` as a garbage registry host).
ECR_REPO ?= $(shell cd $(TF_DIR) && tofu output -no-color -raw ecr_repo_url 2>/dev/null)
ECR_REGISTRY = $(firstword $(subst /, ,$(ECR_REPO)))

# Worker-image tag: short-sha of the last commit that touched anything baked
# INTO the image (the Dockerfile + the dirs it COPYs). Commits that only change
# Makefile / infra / docs leave this put — so the ECR tag, the job-def image
# pins, and the AMI don't churn on every commit, and `make infra-plan` stops
# showing phantom job-def re-tags. Falls back to HEAD if git isn't available.
# Only the paths the WORKERS actually run out of the image (Dockerfile + the
# Python package + static). The Go binary is also baked in but is NEVER
# executed on a worker (Batch/legacy override the entrypoint to python), and
# the local server runs from a fresh `make build`, not the ECR image — so a
# Go-only change must NOT bump the worker tag (that just forces a pointless
# re-push + AMI re-bake). cmd/ internal/ go.mod are deliberately excluded.
IMAGE_TAG := $(shell git log -1 --format=%h -- Dockerfile requirements.txt scripts static 2>/dev/null || echo $(GIT_SHA))

# The SHA tag of the image ACTUALLY in ECR (most-recent push, excluding the
# mutable :latest). This is what a cloud remote can definitely pull — using the
# local IMAGE_TAG would break the legacy target whenever a bare `make restart`
# advanced it to a tag that was only built locally, never pushed. A sha (not
# :latest) so the shared-AMI lookup, which keys on image_tag, still matches.
ECR_PUSHED_TAG := $(shell aws ecr describe-images --repository-name infinite-streaming-encoder-worker \
	--region $(AWS_REGION) --query 'reverse(sort_by(imageDetails,&imagePushedAt))[0].imageTags' \
	--output text 2>/dev/null | tr '\t' '\n' | grep -v '^latest$$' | head -1)

# Image the LEGACY (single-instance) cloud target pulls on the remote — the
# same ECR image the Batch target runs (PAT-free, apples-to-apples). Defaults
# to the last-pushed sha so it's always pullable; falls back to IMAGE_TAG only
# if ECR can't be reached. Override in .env to pin a specific tag.
DOCKER_IMAGE ?= $(ECR_REPO):$(if $(ECR_PUSHED_TAG),$(ECR_PUSHED_TAG),$(IMAGE_TAG))

# Step Functions ARN — auto-resolved from Terraform state (like ECR_REPO) so a
# fresh `cloud-up` needs nothing hand-copied into .env. A value in .env wins (?=).
STATE_MACHINE_ARN ?= $(shell cd $(TF_DIR) && tofu output -no-color -raw state_machine_arn 2>/dev/null)

# USE_AMI=1 pre-bakes the worker AMI during `cloud-up` for faster cold starts
# (~$1.50/mo until cloud-clear/cloud-down). Default off: cold ECR pull (~60s).
USE_AMI ?=

.PHONY: ecr-login ecr-publish infra-init infra-plan infra-apply deploy deploy-review timing cpu-report cloud-up cloud-clear cloud-down cloud-check ami-up ami-down

# Resolve the pre-baked worker AMI for the CURRENT image tag, if one exists.
# Empty when nothing is baked -> Batch pulls the image on boot. This is what
# makes the AMI cache opt-in and self-correcting: bake before an encode
# session, `make ami-down` after, and infra-plan/apply just pick up whatever
# is (or isn't) there for this image tag.
WORKER_AMI ?= $(shell aws ec2 describe-images --owners self --region $(AWS_REGION) \
	--filters "Name=tag:image_tag,Values=$(IMAGE_TAG)" "Name=state,Values=available" \
	--query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null | grep -v '^None$$' || true)

ecr-login:
	@: $${ECR_REPO:?ECR_REPO empty — run `make infra-apply` first, or set it in .env}
	aws ecr get-login-password --region $(AWS_REGION) | \
	  docker login --username AWS --password-stdin $(ECR_REGISTRY)

# DEPRECATED: folded into `publish` (#55), which now pushes ECR whenever a cloud
# stack is configured (so ECR + GHCR stay in sync). Kept as a thin alias so
# existing muscle-memory and any external scripts keep working.
ecr-publish: publish   ## DEPRECATED alias for `publish` (pushes ECR when cloud is configured)

infra-init:           ## tofu init (local backend override, if present)
	cd $(TF_DIR) && tofu init

infra-plan:           ## tofu plan -> tf.plan (review before infra-apply)
	@echo ">>> image_tag=$(IMAGE_TAG)  worker_ami_id=$(if $(WORKER_AMI),$(WORKER_AMI),<none, pull-on-boot>)"
	cd $(TF_DIR) && AWS_REGION=$(AWS_REGION) tofu plan \
		-var image_tag=$(IMAGE_TAG) \
		-var worker_ami_id="$(WORKER_AMI)" \
		-out=tf.plan

infra-apply:          ## apply the saved tf.plan (run only after reviewing the plan)
	cd $(TF_DIR) && AWS_REGION=$(AWS_REGION) tofu apply tf.plan

cloud-down:           ## FULL teardown -> $0/mo (no prompt): tofu destroy -auto-approve + remove AMI
	cd $(TF_DIR) && AWS_REGION=$(AWS_REGION) tofu destroy -auto-approve
	$(MAKE) ami-down   # AMI isn't tofu-managed; remove it too so nothing bills

# Bring the cloud stack up to match the CURRENT code — the everyday cloud action
# (run it for a new account, or after a worker-image change). Idempotent: tofu
# creates on first run and reconciles after; every step is a sub-make so it
# re-resolves ECR_REPO / WORKER_AMI from CURRENT state. Order matters:
#   1. init + apply   — create/reconcile ECR, Batch, VPC, SFN, IAM (image pulls
#                       on boot unless an AMI is wired in below).
#   2. publish        — push :IMAGE_TAG to ECR (+ GHCR) so the image exists to run.
#   3. USE_AMI=1 only — ami-up (bake), wait for it, then a second plan+apply wires
#                       the AMI into the launch template. Off by default (~60s
#                       cold ECR pull is cheaper than the standing ~$1.50/mo).
#   4. cloud-check    — verify creds + state machine + bucket are actually usable.
cloud-up:             ## provision/reconcile the cloud stack to current code + verify (USE_AMI=1 bakes the AMI)
	$(MAKE) infra-init
	$(MAKE) infra-plan
	$(MAKE) infra-apply
	$(MAKE) publish
	@if [ "$(USE_AMI)" = 1 ]; then \
	  $(MAKE) ami-up; \
	  echo ">>> waiting for the baked AMI to be queryable (EC2 is eventually consistent)..."; \
	  until [ -n "$$(aws ec2 describe-images --owners self --region $(AWS_REGION) \
	      --filters Name=tag:image_tag,Values=$(IMAGE_TAG) Name=state,Values=available \
	      --query 'Images[0].ImageId' --output text 2>/dev/null | grep -v '^None$$')" ]; do sleep 3; done; \
	  $(MAKE) infra-plan; $(MAKE) infra-apply; \
	  echo ">>> AMI baked + wired — cold starts skip the ECR pull (~\$$1.50/mo until cloud-clear)."; \
	else echo ">>> skipping AMI bake (USE_AMI unset -> pull-on-boot). Set USE_AMI=1 for warm starts."; fi
	@$(MAKE) cloud-check
	@echo ">>> cloud-up complete."

# Live readiness: proves a cloud job could actually run — creds valid, the state
# machine exists, the staging bucket is reachable. Uses runtime creds; seconds,
# not a tofu plan. Run standalone anytime to answer "is cloud usable right now?"
cloud-check:          ## live cloud readiness: AWS creds + state machine + S3 bucket reachable
	@aws sts get-caller-identity >/dev/null 2>&1 \
	  && echo "  ok: AWS credentials valid" || { echo "  FAIL: AWS credentials (check ~/.aws)"; exit 1; }
	@aws stepfunctions describe-state-machine --state-machine-arn "$(STATE_MACHINE_ARN)" \
	    --region $(AWS_REGION) >/dev/null 2>&1 \
	  && echo "  ok: state machine live" || { echo "  FAIL: STATE_MACHINE_ARN not reachable — run cloud-up"; exit 1; }
	@aws s3api head-bucket --bucket "$(S3_BUCKET)" 2>/dev/null \
	  && echo "  ok: S3 bucket $(S3_BUCKET) reachable" || { echo "  FAIL: S3_BUCKET '$(S3_BUCKET)' not reachable"; exit 1; }

# Deploy stops at the plan on purpose — review it, then run `make infra-apply`.
# (Keeping preview and apply as separate, deliberate steps for live IaC.)
deploy:               ## push image + restart + plan + APPLY infra (one shot)
	@start=$$(date +%s); \
	echo ">>> deploy started $$(date '+%H:%M:%S')  worker=$(IMAGE_TAG)"; \
	if $(MAKE) publish && $(MAKE) restart && $(MAKE) infra-plan && $(MAKE) infra-apply; then \
		el=$$(( $$(date +%s) - start )); \
		printf '\a\n\033[1;32m==================================================\n'; \
		printf '  DEPLOY COMPLETE  %dm %02ds   worker=%s\n' $$((el/60)) $$((el%60)) "$(IMAGE_TAG)"; \
		printf '  image pushed - server restarted - infra applied\n'; \
		printf '==================================================\033[0m\n'; \
	else \
		el=$$(( $$(date +%s) - start )); \
		printf '\a\n\033[1;31m!!! DEPLOY FAILED after %dm %02ds - see output above\033[0m\n' $$((el/60)) $$((el%60)); \
		exit 1; \
	fi

deploy-review:        ## like deploy but stop at the plan (review before infra-apply)
	$(MAKE) ecr-publish
	$(MAKE) restart
	$(MAKE) infra-plan
	@echo ">>> Review the plan above. To apply it:  make infra-apply"

timing:               ## where-did-the-time-go for an execution: make timing EXEC=<arn>
	@: $${EXEC:?set EXEC=<execution-arn>}
	docker exec $(CONTAINER_NAME) python3 -m infinite_streaming_encoder.cloud.timing --execution-arn $(EXEC)

cpu-report:           ## per-tier encode CPU utilization vs reserved vCPU: make cpu-report EXEC=<arn>
	@: $${EXEC:?set EXEC=<execution-arn>}
	docker exec $(CONTAINER_NAME) python3 -m infinite_streaming_encoder.cloud.cpu_report --execution-arn $(EXEC)

# ---- Worker-AMI cache (opt-in, one at a time) --------------------------------
# The AMI is a pre-warmed cache: a cold spot instance boots with the encoder
# image already resident, skipping the ~60s ECR pull. It's OPT-IN and costs
# ~$1.50/mo in EBS-snapshot storage while it exists, so bake it before an
# encode session and `make ami-down` after. Exactly one infinite-streaming-encoder-worker AMI
# is ever kept: bake prunes every other one, unbake removes them all.

ami-up:             ## build a worker AMI with the current image pre-pulled (keeps only this one)
	@: $${ECR_REPO:?ECR_REPO empty — run `make infra-apply` first, or set it in .env}
	cd infra/packer && packer init worker-ami.pkr.hcl && \
	  packer build -var region=$(AWS_REGION) -var ecr_repo=$(ECR_REPO) \
	    -var image_tag=$(IMAGE_TAG) worker-ami.pkr.hcl
	@echo ">>> keeping only infinite-streaming-encoder-worker-$(IMAGE_TAG); removing any older worker AMIs..."
	@ids=$$(aws ec2 describe-images --owners self --region $(AWS_REGION) \
	    --filters "Name=tag:Name,Values=infinite-streaming-encoder-worker" \
	    --query "Images[?Tags[?Key=='image_tag'&&Value!='$(IMAGE_TAG)']].ImageId" --output text); \
	  for ami in $$ids; do \
	    [ "$$ami" = "None" ] && continue; \
	    snaps=$$(aws ec2 describe-images --owners self --region $(AWS_REGION) --image-ids $$ami \
	      --query 'Images[].BlockDeviceMappings[].Ebs.SnapshotId' --output text); \
	    echo "deregister $$ami"; aws ec2 deregister-image --region $(AWS_REGION) --image-id $$ami; \
	    for s in $$snaps; do echo "  delete snapshot $$s"; aws ec2 delete-snapshot --region $(AWS_REGION) --snapshot-id $$s; done; \
	  done
	@echo ">>> Baked infinite-streaming-encoder-worker-$(IMAGE_TAG) (1 AMI total). Now: make infra-apply  (wires it in)"

ami-down:           ## clear the compute-env AMI pointer, THEN delete the AMIs (self-clearing, no dangling ref)
	# Clear the compute env's image_id_override FIRST so we never delete an AMI
	# the env still points at — no dangling pointer, no manual follow-up apply.
	# Targeted to the compute env only (won't touch job defs). Guarded so it
	# no-ops on an already-destroyed stack (cloud-down calls this after
	# teardown, when there's nothing left in state to apply).
	@if cd $(TF_DIR) && tofu state list 2>/dev/null | grep -q 'aws_batch_compute_environment.spot_graviton'; then \
	  echo ">>> clearing compute-env AMI pointer (-> pull-on-boot)..."; \
	  AWS_REGION=$(AWS_REGION) tofu apply -auto-approve \
	    -target=module.compute.aws_batch_compute_environment.spot_graviton \
	    -var image_tag=$(IMAGE_TAG) -var worker_ami_id="" ; \
	else echo ">>> no compute env in state — skipping pointer clear"; fi
	@ids=$$(aws ec2 describe-images --owners self --region $(AWS_REGION) \
	    --filters "Name=tag:Name,Values=infinite-streaming-encoder-worker" --query 'Images[].ImageId' --output text); \
	  if [ -z "$$ids" ] || [ "$$ids" = "None" ]; then echo "no infinite-streaming-encoder-worker AMI to remove"; else \
	  for ami in $$ids; do \
	    snaps=$$(aws ec2 describe-images --owners self --region $(AWS_REGION) --image-ids $$ami \
	      --query 'Images[].BlockDeviceMappings[].Ebs.SnapshotId' --output text); \
	    echo "deregister $$ami"; aws ec2 deregister-image --region $(AWS_REGION) --image-id $$ami; \
	    for s in $$snaps; do echo "  delete snapshot $$s"; aws ec2 delete-snapshot --region $(AWS_REGION) --snapshot-id $$s; done; \
	  done; fi
	@echo ">>> Removed. Compute env is on pull-on-boot; nothing dangling."

# ---- Cost teardown -----------------------------------------------------------
# cloud-clear kills everything that bills while IDLE without destroying the
# reusable stack (compute env, queue, SFN, VPC, IAM are all ~$0 at rest —
# scale-to-zero spot, IGW/public subnets, free S3 gateway endpoint). It runs
# the same tagged sweep as the app's Emergency Clear (instances / orphan
# volumes / spot requests / S3 data tagged Application=infinite-streaming-encoder-app) and removes
# the worker AMI + snapshot. For a TOTAL teardown (also drops ECR images, log
# groups, VPC — next use needs a full re-deploy) use `make cloud-down`.

cloud-clear:          ## kill every idle AWS cost: sweep tagged instances/volumes/spot/S3 + remove worker AMI
	@echo ">>> sweeping Application=infinite-streaming-encoder-app runtime resources (instances, volumes, spot, S3)..."
	docker exec $(CONTAINER_NAME) python3 -m infinite_streaming_encoder.cloud.cleanup --sweep-all
	@echo ">>> removing worker AMI(s)..."
	$(MAKE) ami-down
	@echo ">>> Idle cost generators cleared (AMI pointer self-cleared to pull-on-boot)."
	@echo ">>> The Batch stack stays (it's ~\$$0 at rest). Full teardown: make cloud-down"

# ---- Distributed-local encoding (Temporal + MinIO, no AWS) --------------------
# All-container control plane on this (master) box; workers run one-per-box and
# pull work. All of this now lives in the unified docker-compose.yml (master /
# worker profiles); these are convenience aliases for pieces of it.
.PHONY: dist-up dist-down dist-worker dist-logs dist-ps

dist-up: require-paths   ## bring up ONLY the local cluster (temporal + ui + postgres + minio)
	$(COMPOSE_BASE) up -d postgresql temporal temporal-ui minio
	@echo ">>> Temporal UI: http://localhost:$${TEMPORAL_UI_PORT:-8233}   MinIO console: http://localhost:$${MINIO_CONSOLE_PORT:-9001}"

dist-down:            ## stop the whole master stack (cluster + server + worker; ARGS=-v wipes volumes)
	$(COMPOSE_BASE) --profile master down $(ARGS)

dist-worker:          ## (re)build + run the local worker on THIS box (no source dirs needed)
	ENCODER_IMAGE=$(IMAGE_NAME) ENCODE_SLOTS=$(FARM_ENCODE_SLOTS) $(COMPOSE_BASE) up -d --build worker

dist-logs:            ## follow the local worker log
	docker logs -f $${DIST_WORKER_CONTAINER:-encode-worker}

dist-ps:              ## cluster + worker + server containers
	$(COMPOSE_BASE) --profile master ps

# DIST_WORKERS: space-separated label=ssh_target pairs of remote worker boxes,
# e.g. DIST_WORKERS = ubuntu=me@worker-box.local
# MASTER_IP: the master box's LAN IP that workers dial for Temporal + MinIO.
DIST_WORKERS ?=
MASTER_IP ?= 192.168.1.10
.PHONY: dist-deploy-workers dist-deploy dist-deploy-ghcr

dist-deploy-workers:  ## rsync code + rebuild image + (re)start worker on each DIST_WORKERS box
	@if [ -z "$(DIST_WORKERS)" ]; then echo "set DIST_WORKERS=label=ssh_target [..] (in .env)"; exit 1; fi
	@for w in $(DIST_WORKERS); do \
	  label=$${w%%=*}; host=$${w#*=}; \
	  MASTER_IP=$(MASTER_IP) MINIO_ROOT_USER=$(MINIO_ROOT_USER) MINIO_ROOT_PASSWORD=$(MINIO_ROOT_PASSWORD) \
	  DEV_BUILD=$(DEV_BUILD) FORCE_IMAGE=$(FORCE_IMAGE) \
	    bash infra/local-cluster/deploy-worker.sh "$$host" "$$label" || exit 1; \
	done
	@echo ">>> remote workers deployed (DEV_BUILD=1 native-builds uncommitted deps on cross-arch boxes)."

dist-deploy: build dist-worker dist-deploy-workers  ## deploy distributed-local to the master + all remote boxes
	@echo ">>> distributed-local deployed: master worker + $(words $(DIST_WORKERS)) remote box(es)."

# Like dist-deploy-workers, but every box PULLS the published image from GHCR
# (no 900MB image transfer, no per-box build, any arch). The published package
# is PUBLIC, so workers pull with no auth — we blank GHCR_PAT for the worker
# call so the deploy never attempts a `docker login`. Blanking (not just
# omitting) is required because the Makefile `export`s .env, so GHCR_PAT would
# otherwise reach the script via the environment. Why it matters: `docker login`
# over SSH fails headlessly on a macOS box (Docker Desktop's keychain credsStore:
# "-25308 User interaction is not allowed"), which is what used to force the
# macmini onto the image-transfer path. Pair with `make run-remote` on the master
# for a fully no-local-build bring-up. (Forked to a PRIVATE package? Log each
# worker box into GHCR by hand first — not supported headlessly on macOS.)
dist-deploy-ghcr:     ## GHCR-pull workers on each DIST_WORKERS box (no build/transfer, no auth)
	@if [ -z "$(DIST_WORKERS)" ]; then echo "set DIST_WORKERS=label=ssh_target [..] (in .env)"; exit 1; fi
	@for w in $(DIST_WORKERS); do \
	  label=$${w%%=*}; host=$${w#*=}; \
	  GHCR_PAT= MASTER_IP=$(MASTER_IP) IMAGE=$(REMOTE_IMAGE) GHCR_USERNAME=$(GHCR_USERNAME) \
	    MINIO_ROOT_USER=$(MINIO_ACCESS_KEY) MINIO_ROOT_PASSWORD=$(MINIO_SECRET_KEY) \
	    bash infra/local-cluster/deploy-worker-ghcr.sh "$$host" "$$label" || exit 1; \
	done
	@echo ">>> GHCR-pull workers deployed to $(words $(DIST_WORKERS)) box(es)."

# ---- One-command farm --------------------------------------------------------
# `make farm-up` brings the whole master profile up in ONE compose command (cluster
# + server + one local worker), pulling the image from GHCR (no local build). It's
# the single canonical bring-up. Extra boxes come from DIST_WORKERS in .env. Run
# `make push` first so GHCR has your current code.
# `make farm-dev-up` is the developer loop: same bring-up but a local build + the
# dev overlay that bind-mounts your working-tree scripts/infinite_streaming_encoder
# live into the server + worker — re-run after edits (Go/deps still need --build).
# `make farm-down` / `farm-dev-down` are the inverse (local stack + remote workers);
# teardown is mode-agnostic so they're the same operation.
.PHONY: farm-up farm-dev-up farm-dev-down farm-down

farm-up: require-paths   ## bring the whole master farm up from GHCR (cluster + server + worker), + DIST_WORKERS
	@echo ">>> [farm-up] pulling images + bringing up the master profile (cluster + server + worker) from GHCR..."
	@if [ -n "$(GHCR_PAT)" ]; then echo "$(GHCR_PAT)" | docker login ghcr.io -u $(GHCR_USERNAME) --password-stdin; fi
	ENCODER_IMAGE=$(REMOTE_IMAGE) ENCODE_SLOTS=$(FARM_ENCODE_SLOTS) $(COMPOSE_BASE) --profile master pull
	ENCODER_IMAGE=$(REMOTE_IMAGE) ENCODE_SLOTS=$(FARM_ENCODE_SLOTS) $(COMPOSE_BASE) --profile master up -d --no-build
	@echo ">>> [farm-up] remote workers (from GHCR)..."
	@if [ -n "$(DIST_WORKERS)" ]; then $(MAKE) dist-deploy-ghcr; else echo "    (no DIST_WORKERS — master-only farm)"; fi
	@echo ">>> farm up:  UI http://localhost:$(PORT)   Temporal UI http://localhost:$${TEMPORAL_UI_PORT:-8233}"

farm-dev-up: require-paths   ## dev farm from your WORKING TREE (uncommitted): local build + live-mounted code
	@echo ">>> [farm-dev-up] building from working tree + bringing up the master profile with live code..."
	ENCODER_IMAGE=$(IMAGE_NAME) ENCODE_SLOTS=$(FARM_ENCODE_SLOTS) \
	  HOST_SCRIPTS_DIR=$(CURDIR)/scripts/infinite_streaming_encoder \
	  $(COMPOSE_DEV) --profile master up -d --build
	@echo ">>> [farm-dev-up] remote workers (rsync code + transfer/build image)..."
	@if [ -n "$(DIST_WORKERS)" ]; then $(MAKE) dist-deploy-workers; else echo "    (no DIST_WORKERS — master-only)"; fi
	@echo ">>> farm-dev-up complete (working tree — nothing committed/pushed). Re-run 'make farm-dev-up' after edits."

# True inverse of `make farm-up`: stop the local master stack AND the remote workers
# on each DIST_WORKERS box (compose only manages the local project, so the remote
# encode-worker containers are removed over SSH — same target `make farm-up` deploys
# to). ARGS=-v also wipes the local Temporal/MinIO volumes.
farm-down:            ## take the WHOLE farm down: local master stack + remote DIST_WORKERS (ARGS=-v wipes volumes)
	@if [ -n "$(DIST_WORKERS)" ]; then \
	  echo ">>> [farm-down] removing remote workers ($(words $(DIST_WORKERS)) box(es))..."; \
	  for w in $(DIST_WORKERS); do \
	    label=$${w%%=*}; host=$${w#*=}; \
	    printf '    %s (%s): ' "$$label" "$$host"; \
	    ssh -o IgnoreUnknown=UseKeychain -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=6 \
	      "$$host" "docker rm -f $(DIST_WORKER_CONTAINER)" >/dev/null 2>&1 && echo "removed" || echo "(unreachable or no worker)"; \
	  done; \
	else echo ">>> [farm-down] no DIST_WORKERS — local only"; fi
	@echo ">>> [farm-down] stopping local master stack..."
	$(COMPOSE_BASE) --profile master down $(ARGS)

# Teardown is mode-agnostic — a farm brought up by farm-dev-up lives in the same
# compose project + encode-worker containers as one from farm-up — so farm-dev-down
# is exactly farm-down. Kept as a distinct name for farm-dev-up/down symmetry.
farm-dev-down: farm-down   ## take the dev farm down (identical to `make farm-down`; ARGS=-v wipes volumes)

# ---- Smoke test --------------------------------------------------------------
# End-to-end single-device check (docs/TESTING.md, test 1): generate a tiny clip,
# bring up a 1-box farm from the working tree, encode it, assert the output.
# The multi-box and cloud topologies are manual (hardware / cost).
.PHONY: smoke
SMOKE_SRC ?= $(SOURCE_DIR)/smoke.mp4
# Open the jobs page in a browser during the run; set SMOKE_OPEN=0 to skip.
SMOKE_OPEN ?= 1

smoke: require-paths build   ## end-to-end single-device smoke: tiny clip -> local-dist encode -> assert output
	@echo ">>> [smoke] generating $(SMOKE_SRC) (if missing)..."
	@[ -f "$(SMOKE_SRC)" ] || docker run --rm -v "$(SOURCE_DIR):/src" --entrypoint ffmpeg $(IMAGE_NAME) \
	  -f lavfi -i testsrc2=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 \
	  -t 20 -pix_fmt yuv420p -c:v libx264 -c:a aac -shortest -y /src/smoke.mp4
	@echo ">>> [smoke] clearing any prior smoke output..."
	@rm -rf $(OUTPUT_DIR)/smoke_p200* 2>/dev/null || true
	@echo ">>> [smoke] bringing up a single-device farm from the working tree..."
	$(MAKE) farm-dev-up DIST_WORKERS=
	@echo ">>> [smoke] waiting for the server (:$(PORT))..."
	@for i in $$(seq 1 30); do curl -sf http://localhost:$(PORT)/api/jobs >/dev/null 2>&1 && break; sleep 1; done
	@echo ">>> [smoke] opening the jobs page (set SMOKE_OPEN=0 to skip)..."
	@[ "$(SMOKE_OPEN)" = "0" ] || ( open http://localhost:$(PORT)/ 2>/dev/null || xdg-open http://localhost:$(PORT)/ 2>/dev/null ) || echo "    watch it at http://localhost:$(PORT)/"
	@echo ">>> [smoke] submitting encode (h264, 720p, 12s chunks) + waiting (timeout ~300s)..."
	@id=$$(curl -sf -X POST http://localhost:$(PORT)/api/encode -H 'Content-Type: application/json' \
	    -d '{"files":["smoke.mp4"],"target":"local","codec":"h264","max_res":"720p","chunk_duration":"12"}' \
	    | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])'); \
	  echo "    job $$id"; st=pending; \
	  for i in $$(seq 1 60); do \
	    st=$$(curl -sf http://localhost:$(PORT)/api/jobs | python3 -c "import sys,json; j=[x for x in json.load(sys.stdin) if x['id']=='$$id']; print(j[0]['status'] if j else 'gone')"); \
	    echo "    status=$$st"; \
	    case "$$st" in done) break;; failed|gone) echo '>>> SMOKE FAIL: job '"$$st"; exit 1;; esac; \
	    sleep 5; \
	  done; \
	  [ "$$st" = done ] || { echo '>>> SMOKE FAIL: timed out'; exit 1; }
	@d=$$(ls -d $(OUTPUT_DIR)/smoke_p200*h264* 2>/dev/null | head -1); \
	 if [ -n "$$d" ] && ls "$$d"/*.m3u8 >/dev/null 2>&1; then \
	   echo ">>> SMOKE PASS: $$d (has playlists)"; \
	 else echo ">>> SMOKE FAIL: no smoke output dir with playlists in $(OUTPUT_DIR)"; exit 1; fi

# ---- OOBE (out-of-box experience) test ---------------------------------------
# Runs a fully ISOLATED instance — its own dirs, ports, container names, and a
# second Temporal/MinIO cluster — so the first-run-from-nothing path is exercised
# WITHOUT touching your live farm, then tears itself down. Good for catching the
# config/dir/port/wiring problems your warm setup hides.
# LIMIT: the Docker image cache is shared, so this does NOT test cold image pulls
# (ffmpeg/Shaka/base images) or missing host tools — for those, use a fresh box.
OOBE_DIR ?= $(HOME)/encoder-oobe
OOBE_PORT ?= 8090
OOBE_TEMPORAL_PORT ?= 7333
OOBE_TEMPORAL_UI_PORT ?= 8333
OOBE_MINIO_PORT ?= 9100
OOBE_MINIO_CONSOLE_PORT ?= 9101
OOBE_PROJECT ?= encoder-oobe
OOBE_SERVER ?= encoder-oobe
OOBE_WORKER ?= encode-worker-oobe
# OOBE_KEEP=1 leaves the isolated instance up on finish (pass OR fail) so you can
# inspect logs at :$(OOBE_PORT); otherwise it always tears down.
OOBE_KEEP ?=
# All the env an isolated OOBE stack needs: its own container names, host ports,
# dirs, and cluster addresses (server + worker reach the isolated cluster via
# host.docker.internal at the OOBE ports). Fed to the same unified compose file
# via its own project (-p), so it's fully isolated from the live farm.
OOBE_ENV = CONTAINER_NAME=$(OOBE_SERVER) DIST_WORKER_CONTAINER=$(OOBE_WORKER) \
	PORT=$(OOBE_PORT) TEMPORAL_PORT=$(OOBE_TEMPORAL_PORT) TEMPORAL_UI_PORT=$(OOBE_TEMPORAL_UI_PORT) \
	MINIO_API_PORT=$(OOBE_MINIO_PORT) MINIO_CONSOLE_PORT=$(OOBE_MINIO_CONSOLE_PORT) \
	SOURCE_DIR=$(OOBE_DIR)/source OUTPUT_DIR=$(OOBE_DIR)/output TMP_DIR=$(OOBE_DIR)/tmp \
	TEMPORAL_ADDRESS=host.docker.internal:$(OOBE_TEMPORAL_PORT) \
	MINIO_ENDPOINT=http://host.docker.internal:$(OOBE_MINIO_PORT) \
	S3_ENDPOINT_URL=http://host.docker.internal:$(OOBE_MINIO_PORT) \
	LOCAL_WORKER_LABEL=oobe ENCODER_IMAGE=$(IMAGE_NAME) \
	HOST_SCRIPTS_DIR=$(CURDIR)/scripts/infinite_streaming_encoder
OOBE_COMPOSE = docker compose -p $(OOBE_PROJECT) -f docker-compose.yml -f docker-compose.dev.yml

.PHONY: oobe oobe-down
oobe: build   ## isolated first-run test: own dirs/ports/cluster -> encode -> assert -> tear down
	@echo ">>> [oobe] fresh dirs under $(OOBE_DIR)"
	@rm -rf $(OOBE_DIR); mkdir -p $(OOBE_DIR)/source $(OOBE_DIR)/output $(OOBE_DIR)/tmp
	@echo ">>> [oobe] generating a tiny clip in the isolated source dir..."
	@docker run --rm -v "$(OOBE_DIR)/source:/src" --entrypoint ffmpeg $(IMAGE_NAME) \
	  -f lavfi -i testsrc2=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 \
	  -t 20 -pix_fmt yuv420p -c:v libx264 -c:a aac -shortest -y /src/smoke.mp4
	@echo ">>> [oobe] bringing up isolated master stack '$(OOBE_PROJECT)' on ports $(OOBE_PORT)/$(OOBE_TEMPORAL_PORT)/$(OOBE_MINIO_PORT) (one compose up)..."
	$(OOBE_ENV) $(OOBE_COMPOSE) --profile master up -d --build
	@echo ">>> [oobe] waiting for the isolated server (:$(OOBE_PORT))..."
	@for i in $$(seq 1 30); do curl -sf http://localhost:$(OOBE_PORT)/api/jobs >/dev/null 2>&1 && break; sleep 1; done
	@echo ">>> [oobe] submitting encode + waiting (timeout ~300s)..."
	@id=$$(curl -sf -X POST http://localhost:$(OOBE_PORT)/api/encode -H 'Content-Type: application/json' \
	    -d '{"files":["smoke.mp4"],"target":"local","codec":"h264","max_res":"720p","chunk_duration":"12"}' \
	    | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])'); \
	  echo "    job $$id"; st=pending; \
	  for i in $$(seq 1 60); do \
	    st=$$(curl -sf http://localhost:$(OOBE_PORT)/api/jobs | python3 -c "import sys,json; j=[x for x in json.load(sys.stdin) if x['id']=='$$id']; print(j[0]['status'] if j else 'gone')"); \
	    echo "    status=$$st"; \
	    case "$$st" in done) break;; failed|gone) break;; esac; sleep 5; \
	  done; \
	  d=$$(ls -d $(OOBE_DIR)/output/smoke_p200*h264* 2>/dev/null | head -1); \
	  if [ "$$st" = done ] && [ -n "$$d" ] && ls "$$d"/*.m3u8 >/dev/null 2>&1; then res="OOBE PASS: $$d (has playlists)"; else res="OOBE FAIL (status=$$st)"; fi; \
	  if [ "$(OOBE_KEEP)" = "1" ]; then echo ">>> [oobe] OOBE_KEEP=1 — leaving instance up (logs at http://localhost:$(OOBE_PORT); 'make oobe-down' to clean)"; \
	  else echo ">>> [oobe] tearing down..."; $(MAKE) oobe-down >/dev/null 2>&1 || true; fi; \
	  echo ">>> $$res"; case "$$res" in OOBE\ PASS*) exit 0;; *) exit 1;; esac

oobe-down:            ## tear down the isolated OOBE instance (server, worker, cluster + volumes, dirs)
	-$(OOBE_ENV) $(OOBE_COMPOSE) --profile master down -v 2>/dev/null
	@# Workers run as root and (on Linux, no UID remap) leave root-owned files in
	@# the bind-mounted dir, so a plain host rm can hit "Permission denied". Try
	@# the host rm first, then fall back to removing it from inside a container.
	-rm -rf $(OOBE_DIR) 2>/dev/null
	@if [ -d "$(OOBE_DIR)" ]; then \
	  echo "    removing root-owned leftovers via a throwaway container..."; \
	  docker run --rm -v "$(dir $(OOBE_DIR)):/parent" alpine rm -rf "/parent/$(notdir $(OOBE_DIR))" 2>/dev/null || true; \
	fi
	@[ -d "$(OOBE_DIR)" ] && echo ">>> [oobe] WARNING: $(OOBE_DIR) could not be removed" || echo ">>> [oobe] torn down."
