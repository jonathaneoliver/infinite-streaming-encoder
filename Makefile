
-include .env
export

IMAGE_NAME ?= encoder
CONTAINER_NAME ?= encoder
PORT ?= 8080

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
