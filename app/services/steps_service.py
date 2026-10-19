"""Business logic and persistence service for daily activity step counting."""

import logging
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DailyActivityStep, HourlyActivityStep, UserRole
from app.schemas.steps import (
    DailyStepsHistoryResponse,
    DailyStepsSummary,
    DailyStepsSyncPayload,
    HourlyStepItem,
    HourlyStepsResponse,
)

logger = logging.getLogger(__name__)


def sync_daily_steps(db: Session, user_id: int, payload: DailyStepsSyncPayload) -> int:
    """Upsert step count records per hour and activity type for a specific device and date.

    Also aggregates daily totals in DailyActivityStep for fast daily history lookups.
    Idempotent operation: updates existing records or inserts new ones.
    """
    synced_count = 0
    now_utc = datetime.now(UTC)

    # 1. Upsert hourly records
    daily_totals: dict[str, int] = {}
    for hourly_item in payload.hourly_steps:
        daily_totals[hourly_item.activity_type] = (
            daily_totals.get(hourly_item.activity_type, 0) + hourly_item.step_count
        )
        h_stmt = select(HourlyActivityStep).where(
            HourlyActivityStep.device_id == payload.device_id,
            HourlyActivityStep.date == payload.date,
            HourlyActivityStep.hour == hourly_item.hour,
            HourlyActivityStep.activity_type == hourly_item.activity_type,
        )
        existing_h = db.execute(h_stmt).scalar_one_or_none()

        if existing_h:
            existing_h.step_count = hourly_item.step_count
            existing_h.user_id = user_id
            existing_h.updated_at = now_utc
        else:
            new_h = HourlyActivityStep(
                user_id=user_id,
                device_id=payload.device_id,
                date=payload.date,
                hour=hourly_item.hour,
                activity_type=hourly_item.activity_type,
                step_count=hourly_item.step_count,
                updated_at=now_utc,
            )
            db.add(new_h)
        synced_count += 1

    # 2. Upsert aggregated daily records for daily history & calendar heatmap
    for activity_type, step_count in daily_totals.items():
        stmt = select(DailyActivityStep).where(
            DailyActivityStep.device_id == payload.device_id,
            DailyActivityStep.date == payload.date,
            DailyActivityStep.activity_type == activity_type,
        )
        existing = db.execute(stmt).scalar_one_or_none()
        if existing:
            existing.step_count = step_count
            existing.user_id = user_id
            existing.updated_at = now_utc
        else:
            new_record = DailyActivityStep(
                user_id=user_id,
                device_id=payload.device_id,
                date=payload.date,
                activity_type=activity_type,
                step_count=step_count,
                updated_at=now_utc,
            )
            db.add(new_record)

    db.commit()
    return synced_count


def get_steps_history(
    db: Session,
    device_id: str,
    user_id: int,
    user_role: UserRole,
    from_date: date,
    to_date: date,
) -> DailyStepsHistoryResponse:
    """Retrieve historical daily step counts aggregated by activity type across a date range.

    Enforces authorization: regular users can only query devices they own or have uploaded data for.
    """
    stmt = (
        select(DailyActivityStep)
        .where(
            DailyActivityStep.device_id == device_id,
            DailyActivityStep.date >= from_date,
            DailyActivityStep.date <= to_date,
        )
        .order_by(DailyActivityStep.date.asc(), DailyActivityStep.activity_type.asc())
    )

    if user_role not in (UserRole.admin, UserRole.clinician):
        stmt = stmt.where(DailyActivityStep.user_id == user_id)

    rows = db.execute(stmt).scalars().all()

    # Group activities by date
    grouped_by_date: dict[date, dict[str, int]] = {}
    for row in rows:
        if row.date not in grouped_by_date:
            grouped_by_date[row.date] = {}
        grouped_by_date[row.date][row.activity_type] = row.step_count

    history: list[DailyStepsSummary] = []
    for d, activities in grouped_by_date.items():
        total = sum(activities.values())
        history.append(
            DailyStepsSummary(
                date=d,
                total_steps=total,
                by_activity=activities,
            )
        )

    return DailyStepsHistoryResponse(
        device_id=device_id,
        from_date=from_date,
        to_date=to_date,
        history=history,
    )


def get_hourly_steps(
    db: Session,
    device_id: str,
    user_id: int,
    user_role: UserRole,
    target_date: date,
) -> HourlyStepsResponse:
    """Retrieve 24-hour step breakdown for a given device and calendar date.

    Returns the exact measured hourly distribution recorded for this date.
    Hours without recorded activity return 0 steps.
    """
    stmt_hourly = select(HourlyActivityStep).where(
        HourlyActivityStep.device_id == device_id,
        HourlyActivityStep.date == target_date,
    )
    if user_role not in (UserRole.admin, UserRole.clinician):
        stmt_hourly = stmt_hourly.where(HourlyActivityStep.user_id == user_id)

    hourly_records = db.execute(stmt_hourly).scalars().all()

    hourly_data_by_hour: dict[int, dict[str, int]] = {h: {} for h in range(24)}
    for rec in hourly_records:
        if 0 <= rec.hour <= 23:
            hourly_data_by_hour[rec.hour][rec.activity_type] = rec.step_count

    items: list[HourlyStepItem] = []
    for h in range(24):
        by_act = hourly_data_by_hour[h]
        tot = sum(by_act.values())
        items.append(
            HourlyStepItem(
                hour=h,
                total_steps=tot,
                by_activity=by_act,
            )
        )

    return HourlyStepsResponse(
        device_id=device_id,
        date=target_date,
        hourly_data=items,
    )

