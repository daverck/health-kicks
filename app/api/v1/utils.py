"""Common API utilities for query parameter validation and date filtering."""

from datetime import date, datetime, time
from fastapi import HTTPException, status


def validate_and_normalize_date_range(
    start_date: datetime | date | None,
    end_date: datetime | date | None,
    raw_end_date: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Validate and normalize start_date and end_date range for history queries.

    - start_date: converted to datetime if date is passed.
    - end_date: if a date-only string (e.g. '2026-09-13') or date object is passed,
      expanded to end of day (23:59:59.999999). If full timestamp, kept as is.
    - Raises HTTP 400 if start_date > end_date.
    """
    norm_start = start_date if isinstance(start_date, (datetime, date)) else None
    norm_end = end_date if isinstance(end_date, (datetime, date)) else None

    # Normalize pure date to datetime
    if norm_start is not None and type(norm_start) is date:
        norm_start = datetime.combine(norm_start, time.min)

    if norm_end is not None:
        if type(norm_end) is date:
            norm_end = datetime.combine(norm_end, time.max)
        elif isinstance(norm_end, datetime):
            is_date_only = False
            if raw_end_date is not None:
                stripped = raw_end_date.strip()
                if "T" not in stripped and " " not in stripped and ":" not in stripped:
                    is_date_only = True
            if is_date_only:
                norm_end = norm_end.replace(
                    hour=23, minute=59, second=59, microsecond=999999
                )

    if norm_start is not None and norm_end is not None:
        # Align timezones if one is aware and one is naive for comparison
        s_cmp = norm_start
        e_cmp = norm_end
        if s_cmp.tzinfo is not None and e_cmp.tzinfo is None:
            e_cmp = e_cmp.replace(tzinfo=s_cmp.tzinfo)
        elif s_cmp.tzinfo is None and e_cmp.tzinfo is not None:
            s_cmp = s_cmp.replace(tzinfo=e_cmp.tzinfo)

        if s_cmp > e_cmp:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="start_date must be before or equal to end_date",
            )

    return norm_start, norm_end
