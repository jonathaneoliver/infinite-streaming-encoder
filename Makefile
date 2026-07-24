
-include .env
export

IMAGE_NAME ?= encoder
CONTAINER_NAME ?= encoder
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
# Label for the master box's own worker + the container name workers run as
# (run-worker.sh WORKER_NAME) — used by the server to toggle machines on/off.
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

# Promote (staging -> live rsync). All optional; no-ops when unset.
#  - PROMOTE_LOCAL_DIR: host dir mounted at /media/promote-local (a local dest;
#    reference /media/promote-local in PROMOTE_DESTS).
#  - PROMOTE_SSH_HOST: a *.local remote resolved here via mDNS and --add-host'd
#    into the container, since Docker can't resolve .local names itself.
PROMOTE_MOUNT := $(if $(PROMOTE_LOCAL_DIR),-v $(PROMOTE_LOCAL_DIR):/media/promote-local,)
PROMOTE_SSH_IP := $(if $(PROMOTE_SSH_HOST),$(shell dscacheutil -q host -a name $(PROMOTE_SSH_HOST) 2>/dev/null | awk '/^ip_address:/{print $$2; exit}'),)
PROMOTE_ADDHOST := $(if $(PROMOTE_SSH_IP),--add-host $(PROMOTE_SSH_HOST):$(PROMOTE_SSH_IP),)
# Forward the host ssh-agent (Docker Desktop magic socket) so a passphrase-
# protected key whose passphrase is in the macOS keychain works in-container.
PROMOTE_SSH_AGENT := $(if $(PROMOTE_SSH_HOST),-v /run/host-services/ssh-auth.sock:/ssh-agent -e SSH_AUTH_SOCK=/ssh-agent,)

# GHCR publishing
GHCR_IMAGE ?= ghcr.io/jonathaneoliver/encoder
GHCR_USERNAME ?= jonathaneoliver
PLATFORMS ?= linux/amd64,linux/arm64

# Which image `run` / `run-remote` launch. RUN_IMAGE feeds BOTH the server
# container and the worker containers (ENCODER_IMAGE), so the two targets
# differ only in this one value:
#   run        -> the locally-built $(IMAGE_NAME)
#   run-remote -> the published $(REMOTE_IMAGE), pulled from GHCR (no build)
RUN_IMAGE ?= $(IMAGE_NAME)
REMOTE_IMAGE ?= $(GHCR_IMAGE):latest

# Dev only: host path overlaid onto a spawned orchestrator's /app/scripts/encoder
# so it runs current working-tree code without a rebuild. Set by `make farm-dev`;
# empty in normal runs (the orchestrator then uses the image's baked scripts).
HOST_SCRIPTS_DIR ?=

.PHONY: require-paths build run run-remote stop restart logs shell status clean push push-setup cloud-push version setup-hooks

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
		-t $(IMAGE_NAME) .

version:
	@echo $(VERSION) $(GIT_SHA)

.PHONY: doctor
doctor:               ## preflight: check .env / host tools / per-target config, report clearly
	@bash scripts/doctor.sh

