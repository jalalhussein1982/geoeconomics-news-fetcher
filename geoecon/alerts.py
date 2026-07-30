from __future__ import annotations
import logging, smtplib, ssl
from email.message import EmailMessage

log = logging.getLogger("geoecon.alerts")

# Two prefixes, because the two kinds of mail demand different reactions and an
# inbox only shows the subject. On 2026-07-30 a reconciliation email that merely
# closed out runs which had died two nights earlier carried the ALERT prefix and
# was read as a third consecutive outage.
SUBJECT_PREFIX = "[GEOECON ALERT] "     # live failure — something needs attention now
CLEANUP_PREFIX = "[GEOECON CLEANUP] "   # retrospective housekeeping — nothing to do


def _smtp_transport(cfg, msg):
    ctx = ssl.create_default_context()
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(cfg.smtp_user, cfg.smtp_password)
        s.send_message(msg)


def build_alert(cfg, subject, body, *, prefix=SUBJECT_PREFIX) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.recipients)
    msg["Subject"] = prefix + subject
    msg.set_content(body)
    return msg


def send_alert(cfg, subject, body, *, prefix=SUBJECT_PREFIX,
               transport=_smtp_transport) -> bool:
    """Best-effort ops alert. NEVER raises — an alerting failure must not
    mask or alter the original failure path it is reporting on.

    `prefix` defaults to the live-failure one: every genuine failure path calls
    this without a prefix, so the default must never become CLEANUP_PREFIX.
    """
    try:
        transport(cfg, build_alert(cfg, subject, body, prefix=prefix))
        log.info("alert sent: %s", subject)
        return True
    except Exception as e:
        log.error("alert send failed (%s): %s", subject, e)
        return False
