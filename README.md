# HealthKicks Backend

API FastAPI Cloud pour une chaussure connectee HealthKicks : commandes haptiques,
historique des chutes et ingestion d'evenements normalises.

## Architecture

- `app/main.py` assemble l'API Cloud stateless et ses dependances.
- `app/controllers/cloud_controller.py` expose les routes `/api/v1`.
- `app/core/database.py` fournit les sessions SQLAlchemy synchrones.
- `app/models/database.py` contient `Device`, `FallEvent` et `HapticLog`.
- `app/services/aws_iot_publish_service.py` publie avec boto3 `iot-data` sans client MQTT persistant.
- `app/services/ingestion_service.py` est la frontiere callable d'ingestion des chutes.
- `app/core/config.py` charge `config.yaml` et applique les overrides d'environnement.
- `app/services/aws_iot_publish_service.py` utilise boto3 `iot-data` sans client MQTT persistant.
- `app/controllers/` expose uniquement le routeur Cloud v1 dans `app.main`.
- `app/models/` contient les schemas Pydantic.
- `app/services/` contient le stockage de telemetrie et Isolation Forest.
- `app/services/shadow_service.py` gere le Device Shadow AWS.
- `app/controllers/shadow_controller.py` expose l'etat du Shadow.
- `scripts/mock_sensor.py` publie des mesures normales et anormales.
- `scripts/mock_aws_sensor.py` publie directement vers AWS IoT Core.

## Installation et lancement avec uv

```powershell
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

La configuration de production utilise `DATABASE_URL` avec une URL PostgreSQL,
ainsi que `AWS_REGION`, `AWS_IOT_ENDPOINT` et les credentials IAM du role App Runner. Le
defaut local est SQLite. `GET /api/v1/health` execute un test DB et retourne
`ok` ou `degraded`.

## API Cloud

- `POST /api/v1/devices/{device_id}/haptic/trigger`
- `GET /api/v1/devices`
- `GET /api/v1/devices/{device_id}/events/falls?page=1&page_size=50`
- `GET /api/v1/health`

App Runner ne consomme pas directement MQTT : AWS IoT Core doit router les
messages vers SQS, Lambda ou un worker qui appelle `ingest_fall_event`. Cette
architecture evite toute connexion MQTT persistante dans le processus HTTP.

## Docker local

```powershell
docker compose up --build
```

## Tests

```powershell
uv run --group dev pytest -q
```

Le test AWS IoT reel utilise mTLS et le client `healthkicks-backend`. Il est
marque comme integration pour que la suite locale ne depende pas du reseau :

```powershell
uv run --group dev pytest -q --run-integration tests/test_aws_iot.py::test_real_aws_connection_healthkicks_topics -s
```

Ce test publie une telemetrie sur `healthkicks/devices/health_kicks/telemetry/imu`, attend sa reception, puis se deconnecte. La policy attachee au certificat doit autoriser `iot:Connect` pour `client/healthkicks-backend`, ainsi que `iot:Publish`, `iot:Subscribe` et `iot:Receive` sur les topics `healthkicks/*`.

## Lancement de l'API

```powershell
uv run uvicorn app.main:app --reload
```

L'ancien lancement `uv run uvicorn main:app --reload` reste compatible.

## AWS IoT Core et certificats

Place les fichiers mTLS fournis par AWS IoT dans `certs/` :

- `certificate.pem.crt`
- `private.pem.key`
- `AmazonRootCA1.pem`

Le repertoire `certs/` est exclu de Git. Configure `endpoint` et `thing_name` dans [config.yaml](config.yaml), ou utilise les variables `AWS_IOT_*`.
Le Thing AWS doit autoriser les topics de telemetrie, haptique et Shadow utilises par l'application. Le handshake AWS est limite par `connect_timeout_seconds` (15 secondes par defaut).

## Simulateur AWS

```powershell
uv run python -m scripts.mock_aws_sensor
```

## Ancien simulateur local

Un broker MQTT local doit etre accessible sur `localhost:1883`.

```powershell
uv run python -m scripts.mock_sensor
```

Options utiles : `--broker`, `--port`, `--anomaly-interval` et `--sample-interval`.

## Configuration

Les valeurs par defaut sont dans [config.yaml](config.yaml), organise par sections `mqtt`, `telemetry` et `ai`.
Le chemin peut etre remplace avec `HEALTHKICKS_CONFIG_FILE`.

Les variables prefixees par `HEALTHKICKS_` sont prioritaires sur le YAML. Les anciennes variables `SMARTSTRIDE_*` restent acceptees comme alias de migration :

- `HEALTHKICKS_MQTT_BROKER`, `HEALTHKICKS_MQTT_PORT`
- `HEALTHKICKS_MQTT_TELEMETRY_TOPIC`, `HEALTHKICKS_MQTT_HAPTIC_TOPIC`
- `HEALTHKICKS_AI_TRAINING_WINDOW`, `HEALTHKICKS_AI_MIN_TRAINING_SAMPLES`
- `HEALTHKICKS_AI_RETRAIN_INTERVAL`
- `HEALTHKICKS_AI_CONTAMINATION`, `HEALTHKICKS_AI_HAPTIC_COOLDOWN_SECONDS`

Variables AWS disponibles : `AWS_REGION`, `AWS_IOT_ENDPOINT`, `AWS_IOT_HAPTIC_COMMAND_TOPIC`, ainsi que leurs variantes `HEALTHKICKS_AWS_IOT_*`. Le template haptique par defaut est `healthkicks/v1/{device_id}/commands/haptic`.

Endpoints Shadow : `GET /api/aws/shadow` et `PATCH /api/aws/shadow` avec un payload comme `{"state": {"vibration_enabled": true, "sensibility_level": 70}}`.

Le contrat HTTP expose `/api/v1`, ainsi que `/` et `/api/v1/health`. Les anciens
routeurs telemetry et haptic ne sont pas montes dans l'application Cloud.
La creation automatique du schema est active en local ; desactive-la en production
avec `HEALTHKICKS_AUTO_CREATE_TABLES=false` et applique des migrations versionnees.
