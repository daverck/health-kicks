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

A complete local ML training, benchmarking, and embedded transpilation pipeline is available to train lightweight classifiers and automatically deploy them to the ESP32-S3 firmware.

### 1. Unified End-to-End MLOps Pipeline (`scripts.update_edge_model`)

The [`scripts/update_edge_model.py`](scripts/update_edge_model.py) script automates the complete model lifecycle in a single command:
1. **Incremental Data Ingestion**: Synchronizes validated Studio sessions from PostgreSQL and DynamoDB.
2. **Cross-Validation Training**: Benchmarks candidate algorithms via 5-Fold Stratified Cross-Validation optimizing for macro F1-score.
3. **Quality Gate Verification**: Enforces minimum F1-score standards (default: $\ge 85\%$).
4. **C Transpilation**: Converts the champion model into embedded C code (`include/activity_model_generated.h`) via `m2cgen`.
5. **Firmware Integrity Check**: Automatically verifies ESP32-S3 firmware compilation via PlatformIO.

```powershell
# Install ML dependencies in virtualenv
uv sync --group ml

# Authenticate with AWS CLI for live telemetry data
aws sso login   # or aws login / aws configure

# Full end-to-end sync, train, export to C, and firmware build check
uv run python -m scripts.update_edge_model

# Fast offline run with synthetic dataset (no AWS or database connection required)
uv run python -m scripts.update_edge_model --synthetic

# Skip retraining and re-transpile existing model artifact directly
uv run python -m scripts.update_edge_model --skip-train

# Export to C header without running the PlatformIO build check
uv run python -m scripts.update_edge_model --no-build-check
```

### 2. Standalone Training & Hyperparameter Tuning (`scripts.train_detector`)

For detailed model comparison, feature extraction tuning, or custom cross-validation:

```powershell
# Train classifiers with incremental PostgreSQL & DynamoDB caching
uv run python -m scripts.train_detector

# Synthetic offline training
uv run python -m scripts.train_detector --synthetic
```

For full architecture and parameters details, refer to the [Machine Learning Training Guide](docs/ML_TRAINING.md).


