"""Studio session management, authorization, and statistics computation service."""

import base64
import logging
from datetime import datetime
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.core.edge_ai import EDGE_AI_LABELS
from app.core.pseudonymization import get_subject_pseudonym
from app.db.models import StudioSession, User, UserRole
from app.schemas.studio import (
    PaginatedSessionsResponse,
    StudioAuthorSummary,
    StudioSessionSummary,
    StudioSessionTrimPayload,
    StudioSessionUpdatePayload,
)
from app.schemas.telemetry import (
    StudioClassDailyCount,
    StudioClassDetailStatsResponse,
    StudioClassDurationStats,
    StudioClassesSummaryResponse,
    StudioClassMlMetrics,
    StudioClassSummaryItem,
    StudioDatasetStatsResponse,
)
from app.services.telemetry_service import TelemetryService

logger = logging.getLogger(__name__)


def parse_session_uuid(session_id: str | UUID) -> UUID:
    """Safely parse a studio session UUID or raise 404."""
    if isinstance(session_id, UUID):
        return session_id
    try:
        return UUID(str(session_id))
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Studio session '{session_id}' not found",
        )


def encode_cursor(created_at: datetime, session_id: UUID) -> str:
    """Encode created_at timestamp and session UUID into an opaque URL-safe base64 cursor."""
    raw = f"{created_at.isoformat()}|{str(session_id)}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def decode_cursor(cursor_str: str) -> tuple[datetime, UUID] | None:
    """Safely decode an opaque cursor into (created_at, session_id) or return None."""
    try:
        raw = base64.urlsafe_b64decode(cursor_str.encode("ascii")).decode("ascii")
        ts_part, id_part = raw.split("|", 1)
        return datetime.fromisoformat(ts_part), UUID(id_part)
    except Exception:
        return None


def query_studio_sessions(
    db: Session,
    user: User,
    *,
    page: int = 1,
    size: int = 20,
    cursor: str | None = None,
    label: str | None = None,
    device_id: str | None = None,
    pseudonym: str | None = None,
    is_validated: bool | None = None,
    is_hitl: bool | None = None,
    classification: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    sample_count_min: int | None = None,
    sample_count_max: int | None = None,
    duration_sec_min: float | None = None,
    duration_sec_max: float | None = None,
) -> PaginatedSessionsResponse:
    """List studio sessions with RBAC, cursor keyset pagination, and multi-value filters."""
    if sample_count_min is not None and sample_count_max is not None and sample_count_min > sample_count_max:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sample_count_min must be less than or equal to sample_count_max",
        )
    if duration_sec_min is not None and duration_sec_max is not None and duration_sec_min > duration_sec_max:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="duration_sec_min must be less than or equal to duration_sec_max",
        )

    query = db.query(StudioSession)

    if user.role != UserRole.admin:
        query = query.filter(StudioSession.user_id == user.id)
    elif pseudonym is not None:
        val = pseudonym.strip()
        if val and val.lower() != "all":
            query = query.join(User, StudioSession.user_id == User.id).filter(
                User.pseudonym.ilike(f"%{val}%")
            )

    if label is not None:
        target_labels = [lbl.strip() for lbl in label.split(",") if lbl.strip()]
        if target_labels and not any(lbl.lower() == "all" for lbl in target_labels):
            query = query.filter(StudioSession.label.in_(target_labels))

    if device_id is not None:
        target_devs = [d.strip() for d in device_id.split(",") if d.strip()]
        if target_devs and not any(d.lower() == "all" for d in target_devs):
            query = query.filter(StudioSession.device_id.in_(target_devs))

    if is_validated is not None and isinstance(is_validated, bool):
        query = query.filter(StudioSession.is_validated == is_validated)

    if is_hitl is not None and isinstance(is_hitl, bool):
        query = query.filter(StudioSession.is_hitl == is_hitl)

    if classification is not None and classification.strip().lower() != "all":
        edge_ai_labels = EDGE_AI_LABELS
        if classification.strip().lower() == "edge_ai":
            query = query.filter(StudioSession.label.in_(edge_ai_labels))
        elif classification.strip().lower() == "research":
            query = query.filter(~StudioSession.label.in_(edge_ai_labels))

    if start_date is not None:
        query = query.filter(StudioSession.created_at >= start_date)

    if end_date is not None:
        query = query.filter(StudioSession.created_at <= end_date)

    if sample_count_min is not None:
        query = query.filter(StudioSession.sample_count >= sample_count_min)
    if sample_count_max is not None:
        query = query.filter(StudioSession.sample_count <= sample_count_max)
    if duration_sec_min is not None:
        query = query.filter(StudioSession.duration_sec >= duration_sec_min)
    if duration_sec_max is not None:
        query = query.filter(StudioSession.duration_sec <= duration_sec_max)

    total = query.count()
    next_cursor: str | None = None

    if cursor is not None:
        decoded = decode_cursor(cursor)
        if decoded is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid pagination cursor format",
            )
        cursor_dt, cursor_id = decoded
        query = query.filter(
            or_(
                StudioSession.created_at < cursor_dt,
                and_(
                    StudioSession.created_at == cursor_dt,
                    StudioSession.uuid < cursor_id,
                ),
            )
        )

    ordered_query = query.order_by(StudioSession.created_at.desc(), StudioSession.uuid.desc())
    if cursor is None and page > 1:
        ordered_query = ordered_query.offset((page - 1) * size)

    sessions_plus_one = ordered_query.limit(size + 1).all()
    if len(sessions_plus_one) > size:
        sessions = sessions_plus_one[:size]
        next_cursor = encode_cursor(sessions[-1].created_at, sessions[-1].uuid)
    else:
        sessions = sessions_plus_one
        next_cursor = None

    items = [to_session_summary(s, is_admin=user.is_admin, current_user_id=user.id) for s in sessions]
    return PaginatedSessionsResponse(
        items=items,
        total=total,
        page=page,
        size=size,
        next_cursor=next_cursor,
    )


