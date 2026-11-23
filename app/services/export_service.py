"""Domain service for generating GDPR Article 20 data portability ZIP archives."""

import io
import json
import logging
import uuid
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.db.models import (
    AlertContact,
    DailyActivityStep,
    Device,
    DeviceOwnership,
    FallAlert,
    HourlyActivityStep,
    StudioSession,
    User,
)
from app.schemas.export import (
    ActivityStepsSummary,
    AlertContactExport,
    DailyStepSummary,
    DeviceExport,
    ExportMetadata,
    FallAlertExport,
    FallAlertsSummary,
    FallNotificationExport,
    HourlyStepExport,
    MonthlyStepsExport,
    StudioSessionDetailExport,
    StudioSessionSummaryExport,
    UserProfileExport,
)

logger = logging.getLogger(__name__)


def _to_json_bytes(data: Any) -> bytes:
    """Serialize a Pydantic model, dictionary, or list to formatted UTF-8 JSON bytes."""
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    elif isinstance(data, list):
        data = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in data
        ]
    return json.dumps(data, indent=2, ensure_ascii=False, default=str).encode("utf-8")


def build_profile_export(user: User) -> UserProfileExport:
    """Serialize user profile metadata."""
    return UserProfileExport(
        uuid=user.uuid,
        pseudonym=user.pseudonym,
        email=user.email,
        name=user.name,
        avatar_url=user.avatar_url,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_utc=user.last_login_utc,
    )


