#!/bin/sh
# HealthKicks container entrypoint.
#
# Local dev / default:   start uvicorn directly (schema handled by lifespan).
# App Runner release:    set MIGRATE_ON_START=true to run `alembic upgrade head`
#                        against RDS before serving traffic. Safe to keep on
#                        for single-instance services; for multi-instance,
#                        run migrations from a separate one-off task instead.
set -e

# Container startup: runs pending Alembic migrations by default before starting uvicorn
# (set MIGRATE_ON_START=false to disable automatic migrations at container start).
if [ "${MIGRATE_ON_START:-true}" = "true" ]; then
    echo "[entrypoint] Running Alembic migrations against $(echo "${DATABASE_URL:-$HEALTHKICKS_DATABASE_URL}" | sed 's|//[^@]*@|//***@|')"
    alembic upgrade head
    echo "[entrypoint] Migrations completed."
fi

exec "$@"