def get_authorized_session(
    db: Session,
    session_id: str | UUID,
    user: User,
) -> StudioSession:
    """Retrieve a studio session ensuring role-based access control.

    - Requires clinician or admin role.
    - Clinicians can only access their own sessions.
    - Admins can access all sessions.
    """
    if not user.is_clinician_or_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: access to studio sessions requires clinician or admin privileges",
        )

    sess_uuid = parse_session_uuid(session_id)
    session = db.query(StudioSession).filter(StudioSession.uuid == sess_uuid).one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Studio session '{session_id}' not found",
        )

    if not user.is_admin and session.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: cannot access or modify another user's studio session",
        )

    return session


def to_session_summary(
    session: StudioSession,
    is_admin: bool = False,
    current_user_id: int | None = None,
) -> StudioSessionSummary:
    """Serialize a StudioSession ORM model into a StudioSessionSummary schema with GDPR pseudonymization.

    - If the session belongs to the current user (current_user_id == session.user_id):
      PII is preserved, is_pseudonymized is False.
    - If the session belongs to another user (viewed by admin or clinician):
      PII (user_email, author_name) is stripped/pseudonymized, is_pseudonymized is True,
      and subject_pseudonym is deterministically generated (SUBJ-XXXXXXXX).
    """
    is_own = current_user_id is not None and session.user_id == current_user_id
    pseudo = (
        session.user.pseudonym
        if session.user and session.user.pseudonym
        else get_subject_pseudonym(session.user_id)
    )

    if is_own:
        user_email = session.user.email if (is_admin and session.user) else None
        author_name = session.user.name if session.user else None
        is_pseudo = False
    else:
        # Third-party session viewed by clinician or admin: strictly pseudonymize PII
        user_email = None
        author_name = f"Patient {pseudo}"
        is_pseudo = True

    author_uuid = session.user.uuid if session.user else None
    author_pseudonym = pseudo

    return StudioSessionSummary(
        uuid=session.uuid,
        device_id=session.device_id,
        user_id=session.user_id,
        user_email=user_email,
        author_name=author_name,
        subject_pseudonym=pseudo,
        author_uuid=author_uuid,
        author_pseudonym=author_pseudonym,
        is_pseudonymized=is_pseudo,
        label=session.label,
        sample_count=session.sample_count or 0,
        duration_sec=session.duration_sec or 5.0,
        is_validated=bool(session.is_validated),
        is_hitl=bool(getattr(session, "is_hitl", False)),
        created_at=session.created_at,
    )


