# ==============================================================================
# Stage 1 — Build stage: Compile virtual environment with uv
# ==============================================================================
FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Copy dependency manifests first to leverage Docker layer cache
COPY pyproject.toml uv.lock ./

# Install locked production dependencies into a standalone /app/.venv
RUN uv sync --frozen --no-dev --no-install-project

# ==============================================================================
# Stage 2 — Runtime stage: Minimal image without build tools, running non-root
# ==============================================================================
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    MIGRATE_ON_START=true \
    PORT=8000

WORKDIR /app

# Create dedicated non-root system user and group (UID/GID 10001)
RUN addgroup --system --gid 10001 appgroup && \
    adduser --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin appuser

# Copy virtual environment from builder stage (no uv, no compiler, no pip in runtime)
COPY --from=builder --chown=appuser:appgroup /app/.venv /app/.venv

# Copy application sources with non-root ownership
COPY --chown=appuser:appgroup app ./app
COPY --chown=appuser:appgroup alembic ./alembic
COPY --chown=appuser:appgroup alembic.ini ./alembic.ini
COPY --chown=appuser:appgroup main.py ./main.py
COPY --chown=appuser:appgroup config.yaml ./config.yaml
COPY --chown=appuser:appgroup scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh

# Switch to non-root user for security
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:' + str(__import__('os').environ.get('PORT', 8000)) + '/')" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]