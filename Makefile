
-include .env
export

IMAGE_NAME ?= encoder
CONTAINER_NAME ?= encoder
PORT ?= 8080

# Single source of truth: ./VERSION. Embedded into the Go binary via
# -ldflags and stamped on every image tag we publish to GHCR. The
# short git SHA is stamped too, so the About tab can tell you exactly
# which commit the local binary AND the cloud image were built from
# — critical when VERSION hasn't bumped but you've been iterating on
# cloud code.
VERSION := $(shell cat VERSION 2>/dev/null || echo dev)
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)

# GHCR publishing
GHCR_IMAGE ?= ghcr.io/jonathaneoliver/encoder
GHCR_USERNAME ?= jonathaneoliver
PLATFORMS ?= linux/amd64,linux/arm64

.PHONY: require-paths build run stop restart logs shell status clean push push-setup cloud-push version

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

run: require-paths build
	docker run --rm -d \
		--name $(CONTAINER_NAME) \
		-p $(PORT):8080 \
		-v $(SOURCE_DIR):/media/originals:ro \
		-v $(OUTPUT_DIR):/media/dynamic_content \
		-v $(TMP_DIR):/media/tmp \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-v $(HOME)/.aws:/root/.aws:ro \
		-e SOURCE_DIR=/media/originals \
		-e OUTPUT_DIR=/media/dynamic_content \
		-e TMP_DIR=/media/tmp \
		-e SCRIPTS_DIR=/app/scripts \
		-e HOST_SOURCE_DIR=$(SOURCE_DIR) \
		-e HOST_OUTPUT_DIR=$(OUTPUT_DIR) \
		-e HOST_TMP_DIR=$(TMP_DIR) \
		-e HOST_AWS_DIR=$(HOME)/.aws \
		-e ENCODER_IMAGE=$(IMAGE_NAME) \
		-e AUTO_WATCH=$(AUTO_WATCH) \
		-e DEFAULT_TARGET=$(DEFAULT_TARGET) \
		-e DEFAULT_CODEC=$(DEFAULT_CODEC) \
		-e DEFAULT_MAX_RES=$(DEFAULT_MAX_RES) \
		-e MAX_CONCURRENT=$(MAX_CONCURRENT) \
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
		$(IMAGE_NAME)
	@echo "Encoder running at http://localhost:$(PORT)"

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
IMAGE_TAG := $(shell git log -1 --format=%h -- Dockerfile go.mod go.sum requirements.txt cmd internal scripts static 2>/dev/null || echo $(GIT_SHA))

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
deploy:               ## push image + restart + plan infra (then review & infra-apply)
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
