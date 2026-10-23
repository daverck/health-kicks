"""SMTP email delivery service and SMS message builders for fall alerting."""

from __future__ import annotations

import html
import logging
import smtplib
from datetime import UTC, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import Settings, get_settings

logger = logging.getLogger("healthkicks.email")

FALL_TYPE_LABELS_FR: dict[str, str] = {
    "fall_forward": "Chute avant",
    "fall_backward": "Chute arrière",
    "fall_lateral": "Chute latérale",
    "fall_generic": "Chute détectée",
}


def format_paris_datetime(dt: datetime) -> str:
    """Format a UTC datetime into Europe/Paris local time string (dd/mm/YYYY à HH:MM:SS)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    try:
        paris_tz = ZoneInfo("Europe/Paris")
        local_dt = dt.astimezone(paris_tz)
    except ZoneInfoNotFoundError:
        # Fallback CET/CEST calculation for Windows environments without tzdata package
        utc_dt = dt.astimezone(UTC)
        year = utc_dt.year
        # Last Sunday of March at 01:00 UTC
        march_31 = datetime(year, 3, 31, 1, 0, tzinfo=UTC)
        dst_start = march_31 - timedelta(days=(march_31.weekday() + 1) % 7)
        # Last Sunday of October at 01:00 UTC
        oct_31 = datetime(year, 10, 31, 1, 0, tzinfo=UTC)
        dst_end = oct_31 - timedelta(days=(oct_31.weekday() + 1) % 7)
        offset_hours = 2 if dst_start <= utc_dt < dst_end else 1
        local_dt = utc_dt.astimezone(timezone(timedelta(hours=offset_hours)))
    return local_dt.strftime("%d/%m/%Y à %H:%M:%S")


def build_fall_alert_sms_body(
    *,
    patient_name: str,
    detected_at_str: str,
    ack_url: str,
    maps_url: str | None = None,
) -> str:
    """Build a concise plain-text SMS body for emergency fall alerting."""
    location_part = f" Position : {maps_url}" if maps_url else ""
    return (
        f"[HealthKicks URGENCE] Chute détectée pour {patient_name} ({detected_at_str})."
        f"{location_part} Confirmez la prise en charge : {ack_url}"
    )


def build_test_sms_body(*, patient_name: str, priority: int) -> str:
    """Build a plain-text test SMS body for emergency contact verification."""
    return (
        f"[HealthKicks] Test SMS : vous êtes configuré(e) comme proche d'urgence "
        f"(priorité n°{priority}) pour {patient_name}."
    )


class SmtpEmailChannel:
    """Dispatches multipart/alternative (plain-text + HTML) emails over SMTP STARTTLS."""

    def __init__(
        self,
        config: Settings | None = None,
        smtp_factory: Callable[[str, int, float], smtplib.SMTP] | None = None,
    ) -> None:
        self._config = config
        self._smtp_factory = smtp_factory

    @property
    def config(self) -> Settings:
        return self._config or get_settings()

    def _create_smtp(self, host: str, port: int, timeout: float = 10.0) -> smtplib.SMTP:
        if self._smtp_factory is not None:
            return self._smtp_factory(host, port, timeout)
        return smtplib.SMTP(host, port, timeout=timeout)

    def send_message(self, message: EmailMessage, max_attempts: int = 2) -> None:
        """Send an EmailMessage via SMTP STARTTLS with up to `max_attempts` tries."""
        cfg = self.config
        last_exc: Exception | None = None
        for attempt in range(1, max(1, max_attempts) + 1):
            try:
                with self._create_smtp(cfg.smtp_host, cfg.smtp_port, 10.0) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    if cfg.smtp_user and cfg.smtp_password:
                        server.login(cfg.smtp_user, cfg.smtp_password)
                    server.send_message(message)
                return
            except (smtplib.SMTPException, OSError) as exc:
                last_exc = exc
                logger.warning(
                    "SMTP delivery attempt %d/%d failed for recipient %s: %s",
                    attempt,
                    max_attempts,
                    message.get("To"),
                    exc,
                )
        if last_exc is not None:
            raise last_exc

    def send_fall_alert_email(
        self,
        *,
        recipient_email: str,
        contact_first_name: str,
        contact_last_name: str,
        patient_name: str,
        device_id: str,
        event_type: str,
        confidence_score: float,
        detected_at_utc: datetime,
        rank: int,
        total_contacts: int,
        ack_url: str,
        lat: float | None = None,
        lon: float | None = None,
        accuracy_m: float | None = None,
    ) -> None:
        """Build and send an urgent fall alert email with a signed acknowledgment link."""
        cfg = self.config
        sender_email = cfg.smtp_user or "no-reply@healthkicks.local"
        paris_time_str = format_paris_datetime(detected_at_utc)
        fall_label = FALL_TYPE_LABELS_FR.get(event_type, event_type)
        confidence_pct = int(round(confidence_score * 100))

        maps_url: str | None = None
        if lat is not None and lon is not None:
            maps_url = f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"

        subject = (
            f"[URGENCE HealthKicks] Chute détectée pour {patient_name} "
            f"(Contact {rank}/{total_contacts})"
        )

        location_text = (
            f"Position GPS : {maps_url}"
            + (f" (précision ±{accuracy_m:.0f} m)" if accuracy_m is not None else "")
            if maps_url
            else "Position GPS : Non disponible au moment de l'alerte"
        )

        text_body = (
            f"Bonjour {contact_first_name} {contact_last_name},\n\n"
            f"Une chute a été détectée par la chaussure connectée HealthKicks de {patient_name}.\n"
            f"Aucune annulation n'a été reçue dans le délai de sécurité de 60 secondes.\n\n"
            f"Détails de l'alerte :\n"
            f"- Patient : {patient_name}\n"
            f"- Type d'événement : {fall_label} (indice de confiance : {confidence_pct}%)\n"
            f"- Équipement : {device_id}\n"
            f"- Date et heure : {paris_time_str} (heure de Paris)\n"
            f"- Ordre d'escalade : Proche n°{rank} sur {total_contacts}\n"
            f"- {location_text}\n\n"
            f"IMPORTANT — Pour confirmer que vous prenez en charge cette alerte et "
            f"arrêter l'escalade vers les contacts suivants, cliquez sur le lien ci-dessous :\n"
            f"{ack_url}\n\n"
            f"Sans confirmation sous 3 minutes, le proche suivant sera automatiquement sollicité.\n"
            f"En cas d'urgence vitale, appelez immédiatement le 112.\n"
        )

        esc_contact_first = html.escape(contact_first_name)
        esc_contact_last = html.escape(contact_last_name)
        esc_patient = html.escape(patient_name)
        esc_fall_label = html.escape(fall_label)
        esc_device = html.escape(device_id)
        esc_ack_url = html.escape(ack_url, quote=True)

        if maps_url:
            esc_maps_url = html.escape(maps_url, quote=True)
            acc_str = f" (précision ±{accuracy_m:.0f} m)" if accuracy_m is not None else ""
            location_html = (
                f'<a href="{esc_maps_url}" style="color:#0284c7;font-weight:600;">'
                f"Voir la position sur Google Maps ({lat:.5f}, {lon:.5f})</a>{acc_str}"
            )
        else:
            location_html = "Non disponible au moment de l'alerte"

        html_body = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Alerte de chute HealthKicks</title>
</head>
<body style="margin:0;padding:24px;background-color:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0f172a;">
  <div style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;border:1px solid #fecaca;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,0.05);">
    <div style="background:#dc2626;color:#ffffff;padding:20px 24px;">
      <h1 style="margin:0;font-size:20px;font-weight:700;">🚨 Alerte de chute — HealthKicks</h1>
      <p style="margin:6px 0 0;font-size:14px;opacity:0.95;">Contact d'urgence n°{rank} sur {total_contacts}</p>
    </div>
    <div style="padding:24px;">
      <p style="margin-top:0;font-size:16px;">Bonjour <strong>{esc_contact_first} {esc_contact_last}</strong>,</p>
      <p style="font-size:15px;line-height:1.5;">
        La chaussure connectée de <strong>{esc_patient}</strong> a détecté une chute et aucune annulation n'a été reçue dans le délai de 60 secondes.
      </p>
      <table style="width:100%;border-collapse:collapse;margin:20px 0;background:#f8fafc;border-radius:8px;padding:12px;">
        <tr>
          <td style="padding:8px 12px;color:#64748b;font-size:14px;">Patient</td>
          <td style="padding:8px 12px;font-weight:600;font-size:14px;">{esc_patient}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;color:#64748b;font-size:14px;">Type de chute</td>
          <td style="padding:8px 12px;font-weight:600;font-size:14px;">{esc_fall_label} ({confidence_pct}%)</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;color:#64748b;font-size:14px;">Date et heure</td>
          <td style="padding:8px 12px;font-weight:600;font-size:14px;">{paris_time_str} (heure de Paris)</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;color:#64748b;font-size:14px;">Équipement</td>
          <td style="padding:8px 12px;font-size:14px;">{esc_device}</td>
        </tr>
        <tr>
          <td style="padding:8px 12px;color:#64748b;font-size:14px;">Localisation GPS</td>
          <td style="padding:8px 12px;font-size:14px;">{location_html}</td>
        </tr>
      </table>
      <div style="text-align:center;margin:28px 0;">
        <a href="{esc_ack_url}" style="display:inline-block;background:#16a34a;color:#ffffff;text-decoration:none;font-weight:700;font-size:16px;padding:14px 28px;border-radius:8px;">
          Je prends en charge cette alerte
        </a>
      </div>
      <p style="font-size:13px;color:#64748b;line-height:1.5;margin-bottom:0;">
        Sans confirmation de votre part dans un délai de <strong>3 minutes</strong>, le contact d'urgence suivant sera automatiquement prévenu. En cas d'urgence médicale avérée, composez le <strong>112</strong>.
      </p>
    </div>
  </div>
</body>
</html>"""

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((cfg.smtp_from_name, sender_email))
        msg["To"] = formataddr((f"{contact_first_name} {contact_last_name}", recipient_email))
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")

        self.send_message(msg, max_attempts=2)

    def send_test_email(
        self,
        *,
        recipient_email: str,
        contact_first_name: str,
        contact_last_name: str,
        patient_name: str,
        priority: int,
    ) -> None:
        """Send a test verification email to an emergency contact."""
        cfg = self.config
        sender_email = cfg.smtp_user or "no-reply@healthkicks.local"
        now_paris = format_paris_datetime(datetime.now(UTC))

        subject = f"[HealthKicks] Email de test — Contact d'urgence configuré pour {patient_name}"
        text_body = (
            f"Bonjour {contact_first_name} {contact_last_name},\n\n"
            f"Ceci est un email de test envoyé depuis la plateforme HealthKicks le {now_paris} (heure de Paris).\n"
            f"{patient_name} vous a configuré(e) comme contact d'urgence de rang #{priority} en cas de chute.\n\n"
            f"En cas de chute détectée en mode Surveillance Active sans annulation sous 60 secondes, "
            f"vous recevrez un email contenant la position GPS et un bouton de prise en charge.\n\n"
            f"Aucune action n'est requise pour cet email de test.\n"
        )

        esc_first = html.escape(contact_first_name)
        esc_last = html.escape(contact_last_name)
        esc_patient = html.escape(patient_name)

        html_body = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Email de test HealthKicks</title>
</head>
<body style="margin:0;padding:24px;background-color:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0f172a;">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:12px;border:1px solid #e2e8f0;padding:24px;">
    <h2 style="margin-top:0;color:#0284c7;">✅ Configuration vérifiée — HealthKicks</h2>
    <p>Bonjour <strong>{esc_first} {esc_last}</strong>,</p>
    <p>
      Ceci est un email de test envoyé le <strong>{now_paris}</strong> (heure de Paris).
      <strong>{esc_patient}</strong> vous a enregistré(e) comme contact d'urgence (priorité <strong>#{priority}</strong>).
    </p>
    <p style="color:#64748b;font-size:14px;margin-bottom:0;">
      En cas d'alerte réelle, vous recevrez un lien direct permettant de confirmer la prise en charge. Aucune action n'est requise aujourd'hui.
    </p>
  </div>
</body>
</html>"""

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((cfg.smtp_from_name, sender_email))
        msg["To"] = formataddr((f"{contact_first_name} {contact_last_name}", recipient_email))
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")

        self.send_message(msg, max_attempts=1)
