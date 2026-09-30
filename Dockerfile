# syntax=docker/dockerfile:1
# ---- build stage: uv resolves + installs (wheels or source build) ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
# KEY FIX: force uv to install into /opt/venv so the runtime stage finds it.
# Without this, `uv sync` creates .venv (the project default) and the
# runtime stage has a venv with only `python` in bin/ — no alembic, no uvicorn,
# exit 127 crash loop (observed pre-reboot).
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential pkg-config libmariadb-dev ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Cache: copy manifests + lock first, resolve, then code
COPY pyproject.toml README.md ./
COPY uv.lock ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --frozen --python /usr/local/bin/python

# ---- final stage ----
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd cookbook && useradd -m -g cookbook cookbook
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY pyproject.toml ./
COPY uv.lock ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY bin/docker-entrypoint.sh ./bin/docker-entrypoint.sh
RUN chmod +x ./bin/docker-entrypoint.sh && chown -R cookbook:cookbook /app
USER cookbook
VOLUME ["/media"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import httpx,sys; r=httpx.get('http://127.0.0.1:8000/readyz', timeout=5); sys.exit(0 if r.status_code==200 else 1)" || exit 1
ENTRYPOINT ["./bin/docker-entrypoint.sh"]
CMD ["uvicorn", "cookbook.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
