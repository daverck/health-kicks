"""REST endpoints for emergency contacts CRUD, fall alert history, and public acknowledgment."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.api.v1.utils import validate_and_normalize_date_range
from app.core.rate_limit import rate_limiter
from app.db.database import get_db
from app.core.pseudonymization import get_subject_pseudonym
from app.db.models import FallAlert, FallAlertNotification, FallAlertState, User, UserRole
from app.schemas.fall_alert import (
    AlertContactCreate,
    AlertContactResponse,
    AlertContactsOrderPayload,
    AlertContactTestEmailResponse,
    AlertContactUpdate,
    FallAlertAckInfoResponse,
    FallAlertAckPayload,
    FallAlertAckResultResponse,
    FallAlertDetail,
    FallAlertIngestResponse,
    FallAlertSmsStatusRequest,
    PaginatedFallAlertsResponse,
)
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.email_service import FALL_TYPE_LABELS_FR, SmtpEmailChannel, format_paris_datetime
from app.services.fall_alert_service import (
    AckTokenError,
    acknowledge_fall_alert,
    create_alert_contact,
    delete_alert_contact,
    get_alert_and_notification_for_token,
    list_alert_contacts,
    reorder_alert_contacts,
    send_test_email_to_contact,
    to_fall_alert_detail,
    to_fall_alert_summary,
    update_alert_contact,
    update_sms_notification_status,
)

logger = logging.getLogger("healthkicks.api.fall_alerts")


def create_fall_alerts_router(
    publisher: AWSIoTPublishService | None = None,
    email_channel: SmtpEmailChannel | None = None,
) -> APIRouter:
    """Create the router for emergency contacts CRUD, fall alert history, and public ACK pages."""
    router = APIRouter(prefix="/api/v1", tags=["Fall Alerts"])

    # ------------------------------------------------------------------
    # B. Emergency Contacts CRUD (/api/v1/users/me/alert-contacts)
    # ------------------------------------------------------------------

    @router.get(
        "/users/me/alert-contacts",
        response_model=list[AlertContactResponse],
    )
    def get_my_alert_contacts(
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> list[AlertContactResponse]:
        """List emergency contacts for the authenticated user ordered by priority."""
        return list_alert_contacts(db=db, user_id=user.id)

    @router.post(
        "/users/me/alert-contacts",
        response_model=AlertContactResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def add_my_alert_contact(
        payload: AlertContactCreate,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> AlertContactResponse:
        """Add a new emergency contact at the lowest priority slot (max 5)."""
        return create_alert_contact(db=db, user_id=user.id, payload=payload)

    @router.put(
        "/users/me/alert-contacts/order",
        response_model=list[AlertContactResponse],
    )
    def reorder_my_alert_contacts(
        payload: AlertContactsOrderPayload,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> list[AlertContactResponse]:
        """Reorder emergency contacts priorities (1..N)."""
        return reorder_alert_contacts(db=db, user_id=user.id, payload=payload)

    @router.patch(
        "/users/me/alert-contacts/{contact_id}",
        response_model=AlertContactResponse,
    )
    def patch_my_alert_contact(
        contact_id: int,
        payload: AlertContactUpdate,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> AlertContactResponse:
        """Update first_name, last_name, email, or phone of an emergency contact."""
        return update_alert_contact(
            db=db,
            user_id=user.id,
            contact_id=contact_id,
            payload=payload,
        )

    @router.delete(
        "/users/me/alert-contacts/{contact_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def remove_my_alert_contact(
        contact_id: int,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> Response:
        """Delete an emergency contact and re-compact remaining priorities."""
        delete_alert_contact(db=db, user_id=user.id, contact_id=contact_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/users/me/alert-contacts/{contact_id}/test-email",
        response_model=AlertContactTestEmailResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def send_my_alert_contact_test_email(
        contact_id: int,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> AlertContactTestEmailResponse:
        """Send a verification email (and optional test SMS) to an emergency contact (rate-limited to 3/hour per user)."""
        rl_key = f"alert_contact_test_email:user:{user.id}"
        allowed, retry_after = rate_limiter.is_allowed(
            rl_key, max_requests=3, window_seconds=3600.0
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Maximum of 3 test emails per hour reached. Please retry after {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )
        return send_test_email_to_contact(
            db=db,
            user=user,
            contact_id=contact_id,
            email_channel=email_channel,
            iot_publisher=publisher,
        )

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # D. Public Acknowledgment Endpoints (/api/v1/fall-alerts/ack)
    # Must be registered BEFORE /fall-alerts/{alert_id}
    # ------------------------------------------------------------------

    @router.get(
        "/fall-alerts/ack/info",
        response_model=FallAlertAckInfoResponse,
    )
    def get_fall_alert_ack_info(
        token: str = Query(default=""),
        db: Session = Depends(get_db),
    ) -> FallAlertAckInfoResponse:
        """Safe read-only information endpoint consumed by the frontend acknowledgment page."""
        try:
            alert, notif = get_alert_and_notification_for_token(db, token)
        except AckTokenError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=err.message,
            )

        patient_name = (
            (alert.user.name if alert.user and alert.user.name else None)
            or (alert.user.email if alert.user else None)
            or f"Patient #{alert.user_id}"
        )
        contact_name = f"{notif.contact_first_name} {notif.contact_last_name}".strip()
        paris_dt = format_paris_datetime(alert.detected_at_utc)
        fall_label = FALL_TYPE_LABELS_FR.get(alert.event_type, alert.event_type)
        confidence_pct = int(round(alert.confidence_score * 100))
        maps_url = (
            f"https://www.google.com/maps?q={alert.lat:.6f},{alert.lon:.6f}"
            if alert.lat is not None and alert.lon is not None
            else None
        )
        ack_person = (
            f"{alert.acknowledged_by_first_name or ''} {alert.acknowledged_by_last_name or ''}".strip()
            if (alert.acknowledged_by_first_name or alert.acknowledged_by_last_name)
            else None
        )
        return FallAlertAckInfoResponse(
            alert_id=alert.id,
            patient_name=patient_name,
            contact_name=contact_name,
            detected_at=alert.detected_at_utc,
            detected_at_paris=paris_dt,
            event_type=alert.event_type,
            event_type_label=fall_label,
            confidence_pct=confidence_pct,
            state=alert.state,
            lat=alert.lat,
            lon=alert.lon,
            accuracy_m=alert.accuracy_m,
            maps_url=maps_url,
            already_acknowledged_by=ack_person,
            cancel_reason=alert.cancel_reason,
        )

    @router.post(
        "/fall-alerts/ack",
        response_model=FallAlertAckResultResponse,
    )
    def confirm_fall_alert_ack(
        payload: FallAlertAckPayload,
        db: Session = Depends(get_db),
    ) -> FallAlertAckResultResponse:
        """Confirm acknowledgment of a fall alert via JSON payload."""
        try:
            alert, notif, _transitioned = acknowledge_fall_alert(
                db=db,
                token=payload.token,
                iot_publisher=publisher,
            )
        except AckTokenError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=err.message,
            )

        ack_name = f"{alert.acknowledged_by_first_name or notif.contact_first_name or ''} {alert.acknowledged_by_last_name or notif.contact_last_name or ''}".strip()
        return FallAlertAckResultResponse(
            status="ok",
            state=alert.state,
            alert_id=alert.id,
            acknowledged_by=ack_name,
            message=f"Prise en charge enregistrée avec succès par {ack_name}.",
        )

    # C. Fall Alert History (/api/v1/fall-alerts)
    # ------------------------------------------------------------------

    @router.post(
        "/fall-alerts/sms-status",
        response_model=FallAlertIngestResponse,
        summary="Report native SMS delivery status from mobile gateway",
    )
    def report_sms_status(
        payload: FallAlertSmsStatusRequest,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> FallAlertIngestResponse:
        """Report execution status of SMS relay directly from authenticated mobile client."""
        return update_sms_notification_status(
            db=db,
            user_id=user.id,
            payload=payload,
            iot_publisher=publisher,
        )

    @router.get(
        "/fall-alerts",
        response_model=PaginatedFallAlertsResponse,
    )
    def list_fall_alerts(
        user: CurrentUser,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        state: str | None = Query(
            default=None,
            description="Single state, comma-separated states (e.g. ACKNOWLEDGED,EXHAUSTED), or 'all'",
        ),
        start_date: datetime | None = Query(default=None),
        end_date: datetime | None = Query(default=None),
        user_id: str | None = Query(
            default=None,
            description="Filtrer par user_id numérique ou pseudonyme sujet SUBJ-XXXXXXXX (admin uniquement)",
        ),
        db: Session = Depends(get_db),
        request: Request = None,  # type: ignore[assignment]
    ) -> PaginatedFallAlertsResponse:
        """List fall alerts with pagination, state/date filtering, RBAC isolation, and GDPR pseudonymization."""
        raw_end = request.query_params.get("end_date") if request is not None else None
        norm_start, norm_end = validate_and_normalize_date_range(start_date, end_date, raw_end)

        query = db.query(FallAlert)

        if user.role != UserRole.admin:
            query = query.filter(FallAlert.user_id == user.id)
        elif user_id is not None:
            val = user_id.strip()
            if val:
                if val.isdigit():
                    query = query.filter(FallAlert.user_id == int(val))
                else:
                    user_ids = [uid for (uid,) in db.query(User.id).all()]
                    matching_user_ids = [
                        uid for uid in user_ids if val.lower() in get_subject_pseudonym(uid).lower()
                    ]
                    if matching_user_ids:
                        query = query.filter(FallAlert.user_id.in_(matching_user_ids))
                    else:
                        query = query.filter(FallAlert.user_id == -1)

        if state is not None and isinstance(state, str):
            raw_states = [s.strip().upper() for s in state.split(",") if s.strip()]
            if raw_states and not any(s == "ALL" for s in raw_states):
                query = query.filter(FallAlert.state.in_(raw_states))

        if norm_start is not None:
            query = query.filter(FallAlert.detected_at_utc >= norm_start)
        if norm_end is not None:
            query = query.filter(FallAlert.detected_at_utc <= norm_end)

        total = query.count()
        alerts = (
            query.order_by(FallAlert.detected_at_utc.desc(), FallAlert.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        items = [to_fall_alert_summary(a, user) for a in alerts]
        return PaginatedFallAlertsResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
        )

    @router.get(
        "/fall-alerts/{alert_id}",
        response_model=FallAlertDetail,
    )
    def get_fall_alert_detail(
        alert_id: int,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> FallAlertDetail:
        """Retrieve full details and notification log for a single fall alert."""
        alert = db.query(FallAlert).filter(FallAlert.id == alert_id).one_or_none()
        if alert is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Fall alert not found",
            )
        if user.role != UserRole.admin and alert.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Fall alert not found",
            )
        return to_fall_alert_detail(alert, user)

    return router
