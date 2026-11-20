import pytest
from geoecon.stages import send

class Cfg:
    from_addr = "me@gmail.com"; recipients = ("a@x.com", "b@y.com")
    smtp_host = "h"; smtp_port = 587; smtp_user = "me@gmail.com"; smtp_password = "pw"

def test_build_message_sets_headers_and_html():
    msg = send.build_message(Cfg(), "Subject X", "<b>hi</b>")
    assert msg["From"] == "me@gmail.com"
    assert msg["To"] == "a@x.com, b@y.com"
    assert msg["Subject"] == "Subject X"
    assert msg.get_content_type() == "multipart/alternative"
    html_part = msg.get_body(preferencelist=("html",))
    assert html_part is not None and html_part.get_content_type() == "text/html"
    assert "<b>hi</b>" in html_part.get_content()

def test_run_marks_sent_on_success():
    sent = {}
    class FakeRepo:
        def update_newsletter_send(self, nid, status, send_error=None):
            sent["status"] = status
    def transport(cfg, msg): return None  # success
    out = send.run(Cfg(), FakeRepo(), 1, "S", "<b>h</b>", transport=transport)
    assert out["send_status"] == "sent" and sent["status"] == "sent"

def test_run_marks_failed_on_error():
    sent = {}
    class FakeRepo:
        def update_newsletter_send(self, nid, status, send_error=None):
            sent["status"] = status; sent["err"] = send_error
    def transport(cfg, msg): raise RuntimeError("smtp down")
    out = send.run(Cfg(), FakeRepo(), 1, "S", "<b>h</b>", transport=transport)
    assert out["send_status"] == "failed" and "smtp down" in sent["err"]

def test_send_run_still_sends_one_newsletter():
    # send.run contract is unchanged: one newsletter at a time
    sent = {}
    class FakeRepo:
        def update_newsletter_send(self, nid, status, send_error=None):
            sent["status"] = status
    out = send.run(Cfg(), FakeRepo(), 1, "Geoeconomic Brief — Middle East — 2026-06-21",
                   "<b>h</b>", transport=lambda cfg, msg: None)
    assert out["send_status"] == "sent" and sent["status"] == "sent"


def test_send_pending_all_success_marks_all_sent():
    class FakeRepo:
        def update_newsletter_send(self, nid, status, send_error=None): pass
    newsletters = [
        {"newsletter_id": 1, "bucket": "middle_east", "email_html": "<b>me</b>", "send_status": "pending"},
        {"newsletter_id": 2, "bucket": "latam", "email_html": "<b>la</b>", "send_status": "pending"},
    ]
    out = send.send_pending(Cfg(), FakeRepo(), newsletters, lambda b: f"S {b}",
                            transport=lambda cfg, msg: None)
    assert out["all_sent"] is True and out["any_sent"] is True
    assert ("middle_east", "sent") in out["results"] and ("latam", "sent") in out["results"]


def test_send_pending_partial_failure_is_not_all_sent():
    class FakeRepo:
        def update_newsletter_send(self, nid, status, send_error=None): pass
    def transport(cfg, msg):
        if "middle_east" in msg["Subject"]:
            raise RuntimeError("smtp down")
    newsletters = [
        {"newsletter_id": 1, "bucket": "middle_east", "email_html": "<b>me</b>", "send_status": "pending"},
        {"newsletter_id": 2, "bucket": "latam", "email_html": "<b>la</b>", "send_status": "pending"},
    ]
    out = send.send_pending(Cfg(), FakeRepo(), newsletters, lambda b: f"S {b}", transport=transport)
    assert out["all_sent"] is False and out["any_sent"] is True


