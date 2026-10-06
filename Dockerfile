FROM golang:1.26.8-bookworm AS buildx-builder

ARG BUILDX_VERSION=v0.37.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${BUILDX_VERSION}" https://github.com/docker/buildx.git /src
WORKDIR /src
RUN go mod edit -replace=github.com/moby/go-archive=github.com/moby/go-archive@v0.3.3 \
    && CGO_ENABLED=0 go build -mod=mod -trimpath \
            -ldflags "-s -w -X github.com/docker/buildx/version.Version=${BUILDX_VERSION}" \
            -o /out/docker-buildx ./cmd/buildx

FROM python:3.13-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        openssh-client \
        nodejs \
        npm \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-ce-cli \
        docker-compose-plugin \
    && npm install -g \
        @devcontainers/cli \
        @openai/codex \
    && printf '#!/bin/sh\nexec docker compose "$@"\n' \
        > /usr/local/bin/docker-compose \
    && chmod +x /usr/local/bin/docker-compose \
    && docker --version \
    && docker compose version \
    && docker-compose version \
    && devcontainer --version \
    && codex --version \
    && rm -rf /var/lib/apt/lists/*

COPY --from=buildx-builder /out/docker-buildx /usr/libexec/docker/cli-plugins/docker-buildx
RUN chmod 0755 /usr/libexec/docker/cli-plugins/docker-buildx \
    && docker buildx version

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade 'pip' \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . .
COPY scripts/orchestrator-entrypoint.sh /usr/local/bin/orchestrator-entrypoint
RUN chmod 0755 /usr/local/bin/orchestrator-entrypoint

ENTRYPOINT ["/usr/local/bin/orchestrator-entrypoint"]
CMD ["python", "-m", "app", "--help"]
