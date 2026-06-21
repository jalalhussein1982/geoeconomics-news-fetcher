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
