from __future__ import annotations
import logging, smtplib, ssl
from email.message import EmailMessage

log = logging.getLogger("geoecon.send")


def build_message(cfg, subject, html) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.recipients)
    msg["Subject"] = subject
    msg.set_content("HTML email — please view in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")
    return msg


def _smtp_transport(cfg, msg):
    ctx = ssl.create_default_context()
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60) as s:
        s.starttls(context=ctx)
        s.login(cfg.smtp_user, cfg.smtp_password)
        s.send_message(msg)


def run(cfg, repo, newsletter_id, subject, html, *, transport=_smtp_transport) -> dict:
    msg = build_message(cfg, subject, html)
    try:
        transport(cfg, msg)
    except Exception as e:
        log.error("send failed: %s", e)
        repo.update_newsletter_send(newsletter_id, "failed", send_error=str(e)[:2000])
        return {"send_status": "failed", "error": str(e)}
    repo.update_newsletter_send(newsletter_id, "sent")
    log.info("send: ok -> %s", cfg.recipients)
    return {"send_status": "sent"}
