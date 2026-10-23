"""SQLAlchemy persistence models for the Cloud API."""

from datetime import UTC, date, datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
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
    firmware_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default="v1.2.0-esp32s3"
    )
    last_calibration_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
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
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
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
    google_sub: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    azure_sub: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    role: Mapped[UserRole] = mapped_column(default=UserRole.user)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_login_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_admin(self) -> bool:
        """Check if user has admin privileges."""
        return self.role == UserRole.admin

    @property
    def is_clinician_or_admin(self) -> bool:
        """Check if user has clinician or admin privileges."""
        return self.role in (UserRole.admin, UserRole.clinician)


class DeviceOwnership(Base):
    """Binding between a user account and an IoT device (future phase)."""

    __tablename__ = "device_ownership"
    __table_args__ = (UniqueConstraint("user_id", "device_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    bound_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class StudioSession(Base):
    """Recorded IMU data capture session for ML dataset curation."""

    __tablename__ = "studio_sessions"
    __table_args__ = (Index("ix_studio_sessions_created_at", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str] = mapped_column(String(64), index=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_sec: Mapped[float] = mapped_column(Float, default=5.0)
    is_validated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped["User"] = relationship("User", lazy="joined")


class DailyActivityStep(Base):
    """Daily step counts aggregated per activity type and device."""

    __tablename__ = "daily_activity_steps"
    __table_args__ = (
        UniqueConstraint("device_id", "date", "activity_type", name="uq_device_date_activity"),
        Index("ix_daily_activity_steps_user_date", "user_id", "date"),
        Index("ix_daily_activity_steps_device_date", "device_id", "date"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    activity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped["User"] = relationship("User", lazy="joined")


class HourlyActivityStep(Base):
    """Hourly step counts aggregated per activity type, hour and device."""

    __tablename__ = "hourly_activity_steps"
    __table_args__ = (
        UniqueConstraint(
            "device_id", "date", "hour", "activity_type", name="uq_device_date_hour_activity"
        ),
        Index("ix_hourly_activity_steps_user_date", "user_id", "date"),
        Index("ix_hourly_activity_steps_device_date", "device_id", "date"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    hour: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    activity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped["User"] = relationship("User", lazy="joined")


# --- Fall Alerting & Emergency Contacts (Step 5) ---


class FallAlertState(str, Enum):
    """Lifecycle states for a fall alert."""

    PENDING = "PENDING"
    ESCALATING = "ESCALATING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    CANCELLED = "CANCELLED"
    EXHAUSTED = "EXHAUSTED"
    SUPPRESSED = "SUPPRESSED"


class FallAlertAction(str, Enum):
    """Uplink MQTT actions for fall alert ingestion."""

    DETECTED = "DETECTED"
    CONFIRM = "CONFIRM"
    CANCEL = "CANCEL"
    SMS_STATUS = "SMS_STATUS"


class FallAlertDelivery(str, Enum):
    """Delivery mode indicating live active monitoring vs offline replay."""

    live = "live"
    replayed = "replayed"


class FallAlertCancelReason(str, Enum):
    """Reasons why a fall alert was cancelled."""

    user_cancel = "user_cancel"
    fall_recovery = "fall_recovery"


class FallAlertNotificationStatus(str, Enum):
    """Delivery status for an individual emergency contact notification (email or SMS)."""

    SENT = "SENT"
    PENDING_SMS = "PENDING_SMS"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FAILED = "FAILED"


class AlertContact(Base):
    """Emergency contact (proche) associated with a user account."""

    __tablename__ = "alert_contacts"
    __table_args__ = (
        UniqueConstraint("user_id", "email", name="uq_alert_contacts_user_email"),
        Index("ix_alert_contacts_user_priority", "user_id", "priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    last_test_email_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class FallAlert(Base):
    """Fall alert lifecycle record with sequential email and SMS escalation state."""

    __tablename__ = "fall_alerts"
    __table_args__ = (
        Index("ix_fall_alerts_state_next_action", "state", "next_action_at_utc"),
        Index("ix_fall_alerts_user_detected", "user_id", "detected_at_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    delivery: Mapped[str] = mapped_column(String(16), nullable=False, default="live")
    state: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    cancel_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_captured_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_contact_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contacts_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged_by_first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    acknowledged_by_last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    acknowledged_by_contact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detected_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    escalation_started_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_action_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    closed_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    user: Mapped["User"] = relationship("User", lazy="joined")
    notifications: Mapped[list["FallAlertNotification"]] = relationship(
        "FallAlertNotification",
        back_populates="alert",
        cascade="all, delete-orphan",
        order_by="FallAlertNotification.rank.asc(), FallAlertNotification.id.asc()",
    )


class FallAlertNotification(Base):
    """Audit trail of each notification (email or SMS) dispatched during fall alert escalation."""

    __tablename__ = "fall_alert_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("fall_alerts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, default="email", server_default="email"
    )
    contact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contact_first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    contact_last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    contact_email: Mapped[str] = mapped_column(String(320), nullable=False)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    acknowledged_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)

    alert: Mapped["FallAlert"] = relationship("FallAlert", back_populates="notifications")