# Shared server-launch recipe used by both `run` and `run-remote`. $(RUN_IMAGE)
# selects the image for the server AND the worker containers it spawns, so the
# two targets share every mount/env and differ only in that one value.
define ENCODER_DOCKER_RUN
docker run --rm -d \
	--name $(CONTAINER_NAME) \
	-p $(PORT):8080 \
	--add-host host.docker.internal:host-gateway \
	-v $(SOURCE_DIR):/media/originals \
	-v $(OUTPUT_DIR):/media/dynamic_content \
	-v $(TMP_DIR):/media/tmp \
	-v /var/run/docker.sock:/var/run/docker.sock \
	-v $(HOME)/.aws:/root/.aws:ro \
	-v $(HOME)/.ssh:/root/.ssh:ro \
	$(PROMOTE_MOUNT) \
	$(PROMOTE_ADDHOST) \
	$(PROMOTE_SSH_AGENT) \
	-e 'PROMOTE_DESTS=$(PROMOTE_DESTS)' \
	-e SOURCE_DIR=/media/originals \
	-e OUTPUT_DIR=/media/dynamic_content \
	-e TMP_DIR=/media/tmp \
	-e SCRIPTS_DIR=/app/scripts \
	-e HOST_SOURCE_DIR=$(SOURCE_DIR) \
	-e HOST_OUTPUT_DIR=$(OUTPUT_DIR) \
	-e HOST_TMP_DIR=$(TMP_DIR) \
	-e HOST_AWS_DIR=$(HOME)/.aws \
	-e ENCODER_IMAGE=$(RUN_IMAGE) \
	-e AUTO_WATCH=$(AUTO_WATCH) \
	-e DEFAULT_TARGET=$(DEFAULT_TARGET) \
	-e DEFAULT_CODEC=$(DEFAULT_CODEC) \
	-e DEFAULT_MAX_RES=$(DEFAULT_MAX_RES) \
	-e MAX_CONCURRENT=$(MAX_CONCURRENT) \
	-e WARM_MIN_VCPUS=$(WARM_MIN_VCPUS) \
	-e DOCKER_IMAGE=$(DOCKER_IMAGE) \
	-e WORKER_AMI_ID=$(WORKER_AMI) \
	-e AWS_REGION=$(AWS_REGION) \
	-e S3_BUCKET=$(S3_BUCKET) \
	-e SUBNET_ID=$(SUBNET_ID) \
	-e SECURITY_GROUP_ID=$(SECURITY_GROUP_ID) \
	-e INSTANCE_PROFILE=$(INSTANCE_PROFILE) \
	-e INSTANCE_TYPE=$(INSTANCE_TYPE) \
	-e GHCR_PAT=$(GHCR_PAT) \
	-e STATE_MACHINE_ARN=$(STATE_MACHINE_ARN) \
	-e TEMPORAL_UI_ADDR=$(TEMPORAL_UI_ADDR) \
	-e TEMPORAL_ADDRESS=$(TEMPORAL_ADDRESS) \
	-e MINIO_ENDPOINT=$(MINIO_ENDPOINT) \
	-e MINIO_ACCESS_KEY=$(MINIO_ACCESS_KEY) \
	-e MINIO_SECRET_KEY=$(MINIO_SECRET_KEY) \
	-e DIST_S3_BUCKET=$(DIST_S3_BUCKET) \
	-e 'DIST_WORKERS=$(DIST_WORKERS)' \
	-e LOCAL_WORKER_LABEL=$(LOCAL_WORKER_LABEL) \
	-e DIST_WORKER_CONTAINER=$(DIST_WORKER_CONTAINER) \
	-e HOST_SCRIPTS_DIR=$(HOST_SCRIPTS_DIR) \
	$(RUN_IMAGE)
@echo "Encoder running at http://localhost:$(PORT)"
endef

run: require-paths build
	$(ENCODER_DOCKER_RUN)

# Fire up from the published GHCR image instead of a local build — for a fresh
# machine that just wants to run it. Pulls $(REMOTE_IMAGE) (which also becomes
# ENCODER_IMAGE for the worker containers); logs into GHCR first only if
# GHCR_PAT is set, which is needed when the package is private.
run-remote: RUN_IMAGE = $(REMOTE_IMAGE)
run-remote: require-paths
	@if [ -n "$$GHCR_PAT" ]; then \
		echo "$$GHCR_PAT" | docker login ghcr.io -u $(GHCR_USERNAME) --password-stdin; \
	fi
	docker pull $(REMOTE_IMAGE)
	$(ENCODER_DOCKER_RUN)

stop:
	docker stop $(CONTAINER_NAME) 2>/dev/null || true

restart: stop run

logs:
	docker logs -f $(CONTAINER_NAME)

shell:
	docker exec -it $(CONTAINER_NAME) /bin/sh

status:
	@docker ps --filter name=$(CONTAINER_NAME) --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || echo "not running"

clean: stop
	docker rmi $(IMAGE_NAME) 2>/dev/null || true

# One-time setup for multi-arch push. Requires GHCR_PAT in the
# environment (or .env) with write:packages scope.
push-setup:
	@: $${GHCR_PAT:?GHCR_PAT is not set — create a classic PAT with write:packages scope}
	@echo "$$GHCR_PAT" | docker login ghcr.io -u $(GHCR_USERNAME) --password-stdin
	@docker buildx inspect encoder-builder >/dev/null 2>&1 || \
		docker buildx create --name encoder-builder --driver docker-container --use
	@docker buildx use encoder-builder
	@docker buildx inspect --bootstrap