def compute_studio_stats(
    db: Session,
    user: User,
    device_id: str | None = None,
) -> StudioDatasetStatsResponse:
    """Compute studio dataset metrics from PostgreSQL (Aurora).

    - Non-admins only see aggregate stats for their own sessions.
    - Admins see global aggregates or device aggregates across all users.
    """
    query = db.query(StudioSession)

    if not user.is_admin:
        query = query.filter(StudioSession.user_id == user.id)

    if device_id is not None:
        query = query.filter(StudioSession.device_id == device_id)

    label_rows = (
        query.with_entities(
            StudioSession.label,
            func.count(StudioSession.uuid).label("total"),
            func.coalesce(func.sum(case((StudioSession.is_validated.is_(True), 1), else_=0)), 0).label("validated"),
            func.coalesce(func.sum(case((StudioSession.is_validated.is_(False), 1), else_=0)), 0).label("pending"),
            func.coalesce(func.sum(StudioSession.duration_sec), 0.0).label("duration_sum"),
            func.coalesce(func.sum(StudioSession.sample_count), 0).label("sample_sum"),
        )
        .group_by(StudioSession.label)
        .all()
    )
    total_sessions = sum(int(tot) for _lbl, tot, _val, _pend, _dur, _smp in label_rows)
    total_duration = sum(float(dur or 0.0) for _lbl, _tot, _val, _pend, dur, _smp in label_rows)
    total_samples = sum(int(smp or 0) for _lbl, _tot, _val, _pend, _dur, smp in label_rows)

    by_label = {str(lbl): int(total) for lbl, total, _val, _pend, _dur, _smp in label_rows}
    by_label_validated = {str(lbl): int(val) for lbl, _tot, val, _pend, _dur, _smp in label_rows}
    by_label_pending = {str(lbl): int(pend) for lbl, _tot, _val, pend, _dur, _smp in label_rows}

    return StudioDatasetStatsResponse(
        device_id=device_id,
        total_sessions=total_sessions,
        total_duration_sec=round(float(total_duration), 2),
        total_samples=int(total_samples),
        by_label=by_label,
        by_label_validated=by_label_validated,
        by_label_pending=by_label_pending,
    )


