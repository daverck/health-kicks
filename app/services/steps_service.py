"""Business logic and persistence service for daily activity step counting."""

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


def _apply_daily_steps_upsert_pass(
    db: Session,
    user_id: int,
    payload: DailyStepsSyncPayload,
    now_utc: datetime,
) -> int:
    """Prefetch existing hourly/daily rows in 2 queries and apply upserts in memory."""
    existing_hourly_rows = (
        db.execute(
            select(HourlyActivityStep).where(
                HourlyActivityStep.device_id == payload.device_id,
                HourlyActivityStep.date == payload.date,
            )
        )
        .scalars()
        .all()
    )
    hourly_map: dict[tuple[int, str], HourlyActivityStep] = {
        (row.hour, row.activity_type): row for row in existing_hourly_rows
    }

    existing_daily_rows = (
        db.execute(
            select(DailyActivityStep).where(
                DailyActivityStep.device_id == payload.device_id,
                DailyActivityStep.date == payload.date,
            )
        )
        .scalars()
        .all()
    )
    daily_map: dict[str, DailyActivityStep] = {
        row.activity_type: row for row in existing_daily_rows
    }

    synced_count = 0
    daily_totals: dict[str, int] = {}
    for hourly_item in payload.hourly_steps:
        daily_totals[hourly_item.activity_type] = (
            daily_totals.get(hourly_item.activity_type, 0) + hourly_item.step_count
        )
        key = (hourly_item.hour, hourly_item.activity_type)
        existing_h = hourly_map.get(key)
        if existing_h is not None:
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
            hourly_map[key] = new_h
        synced_count += 1

    for activity_type, step_count in daily_totals.items():
        existing_d = daily_map.get(activity_type)
        if existing_d is not None:
            existing_d.step_count = step_count
            existing_d.user_id = user_id
            existing_d.updated_at = now_utc
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
            daily_map[activity_type] = new_record

    return synced_count


def sync_daily_steps(db: Session, user_id: int, payload: DailyStepsSyncPayload) -> int:
    """Upsert step count records per hour and activity type for a specific device and date.

    Uses batch prefetching (2 SELECT queries total) and an IntegrityError retry pass
    to guarantee lock-free concurrency safety under simultaneous mobile syncs.
    """
    now_utc = datetime.now(UTC)
    synced_count = _apply_daily_steps_upsert_pass(db, user_id, payload, now_utc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        synced_count = _apply_daily_steps_upsert_pass(db, user_id, payload, now_utc)
        db.commit()
    return synced_count


def resolve_steps_date_range(
    from_date: date | None,
    to_date: date | None,
    days: int = 30,
) -> tuple[date, date]:
    """Resolve and validate a date range for steps history.

    Defaults to_date to today UTC if not specified.
    Defaults from_date to (to_date - timedelta(days=days - 1)) if not specified.
    Raises HTTPException(422) if from_date > to_date.
    """
    today = datetime.now(UTC).date()
    resolved_to_date = today if to_date is None else to_date
    resolved_from_date = (
        resolved_to_date - timedelta(days=days - 1)
        if from_date is None
        else from_date
    )

    if resolved_from_date > resolved_to_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="from_date cannot be greater than to_date",
        )

    return resolved_from_date, resolved_to_date


def aggregate_daily_steps_summaries(
    rows: Sequence[DailyActivityStep],
) -> list[DailyStepsSummary]:
    """Group and aggregate daily activity steps by date into DailyStepsSummary objects."""
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
    return history


def build_24h_hourly_step_items(
    records: Sequence[HourlyActivityStep],
) -> list[HourlyStepItem]:
    """Construct 24-hour step items (hours 0 to 23), defaulting unrecorded hours to 0 steps."""
    hourly_data_by_hour: dict[int, dict[str, int]] = {h: {} for h in range(24)}
    for rec in records:
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
    return items


def get_steps_history(
    db: Session,
    device_id: str,
    user_id: int,
    user_role: UserRole,
    from_date: date,
    to_date: date,
) -> DailyStepsHistoryResponse:
    """Retrieve historical daily step counts aggregated by activity type across a date range.

    Enforces authorization: non-admin users (user and clinician) can only query their own data.
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

    if user_role != UserRole.admin:
        stmt = stmt.where(DailyActivityStep.user_id == user_id)

    rows = db.execute(stmt).scalars().all()
    history = aggregate_daily_steps_summaries(rows)

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
    if user_role != UserRole.admin:
        stmt_hourly = stmt_hourly.where(HourlyActivityStep.user_id == user_id)

    hourly_records = db.execute(stmt_hourly).scalars().all()
    items = build_24h_hourly_step_items(hourly_records)

    return HourlyStepsResponse(
        device_id=device_id,
        date=target_date,
        hourly_data=items,
    )
