# Copyright 2026 MSPbots.ai
# SPDX-License-Identifier: Apache-2.0

# --- Builder ---
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first so the layer is cached when only src changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# --- Production ---
FROM python:3.12-slim AS production

RUN groupadd -g 1001 app && useradd -u 1001 -g app -s /bin/sh -m app

# slim images ship neither curl nor wget; the health check needs one of them.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
ENV PATH="/app/.venv/bin:$PATH"

ENV MCP_HTTP_PORT=8080 MCP_HTTP_HOST=0.0.0.0
USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["python", "-m", "unitrends_mcp"]

LABEL org.opencontainers.image.title="unitrends-mcp"
LABEL org.opencontainers.image.description="Unitrends MCP server"
LABEL org.opencontainers.image.vendor="MSPbots.ai"
LABEL org.opencontainers.image.licenses="Apache-2.0"
