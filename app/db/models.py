"""SQLAlchemy persistence models for the Cloud API."""

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for application tables."""


class DeviceStatus(str, Enum):
    """Known connectivity states for a device."""

    online = "online"
    offline = "offline"


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


class ActivityEvent(Base):
    __tablename__ = "activity_events"
    __table_args__ = (
        Index("ix_activity_events_device_id_timestamp_utc", "device_id", "timestamp_utc"),
        Index("ix_activity_events_device_id_event_type", "device_id", "event_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="walk")
    timestamp_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)


class HapticLog(Base):
    __tablename__ = "haptic_commands_log"
    __table_args__ = (
        Index("ix_haptic_log_device_id_triggered_at", "device_id", "triggered_at_utc"),
    )

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

# --- Auth & user management (Step 2) ---


class UserRole(str, Enum):
    """Role hierarchy for authorization checks."""

    admin = "admin"
    clinician = "clinician"
    user = "user"


class User(Base):
    """User account provisioned via Google or Microsoft Entra ID (Azure AD) SSO."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    azure_sub: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    role: Mapped[UserRole] = mapped_column(default=UserRole.user)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_login_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceOwnership(Base):
    """Binding between a user account and an IoT device (future phase)."""

    __tablename__ = "device_ownership"
    __table_args__ = (UniqueConstraint("user_id", "device_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    bound_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class StudioSession(Base):
    """Recorded IMU data capture session for ML dataset curation."""

    __tablename__ = "studio_sessions"
    __table_args__ = (
        Index("ix_studio_sessions_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str] = mapped_column(String(64), index=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_sec: Mapped[float] = mapped_column(Float, default=5.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    user: Mapped["User"] = relationship("User", lazy="joined")