def compute_class_detail_stats(
    db: Session,
    user: User,
    label: str,
) -> StudioClassDetailStatsResponse:
    clean_label = str(label).strip()
    query = db.query(
        StudioSession.label,
        StudioSession.is_validated,
        StudioSession.duration_sec,
        StudioSession.created_at,
    ).filter(StudioSession.label == clean_label)

    if not user.is_admin:
        query = query.filter(StudioSession.user_id == user.id)

    sessions = query.order_by(StudioSession.created_at.asc()).all()

    total_sessions = len(sessions)
    validated_sessions = sum(1 for s in sessions if bool(s.is_validated))
    pending_sessions = total_sessions - validated_sessions

    durations = [float(s.duration_sec) for s in sessions if s.duration_sec is not None and s.duration_sec > 0]
    if durations:
        import numpy as np

        median_sec = float(np.median(durations))
        mean_sec = float(np.mean(durations))
        std_sec = float(np.std(durations))
        min_sec = float(np.min(durations))
        max_sec = float(np.max(durations))
    else:
        median_sec = mean_sec = std_sec = min_sec = max_sec = 0.0

    duration_metrics = StudioClassDurationStats(
        median_sec=round(median_sec, 2),
        mean_sec=round(mean_sec, 2),
        std_sec=round(std_sec, 2),
        min_sec=round(min_sec, 2),
        max_sec=round(max_sec, 2),
    )

    daily_map: dict[str, dict[str, int]] = {}
    for s in sessions:
        if s.created_at:
            d_str = s.created_at.strftime("%Y-%m-%d")
        else:
            d_str = "unknown"
        if d_str not in daily_map:
            daily_map[d_str] = {"total": 0, "validated": 0, "pending": 0}
        daily_map[d_str]["total"] += 1
        if bool(s.is_validated):
            daily_map[d_str]["validated"] += 1
        else:
            daily_map[d_str]["pending"] += 1

    daily_counts = [
        StudioClassDailyCount(
            date=d_key,
            count=d_val["total"],
            validated_count=d_val["validated"],
            pending_count=d_val["pending"],
        )
        for d_key, d_val in sorted(daily_map.items())
    ]

    ml_metrics = StudioClassMlMetrics()
    try:
        from app.core.model_metrics import load_champion_model_metrics

        metrics_pkg = load_champion_model_metrics()
        class_metrics = metrics_pkg.get("class_metrics", {})
        if clean_label in class_metrics:
            cm = class_metrics[clean_label]
            ml_metrics = StudioClassMlMetrics(
                precision=round(float(cm.get("precision", 0.0)), 4),
                recall=round(float(cm.get("recall", 0.0)), 4),
                f1_score=round(float(cm.get("f1_score", 0.0)), 4),
                support_windows=int(cm.get("support_windows", 0)),
            )
    except Exception:
        pass

    sorted_days = [d.date for d in daily_counts if d.date != "unknown"]
    first_session_date = sorted_days[0] if sorted_days else None
    latest_session_date = sorted_days[-1] if sorted_days else None

    return StudioClassDetailStatsResponse(
        label=clean_label,
        total_sessions=total_sessions,
        validated_sessions=validated_sessions,
        pending_sessions=pending_sessions,
        first_session_date=first_session_date,
        latest_session_date=latest_session_date,
        duration_metrics=duration_metrics,
        ml_metrics=ml_metrics,
        daily_counts=daily_counts,
    )



