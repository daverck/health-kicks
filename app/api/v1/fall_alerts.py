"""REST and HTML endpoints for emergency contacts CRUD, fall alert history, and public acknowledgment."""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.api.v1.utils import validate_and_normalize_date_range
from app.core.rate_limit import rate_limiter
from app.db.database import get_db
from app.db.models import FallAlert, FallAlertNotification, FallAlertState, UserRole
from app.schemas.fall_alert import (
    AlertContactCreate,
    AlertContactResponse,
    AlertContactsOrderPayload,
    AlertContactTestEmailResponse,
    AlertContactUpdate,
    FallAlertDetail,
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
)

logger = logging.getLogger("healthkicks.api.fall_alerts")


def _render_html_page(*, title: str, header_bg: str, heading: str, body_html: str) -> str:
    esc_title = html.escape(title)
    esc_heading = html.escape(heading)
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc_title}</title>
</head>
<body style="margin:0;padding:24px 16px;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0f172a;">
  <main style="max-width:560px;margin:24px auto;background:#ffffff;border-radius:14px;border:1px solid #e2e8f0;overflow:hidden;box-shadow:0 8px 24px rgba(15,23,42,0.06);">
    <header style="background:{header_bg};color:#ffffff;padding:20px 24px;">
      <h1 style="margin:0;font-size:20px;font-weight:700;">{esc_heading}</h1>
    </header>
    <section style="padding:24px;line-height:1.55;font-size:15px;">
      {body_html}
    </section>
  </main>
