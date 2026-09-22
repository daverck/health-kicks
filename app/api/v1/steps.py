"""FastAPI router for daily activity steps synchronization and history."""

from datetime import date, datetime, timedelta, timezone
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db.database import get_db
from app.schemas.steps import (
    DailyStepsHistoryResponse,
    DailyStepsSyncPayload,
    DailyStepsSyncResponse,
)
from app.services.steps_service import get_steps_history, sync_daily_steps

logger = logging.getLogger(__name__)


def create_steps_router() -> APIRouter:
    """Instantiate and configure the steps router."""
    router = APIRouter(prefix="/api/v1/steps", tags=["Steps"])

    @router.post(
        "/sync",
        response_model=DailyStepsSyncResponse,
        status_code=status.HTTP_200_OK,
        summary="Idempotently synchronize daily step counts broken down by activity",
    )
    def sync_steps(
        payload: DailyStepsSyncPayload,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> DailyStepsSyncResponse:
        """Upsert daily activity step snapshots recorded by the edge device or mobile application."""
        synced = sync_daily_steps(db=db, user_id=user.id, payload=payload)
        return DailyStepsSyncResponse(status="synchronized", synced_records=synced)

    @router.get(
        "/history",
        response_model=DailyStepsHistoryResponse,
        summary="Retrieve daily step counts and activity breakdown for a device",
    )
    def get_history(
        device_id: Annotated[str, Query(description="Target device ID", min_length=1, max_length=64)],
        user: CurrentUser,
        from_date: Annotated[date | None, Query(description="Start date (YYYY-MM-DD)")] = None,
        to_date: Annotated[date | None, Query(description="End date (YYYY-MM-DD)")] = None,
        days: Annotated[int, Query(ge=1, le=365, description="Number of past days (used if from_date is omitted)")] = 30,
        db: Session = Depends(get_db),
    ) -> DailyStepsHistoryResponse:
        """Fetch step counts history grouped by day and activity type."""
        today = datetime.now(timezone.utc).date()

        if to_date is None:
            resolved_to_date = today
        else:
            resolved_to_date = to_date

        if from_date is None:
            resolved_from_date = resolved_to_date - timedelta(days=days - 1)
        else:
            resolved_from_date = from_date

        if resolved_from_date > resolved_to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="from_date cannot be greater than to_date",
            )

        return get_steps_history(
            db=db,
            device_id=device_id,
            user_id=user.id,
            user_role=user.role,
            from_date=resolved_from_date,
            to_date=resolved_to_date,
        )

    return router

