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


def send_pending(cfg, repo, newsletters, subject_for, *, transport=_smtp_transport) -> dict:
    """Send every not-yet-sent newsletter. Returns {results, any_sent, all_sent}.

    subject_for(bucket) -> subject string. Does NOT touch the seen-store; the
    caller decides whether to mark events emitted based on all_sent.
    """
    results = []
    any_sent = False
    any_failed = False
    for nl in newsletters:
        if nl.get("send_status") == "sent":
            results.append((nl["bucket"], "already_sent"))
            continue
        res = run(cfg, repo, nl["newsletter_id"], subject_for(nl["bucket"]),
                  nl["email_html"], transport=transport)
        status = res.get("send_status")
        results.append((nl["bucket"], status))
        if status == "sent":
            any_sent = True
        else:
            any_failed = True
    return {"results": results, "any_sent": any_sent, "all_sent": not any_failed}
