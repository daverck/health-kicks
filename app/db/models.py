"""SQLAlchemy persistence models for the Cloud API."""

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for application tables."""


class DeviceStatus(str, Enum):
    """Known connectivity states for a device."""

    online = "online"
    offline = "offline"


class FallStatus(str, Enum):
    """Processing state of a persisted event."""

    detected = "detected"
    acknowledged = "acknowledged"


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[DeviceStatus] = mapped_column(default=DeviceStatus.offline)
    last_seen_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class FallEvent(Base):
    __tablename__ = "fall_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="fall")
    timestamp_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_imu_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status_enum: Mapped[FallStatus] = mapped_column(default=FallStatus.detected)


class HapticLog(Base):
    __tablename__ = "haptic_commands_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    intensity: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    triggered_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    triggered_by_user: Mapped[bool] = mapped_column(Boolean, default=True)


class ProcessedMessage(Base):
    """Technical idempotency ledger for webhook message identifiers."""

    __tablename__ = "processed_messages"
    __table_args__ = (UniqueConstraint("msg_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    msg_id: Mapped[str] = mapped_column(String(255), nullable=False)