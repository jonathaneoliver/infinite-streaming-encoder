FROM golang:1.22-alpine AS builder
WORKDIR /build
COPY go.mod .
COPY cmd/ cmd/
COPY internal/ internal/
RUN CGO_ENABLED=0 go build -o /encoder ./cmd/server

FROM alpine:3.20

# Encoding toolchain
RUN apk add --no-cache \
    bash curl gettext \
    ffmpeg \
    python3 \
    ttf-dejavu \
    aws-cli \
    docker-cli

# Shaka Packager — multi-arch binary
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) pkg="packager-linux-arm64" ;; \
      amd64) pkg="packager-linux-x64" ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -L "https://github.com/shaka-project/shaka-packager/releases/download/v3.4.2/${pkg}" \
      -o /usr/local/bin/packager; \
    chmod +x /usr/local/bin/packager

COPY --from=builder /encoder /usr/local/bin/encoder
COPY static/ /app/static/
COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*.sh /app/scripts/*.py 2>/dev/null || true
WORKDIR /app
EXPOSE 8080
ENTRYPOINT ["encoder"]
