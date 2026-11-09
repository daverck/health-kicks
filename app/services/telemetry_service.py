"""Stateless DynamoDB telemetry data access service."""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.db.models import DeviceOwnership, StudioSession, User, UserRole
from app.schemas.telemetry import (
    ImuReadingResponse,
    StudioDatasetStatsResponse,
    StudioSessionReadingsResponse,
)

logger = logging.getLogger(__name__)


def _decimal_to_float(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value) if value is not None else 0.0


def _item_to_reading(item: dict[str, Any]) -> ImuReadingResponse:
    ts_us = int(item["timestamp"])
    ts_iso = datetime.fromtimestamp(ts_us / 1_000_000, tz=UTC)
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

    def trim_session_readings(
        self,
        device_id: str,
        session_id: str,
        start_offset_sec: float,
        end_offset_sec: float,
    ) -> int:
        """Keep readings inside the requested interval and return the retained count."""
        table = self._get_table()
        items: list[dict[str, Any]] = []
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
                    "DynamoDB query failed while trimming session %s: %s",
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
            return 0

        items.sort(key=lambda item: int(item["timestamp"]))
        first_timestamp = int(items[0]["timestamp"])
        start_timestamp = first_timestamp + round(start_offset_sec * 1_000_000)
        end_timestamp = first_timestamp + round(end_offset_sec * 1_000_000)
        items_to_delete = [
            item
            for item in items
            if int(item["timestamp"]) < start_timestamp
            or int(item["timestamp"]) > end_timestamp
        ]

        if items_to_delete:
            with table.batch_writer() as batch:
                for item in items_to_delete:
                    batch.delete_item(
                        Key={
                            "device_id": item["device_id"],
                            "timestamp": item["timestamp"],
                        }
                    )

        retained_count = len(items) - len(items_to_delete)
        logger.info(
            "Trimmed session %s to interval %.3f-%.3f seconds, retaining %d telemetry points",
            session_id,
            start_offset_sec,
            end_offset_sec,
            retained_count,
        )
        return retained_count

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

    def get_dataset_stats(
        self,
        device_id: str | None = None,
    ) -> StudioDatasetStatsResponse:
        """Aggregate recorded studio sessions by label and compute total sessions."""
        table = self._get_table()
        items: list[dict[str, Any]] = []
        filter_expr = Attr("session_id").exists() & Attr("label").exists()
        proj_expr = "session_id, #lbl"
        attr_names = {"#lbl": "label"}

        if device_id:
            query_kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("device_id").eq(device_id),
                "FilterExpression": filter_expr,
                "ProjectionExpression": proj_expr,
                "ExpressionAttributeNames": attr_names,
            }
            fetch_fn = table.query
        else:
            query_kwargs = {
                "FilterExpression": filter_expr,
                "ProjectionExpression": proj_expr,
                "ExpressionAttributeNames": attr_names,
            }
            fetch_fn = table.scan

        while True:
            try:
                response = fetch_fn(**query_kwargs)
            except (BotoCoreError, ClientError) as error:
                logger.error(
                    "DynamoDB stats query failed for device %s: %s",
                    device_id,
                    error,
                )
                raise

            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key

        sessions: dict[str, str] = {}
        for item in items:
            s_id = item.get("session_id")
            lbl = item.get("label")
            if s_id and lbl:
                sessions[str(s_id)] = str(lbl)

        by_label: dict[str, int] = {}
        for lbl in sessions.values():
            by_label[lbl] = by_label.get(lbl, 0) + 1

        return StudioDatasetStatsResponse(
            device_id=device_id,
            total_sessions=len(sessions),
            by_label=by_label,
        )

    def update_session_label(
        self,
        device_id: str,
        session_id: str,
        new_label: str,
    ) -> int:
        """Update the label of all readings belonging to a Studio session in DynamoDB."""
        table = self._get_table()
        items_to_update: list[dict[str, Any]] = []
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
                    "DynamoDB query failed while updating session %s label: %s",
                    session_id,
                    error,
                )
                raise

            items_to_update.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key

        if not items_to_update:
            return 0

        for item in items_to_update:
            try:
                table.update_item(
                    Key={
                        "device_id": item["device_id"],
                        "timestamp": item["timestamp"],
                    },
                    UpdateExpression="SET #lbl = :new_label",
                    ExpressionAttributeNames={"#lbl": "label"},
                    ExpressionAttributeValues={":new_label": new_label},
                )
            except (BotoCoreError, ClientError) as error:
                logger.error(
                    "DynamoDB update_item failed for device %s ts %s: %s",
                    item["device_id"],
                    item["timestamp"],
                    error,
                )
                raise

        logger.info(
            "Updated label to '%s' for %d readings of device %s session %s",
            new_label,
            len(items_to_update),
            device_id,
            session_id,
        )
        return len(items_to_update)

    def query_device_telemetry(
        self,
        db: Session,
        user: User,
        device_id: str,
        session_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 1000,
    ) -> StudioSessionReadingsResponse | list[ImuReadingResponse]:
        """Query IMU telemetry with temporal isolation and session ownership checks."""
        bound_at_utc: datetime | None = None
        if user.role == UserRole.user:
            ownership = (
                db.query(DeviceOwnership.bound_at_utc)
                .filter(DeviceOwnership.device_id == device_id, DeviceOwnership.user_id == user.id)
                .first()
            )
            if ownership:
                bound_at_utc = ownership[0]

        if session_id is not None:
            if user.role == UserRole.user and bound_at_utc is not None:
                try:
                    session_uuid = uuid.UUID(session_id)
                    sess = db.query(StudioSession).filter_by(uuid=session_uuid, device_id=device_id).first()
                    if sess is not None:
                        sess_created = sess.created_at
                        b_cmp = bound_at_utc
                        if sess_created.tzinfo is not None and b_cmp.tzinfo is None:
                            b_cmp = b_cmp.replace(tzinfo=sess_created.tzinfo)
                        elif sess_created.tzinfo is None and b_cmp.tzinfo is not None:
                            sess_created = sess_created.replace(tzinfo=b_cmp.tzinfo)
                        if sess_created < b_cmp and sess.user_id != user.id:
                            raise HTTPException(
                                status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Studio session '{session_id}' not found for device '{device_id}'",
                            )
                except ValueError:
                    pass

            result = self.get_session_readings(
                device_id=device_id,
                session_id=session_id,
            )
            if result is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Studio session '{session_id}' not found for device '{device_id}'",
                )
            return result

        if start_time is not None and end_time is not None:
            if start_time > end_time:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="start_time must be less than or equal to end_time",
                )

            # Temporal isolation for regular users
            if user.role == UserRole.user and bound_at_utc is not None:
                b_cmp = bound_at_utc
                if start_time.tzinfo is not None and b_cmp.tzinfo is None:
                    b_cmp = b_cmp.replace(tzinfo=start_time.tzinfo)
                elif start_time.tzinfo is None and b_cmp.tzinfo is not None:
                    b_cmp = b_cmp.replace(tzinfo=None)

                end_cmp = end_time
                if end_time.tzinfo is None and b_cmp.tzinfo is not None:
                    end_cmp = end_time.replace(tzinfo=b_cmp.tzinfo)
                elif end_time.tzinfo is not None and b_cmp.tzinfo is None:
                    end_cmp = end_time.replace(tzinfo=None)

                if end_cmp < b_cmp:
                    return []
                start_time = max(start_time, b_cmp)

            start_us = int(start_time.timestamp() * 1_000_000)
            end_us = int(end_time.timestamp() * 1_000_000)
            return self.get_timerange_readings(
                device_id=device_id,
                start_epoch_us=start_us,
                end_epoch_us=end_us,
                limit=limit,
            )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide either session_id or both start_time and end_time",
        )

