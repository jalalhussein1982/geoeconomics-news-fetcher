from geoecon.stages import send

class Cfg:
    from_addr = "me@gmail.com"; recipients = ("a@x.com", "b@y.com")
    smtp_host = "h"; smtp_port = 587; smtp_user = "me@gmail.com"; smtp_password = "pw"

def test_build_message_sets_headers_and_html():
    msg = send.build_message(Cfg(), "Subject X", "<b>hi</b>")
    assert msg["From"] == "me@gmail.com"
    assert msg["To"] == "a@x.com, b@y.com"
    assert msg["Subject"] == "Subject X"
    assert msg.get_content_type() == "text/html"

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
