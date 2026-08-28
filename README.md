# HealthKicks Backend

API FastAPI Cloud pour une chaussure connectee HealthKicks : commandes haptiques,
historique des chutes et webhook d'ingestion AWS IoT.

## Architecture

- `app/main.py` assemble l'API Cloud stateless et ses dependances.
- `app/api/v1/` expose les routeurs Cloud et le webhook `/api/v1/ingest/event`.
- `app/db/database.py` fournit les sessions SQLAlchemy synchrones.
- `app/db/models.py` contient `Device`, `FallEvent`, `HapticLog` et le ledger idempotent.
- `app/schemas/` contient les contrats Pydantic stricts.
- `app/services/aws_iot_service.py` publie avec boto3 `iot-data` sans client MQTT persistant.
- `app/services/ingestion_service.py` valide et persiste les evenements AWS IoT.
- `app/core/config.py` charge `config.yaml` et applique les overrides d'environnement.

## Installation et lancement avec uv

```powershell
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

La configuration utilise `DATABASE_URL` avec une URL PostgreSQL,
ainsi que `AWS_REGION`, `AWS_IOT_ENDPOINT` et les credentials IAM. Le
defaut est PostgreSQL; SQLite n'est utilise que si `DATABASE_URL` le demande explicitement.
`GET /api/v1/health` execute un test DB et retourne
`ok` ou `degraded`.

## API Cloud

- `POST /api/v1/devices/{device_id}/haptic/trigger`
- `GET /api/v1/devices`
- `GET /api/v1/devices/{device_id}/events/falls?page=1&page_size=50`
- `GET /api/v1/health`
- `POST /api/v1/ingest/event`

Le webhook attend `X-HealthKicks-Ingest-Token` en production. Le corps doit contenir
`header` (`device_id`, `msg_id`, `timestamp` ou `timestamp_utc`) et `payload`
(`event_type`, `confidence_score`, `raw_imu_snapshot`). Les `msg_id` deja vus sont
acknowledges comme doublons sans creer de nouvelle ligne.

## Docker local

```powershell
docker compose up --build
```

## Tests

```powershell
uv run --group dev pytest -q
```

## Configuration

Les valeurs par defaut sont dans [config.yaml](config.yaml).
Le chemin peut etre remplace avec `HEALTHKICKS_CONFIG_FILE`.

Variables principales : `DATABASE_URL`, `HEALTHKICKS_INGEST_TOKEN`,
`HEALTHKICKS_ENVIRONMENT`, `AWS_REGION`, `AWS_IOT_ENDPOINT` et
`AWS_IOT_HAPTIC_COMMAND_TOPIC`.
