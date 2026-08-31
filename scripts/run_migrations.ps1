#!/usr/bin/env pwsh
# Run Alembic migrations against a REMOTE PostgreSQL database (e.g. AWS RDS).
#
# Usage:
#   .\scripts\run_migrations.ps1 upgrade              # apply pending migrations (default)
#   .\scripts\run_migrations.ps1 current              # show applied revision
#   .\scripts\run_migrations.ps1 history              # list migration history
#   .\scripts\run_migrations.ps1 downgrade -1         # revert one revision
#   .\scripts\run_migrations.ps1 upgrade -Url "postgresql+psycopg2://user:pass@rds-host:5432/healthkicks?sslmode=require"
#
# The URL can also come from the environment: DATABASE_URL or HEALTHKICKS_DATABASE_URL.
# RDS connection strings MUST include ?sslmode=require (or verify-full with the RDS CA).
param(
    [Parameter(Position = 0)]
    [string]$Command = "upgrade",

    [Parameter(Position = 1)]
    [string]$Revision = "",

    [string]$Url = "",

    [string]$Service = "migrate"
)

$ErrorActionPreference = "Stop"

if (-not $Url) {
    $Url = $env:DATABASE_URL
    if (-not $Url) { $Url = $env:HEALTHKICKS_DATABASE_URL }
}
if (-not $Url) {
    Write-Error "No database URL. Pass -Url or set DATABASE_URL / HEALTHKICKS_DATABASE_URL.`nFor RDS append ?sslmode=require to the connection string."
}

# Basic guard: refuse non-SSL remote URLs (RDS rejects nothing by default).
if ($Url -match "postgres" -and $Url -notmatch "sslmode=" -and $Url -notmatch "localhost|127\.0\.0\.1|@postgres") {
    Write-Warning "Remote Postgres URL without sslmode - appending sslmode=require."
    $Url = "$Url?sslmode=require"
}

Write-Host "==> alembic $Command $Revision (against remote DB)" -ForegroundColor Cyan

if ($Service -eq "local") {
    # Run migrations from the host venv (no Docker needed).
    $env:DATABASE_URL = $Url
    uv run alembic $Command $Revision
    if ($LASTEXITCODE -ne 0) { Write-Error "alembic $Command failed with exit code $LASTEXITCODE" }
    return
}

# Default: run inside the API image so tool versions match production exactly.
docker compose run --rm `
    -e DATABASE_URL="$Url" `
    $Service alembic $Command $Revision

if ($LASTEXITCODE -ne 0) { Write-Error "alembic $Command failed with exit code $LASTEXITCODE" }
Write-Host "==> done." -ForegroundColor Green
