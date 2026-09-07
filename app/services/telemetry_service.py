"""Stateless DynamoDB telemetry data access service."""

from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings, settings
from app.schemas.telemetry import ImuReadingResponse, StudioSessionReadingsResponse

logger = logging.getLogger(__name__)


def _decimal_to_float(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value) if value is not None else 0.0


def _item_to_reading(item: dict[str, Any]) -> ImuReadingResponse:
    ts_us = int(item["timestamp"])
    ts_iso = datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc)
    return ImuReadingResponse(
        timestamp_epoch_us=ts_us,
        timestamp_iso=ts_iso,
        ax=_decimal_to_float(item.get("ax")),
        ay=_decimal_to_float(item.get("ay")),
        az=_decimal_to_float(item.get("az")),
        gx=_decimal_to_float(item.get("gx")),
        gy=_decimal_to_float(item.get("gy")),
        gz=_decimal_to_float(item.get("gz")),
        session_id=item.get("session_id"),
        label=item.get("label"),
    )


class TelemetryService:
    """Service for querying and purging IMU telemetry in DynamoDB."""

    def __init__(
        self,
        table: Any | None = None,
        config: Settings = settings,
    ) -> None:
        self._table = table
        self._config = config

    def _get_table(self) -> Any:
        if self._table is None:
            dynamodb = boto3.resource("dynamodb", region_name=self._config.aws_region)
            return dynamodb.Table(self._config.dynamodb_telemetry_table)
        return self._table

    def get_session_readings(
        self,
        device_id: str,
        session_id: str,
    ) -> StudioSessionReadingsResponse | None:
        """Query IMU telemetry points for a specific Studio session."""
        table = self._get_table()
        items: list[dict[str, Any]] = []
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("device_id").eq(device_id),
            "FilterExpression": Attr("session_id").eq(session_id),
        }

        while True:
            try:
                response = table.query(**query_kwargs)
            except (BotoCoreError, ClientError) as error:
                logger.error(
                    "DynamoDB query failed for device %s session %s: %s",
                    device_id,
                    session_id,
                    error,
                )
                raise

            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key

        if not items:
            return None

        # Sort readings in ascending order of timestamp
        items.sort(key=lambda x: int(x["timestamp"]))

        # Extract label if present on any of the session readings
        session_label = next(
            (item["label"] for item in items if item.get("label")),
            None,
        )

        readings = [_item_to_reading(item) for item in items]
        return StudioSessionReadingsResponse(
            device_id=device_id,
            session_id=session_id,
            label=session_label,
            sample_count=len(readings),
            readings=readings,
        )

    def delete_session_readings(self, device_id: str, session_id: str) -> int:
        """Purge all telemetry points associated with a Studio session."""
        table = self._get_table()
        items_to_delete: list[dict[str, Any]] = []
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("device_id").eq(device_id),
            "FilterExpression": Attr("session_id").eq(session_id),
            "ProjectionExpression": "device_id, #ts",
            "ExpressionAttributeNames": {"#ts": "timestamp"},
        }

        while True:
            try:
                response = table.query(**query_kwargs)
            except (BotoCoreError, ClientError) as error:
                logger.error(
                    "DynamoDB query for deletion failed for device %s session %s: %s",
                    device_id,
                    session_id,
                    error,
                )
                raise

            items_to_delete.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key

        if not items_to_delete:
            return 0

        with table.batch_writer() as batch:
            for item in items_to_delete:
                batch.delete_item(
                    Key={
                        "device_id": item["device_id"],
                        "timestamp": item["timestamp"],
                    }
                )

        logger.info(
            "Deleted %d telemetry points for device %s session %s",
            len(items_to_delete),
            device_id,
            session_id,
        )
        return len(items_to_delete)

    def get_timerange_readings(
        self,
        device_id: str,
        start_epoch_us: int,
        end_epoch_us: int,
        limit: int = 1000,
    ) -> list[ImuReadingResponse]:
        """Query IMU telemetry points for a device between two timestamps."""
        table = self._get_table()
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("device_id").eq(device_id)
            & Key("timestamp").between(start_epoch_us, end_epoch_us),
            "Limit": limit,
            "ScanIndexForward": True,
        }

        try:
            response = table.query(**query_kwargs)
        except (BotoCoreError, ClientError) as error:
            logger.error("DynamoDB timerange query failed for device %s: %s", device_id, error)
            raise

        items = response.get("Items", [])
        items.sort(key=lambda x: int(x["timestamp"]))
        return [_item_to_reading(item) for item in items]
