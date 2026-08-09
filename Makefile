
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

# GHCR publishing. Both come from .env and have NO defaults on purpose: a
# personal namespace baked in here silently misdirects every fork — `docker
# login` succeeds with the forker's own PAT and the push then fails on a
# namespace they can't write, which reads like a credentials problem rather
# than a setting they were meant to change (#149).
#
# GHCR_ORG is the registry+namespace (e.g. ghcr.io/yourname). GHCR_USERNAME is
# the GitHub account used for `docker login`. They are deliberately separate:
# they coincide for a personal namespace but differ for an org, so deriving one
# from the other would reintroduce exactly the silent misconfiguration above.
GHCR_ORG ?=
GHCR_USERNAME ?=
GHCR_IMAGE ?= $(GHCR_ORG)/infinite-streaming-encoder
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
# Gate on the RESOLVED IP, not on PROMOTE_SSH_HOST. The overlay interpolates
# "$$(PROMOTE_SSH_HOST):$$(PROMOTE_SSH_IP)" into extra_hosts, and PROMOTE_SSH_IP
# comes from an mDNS lookup that returns nothing when the promote box is asleep
# or off. Gating on the host name then layers an overlay that renders
# "somebox.local:" — which docker rejects with `invalid IP address in add-host`,
# taking down the WHOLE farm because an optional rsync destination is offline.
# The failure only appears when a container is actually recreated (a changed
# image tag), so it hides for weeks and then blocks a deploy.
#
# If the box is unreachable, promote-over-SSH cannot work anyway; bringing the
# farm up without it is strictly better than not bringing it up at all.
ifneq ($(strip $(PROMOTE_SSH_IP)),)
COMPOSE_PROMOTE += -f docker-compose.promote-ssh.yml
else ifneq ($(strip $(PROMOTE_SSH_HOST)),)
$(warning PROMOTE_SSH_HOST=$(PROMOTE_SSH_HOST) did not resolve via mDNS — SSH promote is DISABLED for this run. Wake the box, or set PROMOTE_SSH_IP in .env.)
endif
COMPOSE_BASE := $(COMPOSE) -p $(COMPOSE_PROJECT) -f docker-compose.yml $(COMPOSE_PROMOTE)
COMPOSE_DEV  := $(COMPOSE_BASE) -f docker-compose.dev.yml
# Mac master only: size worker concurrency from HOST performance cores (the
# Docker VM hides P/E cores, so in-container detection over-counts). Empty on
# Linux / non-Mac -> the worker detects physical cores in-container. Passed to
# compose as ENCODE_SLOTS (empty string is safe: compose ${ENCODE_SLOTS:-0}).
FARM_ENCODE_SLOTS := $(shell P=$$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null); if [ -n "$$P" ] && [ "$$P" -gt 1 ]; then echo $$((P/2)); fi)

.PHONY: require-paths require-ghcr require-s3-bucket require-idle build run run-remote down stop restart logs shell status clean publish version setup-hooks

# Point git at the committed hooks (scripts/git-hooks/) so the pre-push guard
# that blocks direct pushes to main is active in this clone. Run once per clone.
setup-hooks:
	git config core.hooksPath scripts/git-hooks
	@echo "git hooks active (scripts/git-hooks). Direct pushes to main are now blocked — use a PR."
	@echo "'make check' now also runs on every push."