def compute_classes_summary(
    db: Session,
    user: User,
) -> StudioClassesSummaryResponse:
    from typing import Any

    import numpy as np

    from app.core.edge_ai import edge_ai_labels_sorted

    query = db.query(
        StudioSession.label,
        StudioSession.is_validated,
        StudioSession.duration_sec,
        StudioSession.created_at,
    )
    if not user.is_admin:
        query = query.filter(StudioSession.user_id == user.id)

    sessions = query.order_by(StudioSession.created_at.asc()).all()

    class_metrics_map: dict[str, dict[str, Any]] = {}
    macro_metrics = {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    confusion_matrix_obj = None
    try:
        from app.core.model_metrics import load_champion_model_metrics
        from app.schemas.telemetry import StudioConfusionMatrix

        metrics_dict = load_champion_model_metrics()
        class_metrics_map = metrics_dict.get("class_metrics", {})
        macro_metrics["precision"] = float(metrics_dict.get("precision_macro", metrics_dict.get("macro_precision", 0.0)))
        macro_metrics["recall"] = float(metrics_dict.get("recall_macro", metrics_dict.get("macro_recall", 0.0)))
        macro_metrics["f1"] = float(metrics_dict.get("f1_macro", metrics_dict.get("macro_f1", 0.0)))
        raw_cm = metrics_dict.get("confusion_matrix")
        if raw_cm and isinstance(raw_cm, dict) and "labels" in raw_cm and "matrix" in raw_cm:
            confusion_matrix_obj = StudioConfusionMatrix(
                labels=[str(lbl) for lbl in raw_cm["labels"]],
                matrix=[[float(cell) for cell in row] for row in raw_cm["matrix"]],
            )
    except Exception:
        pass

    sessions_by_label: dict[str, list[Any]] = {}
    daily_map: dict[str, dict[str, int]] = {}
    for s in sessions:
        lbl = (s.label or "unknown").strip()
        if lbl not in sessions_by_label:
            sessions_by_label[lbl] = []
        sessions_by_label[lbl].append(s)

        d_str = s.created_at.strftime("%Y-%m-%d") if s.created_at else "unknown"
        if d_str not in daily_map:
            daily_map[d_str] = {"total": 0, "validated": 0, "pending": 0}
        daily_map[d_str]["total"] += 1
        if bool(s.is_validated):
            daily_map[d_str]["validated"] += 1
        else:
            daily_map[d_str]["pending"] += 1

    daily_counts = [
        StudioClassDailyCount(
            date=d_key,
            count=d_val["total"],
            validated_count=d_val["validated"],
            pending_count=d_val["pending"],
        )
        for d_key, d_val in sorted(daily_map.items())
    ]

    all_labels = set(edge_ai_labels_sorted()) | set(sessions_by_label.keys()) | set(class_metrics_map.keys())
    sorted_labels = sorted(all_labels)

    items: list[StudioClassSummaryItem] = []
    total_duration_sec = 0.0
    total_sessions_count = len(sessions)
    validated_sessions_count = sum(1 for s in sessions if bool(s.is_validated))
    pending_sessions_count = total_sessions_count - validated_sessions_count

    for lbl in sorted_labels:
        sess_list = sessions_by_label.get(lbl, [])
        c_total = len(sess_list)
        c_validated = sum(1 for s in sess_list if bool(s.is_validated))
        c_pending = c_total - c_validated

        c_durations = [float(s.duration_sec) for s in sess_list if s.duration_sec is not None and s.duration_sec > 0]
        c_tot_dur = round(sum(c_durations), 2)
        total_duration_sec += c_tot_dur
        c_median_dur = round(float(np.median(c_durations)), 2) if c_durations else 0.0

        first_date: str | None = None
        latest_date: str | None = None
        dates = [s.created_at.strftime("%Y-%m-%d") for s in sess_list if s.created_at]
        if dates:
            s_dates = sorted(dates)
            first_date = s_dates[0]
            latest_date = s_dates[-1]

        cm = class_metrics_map.get(lbl, {})
        precision = round(float(cm.get("precision", 0.0)), 4)
        recall = round(float(cm.get("recall", 0.0)), 4)
        f1_score = round(float(cm.get("f1_score", 0.0)), 4)
        support_windows = int(cm.get("support_windows", 0))

        items.append(
            StudioClassSummaryItem(
                label=lbl,
                total_sessions=c_total,
                validated_sessions=c_validated,
                pending_sessions=c_pending,
                total_duration_sec=c_tot_dur,
                median_duration_sec=c_median_dur,
                first_session_date=first_date,
                latest_session_date=latest_date,
                precision=precision,
                recall=recall,
                f1_score=f1_score,
                support_windows=support_windows,
            )
        )

    return StudioClassesSummaryResponse(
        total_classes=len(items),
        total_sessions=total_sessions_count,
        validated_sessions=validated_sessions_count,
        pending_sessions=pending_sessions_count,
        total_duration_sec=round(total_duration_sec, 2),
        macro_precision=round(macro_metrics["precision"], 4),
        macro_recall=round(macro_metrics["recall"], 4),
        macro_f1=round(macro_metrics["f1"], 4),
        daily_counts=daily_counts,
        classes=items,
        confusion_matrix=confusion_matrix_obj,
    )


def query_studio_authors(db: Session, user: User) -> list[StudioAuthorSummary]:
    """List authors who have recorded studio sessions with GDPR pseudonymization for third-party authors."""
    if user.role == UserRole.admin:
        users = (
            db.query(User.id, User.email, User.name, User.uuid, User.pseudonym)
            .join(StudioSession, StudioSession.user_id == User.id)
            .distinct()
            .order_by(User.id)
            .all()
        )
        authors: list[StudioAuthorSummary] = []
        for u in users:
            is_own = (u.id == user.id)
            pseudo = u.pseudonym or get_subject_pseudonym(str(u.uuid) if u.uuid else u.id)
            if is_own:
                authors.append(
                    StudioAuthorSummary(
                        author_uuid=u.uuid,
                        author_pseudonym=pseudo,
                        email=u.email,
                        name=u.name,
                        subject_pseudonym=pseudo,
                        is_pseudonymized=False,
                    )
                )
            else:
                authors.append(
                    StudioAuthorSummary(
                        author_uuid=u.uuid,
                        author_pseudonym=pseudo,
                        email=f"{pseudo.lower()}@pseudonym.healthkicks.local",
                        name=f"Patient {pseudo}",
                        subject_pseudonym=pseudo,
                        is_pseudonymized=True,
                    )
                )
        return authors

    own_pseudo = user.pseudonym or get_subject_pseudonym(str(user.uuid) if user.uuid else user.id)
    return [
        StudioAuthorSummary(
            author_uuid=user.uuid,
            author_pseudonym=own_pseudo,
            email=user.email,
            name=user.name,
            subject_pseudonym=own_pseudo,
            is_pseudonymized=False,
        )
    ]


def trim_studio_session(
    db: Session,
    session: StudioSession,
    payload: StudioSessionTrimPayload,
    telemetry_service: TelemetryService,
) -> StudioSession:
    """Trim an unvalidated studio session to a selected time interval in PostgreSQL and DynamoDB."""
    if session.is_validated is True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Impossible d'écourter une session déjà confirmée/validée",
        )
    end_offset_sec = payload.end_offset_sec or payload.new_duration_sec
    if end_offset_sec is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="An end offset is required",
        )
    if end_offset_sec > session.duration_sec:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Le point de fin doit être inférieur ou égal à la durée actuelle",
        )
    if payload.start_offset_sec == 0 and end_offset_sec >= session.duration_sec:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="L'intervalle sélectionné doit être plus court que la session actuelle",
        )

    try:
        retained_count = telemetry_service.trim_session_readings(
            device_id=session.device_id,
            session_id=str(session.uuid),
            start_offset_sec=payload.start_offset_sec,
            end_offset_sec=end_offset_sec,
        )
    except (BotoCoreError, ClientError) as error:
        logger.error(
            "Could not trim readings from DynamoDB for session %s: %s",
            session.uuid,
            error,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to trim studio session readings from telemetry store",
        )

    session.duration_sec = end_offset_sec - payload.start_offset_sec
    session.sample_count = retained_count
    db.commit()
    db.refresh(session)
    return session


