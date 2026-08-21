# HealthKicks Backend

Backend FastAPI pour une chaussure connectee HealthKicks : telemetrie IMU via MQTT, detection d'anomalies et retour haptique.

## Architecture

- `app/main.py` assemble FastAPI, les routeurs, le stockage et les services.
- `app/core/config.py` charge `config.yaml` et applique les overrides d'environnement.
- `app/core/aws_iot_client.py` gere AWS IoT Core, mTLS, les reconnexions et les messages.
- `app/controllers/` expose les endpoints REST.
- `app/models/` contient les schemas Pydantic.
- `app/services/` contient le stockage de telemetrie et Isolation Forest.
- `app/services/shadow_service.py` gere le Device Shadow AWS.
- `app/controllers/shadow_controller.py` expose l'etat du Shadow.
- `scripts/mock_sensor.py` publie des mesures normales et anormales.
- `scripts/mock_aws_sensor.py` publie directement vers AWS IoT Core.

## Installation avec uv

```powershell
uv sync
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

Variables AWS disponibles : `HEALTHKICKS_AWS_IOT_ENDPOINT`, `HEALTHKICKS_AWS_IOT_THING_NAME`, `HEALTHKICKS_AWS_IOT_CLIENT_ID`, `HEALTHKICKS_AWS_IOT_CERT_PATH`, `HEALTHKICKS_AWS_IOT_PRIVATE_KEY_PATH` et `HEALTHKICKS_AWS_IOT_ROOT_CA_PATH`. Les noms `AWS_IOT_*` restent compatibles.

Endpoints Shadow : `GET /api/aws/shadow` et `PATCH /api/aws/shadow` avec un payload comme `{"state": {"vibration_enabled": true, "sensibility_level": 70}}`.

Le modele attend les six champs IMU `ax`, `ay`, `az`, `gx`, `gy`, `gz`. Il se forme apres le warm-up initial, puis declenche une vibration lors d'une prediction d'anomalie si MQTT est connecte.
