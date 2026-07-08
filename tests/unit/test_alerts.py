from geoecon import alerts

class Cfg:
    from_addr = "me@gmail.com"; recipients = ("a@x.com", "b@y.com")
    smtp_host = "h"; smtp_port = 587; smtp_user = "me@gmail.com"; smtp_password = "pw"

def test_build_alert_is_plaintext_with_prefix():
    msg = alerts.build_alert(Cfg(), "pipeline failed at stage gate", "boom\ntrace")
    assert msg["From"] == "me@gmail.com"
    assert msg["To"] == "a@x.com, b@y.com"
    assert msg["Subject"] == "[GEOECON ALERT] pipeline failed at stage gate"
    assert msg.get_content_type() == "text/plain"
    assert "boom" in msg.get_content()

def test_send_alert_returns_true_on_success():
    seen = {}
    def transport(cfg, msg): seen["subject"] = msg["Subject"]
    assert alerts.send_alert(Cfg(), "s", "b", transport=transport) is True
    assert seen["subject"] == "[GEOECON ALERT] s"

def test_send_alert_never_raises():
    def transport(cfg, msg): raise RuntimeError("smtp down")
    assert alerts.send_alert(Cfg(), "s", "b", transport=transport) is False