def test_send_pending_skips_already_sent():
    calls = []
    class FakeRepo:
        def update_newsletter_send(self, nid, status, send_error=None): calls.append(nid)
    newsletters = [
        {"newsletter_id": 1, "bucket": "latam", "email_html": "<b>la</b>", "send_status": "sent"},
    ]
    out = send.send_pending(Cfg(), FakeRepo(), newsletters, lambda b: f"S {b}",
                            transport=lambda cfg, msg: None)
    assert out["results"] == [("latam", "already_sent")] and out["all_sent"] is True
    assert calls == []


# ---- transient-failure retry (2026-11-06: one 60s Gmail timeout cost the LatAm brief) ----
import smtplib


class _RecordingRepo:
    def __init__(self): self.updates = []
    def update_newsletter_send(self, nid, status, send_error=None):
        self.updates.append((status, send_error))


def _flaky(failures):
    """Transport that raises each exception in `failures` in turn, then succeeds."""
    calls = []
    def transport(cfg, msg):
        calls.append(msg["Subject"])
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
    return transport, calls


def test_run_retries_a_timed_out_connection_and_then_sends():
    transport, calls = _flaky([smtplib.SMTPServerDisconnected(
        "Connection unexpectedly closed: timed out")])
    repo, waits = _RecordingRepo(), []
    out = send.run(Cfg(), repo, 1, "S", "<b>h</b>", transport=transport, sleep=waits.append)
    assert out["send_status"] == "sent"
    assert len(calls) == 2
    assert repo.updates == [("sent", None)]      # never recorded as failed along the way
    assert len(waits) == 1 and waits[0] > 0      # backed off before the second attempt


def test_run_gives_up_after_three_transient_failures():
    transport, calls = _flaky([TimeoutError("timed out")] * 10)
    repo, waits = _RecordingRepo(), []
    out = send.run(Cfg(), repo, 1, "S", "<b>h</b>", transport=transport, sleep=waits.append)
    assert out["send_status"] == "failed"
    assert len(calls) == 3 and len(waits) == 2
    assert repo.updates[-1][0] == "failed" and "timed out" in repo.updates[-1][1]


def test_run_retries_a_temporary_4xx_smtp_response():
    transport, calls = _flaky([smtplib.SMTPDataError(451, b"4.3.0 try again later")])
    out = send.run(Cfg(), _RecordingRepo(), 1, "S", "<b>h</b>", transport=transport,
                   sleep=lambda s: None)
    assert out["send_status"] == "sent" and len(calls) == 2


@pytest.mark.parametrize("exc", [
    smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"),
    smtplib.SMTPRecipientsRefused({"a@x.com": (550, b"no such user")}),
    RuntimeError("bug, not a network problem"),
])
def test_run_does_not_retry_a_permanent_failure(exc):
    # Retrying a rejected password just delays the alert by the full backoff.
    transport, calls = _flaky([exc] * 10)
    waits = []
    out = send.run(Cfg(), _RecordingRepo(), 1, "S", "<b>h</b>", transport=transport,
                   sleep=waits.append)
    assert out["send_status"] == "failed" and len(calls) == 1 and waits == []


def test_send_pending_retries_the_failing_region_so_all_three_go_out():
    # The 2026-11-06 shape: first region's connection hangs once, the others are fine.
    attempts = []
    def transport(cfg, msg):
        attempts.append(msg["Subject"])
        if attempts.count("S latam") == 1 and msg["Subject"] == "S latam":
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed: timed out")
    newsletters = [
        {"newsletter_id": 1, "bucket": "latam", "email_html": "<b>la</b>", "send_status": "pending"},
        {"newsletter_id": 2, "bucket": "middle_east", "email_html": "<b>me</b>", "send_status": "pending"},
        {"newsletter_id": 3, "bucket": "north_africa", "email_html": "<b>na</b>", "send_status": "pending"},
    ]
    out = send.send_pending(Cfg(), _RecordingRepo(), newsletters, lambda b: f"S {b}",
                            transport=transport, sleep=lambda s: None)
    assert out["all_sent"] is True
    assert out["results"] == [("latam", "sent"), ("middle_east", "sent"), ("north_africa", "sent")]