# Full multi-arch release push. Use when cutting a release (bumping
# VERSION) or when you've touched something Graviton-specific.
# Publishes three tags: latest, $(VERSION), and $(GIT_SHA).
push:
	docker buildx build \
		--platform $(PLATFORMS) \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_SHA=$(GIT_SHA) \
		--tag $(GHCR_IMAGE):latest \
		--tag $(GHCR_IMAGE):$(VERSION) \
		--tag $(GHCR_IMAGE):$(GIT_SHA) \
		--push \
		.
	@echo "Published $(GHCR_IMAGE):latest :$(VERSION) :$(GIT_SHA) for $(PLATFORMS)"

# Fast iterative push for cloud-feature work. Single-arch (linux/amd64,
# matches the default c7i instance family), so no 5-10 minute ARM QEMU
# build. Useful when you're debugging the cloud pipeline and want the
# next EC2 launch to pick up your changes. Publishes :latest + commit
# SHA tags; skips the :$(VERSION) tag so release tags stay multi-arch.
# If you need Graviton after iterating, finish with `make push`.
cloud-push:
	docker buildx build \
		--platform linux/amd64 \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_SHA=$(GIT_SHA) \
		--tag $(GHCR_IMAGE):latest \
		--tag $(GHCR_IMAGE):$(GIT_SHA) \
		--push \
		.
	@echo "Published $(GHCR_IMAGE):latest :$(GIT_SHA) (linux/amd64 only)"

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
ECR_PUSHED_TAG := $(shell aws ecr describe-images --repository-name encoder-worker \
	--region $(AWS_REGION) --query 'reverse(sort_by(imageDetails,&imagePushedAt))[0].imageTags' \
	--output text 2>/dev/null | tr '\t' '\n' | grep -v '^latest$$' | head -1)

# Image the LEGACY (single-instance) cloud target pulls on the remote — the
# same ECR image the Batch target runs (PAT-free, apples-to-apples). Defaults
# to the last-pushed sha so it's always pullable; falls back to IMAGE_TAG only
# if ECR can't be reached. Override in .env to pin a specific tag.
DOCKER_IMAGE ?= $(ECR_REPO):$(if $(ECR_PUSHED_TAG),$(ECR_PUSHED_TAG),$(IMAGE_TAG))

.PHONY: ecr-login ecr-push infra-init infra-plan infra-apply infra-destroy infra-teardown infra-setup deploy timing cpu-report bake-ami unbake-ami clear-costs

# Resolve the pre-baked worker AMI for the CURRENT image tag, if one exists.
# Empty when nothing is baked -> Batch pulls the image on boot. This is what
# makes the AMI cache opt-in and self-correcting: bake before an encode
# session, `make unbake-ami` after, and infra-plan/apply just pick up whatever
# is (or isn't) there for this image tag.
WORKER_AMI ?= $(shell aws ec2 describe-images --owners self --region $(AWS_REGION) \
	--filters "Name=tag:image_tag,Values=$(IMAGE_TAG)" "Name=state,Values=available" \
	--query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null | grep -v '^None$$' || true)

ecr-login:
	@: $${ECR_REPO:?ECR_REPO empty — run `make infra-apply` first, or set it in .env}
	aws ecr get-login-password --region $(AWS_REGION) | \
	  docker login --username AWS --password-stdin $(ECR_REGISTRY)

ecr-push: ecr-login   ## build arm64 (Graviton) + push the worker image to ECR
	# The default buildx "docker" driver already caches layers in the local
	# daemon across builds, so an incremental (script-only) build reuses the
	# heavy ffmpeg/Shaka layers. External registry cache (--cache-to) needs a
	# docker-container buildx builder; not worth it on one host, adopt only if
	# builds move to multiple machines/CI.
	docker buildx build --platform linux/arm64 \
		--build-arg VERSION=$(VERSION) --build-arg GIT_SHA=$(IMAGE_TAG) \
		--tag $(ECR_REPO):latest --tag $(ECR_REPO):$(IMAGE_TAG) --push .
	@echo "Pushed $(ECR_REPO):latest :$(IMAGE_TAG) (linux/arm64)"

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

infra-destroy:        ## tear the whole stack down to $0 (also removes the worker AMI)
	cd $(TF_DIR) && AWS_REGION=$(AWS_REGION) tofu destroy
	$(MAKE) unbake-ami   # AMI isn't tofu-managed; remove it too so nothing bills

