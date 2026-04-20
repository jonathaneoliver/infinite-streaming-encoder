
-include .env
export

IMAGE_NAME ?= encoder
CONTAINER_NAME ?= encoder
PORT ?= 8080

# GHCR publishing
GHCR_IMAGE ?= ghcr.io/jonathaneoliver/encoder
GHCR_USERNAME ?= jonathaneoliver
PLATFORMS ?= linux/amd64,linux/arm64
TAG ?= latest

require-paths:
	@: $${SOURCE_DIR:?SOURCE_DIR is not set — create a .env (see .env.example)}
	@: $${OUTPUT_DIR:?OUTPUT_DIR is not set — create a .env (see .env.example)}
	@: $${TMP_DIR:?TMP_DIR is not set — create a .env (see .env.example)}

build:
	docker build -t $(IMAGE_NAME) .

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

# Build + push the multi-arch image. Override TAG to publish alternate
# tags, e.g. `make push TAG=v1.0.0` or `make push TAG=$(git rev-parse --short HEAD)`.
push:
	docker buildx build \
		--platform $(PLATFORMS) \
		--tag $(GHCR_IMAGE):$(TAG) \
		--push \
		.
	@echo "Published $(GHCR_IMAGE):$(TAG) for $(PLATFORMS)"
