# syntax=docker/dockerfile:1

# Build stage: install the locked dependencies and the project into a virtualenv.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so this layer stays cached until uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --extra postgres --no-install-project

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra postgres --no-editable


# Runtime stage: the virtualenv, the migrations and an unprivileged user. No uv,
# no compilers, no source tree.
FROM python:3.12-slim AS runtime
RUN groupadd --system ledgerline \
    && useradd --system --gid ledgerline --home-dir /app --no-create-home ledgerline
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
# Writable location for the default SQLite database; set DATABASE_URL for PostgreSQL.
RUN mkdir /app/data && chown ledgerline:ledgerline /app/data
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////app/data/ledgerline.db
USER ledgerline
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["ledgerline", "serve", "--host", "0.0.0.0", "--port", "8000"]
