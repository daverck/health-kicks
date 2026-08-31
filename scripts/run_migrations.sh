#!/usr/bin/env bash
# Run Alembic migrations against a REMOTE PostgreSQL database (e.g. AWS RDS).
# Usage: ./scripts/run_migrations.sh [upgrade|current|history|downgrade] [-1] [-U url]
# The URL can also come from DATABASE_URL / HEALTHKICKS_DATABASE_URL.
# RDS connection strings should include ?sslmode=require.
set -euo pipefail

COMMAND="${1:-upgrade}"
REVISION="${2:-}"
URL="${DATABASE_URL:-${HEALTHKICKS_DATABASE_URL:-}}"
SERVICE="${MIGRATION_SERVICE:-migrate}"

if [ -n "${3:-}" ]; then URL="$3"; fi

if [ -z "$URL" ]; then
    echo "error: no database URL (pass as 3rd arg or set DATABASE_URL)" >&2
    echo "       For RDS append ?sslmode=require to the connection string." >&2
    exit 2
fi

case "$URL" in
    *localhost*|*127.0.0.1*|*@postgres*) ;;
    *sslmode=*) ;;
    *postgres*) URL="$URL?sslmode=require"; echo "==> appended sslmode=require" ;;
esac

echo "==> alembic $COMMAND $REVISION (against remote DB)"
docker compose run --rm -e DATABASE_URL="$URL" "$SERVICE" alembic "$COMMAND" $REVISION
echo "==> done."
