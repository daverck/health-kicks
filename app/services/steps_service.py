"""Business logic and persistence service for daily activity step counting."""

from datetime import date, datetime, timezone
import logging
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DailyActivityStep, DeviceOwnership, UserRole
from app.schemas.steps import (
    DailyStepsHistoryResponse,
    DailyStepsSummary,
    DailyStepsSyncPayload,
)

logger = logging.getLogger(__name__)


def sync_daily_steps(db: Session, user_id: int, payload: DailyStepsSyncPayload) -> int:
    """Upsert step count records per activity type for a specific device and date.

    Idempotent operation: updates existing records or inserts new ones.
    """
    synced_count = 0
    now_utc = datetime.now(timezone.utc)

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