.PHONY: check
check:                ## run the same static checks CI runs (gofmt/vet/build/test, staticcheck, govulncheck, tofu fmt, py compile, page JS)
	@fail=0; \
	: 'go install drops binaries in $$(go env GOPATH)/bin, which is not on PATH'; \
	: 'by default on macOS. Without this, staticcheck/govulncheck report'; \
	: '"skipped (go install ...)" to someone who has already installed them —'; \
	: 'a gate that lies about being absent is worse than one that is.'; \
	PATH="$$PATH:$$(go env GOPATH)/bin"; export PATH; \
	printf '  gofmt          '; \
	unformatted=$$(gofmt -l . 2>/dev/null); \
	if [ -n "$$unformatted" ]; then \
	  echo "FAIL"; echo "$$unformatted" | sed 's/^/                 /'; \
	  echo "                 fix: gofmt -w ."; fail=1; \
	else echo "ok"; fi; \
	printf '  go vet         '; \
	if out=$$(go vet ./... 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  go build       '; \
	if out=$$(go build ./... 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  tofu fmt       '; \
	if ! command -v tofu >/dev/null 2>&1; then echo "skipped (tofu not installed)"; \
	elif out=$$(tofu -chdir=infra/terraform fmt -check -recursive 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; \
	  echo "                 fix: tofu -chdir=infra/terraform fmt -recursive"; fail=1; fi; \
	printf '  sfn scopes     '; \
	if out=$$(python3 scripts/check_sfn_scopes.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  go test        '; \
	if out=$$(go test -race ./... 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  staticcheck    '; \
	if ! command -v staticcheck >/dev/null 2>&1; then echo "skipped (go install honnef.co/go/tools/cmd/staticcheck@latest)"; \
	elif out=$$(staticcheck -checks all ./... 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  govulncheck    '; \
	if ! command -v govulncheck >/dev/null 2>&1; then echo "skipped (go install golang.org/x/vuln/cmd/govulncheck@latest)"; \
	elif out=$$(govulncheck ./... 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py pyflakes    '; \
	if ! command -v ruff >/dev/null 2>&1; then echo "skipped (pip install ruff)"; \
	elif out=$$(ruff check --select F scripts/ 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py imports     '; \
	if out=$$(cd scripts && python3 -c 'import infinite_streaming_encoder.cli_batch, infinite_streaming_encoder.cli_local, infinite_streaming_encoder.cli_phase, infinite_streaming_encoder.telemetry, infinite_streaming_encoder.progress' 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py telemetry   '; \
	if out=$$(python3 scripts/test_telemetry.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py stagestate  '; \
	if out=$$(python3 scripts/test_stage_state.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py hostmezz    '; \
	if out=$$(python3 scripts/test_host_mezzanine.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py hostpkg     '; \
	if out=$$(python3 scripts/test_host_package.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py deferpkg    '; \
	if out=$$(python3 scripts/test_deferred_packaging.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py fleetver    '; \
	if out=$$(python3 scripts/test_fleet_version_marker.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py machinerent '; \
	if out=$$(python3 scripts/test_machine_rental.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py threads     '; \
	if out=$$(python3 scripts/test_encode_threads.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py diststate   '; \
	if out=$$(python3 scripts/test_dist_stage_state.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py mezzcache   '; \
	if out=$$(python3 scripts/test_mezz_cache.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py timelimit   '; \
	if out=$$(python3 scripts/test_time_limit.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py pkgfetch    '; \
	if out=$$(python3 scripts/test_package_fetch.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py outfetch    '; \
	if out=$$(python3 scripts/test_output_fetch.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  py cli         '; \
	if out=$$(python3 scripts/test_encoder_cli.py 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  python compile '; \
	if out=$$(cd scripts && python3 -m compileall -q infinite_streaming_encoder 2>&1); then echo "ok"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	printf '  page JS       '; \
	if out=$$(python3 scripts/check_page_js.py 2>&1); then echo "$$out"; \
	else echo "FAIL"; echo "$$out" | sed 's/^/                 /'; fail=1; fi; \
	if [ $$fail -ne 0 ]; then echo; echo "make check: FAILED"; exit 1; fi; \
	echo "make check: all passed"

require-paths:
	@: $${SOURCE_DIR:?SOURCE_DIR is not set — create a .env (see .env.example)}
	@: $${OUTPUT_DIR:?OUTPUT_DIR is not set — create a .env (see .env.example)}
	@: $${TMP_DIR:?TMP_DIR is not set — create a .env (see .env.example)}

# Guards for values that CANNOT ship with a working default because they name
# resources in an account only their owner controls (#149). Fail naming the
# setting, rather than proceeding against somebody else's namespace/bucket.
# Refuse to disturb work in flight. Both halves matter: infra-apply deregisters
# job-def revisions, which FAILS a running Step Functions execution mid-encode,
# and farm-up/restart bounce workers and the server out from under a local one.
# cloud-batch reattaches to its execution after a server bounce, so a local
# encode is the more fragile of the two.
#
# Degrades open, deliberately: no AWS creds or no server running means nothing to
# protect, and a guard that blocks when it cannot see is worse than no guard.
require-idle:
	@running=$$(aws stepfunctions list-executions --region $(AWS_REGION) \
	    --state-machine-arn "$(STATE_MACHINE_ARN)" --status-filter RUNNING \
	    --query 'length(executions)' --output text 2>/dev/null || echo 0); \
	  if [ "$$running" != "0" ] && [ -n "$$running" ] && [ "$$running" != "None" ]; then \
	    echo "!!! $$running cloud execution(s) RUNNING — applying now would deregister"; \
	    echo "    their job definitions and fail them. Wait for them to finish."; \
	    exit 1; \
	  fi
	@n=$$(curl -fsS --max-time 3 http://localhost:$(PORT)/api/jobs 2>/dev/null \
	    | python3 -c "import json,sys; print(sum(1 for j in json.load(sys.stdin) if j.get('status')=='running'))" 2>/dev/null || echo 0); \
	  if [ "$$n" != "0" ]; then \
	    echo "!!! $$n local encode(s) RUNNING — this bounces the server and workers"; \
	    echo "    out from under them. Wait, or cancel them in the UI."; \
	    exit 1; \
	  fi

require-ghcr:
	@: $${GHCR_ORG:?GHCR_ORG is not set — your GHCR namespace, e.g. ghcr.io/yourname (see .env.example)}
	@: $${GHCR_USERNAME:?GHCR_USERNAME is not set — the GitHub account to docker-login with (see .env.example)}

require-s3-bucket:
	@: $${S3_BUCKET:?S3_BUCKET is not set — the job-I/O bucket is a prerequisite you create \
	yourself (S3 names are globally unique). Set it in .env (see .env.example)}

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
	ENCODER_IMAGE=$(RUN_IMAGE) DOCKER_IMAGE=$(DOCKER_IMAGE) $(COMPOSE_BASE) up -d --build --no-deps server
	@echo "Encoder running at http://localhost:$(PORT)"

# Fire up the server from the published GHCR image instead of a local build.
# Logs into GHCR first only if GHCR_PAT is set (needed when the package is
# private). Pairs with `make farm-up` for a fully no-local-build bring-up.
run-remote: RUN_IMAGE = $(REMOTE_IMAGE)
run-remote: require-paths require-ghcr
	@if [ -n "$$GHCR_PAT" ]; then \
		echo "$$GHCR_PAT" | docker login ghcr.io -u $(GHCR_USERNAME) --password-stdin; \
	fi
	ENCODER_IMAGE=$(REMOTE_IMAGE) $(COMPOSE_BASE) pull server
	ENCODER_IMAGE=$(REMOTE_IMAGE) DOCKER_IMAGE=$(DOCKER_IMAGE) $(COMPOSE_BASE) up -d --no-build --no-deps server
	@echo "Encoder running at http://localhost:$(PORT)"

# Bring the whole master stack down (cluster + server + worker). ARGS=-v wipes
# the Temporal/MinIO volumes.
down:
	$(COMPOSE_BASE) --profile master down $(ARGS)

# Stop just the server (leaves the cluster + worker running). `restart` bounces
# it via `run` so a fresh image is picked up.
stop:
	$(COMPOSE_BASE) stop server 2>/dev/null || docker stop $(CONTAINER_NAME) 2>/dev/null || true

# Guarded, because bouncing the server mid-encode LOSES that job's progress
# state. The work itself survives — cloud chunks keep running in Batch, local
# ones in their detached containers — but the server rebuilds a cloud job's
# stages by replaying the execution, and the replay does not recover everything:
# a job observed at 336/336 came back as 159/336 and stayed there. No compute is
# wasted and nothing is re-encoded; the run's progress display is simply wrong
# from then on.
#
# FORCE=1 skips the check for when you know the job is expendable.
restart:
	@if [ -z "$(FORCE)" ]; then $(MAKE) --no-print-directory require-idle || 	  { echo "    (set FORCE=1 to restart anyway — the running job's progress display will be lost)"; exit 1; }; fi
	@$(MAKE) --no-print-directory stop run

logs:
	docker logs -f $(CONTAINER_NAME)

shell:
	docker exec -it $(CONTAINER_NAME) /bin/sh

status:               ## what is deployed where, vs what this tree says it should be
	@docker ps --filter name=$(CONTAINER_NAME) --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || echo "not running"
	@bash scripts/status.sh

ffmpeg-cmds:          ## the exact ffmpeg argv from every encode path, diffed by rung
	@python3 scripts/ffmpeg_cmds.py $(ARGS)

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
publish: require-ghcr   ## build once (multi-arch) → GHCR always, ECR when cloud is configured
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

publish-tag: require-ghcr ## push a build under ONE explicit tag, leaving :latest alone (TAG=<name>, SKIP_ECR=1, ALSO_TAG=<alias> GHCR-only)
	@# Checks the MAKE-level $(TAG) — the value the rest of this recipe actually
	@# uses — not the shell's $$TAG. They are not the same variable here, and the
	@# difference is not theoretical: `farm-test-up: TAG ?= $$(DEV_TAG)` is a
	@# target-specific assignment, and GNU make 3.81 (what Xcode ships, and so
	@# what `make` is on a stock Mac) stops exporting a variable to EVERY recipe
	@# shell once any target-specific assignment for it exists — even one on an
	@# unrelated target, even when TAG came from the command line. The blanket
	@# `export` at the top of this file does not save it. So the old
	@# `$$(TAG:?...)` guard rejected a TAG that was correctly set, and
	@# `make farm-test-up` could not run at all. Do not "simplify" this back.
	@[ -n '$(TAG)' ] || { \
	  echo "!!! TAG is not set — e.g. TAG=test-145. Use a name that cannot be mistaken for a release" >&2; \
	  exit 1; }
	@: $${GHCR_PAT:?GHCR_PAT is not set — create a classic PAT with write:packages scope}
	@# Validate here rather than at each caller: this is the single choke point
	@# for every tagged push (GHCR + ECR), so it also catches a hand-passed TAG.
	@# An invalid tag otherwise fails deep inside buildx, after the build.
	@for t in '$(TAG)' $(if $(ALSO_TAG),'$(ALSO_TAG)'); do \
	  printf '%s' "$$t" | grep -qE '^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$$' || { \
	    echo "!!! TAG='$$t' is not a valid Docker tag."; \
	    echo "    Must match [A-Za-z0-9_][A-Za-z0-9._-]{0,127} — no '/', no leading '.' or '-'."; \
	    exit 1; }; \
	done
	@# Testing lane. `publish` moves :latest, which is public AND what remote
	@# workers pull via REMOTE_IMAGE — so publishing an unvalidated build there
	@# hands it to every consumer at once. This pushes exactly one tag and
	@# touches nothing else, so a cloud test can point at it (infra-apply with
	@# -var image_tag=$$TAG) while :latest keeps serving the known-good image.
	@# See #144: the durable fix is a :stable pointer; this is the safe lane
	@# that makes testing possible before that lands.
	@echo "$$GHCR_PAT" | docker login ghcr.io -u $(GHCR_USERNAME) --password-stdin
	@docker buildx inspect encoder-builder >/dev/null 2>&1 || \
		docker buildx create --name encoder-builder --driver docker-container >/dev/null
	@set -e; ecr_tags=""; ecr_note=""; \
	if [ -n "$(SKIP_ECR)" ]; then \
	  echo ">>> SKIP_ECR — GHCR only (ECR untouched)"; \
	  ecr_note=" (ECR skipped)"; \
	elif echo "$(ECR_REPO)" | grep -qE '\.dkr\.ecr\.'; then \
	  echo ">>> cloud configured — pushing ECR ($(ECR_REPO)):$(TAG) too, so a Batch job def can point at it"; \
	  aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $(ECR_REGISTRY); \
	  ecr_tags="--tag $(ECR_REPO):$(TAG)"; \
	  ecr_note=" + ECR ($(ECR_REPO)):$(TAG)"; \
	else \
	  echo "!!! ECR_REPO did not resolve — pushing GHCR ONLY, ECR is NOT updated."; \
	  echo "    ECR_REPO comes from \`tofu output ecr_repo_url\`, which fails when"; \
	  echo "    terraform is not initialised in THIS checkout. Fix:  make infra-init"; \
	  echo "    Cloud workers keep running whatever tag the Batch job defs pin."; \
	  ecr_note=" (ECR NOT UPDATED — ECR_REPO unresolved)"; \
	fi; \
	docker buildx build --builder encoder-builder --platform $(PLATFORMS) \
		--build-arg VERSION=$(VERSION) --build-arg GIT_SHA=$(GIT_SHA) --build-arg IMAGE_TAG=$(TAG) \
		--tag $(GHCR_IMAGE):$(TAG) $(if $(ALSO_TAG),--tag $(GHCR_IMAGE):$(ALSO_TAG)) $$ecr_tags --push . ; \
	printf '%s' '$(TAG)' > $(LAST_TAG_FILE); \
	echo "Published GHCR ($(GHCR_IMAGE)):$(TAG)$(if $(ALSO_TAG), + alias :$(ALSO_TAG) [GHCR only])$$ecr_note [$(PLATFORMS)] — :latest UNCHANGED"; \
	echo "   cloud test:  cd ~/Projects/Encoder \\"; \
	echo "                  && make infra-plan IMAGE_TAG=$(TAG) && make infra-apply"

# ---------------------------------------------------------------------------
# Cloud-batch deploy (AWS Batch + Step Functions). Replaces the manual
# "build → ECR push → tofu plan/apply → restart" dance we were doing by hand.
# AWS_REGION / S3_BUCKET / STATE_MACHINE_ARN come from .env like everything else.
# ---------------------------------------------------------------------------
AWS_REGION ?= us-west-2
TF_DIR := infra/terraform

# Terraform state backend (S3 + DynamoDB lock). TFSTATE_BUCKET has NO default on
# purpose: S3 bucket names are globally unique across all of AWS, so a name baked
# into this repo could only ever work for one account — everyone else would get a
# bucket they don't own. Same reasoning as S3_BUCKET, and it lives in .env for the
# same reason. The key and table names are namespaced under the bucket, so those
# CAN have defaults. Bootstrap instructions: infra/terraform/README.md.
TFSTATE_BUCKET ?=
TFSTATE_KEY    ?= encoder/batch.tfstate
TFSTATE_TABLE  ?= terraform-lock

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
#
# Two guards against untagged images, which are common here: pushing a tag
# untags its predecessor rather than deleting it, so the NEWEST image in the
# repo is frequently untagged.
#   [?imageTags]  - consider only images that still have a tag. Without it the
#                   query returns the newest image whatever its state, and an
#                   untagged one yields nothing usable.
#   ^None$        - `--output text` prints the literal string None for an
#                   image with no tags, which would otherwise pass through as
#                   a tag name. DOCKER_IMAGE then became <repo>:None — a 404
#                   in the About tab and an unpullable ref for the legacy
#                   single-instance cloud target.
ECR_PUSHED_TAG := $(shell aws ecr describe-images --repository-name infinite-streaming-encoder-worker \
	--region $(AWS_REGION) --query 'reverse(sort_by(imageDetails[?imageTags],&imagePushedAt))[0].imageTags' \
	--output text 2>/dev/null | tr '\t' '\n' | grep -v '^latest$$' | grep -v '^None$$' | head -1)

# Image the LEGACY (single-instance) cloud target pulls on the remote — the
# same ECR image the Batch target runs (PAT-free, apples-to-apples). Defaults
# to the last-pushed sha so it's always pullable; falls back to IMAGE_TAG only
# if ECR can't be reached. Override in .env to pin a specific tag.
DOCKER_IMAGE ?= $(ECR_REPO):$(if $(ECR_PUSHED_TAG),$(ECR_PUSHED_TAG),$(IMAGE_TAG))

# NOT exported, deliberately — and every recipe that needs it passes it
# explicitly on the compose command line instead.
#
# The Makefile `export`s everything, and `?=` keeps a value already present in
# the ENVIRONMENT. So a parent make that resolved DOCKER_IMAGE before publishing
# handed the stale tag to every sub-make, which then kept it rather than
# re-querying ECR. `make deploy` does exactly that: it parses (resolving
# ECR_PUSHED_TAG against ECR as it was), THEN publishes a new image, THEN calls
# $(MAKE) farm-up — which inherited the pre-publish tag and started the server
# with it. The About tab then reported "drifted — cloud is <old sha>" straight
# after a clean deploy, which is worse than showing nothing.
#
# ECR_PUSHED_TAG itself is fine: `:=` overrides the environment, so a sub-make
# re-evaluates it. Only this `?=` needed protecting. Unexporting keeps the .env
# override working — that arrives via `-include .env` as a make variable, which
# `?=` still respects.
unexport DOCKER_IMAGE

# Step Functions ARN — auto-resolved from Terraform state (like ECR_REPO) so a
# fresh `cloud-up` needs nothing hand-copied into .env. A value in .env wins (?=).
STATE_MACHINE_ARN ?= $(shell cd $(TF_DIR) && tofu output -no-color -raw state_machine_arn 2>/dev/null)

# USE_AMI=1 pre-bakes the worker AMI during `cloud-up` for faster cold starts
# (~$1.50/mo until cloud-clear/cloud-down). Default off: cold ECR pull (~60s).
USE_AMI ?=

.PHONY: ladder-audit ladder-audit-all
.PHONY: publish-tag promote cloud-dev-up cloud-dev-down cloud-promote
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

infra-init:           ## tofu init against the S3 backend (needs TFSTATE_BUCKET in .env)
	@: $${TFSTATE_BUCKET:?TFSTATE_BUCKET is not set — create a state bucket you own \
	(S3 names are globally unique) and set it in .env. See infra/terraform/README.md}
	cd $(TF_DIR) && tofu init \
		-backend-config="bucket=$(TFSTATE_BUCKET)" \
		-backend-config="key=$(TFSTATE_KEY)" \
		-backend-config="region=$(AWS_REGION)" \
		-backend-config="dynamodb_table=$(TFSTATE_TABLE)"

infra-plan: require-s3-bucket ## tofu plan -> tf.plan (review before infra-apply)
	@echo ">>> image_tag=$(IMAGE_TAG)  worker_ami_id=$(if $(WORKER_AMI),$(WORKER_AMI),<none, pull-on-boot>)"
	cd $(TF_DIR) && AWS_REGION=$(AWS_REGION) tofu plan \
		-var s3_bucket=$(S3_BUCKET) \
		-var image_tag=$(IMAGE_TAG) \
		-var worker_ami_id="$(WORKER_AMI)" \
		-out=tf.plan

infra-apply:          ## apply the saved tf.plan (run only after reviewing the plan)
	cd $(TF_DIR) && AWS_REGION=$(AWS_REGION) tofu apply tf.plan

# Dev counterpart to cloud-up, named for the same reason farm-dev-up is: the
# -dev- variant runs YOUR WORKING TREE instead of the published image.
#
# cloud-up publishes to :latest — public, and what remote workers pull — so it
# cannot be used to try something out. This publishes under a throwaway tag and
# points ONLY the Batch job definitions at it, leaving :latest serving the
# known-good image throughout. Rolling back is re-applying the previous tag;
# nothing was overwritten, so there is nothing to restore.
# Throwaway tag for the testing lanes (cloud-dev-up, farm-test-up). Derived from
# branch + sha so it is self-describing and cannot be mistaken for a release, and
# shared so a farm test and a cloud test of the same tree name the same image.
# Non-alphanumerics become '-' rather than being deleted, so `feat/x` reads as
# `feat-x` instead of `featx`. (sed uses | as its delimiter, not the usual /,
# because make treats an unescaped # as a comment even inside $(shell ...).)
# Branch slug: lowercase, every non-alphanumeric becomes '-', runs of '-'
# collapsed, leading/trailing '-' trimmed (before AND after the length cut, so a
# truncation landing on a separator can't leave one dangling). Empty (detached
# HEAD, no git) falls back to 'nobranch' so the tag never contains an empty
# segment. Result feeds a Docker tag, which must match
# [A-Za-z0-9_][A-Za-z0-9._-]{0,127} — publish-tag enforces that on every tag.
DEV_BRANCH := $(shell git rev-parse --abbrev-ref HEAD 2>/dev/null \
	| tr 'A-Z' 'a-z' \
	| sed -e 's|[^a-z0-9]|-|g' -e 's|--*|-|g' -e 's|^-||' -e 's|-$$||' \
	| cut -c1-20 | sed 's|-$$||')
# The sha names the last COMMIT, but these lanes build the WORKING TREE — so a
# tag can otherwise claim a commit whose contents it doesn't carry, and two
# different trees at the same HEAD collide on one tag (farm-test-up always
# republishes, so the second silently overwrites the first). The -dirty suffix
# keeps the tag honest without forcing a commit. Ignored files don't count;
# `git status --porcelain` lists tracked edits + untracked-but-not-ignored.
DEV_DIRTY := $(shell git status --porcelain 2>/dev/null | grep -q . && echo dirty)
DEV_TAG ?= dev-$(if $(DEV_BRANCH),$(DEV_BRANCH),nobranch)-$(GIT_SHA)$(if $(DEV_DIRTY),-dirty)

# Moving alias for the branch's most recent test build, so a second box can pull
# without being handed a sha. Costs no storage: an extra tag on the SAME manifest
# is not an extra image, and registries expire by image. GHCR ONLY, deliberately
# (see ALSO_TAG in publish-tag) — a moving tag in ECR would let an already-
# registered Batch job definition change what it runs with no infra-apply,
# collapsing publish and deploy back into one act (#144).
DEV_TEST_TAG ?= dev-$(if $(DEV_BRANCH),$(DEV_BRANCH),nobranch)-test

# What publish-tag last pushed, so `make promote` needs no argument. DEV_TAG is
# NOT a usable default: you test dirty, then COMMIT, which changes the sha and
# drops -dirty — so by promote time DEV_TAG names a tag that was never pushed.
LAST_TAG_FILE := .last-published-tag
CLOUD_DEV_TAG ?= $(DEV_TAG)

cloud-dev-up: require-idle ## test cloud from your WORKING TREE under a throwaway tag (:latest untouched)
	@# Guard 1: tofu state. State is shared in S3, so any checkout can drive it —
	@# but only once THIS one has been `infra-init`ed (.terraform/ is per-checkout
	@# and gitignored). Uninitialised, an apply would work from an EMPTY state and
	@# try to CREATE infrastructure that already exists — worse than failing
	@# outright. `state list` proves initialised AND non-empty in one call.
	@n=$$(cd $(TF_DIR) && tofu state list 2>/dev/null | wc -l | tr -d ' '); \
	  if [ "$$n" = "0" ]; then \
	    echo "!!! tofu state is empty or the backend is not initialised in this checkout."; \
	    echo "    Run: make infra-init   (needs TFSTATE_BUCKET in .env)"; \
	    exit 1; \
	  fi
	@# Rebuild the SERVER from the working tree before publishing anything. The Go
	@# control plane builds the Step Functions input, so a stale server can emit
	@# input the freshly-applied state machine rejects — and because the variant
	@# fields are non-omitempty on purpose, that FAILS the execution rather than
	@# degrading. `restart` and not `farm-dev-up`: only the server matters here,
	@# and rebuilding remote workers on every cloud test would be minutes of rsync
	@# and cross-arch builds for nothing.
	@#
	@# A recipe step, NOT a prerequisite: prerequisites run before the recipe, so
	@# it would bounce the server BEFORE the guards above could abort on an
	@# in-flight execution.
	@#
	@# NOTE this drops the dev bind-mounts if you were mid-`farm-dev-up` — `run`
	@# uses the base compose file, so the server ends up on its baked code. Re-run
	@# `make farm-dev-up` to get the live mount back.
	$(MAKE) restart

	@# Capture what cloud runs NOW, so the rollback command below is exact.
	@prev=$$(aws batch describe-job-definitions --region $(AWS_REGION) --status ACTIVE \
	    --output json 2>/dev/null | python3 -c "import json,sys; ds=json.load(sys.stdin)['jobDefinitions']; print(sorted(ds,key=lambda x:-x['revision'])[0]['containerProperties']['image'].rsplit(':',1)[-1])" 2>/dev/null || echo unknown); \
	  echo ">>> [cloud-dev-up] cloud currently runs :$$prev"; \
	  echo "$$prev" > $(TF_DIR)/.cloud-dev-prev-tag
	@echo ">>> [cloud-dev-up] publishing working tree as :$(CLOUD_DEV_TAG) (:latest NOT touched)"
	$(MAKE) publish-tag TAG=$(CLOUD_DEV_TAG)
	@echo ">>> [cloud-dev-up] pointing Batch job definitions at :$(CLOUD_DEV_TAG)"
	$(MAKE) infra-plan IMAGE_TAG=$(CLOUD_DEV_TAG)
	$(MAKE) infra-apply
	@prev=$$(cat $(TF_DIR)/.cloud-dev-prev-tag 2>/dev/null || echo '<previous>'); \
	  printf '\n\033[1;32m>>> cloud now runs :%s (working tree)\033[0m\n' "$(CLOUD_DEV_TAG)"; \
	  echo "    :latest is UNCHANGED — users and remote workers are unaffected."; \
	  echo ""; \
	  echo "    now:       submit a cloud encode in the UI and verify it"; \
	  echo "    rollback:  make cloud-dev-down"; \
	  echo "    promote:   make cloud-promote FROM=$(CLOUD_DEV_TAG)   (after it passes)"

cloud-dev-down:       ## put cloud back on the image tag it ran before cloud-dev-up
	@prev=$$(cat $(TF_DIR)/.cloud-dev-prev-tag 2>/dev/null); \
	  test -n "$$prev" || { echo "!!! no recorded previous tag ($(TF_DIR)/.cloud-dev-prev-tag)"; \
	    echo "    pass one explicitly: make infra-plan IMAGE_TAG=<tag> && make infra-apply"; exit 1; }; \
	  echo ">>> [cloud-dev-down] restoring cloud to :$$prev"; \
	  $(MAKE) infra-plan IMAGE_TAG=$$prev && $(MAKE) infra-apply

# Make a TESTED image the released one. RE-TAG, never rebuild: rebuilding
# produces a different image from the one that passed, which defeats the point
# of having tested it (#144).
#
# FROM defaults to whatever publish-tag last pushed. Mirrors `publish`'s tag
# sets exactly, so a promoted image is indistinguishable from a published one —
# GHCR carries :latest :$(VERSION) :<sha> :<IMAGE_TAG>, ECR only :latest and
# :<IMAGE_TAG> (the tag Terraform pins by; VERSION/sha are for humans reading
# the public package).
#
# Refuses to half-promote. publish-tag skips ECR when cloud is unconfigured or
# SKIP_ECR=1 (farm-test-up), so FROM may exist on GHCR alone — moving GHCR
# :latest while cloud stayed pinned to something older is the worst outcome, so
# that case stops and asks for GHCR_ONLY=1.
promote: require-ghcr ## release a TESTED tag: re-tag it (never rebuild) on GHCR + ECR (FROM=<tag>, default: last publish-tag)
	@# dg() must distinguish "missing" from "empty": `imagetools inspect` on an
	@# absent tag prints nothing, and shasum of empty input is a perfectly valid
	@# hash (e3b0c442...), so hashing first silently turns "not found" into a
	@# digest that simply never matches. Test the raw manifest, then hash it.
	@dg() { r=$$(docker buildx imagetools inspect "$$1" --raw 2>/dev/null); \
	        [ -n "$$r" ] || return 1; printf '%s' "$$r" | shasum -a 256 | cut -c1-16; }; \
	  from="$(FROM)"; src="explicit FROM="; \
	  if [ -z "$$from" ] && [ -f $(LAST_TAG_FILE) ]; then from=$$(cat $(LAST_TAG_FILE)); src="$(LAST_TAG_FILE)"; fi; \
	  if [ -z "$$from" ]; then \
	    echo "!!! FROM is not set and $(LAST_TAG_FILE) is missing."; \
	    echo "    Pass FROM=<tag>, or publish one first (make publish-tag / farm-test-up / cloud-dev-up)."; \
	    exit 1; fi; \
	  echo ">>> promoting FROM=$$from   (source: $$src)"; \
	  echo "$$GHCR_PAT" | docker login ghcr.io -u $(GHCR_USERNAME) --password-stdin >/dev/null; \
	  gsrc=$$(dg $(GHCR_IMAGE):$$from) || { echo "!!! $(GHCR_IMAGE):$$from not found in GHCR"; exit 1; }; \
	  gold=$$(dg $(GHCR_IMAGE):latest || echo "<none>"); \
	  echo "    GHCR :latest  $$gold -> $$gsrc"; \
	  do_ecr=""; esrc=""; \
	  if [ -z "$(GHCR_ONLY)" ] && echo "$(ECR_REPO)" | grep -qE '\.dkr\.ecr\.'; then \
	    aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $(ECR_REGISTRY) >/dev/null; \
	    esrc=$$(dg $(ECR_REPO):$$from) || { \
	      echo "!!! cloud is configured but $(ECR_REPO):$$from does NOT exist."; \
	      echo "    That build never reached ECR — farm-test-up and SKIP_ECR=1 publish GHCR only."; \
	      echo "    Validate it for cloud with 'make cloud-dev-up', or promote GHCR alone:"; \
	      echo "      make promote FROM=$$from GHCR_ONLY=1"; \
	      exit 1; }; \
	    eold=$$(dg $(ECR_REPO):latest || echo "<none>"); \
	    echo "    ECR  :latest  $$eold -> $$esrc"; \
	    do_ecr=1; \
	  else echo "    ECR  skipped$(if $(GHCR_ONLY), (GHCR_ONLY=1), (cloud not configured))"; fi; \
	  docker buildx imagetools create \
	    --tag $(GHCR_IMAGE):latest --tag $(GHCR_IMAGE):$(VERSION) \
	    --tag $(GHCR_IMAGE):$(GIT_SHA) --tag $(GHCR_IMAGE):$(IMAGE_TAG) \
	    $(GHCR_IMAGE):$$from; \
	  if [ -n "$$do_ecr" ]; then \
	    docker buildx imagetools create \
	      --tag $(ECR_REPO):latest --tag $(ECR_REPO):$(IMAGE_TAG) $(ECR_REPO):$$from; \
	  fi; \
	  fail=0; \
	  for t in latest $(VERSION) $(GIT_SHA) $(IMAGE_TAG); do \
	    d=$$(dg $(GHCR_IMAGE):$$t || echo "<missing>"); \
	    [ "$$d" = "$$gsrc" ] || { echo "!!! GHCR :$$t = $$d, expected $$gsrc"; fail=1; }; \
	  done; \
	  if [ -n "$$do_ecr" ]; then \
	    for t in latest $(IMAGE_TAG); do \
	      d=$$(dg $(ECR_REPO):$$t || echo "<missing>"); \
	      [ "$$d" = "$$esrc" ] || { echo "!!! ECR :$$t = $$d, expected $$esrc"; fail=1; }; \
	    done; \
	  fi; \
	  [ $$fail -eq 0 ] || { echo "!!! promotion did not verify — tags above may be inconsistent"; exit 1; }; \
	  echo ">>> promoted :$$from -> GHCR :latest :$(VERSION) :$(GIT_SHA) :$(IMAGE_TAG)$$( [ -n "$$do_ecr" ] && echo "  + ECR :latest :$(IMAGE_TAG)" )"; \
	  echo "    every tag verified byte-identical to the source ($$gsrc)"; \
	  if [ -n "$$do_ecr" ]; then \
	    echo ""; \
	    echo "    cloud is a SEPARATE act — Batch still runs its pinned tag until:"; \
	    echo "      make infra-plan IMAGE_TAG=$(IMAGE_TAG) && make infra-apply"; \
	  fi

# DEPRECATED: superseded by `promote`, which also covers ECR and the sha/VERSION
# tags. The name was always a misnomer — it re-tags GHCR :latest, which is what
# the FARM pulls; cloud is pinned via Terraform and unaffected by GHCR tags.
cloud-promote: ## DEPRECATED alias for `promote` (FROM=<tag>)
	@echo ">>> cloud-promote is deprecated — use 'make promote FROM=$(FROM)'"
	@$(MAKE) promote FROM=$(FROM)

cloud-down: require-s3-bucket ## FULL teardown -> $0/mo (no prompt): tofu destroy -auto-approve + remove AMI
	cd $(TF_DIR) && AWS_REGION=$(AWS_REGION) tofu destroy -auto-approve \
		-var s3_bucket=$(S3_BUCKET)
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
	  $(MAKE) ami-wait; \
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
	@# minScaleDownDelayMinutes is how long Batch holds an idle instance before it
	@# is eligible for scale-in. At 0 the fleet is released as soon as Batch's own
	@# evaluation loop notices — measured at 70-77s after the queue drains, with
	@# ~88 vCPU idle across that window. At the alternative extreme it would be
	@# ~10 minutes of FULL-FLEET rental per run, several times the run's entire
	@# encode cost, and completely invisible: every job would still succeed.
	@#
	@# It is asserted rather than pinned because aws_batch_compute_environment in
	@# provider 6.56 has no scaling_policy block — compute_resources accepts only
	@# ec2_configuration and launch_template. So Terraform cannot express it, and
	@# a console edit or a future provider default could move it with nothing to
	@# catch it. Drop this check once the provider gains the block and pin it.
	@d=$$(aws batch describe-compute-environments --region $(AWS_REGION) \
	    --query 'computeEnvironments[0].computeResources.scalingPolicy.minScaleDownDelayMinutes' \
	    --output text 2>/dev/null); \
	  if [ "$$d" = "0" ] || [ "$$d" = "None" ] || [ -z "$$d" ]; then \
	    echo "  ok: Batch scale-down delay 0 (no artificial hold on idle instances)"; \
	  else \
	    echo "  WARN: Batch minScaleDownDelayMinutes=$$d — every run pays $$d min of"; \
	    echo "        full-fleet idle rental. Expected 0; set it back on the compute env."; \
	  fi

# Deploy stops at the plan on purpose — review it, then run `make infra-apply`.
# (Keeping preview and apply as separate, deliberate steps for live IaC.)
# farm-up, not restart: it SUPERSEDES it. `restart` runs the server from a local
# build, leaving remote workers on whatever they last pulled — so "one shot" was
# never true for a farm. `farm-up` brings the server, the cluster, the local
# worker AND every DIST_WORKERS box up on GHCR :latest, which `publish` has just
# moved. Deploying then verifies the artifact you actually shipped rather than a
# parallel local build of the same source.
#
# Heavier than restart: it also starts the master profile (postgres, temporal,
# minio). On a cloud-only host that is more than you need — use
# `make publish && make infra-plan && make infra-apply` there.
#
# NOTE no in-flight guard, unlike cloud-dev-up: infra-apply deregisters job-def
# revisions and farm-up bounces workers, so running this mid-encode will break
# it. Check `make status` / the UI first.
# USE_AMI=1 bakes the worker AMI as PART of the deploy, in the one slot where it
# works: after publish (packer bakes the image that was just pushed) and before
# infra-plan (the plan reads WORKER_AMI, which only resolves once the AMI is
# registered and available).
#
# That slot is the whole point. Baking outside the pipeline — deploy, ami-up,
# deploy — costs a SECOND compute-env update, and each one parks Batch in
# UPDATING with scale-down paused, so idle spot boxes linger. Same three
# artifacts, one apply instead of two.
#
# Off by default: the bake takes ~5min and the AMI carries a standing ~$1.50/mo,
# which is not worth it for a Go-only change that never re-pins IMAGE_TAG.
DEPLOY_AMI_STEP := $(if $(filter 1,$(USE_AMI)),$(MAKE) ami-up && $(MAKE) ami-wait &&,)
DEPLOY_AMI_NOTE := $(if $(filter 1,$(USE_AMI)),- AMI baked + wired,)
DEPLOY_AMI_BANNER := $(if $(filter 1,$(USE_AMI)),+AMI bake,)

# The require-idle prerequisite below fires ONCE, at entry. `publish` then runs
# a multi-arch build and two registry pushes — minutes — before anything
# disruptive happens, so the entry check can be arbitrarily stale by the time
# `farm-up` bounces workers or `infra-apply` deregisters job definitions. A job
# submitted in that window was unguarded AND the guard had already reported the
# farm idle (#248: a smoke encode started 29s after a deploy's entry check
# passed, and lost its pre-bounce worker's telemetry).
#
# So it is re-checked immediately before each disruptive step. Each `$(MAKE)` is
# a fresh sub-process, so the recipe genuinely re-runs rather than being skipped
# as an already-satisfied target. Entry keeps its check too: failing before a
# five-minute build beats failing after one.
deploy: require-idle  ## push image + bring the whole farm up + plan + APPLY infra (one shot; USE_AMI=1 also bakes + wires the worker AMI)
	@start=$$(date +%s); \
	echo ">>> deploy started $$(date '+%H:%M:%S')  worker=$(IMAGE_TAG) $(DEPLOY_AMI_BANNER)"; \
	if $(MAKE) publish && $(MAKE) require-idle && $(DEPLOY_AMI_STEP) $(MAKE) farm-up && $(MAKE) infra-plan && $(MAKE) require-idle && $(MAKE) infra-apply; then \
		el=$$(( $$(date +%s) - start )); \
		printf '\a\n\033[1;32m==================================================\n'; \
		printf '  DEPLOY COMPLETE  %dm %02ds   worker=%s\n' $$((el/60)) $$((el%60)) "$(IMAGE_TAG)"; \
		printf '  image pushed - farm up on :latest - infra applied $(DEPLOY_AMI_NOTE)\n'; \
		printf '==================================================\033[0m\n'; \
	else \
		el=$$(( $$(date +%s) - start )); \
		printf '\a\n\033[1;31m!!! DEPLOY FAILED after %dm %02ds - see output above\033[0m\n' $$((el/60)) $$((el%60)); \
		exit 1; \
	fi
	@$(if $(filter 1,$(USE_AMI)),true,$(MAKE) ami-check)

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
	@wired=$$(aws batch describe-compute-environments --region $(AWS_REGION) \
	    --query "computeEnvironments[?contains(computeEnvironmentName,'infinite-streaming-encoder')].computeResources.ec2Configuration[0].imageIdOverride | [0]" \
	    --output text 2>/dev/null); [ "$$wired" = "None" ] && wired=""; \
	  ids=$$(aws ec2 describe-images --owners self --region $(AWS_REGION) \
	    --filters "Name=tag:Name,Values=infinite-streaming-encoder-worker" \
	    --query "Images[?Tags[?Key=='image_tag'&&Value!='$(IMAGE_TAG)']].ImageId" --output text); \
	  for ami in $$ids; do \
	    [ "$$ami" = "None" ] && continue; \
	    if [ "$$ami" = "$$wired" ]; then \
	      echo "  keep $$ami (wired to the compute env — deregistering would strand Batch; run infra-apply to rewire or ami-down to clear first)"; continue; fi; \
	    snaps=$$(aws ec2 describe-images --owners self --region $(AWS_REGION) --image-ids $$ami \
	      --query 'Images[].BlockDeviceMappings[].Ebs.SnapshotId' --output text); \
	    echo "deregister $$ami"; aws ec2 deregister-image --region $(AWS_REGION) --image-id $$ami; \
	    for s in $$snaps; do echo "  delete snapshot $$s"; aws ec2 delete-snapshot --region $(AWS_REGION) --snapshot-id $$s; done; \
	  done
	@echo ">>> Baked infinite-streaming-encoder-worker-$(IMAGE_TAG) (1 AMI total). Now: make infra-apply  (wires it in)"

# EC2 is eventually consistent: an AMI packer has just registered is not
# necessarily queryable yet. That matters more than it sounds, because
# WORKER_AMI resolving EMPTY does not fail anything — infra-plan simply plans
# pull-on-boot, the bake is silently thrown away, and you find out a week later
# in cold-start times. So the wait is part of the contract, not a nicety.
#
# Bounded, because an unbounded `until` is how a failed bake turns into a hung
# deploy — the exact failure mode #239 was about, in a different costume.
AMI_WAIT_TRIES ?= 60
ami-wait:           ## block until the AMI baked for IMAGE_TAG is queryable (used by deploy/cloud-up)
	@echo ">>> waiting for the baked AMI to be queryable (EC2 is eventually consistent)..."
	@i=0; until [ -n "$$(aws ec2 describe-images --owners self --region $(AWS_REGION) \
	      --filters Name=tag:image_tag,Values=$(IMAGE_TAG) Name=state,Values=available \
	      --query 'Images[0].ImageId' --output text 2>/dev/null | grep -v '^None$$')" ]; do \
	  i=$$((i+1)); \
	  if [ $$i -ge $(AMI_WAIT_TRIES) ]; then \
	    echo "!!! no available AMI tagged image_tag=$(IMAGE_TAG) after $$((i*5))s — did the bake fail?"; \
	    exit 1; \
	  fi; \
	  sleep 5; \
	done; \
	echo ">>> AMI for $(IMAGE_TAG) is available."

# Best-effort staleness warning. The wired AMI carries the image_tag it was baked
# for, and WORKER_AMI looks one up by the CURRENT tag — so promoting a new image
# silently unpins the AMI and cloud falls back to pull-on-boot with nothing
# broken and nothing said. `make status` reports it, but only if you go and look;
# this puts it in front of the one person who just changed the image.
ami-check:          ## warn when the wired worker AMI was baked for a different image tag
	@-wired=$$(aws batch describe-compute-environments --region $(AWS_REGION) \
	    --query "computeEnvironments[?contains(computeEnvironmentName,'infinite-streaming-encoder')].computeResources.ec2Configuration[0].imageIdOverride | [0]" \
	    --output text 2>/dev/null); \
	  [ "$$wired" = "None" ] && wired=""; \
	  if [ -n "$$wired" ]; then \
	    baked=$$(aws ec2 describe-images --owners self --region $(AWS_REGION) --image-ids $$wired \
	      --query "Images[0].Tags[?Key=='image_tag'].Value | [0]" --output text 2>/dev/null); \
	    if [ "$$baked" != "$(IMAGE_TAG)" ]; then \
	      printf '\033[1;33m!!! worker AMI %s is baked for %s, not %s — cloud workers will cold-pull.\n    Bake and wire it in one pass with:  make deploy USE_AMI=1\033[0m\n' \
	        "$$wired" "$$baked" "$(IMAGE_TAG)"; \
	    fi; \
	  fi

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
	    -var s3_bucket=$(S3_BUCKET) -var image_tag=$(IMAGE_TAG) -var worker_ami_id="" ; \
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

cloud-clear: require-s3-bucket ## kill every idle AWS cost: sweep tagged instances/volumes/spot/S3 + remove worker AMI
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
.PHONY: dist-up dist-down dist-worker dist-logs dist-ps minio-usage minio-clean

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

# MinIO staging (#93). A finished job's orchestrator deletes its own staging and
# the server sweeps what failed/cancelled jobs leave behind; these are the manual
# equivalents for when you want to look or reclaim right now. Both run inside the
# server container, which already has the MinIO endpoint + creds in its env.
# MINIO_MAX_AGE_S is the idle-age floor: nothing newer is touched, so a running
# encode is never reclaimed (the server's own sweep also passes a keep-list).
MINIO_MAX_AGE_S ?= 86400

# Measure a finished output's ladder into VMAF-vs-bitrate curve points, which
# the Ladders tab uses for its design-time "is this rung earning its place?"
# estimates. Replaces the built-in seed curves (measured on one 4K FPV clip)
# with numbers from YOUR content. The output must have been encoded with
# burn-in OFF — the overlay biases low rungs more than high ones, which is
# exactly the comparison the curve is for.
#
#   make ladder-audit OUT=<dir under OUTPUT_DIR> SRC=<source file>
#   make ladder-audit-all                      # every eligible output, skips the rest
#   ... LADDER_AUDIT_REFERENCE=1080 LIMIT_S=30
#
# Runs NATIVELY on this machine, not in the container — deliberately:
#   - the container's ffmpeg is a pinned BtbN static build with no macOS binary,
#     so a native run can't match it either way
#   - the seed curves in internal/encode/quality_curve.go were themselves
#     produced natively (see docs/vmaf-audit/README.md), so measuring natively
#     keeps new points consistent with the ones they extend
#   - a 4K comparison is ~1.5-2 GB per libvmaf and OOM-killed the container
#     (exit 137); natively it has the whole machine's RAM
# CONSEQUENCE: these scores are NOT comparable with the in-encode per-chunk
# audit, which runs on workers with the image's ffmpeg. The store records which
# ffmpeg produced each run so the difference is visible rather than assumed.
#
# Curves are kept PER CLIP: quality-vs-bitrate is content-dependent, so an
# extreme-motion clip and a talking head give genuinely different curves and
# pooling them would describe neither.
LADDER_AUDIT_REFERENCE ?= 2160
LADDER_AUDIT_PY = PYTHONPATH=scripts python3 -m infinite_streaming_encoder.ladder_audit

ladder-audit:         ## measure one output's ladder into the VMAF curve store (OUT=, SRC=)
	@: $${OUT:?OUT is not set — the output directory name under OUTPUT_DIR}
	@: $${SRC:?SRC is not set — the source file the output was encoded from}
	@command -v ffmpeg >/dev/null || { echo "ffmpeg not found on PATH — this target runs natively"; exit 1; }
	$(LADDER_AUDIT_PY) \
	  --output-dir "$(OUTPUT_DIR)/$(OUT)" --source "$(SRC)" \
	  --reference $(LADDER_AUDIT_REFERENCE) \
	  --store "$(TMP_DIR)/quality-curves.json" \
	  $(if $(LIMIT_S),--limit-s $(LIMIT_S),)

ladder-audit-all:     ## audit eligible outputs; skips burn-in/no-metadata. MATCH=<substr> scopes to a clip; LATEST=1 = newest per codec only
	@command -v ffmpeg >/dev/null || { echo "ffmpeg not found on PATH — this target runs natively"; exit 1; }
	$(LADDER_AUDIT_PY) \
	  --all "$(OUTPUT_DIR)" --source-dir "$(SOURCE_DIR)" \
	  --reference $(LADDER_AUDIT_REFERENCE) \
	  --store "$(TMP_DIR)/quality-curves.json" \
	  $(if $(MATCH),--match "$(MATCH)",) \
	  $(if $(LATEST),--latest,) \
	  $(if $(LIMIT_S),--limit-s $(LIMIT_S),)

minio-usage:          ## what the local-dist MinIO staging is holding, per job prefix
	docker exec $(CONTAINER_NAME) python3 -m infinite_streaming_encoder.dist_staging --usage

minio-clean:          ## reclaim staging idle > MINIO_MAX_AGE_S (default 24h); DRY_RUN=1 to preview
	docker exec $(CONTAINER_NAME) python3 -m infinite_streaming_encoder.dist_staging \
	  --gc --max-age-s $(MINIO_MAX_AGE_S) $(if $(DRY_RUN),--dry-run,)

# DIST_WORKERS: space-separated label=ssh_target pairs of remote worker boxes,
# e.g. DIST_WORKERS = ubuntu=me@worker-box.local
# MASTER_IP: the master box's LAN IP that workers dial for Temporal + MinIO.
DIST_WORKERS ?=
MASTER_IP ?= 192.168.1.10
.PHONY: dist-deploy-workers dist-deploy dist-deploy-ghcr

# Roll over every DIST_WORKERS box, and DO NOT let one box take the others down.
#
# Remote workers are optional and disposable by design — the farm runs fine
# without them — so a box that does not answer is a SKIP, not a failure. The loop
# used to `|| exit 1` on the first bad box, which cost three things at once: the
# remaining boxes were never attempted, `farm-up` failed, and `make deploy`
# therefore never reached `infra-apply`. One asleep laptop stopped an AWS
# deployment that had nothing to do with it.
#
# A box that DOES answer and then fails is a different claim — that is a broken
# deploy rather than an absent box — so it still fails the target. The two must
# not collapse into one exit code, or "my worker is off" and "my worker is
# broken" become indistinguishable.
#
# Whatever happens, the summary names every box and its outcome: a deploy that
# quietly reached none of them must not look like one that reached them all.
#
# $(1) = the per-box command; $$label and $$host are in scope.
WORKER_SSH_OPTS := -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=10 -o ServerAliveCountMax=3

define for_each_worker
	@if [ -z "$(DIST_WORKERS)" ]; then echo "set DIST_WORKERS=label=ssh_target [..] (in .env)"; exit 1; fi
	@ok=0; skipped=; failed=; \
	for w in $(DIST_WORKERS); do \
	  label=$${w%%=*}; host=$${w#*=}; \
	  if ! ssh $(WORKER_SSH_OPTS) "$$host" true >/dev/null 2>&1; then \
	    echo ">>> [$$label] $$host — UNREACHABLE, skipping (box asleep or off the network)"; \
	    skipped="$$skipped $$label"; \
	    continue; \
	  fi; \
	  if $(1); then \
	    ok=$$((ok+1)); \
	  else \
	    echo ">>> [$$label] $$host — FAILED (the box answered, so this is a real error)"; \
	    failed="$$failed $$label"; \
	  fi; \
	done; \
	echo ">>> workers: $$ok deployed$${skipped:+, unreachable:$$skipped}$${failed:+, FAILED:$$failed}"; \
	[ -z "$$failed" ]
endef

dist-deploy-workers: require-ghcr ## rsync code + rebuild image + (re)start worker on each DIST_WORKERS box
	$(call for_each_worker, MASTER_IP=$(MASTER_IP) MINIO_ROOT_USER=$(MINIO_ROOT_USER) MINIO_ROOT_PASSWORD=$(MINIO_ROOT_PASSWORD) \
	  DEV_BUILD=$(DEV_BUILD) FORCE_IMAGE=$(FORCE_IMAGE) \
	  bash infra/local-cluster/deploy-worker.sh "$$host" "$$label")
	@echo ">>> (DEV_BUILD=1 native-builds uncommitted deps on cross-arch boxes)."

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
dist-deploy-ghcr: require-ghcr ## GHCR-pull workers on each DIST_WORKERS box (no build/transfer, no auth)
	$(call for_each_worker, GHCR_PAT= MASTER_IP=$(MASTER_IP) IMAGE=$(REMOTE_IMAGE) GHCR_USERNAME=$(GHCR_USERNAME) \
	  MINIO_ROOT_USER=$(MINIO_ACCESS_KEY) MINIO_ROOT_PASSWORD=$(MINIO_SECRET_KEY) \
	  bash infra/local-cluster/deploy-worker-ghcr.sh "$$host" "$$label")

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
.PHONY: farm-up farm-test-up farm-dev-up farm-dev-down farm-down

farm-up: require-paths require-ghcr ## bring the whole master farm up from GHCR (cluster + server + worker), + DIST_WORKERS
	@echo ">>> [farm-up] pulling images + bringing up the master profile (cluster + server + worker) from GHCR..."
	@if [ -n "$(GHCR_PAT)" ]; then echo "$(GHCR_PAT)" | docker login ghcr.io -u $(GHCR_USERNAME) --password-stdin; fi
	ENCODER_IMAGE=$(REMOTE_IMAGE) ENCODE_SLOTS=$(FARM_ENCODE_SLOTS) $(COMPOSE_BASE) --profile master pull
	ENCODER_IMAGE=$(REMOTE_IMAGE) ENCODE_SLOTS=$(FARM_ENCODE_SLOTS) DOCKER_IMAGE=$(DOCKER_IMAGE) $(COMPOSE_BASE) --profile master up -d --no-build
	@echo ">>> [farm-up] remote workers (from GHCR)..."
	@if [ -n "$(DIST_WORKERS)" ]; then $(MAKE) dist-deploy-ghcr; else \
	  echo "    (no DIST_WORKERS — not updating remote workers. This does NOT stop them: any that"; \
	  echo "     are up stay on the shared queue and keep taking chunks on their OLD image.)"; fi
	@echo ">>> farm up:  UI http://localhost:$(PORT)   Temporal UI http://localhost:$${TEMPORAL_UI_PORT:-8233}"

# Third farm mode, between farm-up (:latest, known-good) and farm-dev-up (local
# build with the code bind-mounted). This builds your working tree, publishes it
# under ONE throwaway tag, and runs the farm on that published image with NO code
# mount — so it exercises the image as shipped and catches what farm-dev-up
# structurally cannot: a file missing from the Dockerfile COPY set, or a
# requirements.txt dep that only exists in your working tree.
#
# Same shape as cloud-dev-up: publish-tag then point one consumer at it, leaving
# :latest serving the known-good image throughout (#144).
#
# ALWAYS republishes rather than reusing an existing tag. Reusing would mean a
# re-run after an edit silently tests the previous build — the exact failure this
# target exists to catch.
farm-test-up: TAG ?= $(DEV_TAG)
farm-test-up: require-paths require-ghcr ## build+publish your WORKING TREE under one tag and run the farm on it (:latest untouched) (TAG=<name>, defaults to $(DEV_TAG))
	@# Make-level, not shell — see the note on publish-tag's guard. The
	@# `TAG ?= $$(DEV_TAG)` line above is the very thing that unexports it.
	@[ -n '$(TAG)' ] || { \
	  echo "!!! TAG resolved empty — pass TAG=<name> explicitly" >&2; exit 1; }
	@# SKIP_ECR: the farm pulls GHCR exclusively. Pushing ECR here would spend
	@# slots in its `keep last 10` lifecycle rule — enough farm iterations would
	@# expire images cloud still has history in, for an image cloud never runs.
	$(MAKE) publish-tag TAG=$(TAG) SKIP_ECR=1 ALSO_TAG=$(DEV_TEST_TAG)
	@ref="$(GHCR_IMAGE):$(TAG)"; \
	  digest=$$(docker buildx imagetools inspect "$$ref" 2>/dev/null | awk '/^Digest:/{print $$2; exit}'); \
	  echo ">>> [farm-test-up] $$ref"; \
	  echo ">>> [farm-test-up] digest $$digest   (tags are mutable — this is what actually runs)"
	$(MAKE) farm-up REMOTE_IMAGE=$(GHCR_IMAGE):$(TAG)
	@echo ">>> farm-test-up complete on :$(TAG) — :latest UNCHANGED. Back to known-good:  make farm-up"
	@echo "    alias :$(DEV_TEST_TAG) also points here — pull that from another box without a sha."
	@echo "    (the farm RUNS the immutable :$(TAG), so this run stays reproducible)"

farm-dev-up: require-paths   ## dev farm from your WORKING TREE (uncommitted): local build + live-mounted code
	@echo ">>> [farm-dev-up] building from working tree + bringing up the master profile with live code..."
	ENCODER_IMAGE=$(IMAGE_NAME) ENCODE_SLOTS=$(FARM_ENCODE_SLOTS) \
	  HOST_SCRIPTS_DIR=$(CURDIR)/scripts/infinite_streaming_encoder \
	  DOCKER_IMAGE=$(DOCKER_IMAGE) \
	  $(COMPOSE_DEV) --profile master up -d --build
	@echo ">>> [farm-dev-up] remote workers (rsync code + transfer/build image)..."
	@if [ -n "$(DIST_WORKERS)" ]; then $(MAKE) dist-deploy-workers; else \
	  echo "    (no DIST_WORKERS — not updating remote workers. This does NOT stop them: any that"; \
	  echo "     are up stay on the shared queue and keep taking chunks on their OLD image, so this"; \
	  echo "     run can span two code versions. See 'make fleet-check'.)"; fi
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
.PHONY: smoke encode fleet-check

# Who is ACTUALLY going to take chunks from the queue — as opposed to who the
# Makefile was told about via DIST_WORKERS, which is what misled us in #248.
# Empty DIST_WORKERS skips *deploying* to the remote boxes; it does not stop
# them, so intent and reality diverge exactly when it matters. This asks the
# server, which knows who is connected.
#
# Reports rather than fails: a colleague's box being powered on is not a reason
# to refuse someone's smoke run. But it must be said out loud, because the
# symptom of a mixed-version fleet is not an error — it is telemetry that is
# quietly a subset, which reads as complete.
fleet-check:            ## list the workers actually connected to the encode queue
	@fleet=$$(curl -sf --max-time 5 http://localhost:$(PORT)/api/dist/workers 2>/dev/null); \
	 if [ -z "$$fleet" ]; then echo ">>> [fleet] server not reachable on :$(PORT) — cannot tell who is connected"; exit 0; fi; \
	 remote=$$(printf '%s' "$$fleet" | python3 -c \
	   "import json,sys; print(' '.join(m.get('name','?') for m in json.load(sys.stdin).get('machines',[]) if not m.get('local')))" 2>/dev/null); \
	 if [ -n "$$remote" ]; then \
	   printf '\033[1;33m>>> [fleet] NOT master-only — remote worker(s) connected: %s\033[0m\n' "$$remote"; \
	   echo "    They take chunks from the same queue on whatever image they last pulled, so a run"; \
	   echo "    now can span code versions and drop the telemetry of whichever half is older."; \
	   echo "    For a same-version fleet: 'make deploy' (updates every box), or stop those workers."; \
	 else \
	   echo ">>> [fleet] master only — no remote workers connected"; \
	 fi; \
	 printf '%s' "$$fleet" | python3 -c "$$FLEET_VERSION_PY" 2>/dev/null || true

# Reported builds per box, and whether they disagree (#248). Only workers that
# have run a chunk since the server started have said — a box that has been idle
# all session is 'not reported', which is NOT the same as agreeing.
define FLEET_VERSION_PY
import json, sys
d = json.load(sys.stdin)
vers, unknown = d.get("versions") or {}, d.get("versions_unknown") or []
if not vers and not unknown:
    print("    versions: none reported yet (no chunk has run since the server started)")
    raise SystemExit
for m in sorted(vers):
    print(f"    {m:12} {vers[m]}")
for m in unknown:
    print(f"    {m:12} not reported")
if d.get("version_mixed"):
    print("\033[1;31m    MIXED BUILDS — this fleet will produce inconsistent telemetry.\033[0m")
    print("    Run 'make deploy' to put every box on the same image.")
endef
export FLEET_VERSION_PY
# The command-line encode client. Run from the repo so it needs no install; it
# is a plain HTTP client, so nothing but python3 is required on this side.
# `make smoke` and `make oobe` both drive it — they each used to hand-roll the
# same curl-and-poll block, with subtly different failure handling.
ENCODE_CLI = PYTHONPATH=scripts python3 -m infinite_streaming_encoder.encoder_cli

encode:               ## submit an encode from the CLI: make encode ARGS="clip.mp4 --target cloud --wait" (--help for the full option list)
	@$(ENCODE_CLI) --server http://localhost:$(PORT) $(ARGS)

SMOKE_SRC ?= $(SOURCE_DIR)/smoke.mp4
# Open the jobs page in a browser during the run; set SMOKE_OPEN=0 to skip.
SMOKE_OPEN ?= 1

# NOTE: this used to call itself a "single-device" smoke. It is not one, and
# cannot be: `farm-dev-up DIST_WORKERS=` rebuilds only the master, it does not
# stop remote workers, so any that are up keep taking chunks on whatever image
# they last pulled (#248). CLAUDE.md names this target as THE pre-merge gate for
# the chunk/dispatch contract, so a claim it cannot keep is worse here than
# anywhere else — the gate would read as held when the run had spanned two code
# versions. It reports the fleet it actually got instead; see fleet-check.
# Adding `smoke-cloud` gives people a correct command; it does nothing about the
# WRONG one. `make smoke TARGET=cloud` was documented from 2026-07-31, has never
# been read by anything, and make accepts an override for a variable nothing
# consumes without complaint — so it ran the LOCAL smoke and printed SMOKE PASS
# for the cloud path. Anyone with that in muscle memory, a shell history or a
# script keeps getting the same confident lie unless it is made to fail. A knob
# is only real if something breaks when it is set wrong; this is what breaks.
#
# FIRST prerequisite, before `build`: prerequisites run left to right, and
# failing after a multi-minute docker build would be a poor way to say "that
# flag has never done anything".
.PHONY: reject-stale-target
reject-stale-target:
	@if [ -n "$(TARGET)" ]; then \
	  echo "!!! TARGET=$(TARGET) is not read by 'make smoke' and never has been."; \
	  echo "    This target only ever runs the LOCAL (local-dist) path — passing"; \
	  echo "    TARGET did nothing except make the output look like it covered"; \
	  echo "    something it did not."; \
	  echo "    For the cloud path:  make smoke-cloud"; \
	  exit 1; fi

smoke: reject-stale-target require-paths build   ## end-to-end smoke: tiny clip -> local-dist encode -> assert output (reports the fleet it ran on)
	@echo ">>> [smoke] generating $(SMOKE_SRC) (if missing)..."
	@[ -f "$(SMOKE_SRC)" ] || docker run --rm -v "$(SOURCE_DIR):/src" --entrypoint ffmpeg $(IMAGE_NAME) \
	  -f lavfi -i testsrc2=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 \
	  -t 20 -pix_fmt yuv420p -c:v libx264 -c:a aac -shortest -y /src/smoke.mp4
	@echo ">>> [smoke] clearing any prior smoke output..."
	@rm -rf $(OUTPUT_DIR)/smoke_p200* 2>/dev/null || true
	@echo ">>> [smoke] bringing up the master from the working tree..."
	$(MAKE) farm-dev-up DIST_WORKERS=
	@echo ">>> [smoke] waiting for the server (:$(PORT))..."
	@for i in $$(seq 1 30); do curl -sf http://localhost:$(PORT)/api/jobs >/dev/null 2>&1 && break; sleep 1; done
	@$(MAKE) --no-print-directory fleet-check
	@echo ">>> [smoke] opening the jobs page (set SMOKE_OPEN=0 to skip)..."
	@[ "$(SMOKE_OPEN)" = "0" ] || ( open http://localhost:$(PORT)/ 2>/dev/null || xdg-open http://localhost:$(PORT)/ 2>/dev/null ) || echo "    watch it at http://localhost:$(PORT)/"
	@echo ">>> [smoke] submitting encode (h264, 720p, 12s chunks) + waiting (timeout ~300s)..."
	@$(ENCODE_CLI) --server http://localhost:$(PORT) \
	    smoke.mp4 --target local --codec h264 --max-res 720p --chunk-duration 12 \
	    --wait --timeout 300 || { echo '>>> SMOKE FAIL: encode did not finish'; exit 1; }
	@d=$$(ls -d $(OUTPUT_DIR)/smoke_p200*h264* 2>/dev/null | head -1); \
	 if [ -n "$$d" ] && ls "$$d"/*.m3u8 >/dev/null 2>&1; then \
	   echo ">>> SMOKE PASS: $$d (has playlists)"; \
	 else echo ">>> SMOKE FAIL: no smoke output dir with playlists in $(OUTPUT_DIR)"; exit 1; fi

# ---- Cloud smoke -------------------------------------------------------------
# The cloud twin of `make smoke`. CLAUDE.md told people to run
# `make smoke TARGET=cloud` for this; no TARGET variable ever existed, so that
# command silently ran the LOCAL smoke and reported PASS — a confident green for
# a path it never touched. This is the target that claim needed.
#
# THREE WAYS IT DIFFERS FROM `make smoke`, all deliberate:
#
# 1. It does NOT build, and does NOT bounce the farm. A cloud encode runs the
#    image in ECR, under the deployed state machine and job definitions — none
#    of which your working tree affects. Rebuilding the local server would
#    change only who submits the job, which is the one part this is not
#    testing. So it uses the server already running, and tells you what is
#    actually under test: the DEPLOYED artifacts. Run it AFTER `make deploy`.
#
# 2. It forces the media home (`--no-skip-media-download`). The server default
#    may be to leave segments in S3 (#214), and a metadata-only output dir has
#    playlists, rung subdirs and a happy parseOutputMeta — it is indistinguishable
#    from a complete one by every signal this would otherwise check (#225). An
#    assertion that cannot tell those apart is not an assertion. So the download
#    is forced and the segments are checked on disk.
#
# 3. It costs real money and takes real time: spot provisioning, a cold image
#    pull, then the encode. Minutes, not seconds, hence the far larger timeout.
SMOKE_CLOUD_TIMEOUT ?= 1800

.PHONY: smoke-cloud
smoke-cloud: require-paths require-s3-bucket  ## end-to-end CLOUD smoke against the DEPLOYED stack: tiny clip -> Batch -> assert media came back (COSTS MONEY)
	@: $${STATE_MACHINE_ARN:?STATE_MACHINE_ARN is not set — the cloud-batch target is not configured, so there is nothing to smoke. See infra/terraform/README.md}
	@curl -sf http://localhost:$(PORT)/api/jobs >/dev/null 2>&1 || { \
	  echo ">>> SMOKE-CLOUD FAIL: no server on :$(PORT)."; \
	  echo "    This target deliberately does not start one — it tests the DEPLOYED"; \
	  echo "    stack, and bouncing the farm would not change what Batch runs."; \
	  echo "    Bring one up with 'make farm-up' (or 'make run') and re-run."; exit 1; }
	@echo ">>> [smoke-cloud] testing the DEPLOYED image + state machine + job definitions."
	@echo "    A change to the WORKER (encode/package phases) is under test only if"
	@echo "    you deployed it — Batch runs the ECR image, not your working tree."
	@echo "    A change to the CONTROL PLANE (the server: submission, the host-side"
	@echo "    phases, the sync-back) IS under test if you rebuilt and restarted the"
	@echo "    server, which this target deliberately does not do for you."
	@echo "    This launches spot capacity and transfers media: it costs money."
	@echo ">>> [smoke-cloud] generating $(SMOKE_SRC) (if missing)..."
	@[ -f "$(SMOKE_SRC)" ] || docker run --rm -v "$(SOURCE_DIR):/src" --entrypoint ffmpeg $(IMAGE_NAME) \
	  -f lavfi -i testsrc2=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 \
	  -t 20 -pix_fmt yuv420p -c:v libx264 -c:a aac -shortest -y /src/smoke.mp4
	@# The mezzanine cache is keyed on name|size|mtime, and this file is reused
	@# across runs — so by DEFAULT this smoke gets a cache hit and does not
	@# exercise the mezzanine phase at all.
	@#
	@# That used to be forced to a miss on every run (an unconditional touch),
	@# because a cache-hit run is INDISTINGUISHABLE from a working
	@# skip-the-mezzanine-job change: both show no mezz Batch job and go straight
	@# to chunk encodes. Verifying #266 against it, the prediction and the
	@# observation matched perfectly and the code under test had not run.
	@#
	@# But forcing a miss every time also re-uploads a mezzanine and orphans the
	@# previous one under mezz/<key>/ until the lifecycle rule collects it, which
	@# is a lot of garbage for a target run several times a day. So the miss is
	@# now OPT-IN, and the run REPORTS which path it took instead — the actual
	@# lesson was not "always force a miss", it was "never let a pass claim
	@# coverage it does not have".
	@#
	@# FORCE_MEZZ=1 to exercise the mezzanine (touch, never delete: bumping the
	@# mtime moves the key, so the stale entry ages out on the bucket's existing
	@# lifecycle rule — no S3 deletion from a test target).
	@if [ -n "$(FORCE_MEZZ)" ]; then \
	  echo ">>> [smoke-cloud] FORCE_MEZZ=1 — touching $(SMOKE_SRC) to force a mezzanine cache miss..."; \
	  touch "$(SMOKE_SRC)"; \
	else \
	  echo ">>> [smoke-cloud] mezzanine will likely CACHE-HIT (FORCE_MEZZ=1 to exercise it)."; \
	fi
	@echo ">>> [smoke-cloud] clearing any prior smoke output..."
	@rm -rf $(OUTPUT_DIR)/smoke_p200* 2>/dev/null || true
	@echo ">>> [smoke-cloud] submitting (h264, 720p, 12s chunks, media forced home);"
	@echo "    waiting up to $(SMOKE_CLOUD_TIMEOUT)s — spot boot + image pull happen first..."
	@$(ENCODE_CLI) --server http://localhost:$(PORT) \
	    smoke.mp4 --target cloud --codec h264 --max-res 720p --chunk-duration 12 \
	    --no-skip-media-download \
	    --wait --timeout $(SMOKE_CLOUD_TIMEOUT) \
	    || { echo '>>> SMOKE-CLOUD FAIL: encode did not finish (see the job log / AWS tab)'; exit 1; }
	@$(MAKE) --no-print-directory smoke-cloud-assert

# Split out so the assertions can be exercised against fixture directories
# without launching spot capacity — otherwise the only way to find out whether
# a FAIL branch works is to have a real cloud encode fail in that exact way.
# Run it directly: make smoke-cloud-assert OUTPUT_DIR=/path/to/fixture
.PHONY: smoke-cloud-assert
smoke-cloud-assert:
	@d=$$(ls -d $(OUTPUT_DIR)/smoke_p200*h264* 2>/dev/null | head -1); \
	 if [ -z "$$d" ]; then \
	   echo ">>> SMOKE-CLOUD FAIL: no smoke output dir in $(OUTPUT_DIR)"; exit 1; fi; \
	 if ! ls "$$d"/*.m3u8 >/dev/null 2>&1; then \
	   echo ">>> SMOKE-CLOUD FAIL: $$d has no playlists"; exit 1; fi; \
	 if [ -f "$$d/.remote.json" ]; then \
	   echo ">>> SMOKE-CLOUD FAIL: $$d is metadata-only — .remote.json says the media"; \
	   echo "    is still in S3, despite --no-skip-media-download. Play would 404."; exit 1; fi; \
	 segs=$$(find "$$d" -name '*.m4s' 2>/dev/null | wc -l | tr -d ' '); \
	 if [ "$$segs" -eq 0 ]; then \
	   echo ">>> SMOKE-CLOUD FAIL: $$d has playlists but ZERO segments — the manifest"; \
	   echo "    describes media that is not on disk."; exit 1; fi; \
	 mz="not determined"; \
	 jl=$$(ls -t $(TMP_DIR)/logs/*.log 2>/dev/null | head -1); \
	 if [ -n "$$jl" ]; then \
	   if grep -q 'building mezzanine on the host' "$$jl" 2>/dev/null; then \
	     mz="mezzanine BUILT"; else mz="mezzanine CACHE-HIT (not exercised)"; fi; \
	 fi; \
	 echo ">>> SMOKE-CLOUD PASS: $$d ($$segs segments, playlists present, no remote sidecar)"; \
	 case "$$mz" in \
	   *CACHE-HIT*) echo "    $$mz — this run did NOT test the mezzanine path."; \
	                echo "    Re-run with FORCE_MEZZ=1 if you changed anything upstream of the chunk plan.";; \
	   *)           echo "    $$mz — the mezzanine path was exercised.";; \
	 esac

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
	@if $(ENCODE_CLI) --server http://localhost:$(OOBE_PORT) \
	      smoke.mp4 --target local --codec h264 --max-res 720p --chunk-duration 12 \
	      --wait --timeout 300; then st=done; else st=failed; fi; \
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
