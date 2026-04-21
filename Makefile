
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
