from __future__ import annotations
import logging, smtplib, ssl
from email.message import EmailMessage

log = logging.getLogger("geoecon.alerts")

SUBJECT_PREFIX = "[GEOECON ALERT] "


def _smtp_transport(cfg, msg):
    ctx = ssl.create_default_context()
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(cfg.smtp_user, cfg.smtp_password)
        s.send_message(msg)


def build_alert(cfg, subject, body) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.recipients)
    msg["Subject"] = SUBJECT_PREFIX + subject
    msg.set_content(body)
    return msg


def send_alert(cfg, subject, body, *, transport=_smtp_transport) -> bool:
    """Best-effort ops alert. NEVER raises — an alerting failure must not
    mask or alter the original failure path it is reporting on."""
    try:
        transport(cfg, build_alert(cfg, subject, body))
        log.info("alert sent: %s", subject)
        return True
    except Exception as e:
        log.error("alert send failed (%s): %s", subject, e)
        return False