infra-teardown: infra-destroy   ## alias for infra-destroy (mirror of infra-setup)

# Stand the whole stack up from nothing in one shot — the inverse of
# infra-destroy. Every step is a sub-make so it re-parses the Makefile and
# re-resolves ECR_REPO / WORKER_AMI from CURRENT state (the top-level values
# were empty when make started, before any of this existed). Order matters:
#   1. init + first apply       — create ECR, Batch, VPC, SFN, IAM (no AMI yet;
#                                  launch template pulls the image on boot).
#   2. ecr-push                 — push :IMAGE_TAG so the image exists to bake+run.
#   3. bake-ami                 — bake the worker AMI (needs the instance profile
#                                  from step 1 AND the image from step 2).
#   4. second plan + apply      — WORKER_AMI now resolves to the baked AMI, so
#                                  this wires it into the launch template.
# Job defs referencing a not-yet-pushed tag apply fine in step 1 — ECR isn't
# checked at create; step 2 fills it in.
infra-setup:          ## one-shot stand-up: init + apply + ecr-push + bake-ami + wire AMI
	$(MAKE) infra-init
	$(MAKE) infra-plan
	$(MAKE) infra-apply
	$(MAKE) ecr-push
	$(MAKE) bake-ami
	@echo ">>> waiting for the baked AMI to be queryable as available (EC2 is eventually consistent)..."
	@until [ -n "$$(aws ec2 describe-images --owners self --region $(AWS_REGION) \
	    --filters Name=tag:image_tag,Values=$(IMAGE_TAG) Name=state,Values=available \
	    --query 'Images[0].ImageId' --output text 2>/dev/null | grep -v '^None$$')" ]; do \
	  sleep 3; done
	$(MAKE) infra-plan
	$(MAKE) infra-apply
	@echo ">>> Stack up, image pushed, AMI baked + wired. Cold starts skip the ECR pull."
	@echo ">>> (The AMI costs ~\$$1.50/mo while it exists — 'make unbake-ami' when done.)"

# Deploy stops at the plan on purpose — review it, then run `make infra-apply`.
# (Keeping preview and apply as separate, deliberate steps for live IaC.)
deploy:               ## push image + restart + plan + APPLY infra (one shot)
	@start=$$(date +%s); \
	echo ">>> deploy started $$(date '+%H:%M:%S')  worker=$(IMAGE_TAG)"; \
	if $(MAKE) ecr-push && $(MAKE) restart && $(MAKE) infra-plan && $(MAKE) infra-apply; then \
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
	$(MAKE) ecr-push
	$(MAKE) restart
	$(MAKE) infra-plan
	@echo ">>> Review the plan above. To apply it:  make infra-apply"

timing:               ## where-did-the-time-go for an execution: make timing EXEC=<arn>
	@: $${EXEC:?set EXEC=<execution-arn>}
	docker exec $(CONTAINER_NAME) python3 -m encoder.cloud.timing --execution-arn $(EXEC)

cpu-report:           ## per-tier encode CPU utilization vs reserved vCPU: make cpu-report EXEC=<arn>
	@: $${EXEC:?set EXEC=<execution-arn>}
	docker exec $(CONTAINER_NAME) python3 -m encoder.cloud.cpu_report --execution-arn $(EXEC)

# ---- Worker-AMI cache (opt-in, one at a time) --------------------------------
# The AMI is a pre-warmed cache: a cold spot instance boots with the encoder
# image already resident, skipping the ~60s ECR pull. It's OPT-IN and costs
# ~$1.50/mo in EBS-snapshot storage while it exists, so bake it before an
# encode session and `make unbake-ami` after. Exactly one encoder-worker AMI
# is ever kept: bake prunes every other one, unbake removes them all.

