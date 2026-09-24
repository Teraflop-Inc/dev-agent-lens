# Dev Agent Lens, the store side: `dal` with the S3 and OTLP extras, nothing else.
# Built from the repo root:  docker build -f deploy/Dockerfile -t dal:local .
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY dev_agent_lens ./dev_agent_lens
# Every script entrypoint.sh runs must be here; tests/test_deploy_image.py holds the two in step.
COPY scripts/migration_oracle.py scripts/store_health.py scripts/oracle_daily.sh ./scripts/
RUN uv sync --frozen --no-dev --extra s3 --extra otlp && rm -rf /root/.cache
ENV PATH="/app/.venv/bin:${PATH}" DAL_CONFIG_PATH=/data/dal
VOLUME ["/data"]
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["sync-loop"]
