import smtplib
import pytest
from aiosmtpd.controller import Controller
from geoecon.stages import send


class Sink:
    def __init__(self): self.messages = []
    async def handle_DATA(self, server, session, envelope):
        self.messages.append(envelope.content); return "250 OK"


class Cfg:
    from_addr = "me@x.com"; recipients = ("a@x.com",)
    smtp_host = "127.0.0.1"; smtp_port = 8025; smtp_user = "u"; smtp_password = "p"


def test_message_reaches_local_smtp():
    sink = Sink()
    try:
        ctrl = Controller(sink, hostname="127.0.0.1", port=8025)
        ctrl.start()
    except OSError as e:
        pytest.skip(f"Could not bind local SMTP server on 127.0.0.1:8025: {e}")
    try:
        def plain_transport(cfg, msg):
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=10) as s:
                s.send_message(msg)

        out = send.run(
            Cfg(),
            type("R", (), {"update_newsletter_send": lambda *a, **k: None})(),
            1, "Hi", "<b>x</b>",
            transport=plain_transport,
        )
        assert out["send_status"] == "sent" and len(sink.messages) == 1
    finally:
        ctrl.stop()