bake-ami:             ## build a worker AMI with the current image pre-pulled (keeps only this one)
	@: $${ECR_REPO:?ECR_REPO empty — run `make infra-apply` first, or set it in .env}
	cd infra/packer && packer init worker-ami.pkr.hcl && \
	  packer build -var region=$(AWS_REGION) -var ecr_repo=$(ECR_REPO) \
	    -var image_tag=$(IMAGE_TAG) worker-ami.pkr.hcl
	@echo ">>> keeping only encoder-worker-$(IMAGE_TAG); removing any older worker AMIs..."
	@ids=$$(aws ec2 describe-images --owners self --region $(AWS_REGION) \
	    --filters "Name=tag:Name,Values=encoder-worker" \
	    --query "Images[?Tags[?Key=='image_tag'&&Value!='$(IMAGE_TAG)']].ImageId" --output text); \
	  for ami in $$ids; do \
	    [ "$$ami" = "None" ] && continue; \
	    snaps=$$(aws ec2 describe-images --owners self --region $(AWS_REGION) --image-ids $$ami \
	      --query 'Images[].BlockDeviceMappings[].Ebs.SnapshotId' --output text); \
	    echo "deregister $$ami"; aws ec2 deregister-image --region $(AWS_REGION) --image-id $$ami; \
	    for s in $$snaps; do echo "  delete snapshot $$s"; aws ec2 delete-snapshot --region $(AWS_REGION) --snapshot-id $$s; done; \
	  done
	@echo ">>> Baked encoder-worker-$(IMAGE_TAG) (1 AMI total). Now: make infra-apply  (wires it in)"

unbake-ami:           ## clear the compute-env AMI pointer, THEN delete the AMIs (self-clearing, no dangling ref)
	# Clear the compute env's image_id_override FIRST so we never delete an AMI
	# the env still points at — no dangling pointer, no manual follow-up apply.
	# Targeted to the compute env only (won't touch job defs). Guarded so it
	# no-ops on an already-destroyed stack (infra-destroy calls this after
	# teardown, when there's nothing left in state to apply).
	@if cd $(TF_DIR) && tofu state list 2>/dev/null | grep -q 'aws_batch_compute_environment.spot_graviton'; then \
	  echo ">>> clearing compute-env AMI pointer (-> pull-on-boot)..."; \
	  AWS_REGION=$(AWS_REGION) tofu apply -auto-approve \
	    -target=module.compute.aws_batch_compute_environment.spot_graviton \
	    -var image_tag=$(IMAGE_TAG) -var worker_ami_id="" ; \
	else echo ">>> no compute env in state — skipping pointer clear"; fi
	@ids=$$(aws ec2 describe-images --owners self --region $(AWS_REGION) \
	    --filters "Name=tag:Name,Values=encoder-worker" --query 'Images[].ImageId' --output text); \
	  if [ -z "$$ids" ] || [ "$$ids" = "None" ]; then echo "no encoder-worker AMI to remove"; else \
	  for ami in $$ids; do \
	    snaps=$$(aws ec2 describe-images --owners self --region $(AWS_REGION) --image-ids $$ami \
	      --query 'Images[].BlockDeviceMappings[].Ebs.SnapshotId' --output text); \
	    echo "deregister $$ami"; aws ec2 deregister-image --region $(AWS_REGION) --image-id $$ami; \
	    for s in $$snaps; do echo "  delete snapshot $$s"; aws ec2 delete-snapshot --region $(AWS_REGION) --snapshot-id $$s; done; \
	  done; fi
	@echo ">>> Removed. Compute env is on pull-on-boot; nothing dangling."

# ---- Cost teardown -----------------------------------------------------------
# clear-costs kills everything that bills while IDLE without destroying the
# reusable stack (compute env, queue, SFN, VPC, IAM are all ~$0 at rest —
# scale-to-zero spot, IGW/public subnets, free S3 gateway endpoint). It runs
# the same tagged sweep as the app's Emergency Clear (instances / orphan
# volumes / spot requests / S3 data tagged Application=encoder-app) and removes
# the worker AMI + snapshot. For a TOTAL teardown (also drops ECR images, log
# groups, VPC — next use needs a full re-deploy) use `make infra-destroy`.

clear-costs:          ## kill every idle AWS cost: sweep tagged instances/volumes/spot/S3 + remove worker AMI
	@echo ">>> sweeping Application=encoder-app runtime resources (instances, volumes, spot, S3)..."
	docker exec $(CONTAINER_NAME) python3 -m encoder.cloud.cleanup --sweep-all
	@echo ">>> removing worker AMI(s)..."
	$(MAKE) unbake-ami
	@echo ">>> Idle cost generators cleared (AMI pointer self-cleared to pull-on-boot)."
	@echo ">>> The Batch stack stays (it's ~\$$0 at rest). Full teardown: make infra-destroy"

