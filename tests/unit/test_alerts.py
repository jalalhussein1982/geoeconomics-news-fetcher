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


def test_build_alert_can_use_the_cleanup_prefix():
    """Retrospective housekeeping must not look like a live failure in the inbox.
    On 2026-07-30 a reconciliation email that merely closed out runs which had
    died two nights earlier carried the same '[GEOECON ALERT]' subject as a real
    outage, and was read as a third consecutive incident."""
    msg = alerts.build_alert(Cfg(), "3 abandoned run(s) closed out", "body",
                             prefix=alerts.CLEANUP_PREFIX)
    assert msg["Subject"] == "[GEOECON CLEANUP] 3 abandoned run(s) closed out"
    assert not msg["Subject"].startswith(alerts.SUBJECT_PREFIX)


def test_send_alert_passes_the_prefix_through_to_the_message():
    seen = {}
    def transport(cfg, msg): seen["subject"] = msg["Subject"]
    assert alerts.send_alert(Cfg(), "s", "b", prefix=alerts.CLEANUP_PREFIX,
                             transport=transport) is True
    assert seen["subject"] == "[GEOECON CLEANUP] s"


def test_alert_prefix_defaults_to_the_live_failure_one():
    """The default must stay '[GEOECON ALERT]' — every genuine failure path calls
    send_alert without a prefix, so a changed default would silently downgrade
    real outages to housekeeping."""
    seen = {}
    def transport(cfg, msg): seen["subject"] = msg["Subject"]
    alerts.send_alert(Cfg(), "pipeline failed at stage gate", "b", transport=transport)
    assert seen["subject"] == "[GEOECON ALERT] pipeline failed at stage gate"