def build_alert_contacts_export(db: Session, user_id: int) -> list[AlertContactExport]:
    """Retrieve and serialize emergency alert contacts."""
    contacts = (
        db.query(AlertContact)
        .filter(AlertContact.user_id == user_id)
        .order_by(AlertContact.priority.asc(), AlertContact.created_at.asc())
        .all()
    )
    return [
        AlertContactExport(
            uuid=c.uuid,
            first_name=c.first_name,
            last_name=c.last_name,
            email=c.email,
            phone=c.phone,
            priority=c.priority,
            last_test_email_at=c.last_test_email_at,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in contacts
    ]


def build_devices_export(db: Session, user_id: int) -> list[DeviceExport]:
    """Retrieve and serialize bound IoT devices."""
    bindings = (
        db.query(DeviceOwnership, Device)
        .join(Device, DeviceOwnership.device_id == Device.device_id)
        .filter(DeviceOwnership.user_id == user_id)
        .order_by(DeviceOwnership.bound_at_utc.asc())
        .all()
    )
    devices: list[DeviceExport] = []
    for ownership, device in bindings:
        devices.append(
            DeviceExport(
                device_id=device.device_id,
                name=device.name,
                status=device.status.value
                if hasattr(device.status, "value")
                else str(device.status),
                firmware_version=device.firmware_version,
                bound_at_utc=ownership.bound_at_utc,
                last_seen_utc=device.last_seen_utc,
            )
        )
    return devices


def build_fall_alerts_export(
    db: Session, user_id: int
) -> tuple[FallAlertsSummary, dict[int, list[FallAlertExport]]]:
    """Retrieve and group fall alerts by calendar year."""
    alerts = (
        db.query(FallAlert)
        .options(joinedload(FallAlert.notifications))
        .filter(FallAlert.user_id == user_id)
        .order_by(FallAlert.detected_at_utc.asc())
        .all()
    )

    by_year: dict[int, list[FallAlertExport]] = defaultdict(list)
    state_counts: dict[str, int] = defaultdict(int)

    for alert in alerts:
        state_str = alert.state.value if hasattr(alert.state, "value") else str(alert.state)
        delivery_str = (
            alert.delivery.value if hasattr(alert.delivery, "value") else str(alert.delivery)
        )
        cancel_str = (
            alert.cancel_reason.value
            if alert.cancel_reason and hasattr(alert.cancel_reason, "value")
            else str(alert.cancel_reason)
            if alert.cancel_reason
            else None
        )
        state_counts[state_str] += 1

        notifications = [
            FallNotificationExport(
                notification_uuid=getattr(n, "uuid", None) or uuid.uuid4(),
                contact_email=n.contact_email,
                contact_phone=n.contact_phone,
                status=n.status.value if hasattr(n.status, "value") else str(n.status),
                sent_at_utc=n.sent_at_utc,
                acknowledged_at_utc=n.acknowledged_at_utc,
                error_message=n.error_message,
            )
            for n in getattr(alert, "notifications", [])
        ]

        item = FallAlertExport(
            uuid=alert.uuid,
            event_id=alert.event_id,
            state=state_str,
            event_type=alert.event_type,
            confidence=alert.confidence_score,
            delivery=delivery_str,
            location={
                "lat": alert.lat,
                "lon": alert.lon,
                "accuracy_m": alert.accuracy_m,
                "captured_at_utc": alert.location_captured_at_utc,
            }
            if alert.lat is not None and alert.lon is not None
            else None,
            detected_at_utc=alert.detected_at_utc,
            resolved_at_utc=alert.closed_at_utc,
            cancel_reason=cancel_str,
            notifications=notifications,
        )

        year = alert.detected_at_utc.year
        by_year[year].append(item)

    summary = FallAlertsSummary(
        total_alerts=len(alerts),
        by_state=dict(state_counts),
        active_years=sorted(by_year.keys()),
    )
    return summary, dict(by_year)


def build_activity_steps_export(
    db: Session, user_id: int
) -> tuple[ActivityStepsSummary, dict[str, MonthlyStepsExport]]:
    """Retrieve daily and hourly step aggregates grouped by active month."""
    daily_records = (
        db.query(DailyActivityStep)
        .filter(DailyActivityStep.user_id == user_id)
        .order_by(DailyActivityStep.date.asc())
        .all()
    )
    hourly_records = (
        db.query(HourlyActivityStep)
        .filter(HourlyActivityStep.user_id == user_id)
        .order_by(HourlyActivityStep.date.asc(), HourlyActivityStep.hour.asc())
        .all()
    )

    # Group hourly records by (date, device_id, activity_type)
    hourly_map: dict[tuple[Any, str, str], list[HourlyStepExport]] = defaultdict(list)
    for h in hourly_records:
        hourly_map[(h.date, h.device_id, h.activity_type)].append(
            HourlyStepExport(
                hour=h.hour,
                activity_type=h.activity_type,
                step_count=h.step_count,
                device_id=h.device_id,
                updated_at=h.updated_at,
            )
        )

    # Group daily records by YYYY-MM
    months_dict: dict[str, list[DailyStepSummary]] = defaultdict(list)
    monthly_activity_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    lifetime_activity_counts: dict[str, int] = defaultdict(int)
    total_lifetime_steps = 0

    for d in daily_records:
        month_str = d.date.strftime("%Y-%m")
        hours = hourly_map.get((d.date, d.device_id, d.activity_type), [])
        summary_item = DailyStepSummary(
            date=d.date,
            device_id=d.device_id,
            activity_type=d.activity_type,
            total_steps=d.step_count,
            updated_at=d.updated_at,
            hourly_breakdown=hours,
        )
        months_dict[month_str].append(summary_item)
        monthly_activity_counts[month_str][d.activity_type] += d.step_count
        lifetime_activity_counts[d.activity_type] += d.step_count
        total_lifetime_steps += d.step_count

    monthly_exports: dict[str, MonthlyStepsExport] = {}
    for month_str, items in months_dict.items():
        unique_days = len({item.date for item in items})
        month_total = sum(item.total_steps for item in items)
        monthly_exports[month_str] = MonthlyStepsExport(
            month=month_str,
            total_steps=month_total,
            days_recorded=unique_days,
            by_activity=dict(monthly_activity_counts[month_str]),
            daily_records=items,
        )

    summary = ActivityStepsSummary(
        total_steps=total_lifetime_steps,
        active_months=sorted(monthly_exports.keys()),
        by_activity=dict(lifetime_activity_counts),
    )
    return summary, monthly_exports


def build_studio_sessions_export(
    db: Session, user: User
) -> tuple[list[StudioSessionSummaryExport], list[StudioSessionDetailExport]]:
    """Retrieve studio recording sessions authored by the user."""
    sessions = (
        db.query(StudioSession)
        .filter((StudioSession.user_id == user.id) | (StudioSession.author_uuid == user.uuid))
        .order_by(StudioSession.created_at.asc())
        .all()
    )

    summaries: list[StudioSessionSummaryExport] = []
    details: list[StudioSessionDetailExport] = []

    for s in sessions:
        summary_item = StudioSessionSummaryExport(
            uuid=s.uuid,
            device_id=s.device_id,
            label=s.label,
            sample_count=s.sample_count,
            duration_sec=s.duration_sec,
            is_validated=s.is_validated,
            is_hitl=s.is_hitl,
            created_at=s.created_at,
        )
        detail_item = StudioSessionDetailExport(
            uuid=s.uuid,
            device_id=s.device_id,
            label=s.label,
            sample_count=s.sample_count,
            duration_sec=s.duration_sec,
            is_validated=s.is_validated,
            is_hitl=s.is_hitl,
            created_at=s.created_at,
        )
        summaries.append(summary_item)
        details.append(detail_item)

    return summaries, details


def generate_export_zip(db: Session, user: User) -> tuple[bytes, str]:
    """Generate in-memory GDPR Article 20 data portability ZIP archive.

    Returns:
        tuple[bytes, str]: (zip_binary_content, formatted_filename)
    """
    now_utc = datetime.now(UTC)
    timestamp_slug = now_utc.strftime("%Y%m%dT%H%M%SZ")
    filename = f"healthkicks_data_export_{user.pseudonym}_{timestamp_slug}.zip"

    # 1. Fetch domain components
    profile = build_profile_export(user)
    contacts = build_alert_contacts_export(db, user.id)
    devices = build_devices_export(db, user.id)
    fall_summary, falls_by_year = build_fall_alerts_export(db, user.id)
    steps_summary, steps_by_month = build_activity_steps_export(db, user.id)
    studio_summaries, studio_details = build_studio_sessions_export(db, user)

    # 2. Build metadata
    total_fall_alerts = fall_summary.total_alerts
    metadata = ExportMetadata(
        export_timestamp_utc=now_utc,
        user_uuid=user.uuid,
        user_pseudonym=user.pseudonym,
        item_counts={
            "emergency_contacts": len(contacts),
            "devices": len(devices),
            "fall_alerts": total_fall_alerts,
            "active_step_months": len(steps_by_month),
            "total_lifetime_steps": steps_summary.total_steps,
            "studio_sessions": len(studio_summaries),
        },
    )

    # 3. Create ZIP archive in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        # Root configuration and profile files
        zip_file.writestr("export_metadata.json", _to_json_bytes(metadata))
        zip_file.writestr("profile.json", _to_json_bytes(profile))
        zip_file.writestr("emergency_contacts.json", _to_json_bytes(contacts))
        zip_file.writestr("devices.json", _to_json_bytes(devices))

        # Fall alerts subdirectory
        zip_file.writestr("fall_alerts/summary.json", _to_json_bytes(fall_summary))
        for year, year_alerts in falls_by_year.items():
            zip_file.writestr(f"fall_alerts/alerts_{year}.json", _to_json_bytes(year_alerts))

        # Activity steps subdirectory
        zip_file.writestr("activity_steps/summary.json", _to_json_bytes(steps_summary))
        for month_str, month_export in steps_by_month.items():
            zip_file.writestr(
                f"activity_steps/month_{month_str}.json", _to_json_bytes(month_export)
            )

        # Studio sessions subdirectory
        zip_file.writestr("studio_sessions/index.json", _to_json_bytes(studio_summaries))
        for detail in studio_details:
            zip_file.writestr(f"studio_sessions/session_{detail.uuid}.json", _to_json_bytes(detail))

    zip_bytes = zip_buffer.getvalue()
    logger.info(
        "GDPR data portability ZIP generated: user_uuid=%s pseudonym=%s size_bytes=%d",
        user.uuid,
        user.pseudonym,
        len(zip_bytes),
    )
    return zip_bytes, filename
