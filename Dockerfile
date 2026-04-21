FROM golang:1.22-alpine AS builder
ARG VERSION=dev
ARG GIT_SHA=unknown
WORKDIR /build
COPY go.mod .
COPY cmd/ cmd/
COPY internal/ internal/
RUN CGO_ENABLED=0 go build \
    -ldflags "-X main.version=${VERSION} -X main.gitSha=${GIT_SHA}" \
    -o /encoder ./cmd/server

FROM python:3.12-slim

ARG TARGETARCH
ARG VERSION=dev
ARG GIT_SHA=unknown

# OCI labels let the SPA's About tab query the registry (GHCR) and
# report the cloud image's version + commit SHA alongside the local
# binary's, so the user can see when the two have drifted.
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.revision="${GIT_SHA}"
LABEL org.opencontainers.image.source="https://github.com/jonathaneoliver/Encoder"

# OS packages: encoding toolchain + CA certs + fonts for drawtext burn-ins.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash curl ca-certificates gettext-base \
        ffmpeg \
        fonts-dejavu-core \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 (multi-arch; official installer).
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) awsarch="x86_64" ;; \
        arm64) awsarch="aarch64" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${awsarch}.zip" -o /tmp/awscliv2.zip; \
    unzip -q /tmp/awscliv2.zip -d /tmp; \
    /tmp/aws/install; \
    rm -rf /tmp/aws /tmp/awscliv2.zip

# Docker CLI (static binary; talks to the host's daemon via mounted socket).
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) dockerarch="x86_64" ;; \
        arm64) dockerarch="aarch64" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${dockerarch}/docker-27.3.1.tgz" -o /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz -C /tmp; \
    mv /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker /tmp/docker.tgz

# Shaka Packager — multi-arch binary.
RUN set -eux; \
    case "${TARGETARCH}" in \
        arm64) pkg="packager-linux-arm64" ;; \
        amd64) pkg="packager-linux-x64" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/shaka-project/shaka-packager/releases/download/v3.4.2/${pkg}" \
        -o /usr/local/bin/packager; \
    chmod +x /usr/local/bin/packager

# Python deps (boto3 for cloud orchestration).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY --from=builder /encoder /usr/local/bin/encoder
COPY static/ /app/static/
COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*.sh 2>/dev/null || true; \
    find /app/scripts -name '*.py' -exec chmod +x {} + 2>/dev/null || true

# PYTHONPATH lets `python3 -m encoder.foo` and `from encoder.foo import bar`
# resolve regardless of the worker container's CWD.
ENV PYTHONPATH=/app/scripts

WORKDIR /app
EXPOSE 8080
ENTRYPOINT ["encoder"]
