# --- Build stage: export locked production dependencies ----------------------
FROM python:3.11-slim AS builder

WORKDIR /build
RUN pip install --no-cache-dir uv
# Copy lock files first so this layer is cached until dependencies change.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --format requirements-txt -o requirements.txt

# --- Runtime stage ----------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY --from=builder /build/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    # Non-root user for defense in depth (App Runner runs containers as root
    # internally, but this also fixes local `docker run` permissions).
    && adduser --disabled-password --gecos "" --uid 10001 appuser \
    && chown -R appuser:appuser /app

# Application code + Alembic migrations travel with the image so the same
# artifact can serve traffic AND run `alembic upgrade head` against RDS.
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini ./alembic.ini
COPY --chown=appuser:appuser main.py ./main.py
COPY --chown=appuser:appuser config.yaml ./config.yaml
COPY --chown=appuser:appuser scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER appuser
EXPOSE 8000
# App Runner injects PORT; default to 8000 locally.
ENV PORT=8000

# Runs pending Alembic migrations first (set MIGRATE_ON_START=true for App
# Runner single-instance deploys), then starts uvicorn.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]