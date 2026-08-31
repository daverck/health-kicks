# Deployment Guide — AWS App Runner + RDS PostgreSQL

## 1. RDS PostgreSQL setup

1. Create a `db.t3.micro` (or larger) PostgreSQL 16 instance in the same VPC as App Runner.
2. **Security group**: allow inbound TCP 5432 only from App Runner's outbound security group (or, for a private VPC connector, from the App Runner VPC connector SG). Do **not** open 5432 to `0.0.0.0/0`.
3. Enable **storage encryption** and automated backups.
4. Note the endpoint, e.g. `healthkicks.xxxx.eu-north-1.rds.amazonaws.com`.

Connection string format used by the app (SQLAlchemy driver prefix required):

```
postgresql+psycopg2://<user>:<password>@<endpoint>:5432/healthkicks?sslmode=require
```

## 2. App Runner configuration

| Setting                                                                           | Value                                                                          |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Source                                                                            | ECR (push the image built from this Dockerfile)                                |
| Port                                                                              | Leave as **Default** or set `8000` — the image honors the injected `PORT`      |
| CPU / Memory                                                                      | 0.25 vCPU / 0.5 GB to start                                                    |
| Env: `DATABASE_URL`                                                               | RDS URL above (from AWS Secrets Manager / SSM, referenced as a secret)         |
| Env: `MIGRATE_ON_START`                                                           | `true` (single-instance services) — runs `alembic upgrade head` before uvicorn |
| Env: `HEALTHKICKS_INGEST_TOKEN`, `HEALTHKICKS_JWT_SECRET`, `HEALTHKICKS_GOOGLE_*` | Secrets Manager references                                                     |
| Health check                                                                      | Path `/`, interval 10s — the image serves `/` immediately                      |

> **Multi-instance note:** `MIGRATE_ON_START=true` is safe only while the service runs one instance. For auto-scaled services, set it to `false` and run migrations as a separate one-off step (see §3).

## 3. Running migrations against RDS

The application image ships with Alembic and all migrations, so you can apply them from your machine without installing anything locally:

```powershell
# PowerShell
.\scripts\run_migrations.ps1 upgrade -Url "postgresql+psycopg2://user:pass@healthkicks.xxxx.eu-north-1.rds.amazonaws.com:5432/healthkicks?sslmode=require"
.\scripts\run_migrations.ps1 current -Url "..."
.\scripts\run_migrations.ps1 downgrade -1 -Url "..."
```

```bash
# Bash
DATABASE_URL="postgresql+psycopg2://user:pass@...?sslmode=require" ./scripts/run_migrations.sh upgrade
```

Both scripts:

- default to the `migrate` compose service (runs inside the same image that ships to production),
- support `-Service local` / `MIGRATION_SERVICE=local` to use the host `uv` venv instead of Docker,
- refuse to hit a remote Postgres without TLS (auto-append `sslmode=require`).

**Network access:** your machine needs to reach RDS on 5432. Either whitelist your IP in the RDS security group temporarily, or tunnel via SSM:

```powershell
aws ssm start-session --target <bastion-instance-id> --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters '{"host":["healthkicks.xxxx.eu-north-1.rds.amazonaws.com"],"portNumber":["5432"],"localPortNumber":["5432"]}'
# then point the script at localhost:5432
```

**Release flow (recommended):**

1. Build & push image → `docker build -t healthkicks-api . && docker tag ... && docker push ...`
2. Run `scripts/run_migrations.ps1 upgrade` against RDS (migrations are backward compatible).
3. Deploy the new image to App Runner (`aws apprunner start-deployment`).

This keeps migrations ahead of the code, so the old revision keeps working during rollout.

## 4. First admin bootstrap

After the first user signs in via Google SSO, promote them:

```powershell
# From your machine, using the migrate service as a DB-capable container:
.\scripts\run_migrations.ps1 -Command "upgrade" # (ensure migrations are applied first)

docker compose run --rm -e DATABASE_URL="<RDS url>" migrate python -m app.cli promote-admin --first
# or by email:
docker compose run --rm -e DATABASE_URL="<RDS url>" migrate python -m app.cli promote-admin ops@example.com
```

Against the local dev stack:

```powershell
docker compose exec api python -m app.cli promote-admin --first
```

## 5. Local integration testing

```powershell
docker compose up --build
# - `migrate` applies Alembic migrations to the local Postgres
# - `api` waits for migrations, then serves on http://localhost:8000
docker compose ps            # verify api is healthy
docker compose logs migrate  # review migration output
```

The local API runs with `HEALTHKICKS_AUTO_CREATE_TABLES=false`; schema is owned by Alembic in both environments.
