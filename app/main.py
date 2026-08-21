"""FastAPI application entry point."""

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.controllers.haptic_controller import create_router as create_haptic_router
from app.controllers.shadow_controller import create_router as create_shadow_router
from app.controllers.telemetry_controller import create_router as create_telemetry_router
from app.core.aws_iot_client import AWSIoTClient
from app.core.config import settings
from app.models.haptic_model import HapticCommand
from app.models.telemetry_model import IMUTelemetry
from app.services.ai_service import AIService
from app.services.shadow_service import ShadowService
from app.services.telemetry_service import TelemetryService

logging.basicConfig(level=logging.INFO)

telemetry_service = TelemetryService(settings.telemetry_history_size)
aws_iot_client = AWSIoTClient(settings)
ai_service = AIService(
    aws_iot_client.publish_haptic,
    training_window=settings.ai_training_window,
    min_training_samples=settings.ai_min_training_samples,
    retrain_interval=settings.ai_retrain_interval,
    contamination=settings.ai_contamination,
    haptic_cooldown_seconds=settings.ai_haptic_cooldown_seconds,
)
shadow_service = ShadowService(aws_iot_client, settings)


def handle_telemetry(telemetry: IMUTelemetry) -> None:
    """Store telemetry and run anomaly inference for each MQTT packet."""
    telemetry_service.record(telemetry)
    ai_service.process(telemetry)


aws_iot_client.set_telemetry_handler(handle_telemetry)


def handle_cloud_haptic(command: HapticCommand) -> None:
    """Forward a C2D haptic command to the local actuator boundary."""
    logging.getLogger(__name__).info(
        "Local haptic actuator command received: intensity=%s duration_ms=%s",
        command.intensity,
        command.duration_ms,
    )


aws_iot_client.set_haptic_handler(handle_cloud_haptic)
aws_iot_client.subscribe(
    settings.aws_iot_telemetry_topic,
    aws_iot_client.handle_telemetry_message,
)
aws_iot_client.subscribe(
    settings.aws_iot_haptic_command_topic,
    aws_iot_client.handle_haptic_message,
)
shadow_service.set_desired_state_handler(
    lambda desired: ai_service.update_configuration(
        desired.get("anomaly_contamination")
    )
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start and stop infrastructure alongside the FastAPI application."""
    try:
        aws_iot_client.start()
        shadow_service.start()
    except Exception:
        logging.getLogger(__name__).exception(
            "AWS IoT Core unavailable; API remains in offline mode"
        )
    try:
        yield
    finally:
        aws_iot_client.stop()


app = FastAPI(
    title="HealthKicks API",
    description="API Gateway IoT pour l'analyse biomécanique et le contrôle haptique",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(create_telemetry_router(telemetry_service))
app.include_router(create_haptic_router(aws_iot_client))
app.include_router(create_shadow_router(shadow_service))


@app.get("/", tags=["Health"])
def read_root() -> dict[str, str]:
    """Return the API health status."""
    return {"status": "online", "system": "HealthKicks Gateway"}
