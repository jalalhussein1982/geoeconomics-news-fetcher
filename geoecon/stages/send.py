from __future__ import annotations
import logging, smtplib, ssl, time
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


# Seconds to wait before the 2nd and 3rd attempt at one region's email. On
# 2026-11-06 a single hung Gmail connection (60s timeout) failed the LatAm brief
# while the other two regions went out one and two seconds later — one retry
# would have delivered it. Worst case per region: 3 x 60s timeout + 25s waiting.
RETRY_DELAYS = (5, 20)


def _is_transient(e: Exception) -> bool:
    """True for failures worth retrying: timeouts, dropped connections, 4xx replies.

    A rejected password, a refused recipient or a 5xx reply will fail identically
    on every attempt, so retrying only delays the alert. Anything that is not a
    network/SMTP error at all is a bug and is never retried.
    """
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return False
    if isinstance(e, smtplib.SMTPResponseException):   # auth, sender, data, connect, helo
        return 400 <= e.smtp_code < 500
    return isinstance(e, OSError)                      # timeouts, disconnects, TLS, DNS


def run(cfg, repo, newsletter_id, subject, html, *, transport=_smtp_transport,
        sleep=time.sleep) -> dict:
    """Send one newsletter, retrying transient SMTP failures.

    A timeout that strikes after the server accepted the message but before its
    reply arrived makes the retry a duplicate. That is accepted: a rare duplicate
    brief is better than a missing one.
    """
    msg = build_message(cfg, subject, html)
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            transport(cfg, msg)
            break
        except Exception as e:
            if attempt < attempts and _is_transient(e):
                delay = RETRY_DELAYS[attempt - 1]
                log.warning("send attempt %d/%d failed (%s) — retrying in %ss",
                            attempt, attempts, e, delay)
                sleep(delay)
                continue
            log.error("send failed after %d attempt(s): %s", attempt, e)
            repo.update_newsletter_send(newsletter_id, "failed", send_error=str(e)[:2000])
            return {"send_status": "failed", "error": str(e)}
    repo.update_newsletter_send(newsletter_id, "sent")
    log.info("send: ok -> %s", cfg.recipients)
    return {"send_status": "sent"}


def send_pending(cfg, repo, newsletters, subject_for, *, transport=_smtp_transport,
                 sleep=time.sleep) -> dict:
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
                  nl["email_html"], transport=transport, sleep=sleep)
        status = res.get("send_status")
        results.append((nl["bucket"], status))
        if status == "sent":
            any_sent = True
        else:
            any_failed = True
    return {"results": results, "any_sent": any_sent, "all_sent": not any_failed}
