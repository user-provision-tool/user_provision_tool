FROM python:3.13-slim

# Build-time proxy support (temporary build-env adaptation)
ARG HTTP_PROXY
ARG HTTPS_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY}

# ---- System dependencies ----
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null || true; \
    apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

# ---- Docker CLI + Compose plugin ----
# Multi-stage COPY from docker:cli pulls these binaries from Docker Hub
# during the build — no host filesystem involved. The docker binary in
# docker:cli is statically linked (Alpine/musl), so it runs on any Linux.
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:cli /usr/local/libexec/docker/cli-plugins/docker-compose \
                       /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=docker:cli /usr/local/libexec/docker/cli-plugins/docker-buildx \
                       /usr/local/libexec/docker/cli-plugins/docker-buildx

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

ENV HTTP_PROXY= HTTPS_PROXY=

# Copy source
COPY lib/ lib/
COPY cli/ cli/
COPY db/ db/
COPY migrations/ migrations/
COPY api.py ./

EXPOSE 8000

CMD ["/app/.venv/bin/uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
