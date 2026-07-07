FROM python:3.13-slim

# ---- System dependencies ----
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
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

# Copy source
COPY lib/ lib/
COPY cli/ cli/
COPY api.py ./

EXPOSE 8000

CMD ["/app/.venv/bin/uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
