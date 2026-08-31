#!/bin/sh
# HealthKicks container entrypoint.
#
# Local dev / default:   start uvicorn directly (schema handled by lifespan).
# App Runner release:    set MIGRATE_ON_START=true to run `alembic upgrade head`
#                        against RDS before serving traffic. Safe to keep on
#                        for single-instance services; for multi-instance,
#                        run migrations from a separate one-off task instead.
set -e

if [ "${MIGRATE_ON_START:-false}" = "true" ]; then
    echo "[entrypoint] Running Alembic migrations against $(echo "$DATABASE_URL" | sed 's|//[^@]*@|//***@|')"
    alembic upgrade head
    echo "[entrypoint] Migrations up to date."
fi

exec "$@"
