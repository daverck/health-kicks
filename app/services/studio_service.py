"""Studio session management, authorization, and statistics computation service."""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from app.core.pseudonymization import get_subject_pseudonym
from app.db.models import StudioSession, User
from app.schemas.studio import StudioSessionSummary
from app.schemas.telemetry import (
    StudioClassDailyCount,
    StudioClassDetailStatsResponse,
    StudioClassDurationStats,
    StudioClassMlMetrics,
    StudioClassSummaryItem,
    StudioClassesSummaryResponse,
    StudioDatasetStatsResponse,
)


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

    total_sessions = query.count()
    total_duration = (
        query.with_entities(func.coalesce(func.sum(StudioSession.duration_sec), 0.0)).scalar()
        or 0.0
    )
    total_samples = (
        query.with_entities(func.coalesce(func.sum(StudioSession.sample_count), 0)).scalar()
        or 0
    )

    label_rows = (
        query.with_entities(
            StudioSession.label,
            func.count(StudioSession.uuid).label("total"),
            func.coalesce(func.sum(case((StudioSession.is_validated == True, 1), else_=0)), 0).label("validated"),
            func.coalesce(func.sum(case((StudioSession.is_validated == False, 1), else_=0)), 0).label("pending"),
        )
        .group_by(StudioSession.label)
        .all()
    )
    by_label = {str(lbl): int(total) for lbl, total, _val, _pend in label_rows}
    by_label_validated = {str(lbl): int(val) for lbl, _tot, val, _pend in label_rows}
    by_label_pending = {str(lbl): int(pend) for lbl, _tot, _val, pend in label_rows}

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
    query = db.query(StudioSession).filter(StudioSession.label == clean_label)

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
        from pathlib import Path
        import joblib

        model_path = Path("scripts/models/activity_classifier.joblib")
        if not model_path.exists():
            model_path = Path(__file__).resolve().parents[2] / "scripts" / "models" / "activity_classifier.joblib"

        if model_path.exists():
            pkg = joblib.load(model_path)
            class_metrics = pkg.get("metrics", {}).get("class_metrics", {})
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

    return StudioClassDetailStatsResponse(
        label=clean_label,
        total_sessions=total_sessions,
        validated_sessions=validated_sessions,
        pending_sessions=pending_sessions,
        duration_metrics=duration_metrics,
        ml_metrics=ml_metrics,
        daily_counts=daily_counts,
    )



def compute_classes_summary(
    db: Session,
    user: User,
) -> StudioClassesSummaryResponse:
    from typing import Any
    from app.core.edge_ai import edge_ai_labels_sorted
    import numpy as np

    query = db.query(StudioSession)
    if not user.is_admin:
        query = query.filter(StudioSession.user_id == user.id)

    sessions = query.order_by(StudioSession.created_at.asc()).all()

    class_metrics_map: dict[str, dict[str, Any]] = {}
    macro_metrics = {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    try:
        from pathlib import Path
        import joblib

        model_path = Path("scripts/models/activity_classifier.joblib")
        if not model_path.exists():
            model_path = Path(__file__).resolve().parents[2] / "scripts" / "models" / "activity_classifier.joblib"

        if model_path.exists():
            pkg = joblib.load(model_path)
            metrics_dict = pkg.get("metrics", {})
            class_metrics_map = metrics_dict.get("class_metrics", {})
            macro_metrics["precision"] = float(metrics_dict.get("macro_precision", 0.0))
            macro_metrics["recall"] = float(metrics_dict.get("macro_recall", 0.0))
            macro_metrics["f1"] = float(metrics_dict.get("macro_f1", 0.0))
    except Exception:
        pass

    sessions_by_label: dict[str, list[StudioSession]] = {}
    for s in sessions:
        lbl = (s.label or "unknown").strip()
        if lbl not in sessions_by_label:
            sessions_by_label[lbl] = []
        sessions_by_label[lbl].append(s)

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

        latest_date: str | None = None
        dates = [s.created_at.strftime("%Y-%m-%d") for s in sess_list if s.created_at]
        if dates:
            latest_date = sorted(dates)[-1]

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
        classes=items,
    )
