# HealthKicks Backend

FastAPI Cloud API for the **HealthKicks** connected smart shoe: remote haptic stimulation commands, activity event tracking, AWS IoT ingestion webhook, and Studio telemetry curation.

---

## Architecture

- `app/main.py`: Assembles the stateless FastAPI Cloud API, lifecycle hooks, and database migrations.
- `app/api/v1/`: Exposes REST endpoints (authentication, devices, haptics, activities, studio sessions, and the `/api/v1/ingest/event` webhook).
- `app/db/database.py`: Provides synchronous SQLAlchemy engine sessions, connection pool resilience, and automatic AWS RDS IAM DB authentication when enabled.
- `app/db/models.py`: Declares database persistence models (`Device`, `DeviceOwnership`, `ActivityEvent`, `HapticLog`, `User`, `StudioSession`, and the idempotent message ledger).
- `app/schemas/`: Contains strict Pydantic validation contracts and DTO schemas.
- `app/services/aws_iot_service.py`: Dispatches AWS IoT MQTT commands via boto3 `iot-data` without a persistent MQTT client.
- `app/services/telemetry_service.py`: Interfaces with Amazon DynamoDB for high-frequency IMU telemetry storage, time-range queries, and dataset aggregation.
- `app/services/ingestion_service.py`: Validates and persists incoming edge sensor events.
- `app/core/config.py`: Loads `config.yaml` and handles environment variable overrides.

---

## Installation & Running with uv

```powershell
# Install dependencies
uv sync

# Run development server
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The application defaults to PostgreSQL specified via `DATABASE_URL` (with optional `USE_RDS_IAM=true` for token-based RDS IAM authentication).
The health endpoint `GET /api/v1/health` verifies database connectivity and returns `status: "ok"` or `"degraded"`.

---

## REST Endpoints Overview

- `POST /api/v1/auth/google/callback`: Exchange Google OAuth2 authorization code for JWT tokens.
- `POST /api/v1/auth/azure/callback`: Exchange Microsoft Entra ID code for JWT tokens.
- `POST /api/v1/auth/refresh`: Refresh expired access token with token rotation.
- `GET /api/v1/devices`: List registered devices (or user-bound devices).
- `POST /api/v1/devices/{device_id}/haptic/trigger`: Trigger remote haptic vibration command.
- `GET /api/v1/devices/{device_id}/events/activities`: Paginated list of detected activity events.
- `GET /api/v1/studio/sessions`: Paginated historical studio capture sessions with RBAC and label filters.
- `GET /api/v1/studio/sessions/{id}/readings`: Fetch raw IMU sensor frames from DynamoDB for Chart.js inspection.
- `PATCH /api/v1/studio/sessions/{id}`: Reclassify studio session label in PostgreSQL and DynamoDB.
- `DELETE /api/v1/studio/sessions/{id}`: Purge studio session and delete all raw telemetry frames in DynamoDB.
- `POST /api/v1/ingest/event`: Ingestion webhook for AWS IoT rule engine (requires `X-HealthKicks-Ingest-Token`).

---

## Local Docker Stack

```powershell
docker compose up --build
```

Runs a local PostgreSQL 16 instance, applies Alembic migrations, and launches the FastAPI application.

---

## Running Tests

```powershell
uv run pytest
```

---

## Configuration

Default values are stored in [config.yaml](config.yaml).
The configuration file path can be customized with `HEALTHKICKS_CONFIG_FILE`.

Key environment variables:
- `DATABASE_URL`: PostgreSQL connection string.
- `USE_RDS_IAM`: Set to `true` when running on AWS with IAM role authentication.
- `HEALTHKICKS_INGEST_TOKEN`: Shared secret for the event ingestion endpoint.
- `HEALTHKICKS_JWT_SECRET`: Signing secret for JWT access tokens.
- `AWS_REGION`: AWS target region (default: `eu-north-1`).
- `AWS_IOT_ENDPOINT`: AWS IoT Core REST endpoint for publishing commands.
- `DYNAMODB_TELEMETRY_TABLE`: DynamoDB table name (default: `healthkicks_telemetry`).

---

## Machine Learning (Edge Activity Classification & Fall Detection)

A complete local ML training and evaluation pipeline is available to train and compare lightweight classifiers for the embedded Edge service:

```powershell
# Install optional Data Science / ML dependencies
uv sync --group ml

# Authenticate with AWS CLI for live DynamoDB telemetry data
aws sso login   # or aws login / aws configure

# Train classifiers with incremental PostgreSQL & DynamoDB caching (concurrent batch downloads)
uv run python -m scripts.train_detector

# Or run in offline synthetic demo mode (no AWS or database connection required)
uv run python -m scripts.train_detector --synthetic
```

For full details, refer to the [Machine Learning Training Guide](docs/ML_TRAINING.md).