</body>
</html>"""


def _render_ack_landing_html(alert: FallAlert, notif: FallAlertNotification, token: str) -> str:
    patient_name = (
        (alert.user.name if alert.user and alert.user.name else None)
        or (alert.user.email if alert.user else None)
        or f"Patient #{alert.user_id}"
    )
    esc_patient = html.escape(patient_name)
    esc_contact = html.escape(f"{notif.contact_first_name} {notif.contact_last_name}".strip())
    paris_dt = format_paris_datetime(alert.detected_at_utc)
    fall_label = html.escape(FALL_TYPE_LABELS_FR.get(alert.event_type, alert.event_type))
    confidence_pct = int(round(alert.confidence_score * 100))
    esc_token = html.escape(token, quote=True)

    if alert.lat is not None and alert.lon is not None:
        maps_url = f"https://www.google.com/maps?q={alert.lat:.6f},{alert.lon:.6f}"
        esc_maps = html.escape(maps_url, quote=True)
        acc = f" (±{alert.accuracy_m:.0f} m)" if alert.accuracy_m is not None else ""
        maps_row = (
            f'<p style="margin:12px 0;"><strong>Position GPS :</strong> '
            f'<a href="{esc_maps}" target="_blank" rel="noopener" style="color:#0284c7;font-weight:600;">'
            f"Ouvrir dans Google Maps ({alert.lat:.5f}, {alert.lon:.5f})</a>{acc}</p>"
        )
    else:
        maps_row = '<p style="margin:12px 0;"><strong>Position GPS :</strong> Non disponible</p>'

    if alert.state in (FallAlertState.ESCALATING.value, FallAlertState.EXHAUSTED.value):
        body = f"""
      <p style="margin-top:0;">Bonjour <strong>{esc_contact}</strong>,</p>
      <p>Vous consultez l'alerte de chute déclenchée pour <strong>{esc_patient}</strong>.</p>
      <div style="background:#f8fafc;border-radius:10px;padding:14px 16px;margin:16px 0;border:1px solid #e2e8f0;">
        <p style="margin:6px 0;"><strong>Patient :</strong> {esc_patient}</p>
        <p style="margin:6px 0;"><strong>Événement :</strong> {fall_label} ({confidence_pct}%)</p>
        <p style="margin:6px 0;"><strong>Heure de détection :</strong> {paris_dt} (heure de Paris)</p>
        {maps_row}
      </div>
      <form method="post" action="/api/v1/fall-alerts/ack" style="margin:24px 0;text-align:center;">
        <input type="hidden" name="token" value="{esc_token}">
        <button type="submit" style="cursor:pointer;background:#16a34a;color:#ffffff;border:none;border-radius:8px;padding:14px 26px;font-size:16px;font-weight:700;">
          Je confirme prendre en charge cette alerte
        </button>
      </form>
      <p style="font-size:13px;color:#64748b;margin-bottom:0;">
        En confirmant, l'escalade vers les contacts suivants sera immédiatement arrêtée et le téléphone de {esc_patient} sera informé.
      </p>
        """
        return _render_html_page(
            title="Prise en charge d'alerte — HealthKicks",
            header_bg="#dc2626",
            heading="🚨 Alerte de chute — Confirmation requise",
            body_html=body,
        )

    if alert.state == FallAlertState.ACKNOWLEDGED.value:
        ack_person = html.escape(
            f"{alert.acknowledged_by_first_name or ''} {alert.acknowledged_by_last_name or ''}".strip()
            or "un proche"
        )
        body = f"""
      <p style="margin-top:0;">Cette alerte pour <strong>{esc_patient}</strong> (détectée le {paris_dt}) a déjà été prise en charge par <strong>{ack_person}</strong>.</p>
      {maps_row}
      <p style="color:#64748b;font-size:14px;margin-bottom:0;">Les contacts suivants ne sont plus sollicités.</p>
        """
        return _render_html_page(
            title="Alerte déjà prise en charge — HealthKicks",
            header_bg="#16a34a",
            heading="✅ Alerte déjà prise en charge",
            body_html=body,
        )

    if alert.state == FallAlertState.CANCELLED.value:
        body = f"""
      <p style="margin-top:0;">Bonne nouvelle : l'alerte de chute concernant <strong>{esc_patient}</strong> (détectée le {paris_dt}) a été <strong>annulée</strong> (le patient a indiqué aller bien ou s'est relevé).</p>
      <p style="color:#64748b;font-size:14px;margin-bottom:0;">Aucune action supplémentaire n'est requise.</p>
        """
        return _render_html_page(
            title="Alerte annulée — HealthKicks",
            header_bg="#0284c7",
            heading="ℹ️ Alerte annulée par le patient",
            body_html=body,
        )

    body = f"""
      <p style="margin-top:0;">Statut actuel de l'alerte pour <strong>{esc_patient}</strong> : <strong>{html.escape(alert.state)}</strong>.</p>
    """
    return _render_html_page(
        title="Statut de l'alerte — HealthKicks",
        header_bg="#475569",
        heading="Statut de l'alerte HealthKicks",
        body_html=body,
    )


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
        """Update first_name, last_name, or email of an emergency contact."""
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
        """Send a verification email to an emergency contact (rate-limited to 3/hour per user)."""
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
        )

    # ------------------------------------------------------------------
    # D. Public Acknowledgment HTML Routes (/api/v1/fall-alerts/ack)
    # Must be registered BEFORE /fall-alerts/{alert_id}
    # ------------------------------------------------------------------

    @router.get(
        "/fall-alerts/ack",
        response_class=HTMLResponse,
    )
    def get_fall_alert_ack_page(
        token: str = Query(default=""),
        db: Session = Depends(get_db),
    ) -> HTMLResponse:
        """Render the read-only confirmation landing page for an emergency contact (no DB mutation)."""
        try:
            alert, notif = get_alert_and_notification_for_token(db, token)
        except AckTokenError as err:
            heading = "⏳ Lien expiré" if err.expired else "⚠️ Lien invalide"
            body = (
                f"<p style='margin-top:0;'>{html.escape(err.message)}</p>"
                "<p style='margin-bottom:0;color:#64748b;'>En cas de doute ou d'urgence, contactez directement votre proche ou appelez le <strong>112</strong>.</p>"
            )
            return HTMLResponse(
                content=_render_html_page(
                    title="Lien de prise en charge invalide — HealthKicks",
                    header_bg="#dc2626",
                    heading=heading,
                    body_html=body,
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        return HTMLResponse(
            content=_render_ack_landing_html(alert, notif, token),
            status_code=status.HTTP_200_OK,
        )

    @router.post(
        "/fall-alerts/ack",
        response_class=HTMLResponse,
    )
    async def confirm_fall_alert_ack(
        request: Request,
        db: Session = Depends(get_db),
    ) -> HTMLResponse:
        """Confirm acknowledgment of a fall alert via form submission or JSON body."""
        token_value = ""
        content_type = (request.headers.get("content-type") or "").lower()
        raw_body = await request.body()
        body_str = raw_body.decode("utf-8", errors="replace").strip()

        if "application/json" in content_type and body_str:
            try:
                parsed_json = json.loads(body_str)
                if isinstance(parsed_json, dict):
                    token_value = str(parsed_json.get("token") or "")
            except Exception:
                token_value = ""
        elif body_str:
            parsed_form = parse_qs(body_str, keep_blank_values=True)
            if "token" in parsed_form and parsed_form["token"]:
                token_value = parsed_form["token"][0]
            else:
                try:
                    parsed_json = json.loads(body_str)
                    if isinstance(parsed_json, dict):
                        token_value = str(parsed_json.get("token") or "")
                except Exception:
                    token_value = ""

        if not token_value:
            token_value = request.query_params.get("token", "")

        try:
            alert, notif, transitioned = acknowledge_fall_alert(
                db=db,
                token=token_value,
                iot_publisher=publisher,
            )
        except AckTokenError as err:
            heading = "⏳ Lien expiré" if err.expired else "⚠️ Lien invalide"
            body = (
                f"<p style='margin-top:0;'>{html.escape(err.message)}</p>"
                "<p style='margin-bottom:0;color:#64748b;'>En cas de doute ou d'urgence, contactez directement votre proche ou appelez le <strong>112</strong>.</p>"
            )
            return HTMLResponse(
                content=_render_html_page(
                    title="Erreur de prise en charge — HealthKicks",
                    header_bg="#dc2626",
                    heading=heading,
                    body_html=body,
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if alert.state == FallAlertState.ACKNOWLEDGED.value:
            patient_name = (
                (alert.user.name if alert.user and alert.user.name else None)
                or (alert.user.email if alert.user else None)
                or f"Patient #{alert.user_id}"
            )
            esc_patient = html.escape(patient_name)
            esc_contact = html.escape(
                f"{alert.acknowledged_by_first_name or notif.contact_first_name} "
                f"{alert.acknowledged_by_last_name or notif.contact_last_name}".strip()
            )
            maps_html = ""
            if alert.lat is not None and alert.lon is not None:
                maps_url = html.escape(
                    f"https://www.google.com/maps?q={alert.lat:.6f},{alert.lon:.6f}",
                    quote=True,
                )
                maps_html = (
                    f'<p style="margin:14px 0;"><a href="{maps_url}" target="_blank" rel="noopener" '
                    f'style="color:#0284c7;font-weight:600;">Ouvrir la position GPS dans Google Maps</a></p>'
                )
            body = f"""
      <p style="margin-top:0;font-size:16px;">
        <strong>Merci {esc_contact}, votre prise en charge est enregistrée.</strong>
      </p>
      <p>
        Les contacts suivants ne seront pas sollicités et le téléphone de <strong>{esc_patient}</strong> a été informé de votre intervention.
      </p>
      {maps_html}
      <p style="color:#64748b;font-size:13px;margin-bottom:0;">
        Si la situation nécessite des secours médicaux d'urgence, appelez le <strong>112</strong>.
      </p>
            """
            return HTMLResponse(
                content=_render_html_page(
                    title="Prise en charge confirmée — HealthKicks",
                    header_bg="#16a34a",
                    heading="✅ Prise en charge confirmée",
                    body_html=body,
                ),
                status_code=status.HTTP_200_OK,
            )

        return HTMLResponse(
            content=_render_ack_landing_html(alert, notif, token_value),
            status_code=status.HTTP_200_OK,
        )

    # ------------------------------------------------------------------
    # C. Fall Alert History (/api/v1/fall-alerts)
    # ------------------------------------------------------------------

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
        user_id: int | None = Query(
            default=None,
            description="Filter by user_id (taken into account only for admin role)",
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
            query = query.filter(FallAlert.user_id == user_id)

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