# ---- Distributed-local encoding (Temporal + MinIO, no AWS) --------------------
# All-container control plane on this (master) box; workers run one-per-box and
# pull work. See infra/local-cluster/README.md.
DIST_COMPOSE = infra/local-cluster/docker-compose.yml
.PHONY: dist-up dist-down dist-worker dist-logs dist-ps

dist-up:              ## bring up the local cluster (temporal + ui + postgres + minio)
	docker compose -f $(DIST_COMPOSE) up -d
	@echo ">>> Temporal UI: http://localhost:8233   MinIO console: http://localhost:9001"

dist-down:            ## stop the local cluster (volumes persist; add ARGS=-v to wipe)
	docker compose -f $(DIST_COMPOSE) down $(ARGS)

dist-worker: build    ## run an encode worker on THIS box (uses the freshly-built image)
	ENCODER_IMAGE=$(ENCODER_IMAGE) TEMPORAL_ADDRESS=$${TEMPORAL_ADDRESS:-host.docker.internal:7233} \
	S3_ENDPOINT_URL=$${S3_ENDPOINT_URL:-http://host.docker.internal:9000} \
	AWS_ACCESS_KEY_ID=$${MINIO_ROOT_USER:-encoder} \
	AWS_SECRET_ACCESS_KEY=$${MINIO_ROOT_PASSWORD:-encoder-secret} \
	infra/local-cluster/run-worker.sh

dist-logs:            ## follow the local worker log
	docker logs -f $${WORKER_NAME:-encode-worker}

dist-ps:              ## cluster + worker containers
	docker compose -f $(DIST_COMPOSE) ps
	@docker ps --filter name=encode-worker --format 'table {{.Names}}\t{{.Status}}'

# DIST_WORKERS: space-separated label=ssh_target pairs of remote worker boxes,
# e.g. DIST_WORKERS = ubuntu=jonathanoliver@jonathanoliver-ubuntu.local
# MASTER_IP: the master box's LAN IP that workers dial for Temporal + MinIO.
DIST_WORKERS ?=
MASTER_IP ?= 192.168.0.110
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
# (no 900MB image transfer, no per-box build, any arch). Needs GHCR_PAT only if
# the package is private. Pair with `make run-remote` on the master for a fully
# no-local-build bring-up.
dist-deploy-ghcr:     ## GHCR-pull workers on each DIST_WORKERS box (no build/transfer)
	@if [ -z "$(DIST_WORKERS)" ]; then echo "set DIST_WORKERS=label=ssh_target [..] (in .env)"; exit 1; fi
	@for w in $(DIST_WORKERS); do \
	  label=$${w%%=*}; host=$${w#*=}; \
	  MASTER_IP=$(MASTER_IP) IMAGE=$(REMOTE_IMAGE) GHCR_PAT=$(GHCR_PAT) GHCR_USERNAME=$(GHCR_USERNAME) \
	    MINIO_ROOT_USER=$(MINIO_ACCESS_KEY) MINIO_ROOT_PASSWORD=$(MINIO_SECRET_KEY) \
	    bash infra/local-cluster/deploy-worker-ghcr.sh "$$host" "$$label" || exit 1; \
	done
	@echo ">>> GHCR-pull workers deployed to $(words $(DIST_WORKERS)) box(es)."

# ---- One-command farm --------------------------------------------------------
# `make farm` brings the whole distributed-local setup up from THIS machine as
# master, pulling every image from GHCR (no local build). The master always runs
# a worker; extra boxes come from DIST_WORKERS in .env. Run `make push` first so
# GHCR has your current code.
# `make farm-dev` is the developer loop: it bind-mounts your local scripts/encoder
# into every worker, so re-running it just rsyncs the diffs and restarts workers
# (no rebuild, no re-pull) — the fastest way to get local changes onto all boxes.
.PHONY: farm farm-dev

# host.docker.internal reaches the master's own cluster from its worker container.
_MASTER_WORKER_ENV = TEMPORAL_ADDRESS=host.docker.internal:7233 \
	S3_ENDPOINT_URL=http://host.docker.internal:9000 \
	AWS_ACCESS_KEY_ID=$(MINIO_ACCESS_KEY) AWS_SECRET_ACCESS_KEY=$(MINIO_SECRET_KEY) \
	WORKER_LABEL=$(LOCAL_WORKER_LABEL)

farm: require-paths   ## bring the whole farm up from GHCR (cluster + this box's worker + DIST_WORKERS + UI)
	@echo ">>> [farm] 1/5 cluster (temporal + minio)..."
	$(MAKE) dist-up
	@echo ">>> [farm] 2/5 waiting for Temporal (:7233)..."
	@for i in $$(seq 1 60); do nc -z localhost 7233 2>/dev/null && break; sleep 1; done; sleep 5
	@echo ">>> [farm] 3/5 pull $(REMOTE_IMAGE) + start a worker on THIS machine..."
	@if [ -n "$(GHCR_PAT)" ]; then echo "$(GHCR_PAT)" | docker login ghcr.io -u $(GHCR_USERNAME) --password-stdin; fi
	docker pull $(REMOTE_IMAGE)
	@ENCODER_IMAGE=$(REMOTE_IMAGE) $(_MASTER_WORKER_ENV) bash infra/local-cluster/run-worker.sh
	@echo ">>> [farm] 4/5 remote workers (from GHCR)..."
	@if [ -n "$(DIST_WORKERS)" ]; then $(MAKE) dist-deploy-ghcr; else echo "    (no DIST_WORKERS — master-only farm)"; fi
	@echo ">>> [farm] 5/5 server + UI from GHCR..."
	@$(MAKE) stop
	$(MAKE) run-remote
	@echo ">>> farm up:  UI http://localhost:$(PORT)   Temporal UI http://localhost:8233"

farm-dev: require-paths   ## dev farm from your WORKING TREE (uncommitted): local build + bind-mounted code on every box
	@echo ">>> [farm-dev] 1/5 build the image from your working tree (uncommitted Go + deps)..."
	$(MAKE) build
	@echo ">>> [farm-dev] 2/5 cluster (temporal + minio)..."
	$(MAKE) dist-up
	@for i in $$(seq 1 60); do nc -z localhost 7233 2>/dev/null && break; sleep 1; done; sleep 5
	@echo ">>> [farm-dev] 3/5 worker on THIS machine (local image + LIVE working-tree code mount)..."
	@ENCODER_IMAGE=$(IMAGE_NAME) CODE_MOUNT=$(CURDIR)/scripts/encoder $(_MASTER_WORKER_ENV) \
	  bash infra/local-cluster/run-worker.sh
	@echo ">>> [farm-dev] 4/5 sync code + image to DIST_WORKERS boxes (transfer same-arch / build cross-arch; code bind-mounted)..."
	@if [ -n "$(DIST_WORKERS)" ]; then $(MAKE) dist-deploy-workers; else echo "    (no DIST_WORKERS — master-only)"; fi
	@echo ">>> [farm-dev] 5/5 server + UI from the LOCAL build; orchestrator runs your working-tree code..."
	@$(MAKE) stop
	HOST_SCRIPTS_DIR=$(CURDIR)/scripts/encoder $(MAKE) run
	@echo ">>> farm-dev up (working tree — nothing committed/pushed). Re-run 'make farm-dev' after edits."

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
	$(MAKE) farm-dev DIST_WORKERS=
	@echo ">>> [smoke] waiting for the server (:$(PORT))..."
	@for i in $$(seq 1 30); do curl -sf http://localhost:$(PORT)/api/jobs >/dev/null 2>&1 && break; sleep 1; done
	@echo ">>> [smoke] opening the jobs page (set SMOKE_OPEN=0 to skip)..."
	@[ "$(SMOKE_OPEN)" = "0" ] || ( open http://localhost:$(PORT)/ 2>/dev/null || xdg-open http://localhost:$(PORT)/ 2>/dev/null ) || echo "    watch it at http://localhost:$(PORT)/"
	@echo ">>> [smoke] submitting encode (h264, 720p, 12s chunks) + waiting (timeout ~300s)..."
	@id=$$(curl -sf -X POST http://localhost:$(PORT)/api/encode -H 'Content-Type: application/json' \
	    -d '{"files":["smoke.mp4"],"target":"local-dist","codec":"h264","max_res":"720p","chunk_duration":"12"}' \
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
OOBE_CLUSTER_ENV = TEMPORAL_PORT=$(OOBE_TEMPORAL_PORT) TEMPORAL_UI_PORT=$(OOBE_TEMPORAL_UI_PORT) \
	MINIO_API_PORT=$(OOBE_MINIO_PORT) MINIO_CONSOLE_PORT=$(OOBE_MINIO_CONSOLE_PORT)

.PHONY: oobe oobe-down
oobe: build   ## isolated first-run test: own dirs/ports/cluster -> encode -> assert -> tear down
	@echo ">>> [oobe] fresh dirs under $(OOBE_DIR)"
	@rm -rf $(OOBE_DIR); mkdir -p $(OOBE_DIR)/source $(OOBE_DIR)/output $(OOBE_DIR)/tmp
	@echo ">>> [oobe] isolated cluster '$(OOBE_PROJECT)' on ports $(OOBE_TEMPORAL_PORT)/$(OOBE_TEMPORAL_UI_PORT)/$(OOBE_MINIO_PORT)/$(OOBE_MINIO_CONSOLE_PORT)..."
	@$(OOBE_CLUSTER_ENV) docker compose -p $(OOBE_PROJECT) -f $(DIST_COMPOSE) up -d
	@echo ">>> [oobe] waiting for the isolated Temporal (:$(OOBE_TEMPORAL_PORT))..."
	@for i in $$(seq 1 60); do nc -z localhost $(OOBE_TEMPORAL_PORT) 2>/dev/null && break; sleep 1; done; sleep 5
	@echo ">>> [oobe] generating a tiny clip in the isolated source dir..."
	@docker run --rm -v "$(OOBE_DIR)/source:/src" --entrypoint ffmpeg $(IMAGE_NAME) \
	  -f lavfi -i testsrc2=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 \
	  -t 20 -pix_fmt yuv420p -c:v libx264 -c:a aac -shortest -y /src/smoke.mp4
	@echo ">>> [oobe] isolated worker '$(OOBE_WORKER)'..."
	@ENCODER_IMAGE=$(IMAGE_NAME) WORKER_NAME=$(OOBE_WORKER) WORKER_LABEL=oobe \
	  TEMPORAL_ADDRESS=host.docker.internal:$(OOBE_TEMPORAL_PORT) \
	  S3_ENDPOINT_URL=http://host.docker.internal:$(OOBE_MINIO_PORT) \
	  AWS_ACCESS_KEY_ID=$(MINIO_ACCESS_KEY) AWS_SECRET_ACCESS_KEY=$(MINIO_SECRET_KEY) \
	  CODE_MOUNT=$(CURDIR)/scripts/encoder \
	  bash infra/local-cluster/run-worker.sh
	@echo ">>> [oobe] isolated server '$(OOBE_SERVER)' on :$(OOBE_PORT)..."
	@docker rm -f $(OOBE_SERVER) >/dev/null 2>&1 || true
	$(MAKE) run CONTAINER_NAME=$(OOBE_SERVER) PORT=$(OOBE_PORT) \
	  SOURCE_DIR=$(OOBE_DIR)/source OUTPUT_DIR=$(OOBE_DIR)/output TMP_DIR=$(OOBE_DIR)/tmp \
	  TEMPORAL_ADDRESS=host.docker.internal:$(OOBE_TEMPORAL_PORT) \
	  MINIO_ENDPOINT=http://host.docker.internal:$(OOBE_MINIO_PORT) \
	  DIST_WORKER_CONTAINER=$(OOBE_WORKER) HOST_SCRIPTS_DIR=$(CURDIR)/scripts/encoder
	@echo ">>> [oobe] waiting for the isolated server (:$(OOBE_PORT))..."
	@for i in $$(seq 1 30); do curl -sf http://localhost:$(OOBE_PORT)/api/jobs >/dev/null 2>&1 && break; sleep 1; done
	@echo ">>> [oobe] submitting encode + waiting (timeout ~300s)..."
	@id=$$(curl -sf -X POST http://localhost:$(OOBE_PORT)/api/encode -H 'Content-Type: application/json' \
	    -d '{"files":["smoke.mp4"],"target":"local-dist","codec":"h264","max_res":"720p","chunk_duration":"12"}' \
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
	-docker rm -f $(OOBE_SERVER) $(OOBE_WORKER) 2>/dev/null
	-$(OOBE_CLUSTER_ENV) docker compose -p $(OOBE_PROJECT) -f $(DIST_COMPOSE) down -v 2>/dev/null
	-rm -rf $(OOBE_DIR)
	@echo ">>> [oobe] torn down."