def update_studio_session(
    db: Session,
    session: StudioSession,
    payload: StudioSessionUpdatePayload,
    telemetry_service: TelemetryService,
) -> StudioSession:
    """Update session activity label and validation status, propagating label change to DynamoDB."""
    if payload.label is not None:
        session.label = payload.label
    if payload.is_validated is not None:
        session.is_validated = payload.is_validated

    db.commit()
    db.refresh(session)

    if payload.label is not None:
        try:
            telemetry_service.update_session_label(
                device_id=session.device_id,
                session_id=str(session.uuid),
                new_label=payload.label,
            )
        except (BotoCoreError, ClientError) as error:
            logger.warning(
                "Could not propagate label update to DynamoDB for session %s: %s",
                session.uuid,
                error,
            )

    return session


def delete_studio_session(
    db: Session,
    session: StudioSession,
    telemetry_service: TelemetryService,
) -> None:
    """Purge a studio session from PostgreSQL and delete all raw IMU frames in DynamoDB."""
    try:
        telemetry_service.delete_session_readings(
            device_id=session.device_id,
            session_id=str(session.uuid),
        )
    except (BotoCoreError, ClientError) as error:
        logger.warning(
            "Could not purge readings from DynamoDB for session %s: %s",
            session.uuid,
            error,
        )

    db.delete(session)
    db.commit()

