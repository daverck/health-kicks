#!/usr/bin/env bash
# Run Alembic migrations against a REMOTE PostgreSQL database (e.g. AWS RDS).
# Usage: ./scripts/run_migrations.sh [upgrade|current|history|downgrade] [revision] [url]
# The URL can also come from DATABASE_URL / HEALTHKICKS_DATABASE_URL.
# RDS connection strings should include ?sslmode=require.
set -euo pipefail

COMMAND="${1:-upgrade}"
REVISION="${2:-}"
URL="${DATABASE_URL:-${HEALTHKICKS_DATABASE_URL:-}}"
SERVICE="${MIGRATION_SERVICE:-migrate}"

if [ -n "${3:-}" ]; then 
    URL="$3"
fi

# Si la commande est 'upgrade' et qu'aucune révision n'est spécifiée, utiliser 'head'
if [ "$COMMAND" = "upgrade" ] && [ -z "$REVISION" ]; then
    REVISION="head"
fi

if [ -z "$URL" ]; then
    echo "error: no database URL (pass as 3rd arg or set DATABASE_URL)" >&2
    echo "       For RDS append ?sslmode=require to the connection string." >&2
    exit 2
fi

case "$URL" in
    *localhost*|*127.0.0.1*|*@postgres*) ;;
    *sslmode=*) ;;
    *\?*) URL="$URL&sslmode=require"; echo "==> appended &sslmode=require" ;;
    *postgres*) URL="$URL?sslmode=require"; echo "==> appended ?sslmode=require" ;;
esac

echo "==> alembic $COMMAND ${REVISION:-} (against remote DB)"

if [ -n "$REVISION" ]; then
    docker compose run --rm -e DATABASE_URL="$URL" "$SERVICE" alembic "$COMMAND" "$REVISION"
else
    docker compose run --rm -e DATABASE_URL="$URL" "$SERVICE" alembic "$COMMAND"
fi

echo "==> done."
