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
    """Upsert step count records per activity type for a specific device and date.

    Idempotent operation: updates existing records or inserts new ones.
    """
    synced_count = 0
    now_utc = datetime.now(UTC)

    for item in payload.activities:
        stmt = select(DailyActivityStep).where(
            DailyActivityStep.device_id == payload.device_id,
            DailyActivityStep.date == payload.date,
            DailyActivityStep.activity_type == item.activity_type,
        )
        existing = db.execute(stmt).scalar_one_or_none()

        if existing:
            existing.step_count = item.step_count
            existing.user_id = user_id
            existing.updated_at = now_utc
        else:
            new_record = DailyActivityStep(
                user_id=user_id,
                device_id=payload.device_id,
                date=payload.date,
                activity_type=item.activity_type,
                step_count=item.step_count,
                updated_at=now_utc,
            )
            db.add(new_record)
        synced_count += 1

    if payload.hourly_steps:
        for hourly_item in payload.hourly_steps:
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


# Realistic diurnal step distribution weights across 24 hours (sum = 100)
# Waking hours: 7 to 21 with peaks at 8-9h (morning commute), 12-13h (lunch), 17-19h (evening)
DIURNAL_HOURLY_WEIGHTS: dict[int, int] = {
    0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0,
    7: 3,
    8: 9,
    9: 8,
    10: 5,
    11: 6,
    12: 9,
    13: 8,
    14: 5,
    15: 5,
    16: 6,
    17: 9,
    18: 10,
    19: 8,
    20: 5,
    21: 4,
    22: 0, 23: 0,
}


def _distribute_steps(total_steps: int) -> dict[int, int]:
    """Distribute a daily step count across 24 hours deterministically.

    Guarantees that the sum of distributed hourly steps exactly matches total_steps.
    Uses the largest remainder method for integer apportionment.
    """
    if total_steps <= 0:
        return {h: 0 for h in range(24)}

    raw_values = {h: (total_steps * DIURNAL_HOURLY_WEIGHTS[h]) / 100.0 for h in range(24)}
    allocated = {h: int(raw_values[h]) for h in range(24)}
    remainder = total_steps - sum(allocated.values())

    if remainder > 0:
        fractional_parts = [
            (raw_values[h] - allocated[h], DIURNAL_HOURLY_WEIGHTS[h], -h, h)
            for h in range(24)
        ]
        fractional_parts.sort(reverse=True)
        for i in range(remainder):
            target_hour = fractional_parts[i][3]
            allocated[target_hour] += 1

    return allocated


def get_hourly_steps(
    db: Session,
    device_id: str,
    user_id: int,
    user_role: UserRole,
    target_date: date,
) -> HourlyStepsResponse:
    """Retrieve 24-hour step breakdown for a given device and calendar date.

    If fine-grained hourly steps are recorded, returns the exact measured distribution.
    Otherwise, falls back to deterministic diurnal distribution of recorded daily steps.
    """
    stmt_hourly = select(HourlyActivityStep).where(
        HourlyActivityStep.device_id == device_id,
        HourlyActivityStep.date == target_date,
    )
    if user_role not in (UserRole.admin, UserRole.clinician):
        stmt_hourly = stmt_hourly.where(HourlyActivityStep.user_id == user_id)

    hourly_records = db.execute(stmt_hourly).scalars().all()

    if hourly_records:
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

    stmt = select(DailyActivityStep).where(
        DailyActivityStep.device_id == device_id,
        DailyActivityStep.date == target_date,
    )
    if user_role not in (UserRole.admin, UserRole.clinician):
        stmt = stmt.where(DailyActivityStep.user_id == user_id)

    records = db.execute(stmt).scalars().all()

    if not records:
        empty_hourly = [
            HourlyStepItem(hour=h, total_steps=0, by_activity={})
            for h in range(24)
        ]
        return HourlyStepsResponse(
            device_id=device_id,
            date=target_date,
            hourly_data=empty_hourly,
        )

    activity_distributions: dict[str, dict[int, int]] = {}
    for record in records:
        activity_distributions[record.activity_type] = _distribute_steps(record.step_count)

    hourly_items: list[HourlyStepItem] = []
    for h in range(24):
        by_act = {
            act_type: dist[h]
            for act_type, dist in activity_distributions.items()
        }
        total_h = sum(by_act.values())
        hourly_items.append(
            HourlyStepItem(
                hour=h,
                total_steps=total_h,
                by_activity=by_act,
            )
        )

    return HourlyStepsResponse(
        device_id=device_id,
        date=target_date,
        hourly_data=hourly_items,
    )

