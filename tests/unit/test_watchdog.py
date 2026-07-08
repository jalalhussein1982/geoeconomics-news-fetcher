from datetime import datetime, timezone
import pytest
from geoecon import alerts, watchdog


class StubRepo:
    def __init__(self, run=None, newsletters=(), fails=()):
        self._run, self._nls, self._fails = run, list(newsletters), list(fails)
    def latest_run_for_window(self, start, end): return self._run
    def newsletters_for_run(self, rid): return self._nls
    def failed_stages(self, rid): return self._fails


def _completed_run():
    return {"run_id": 9, "status": "completed",
            "started_at": datetime(2026, 7, 8, 5, 0, tzinfo=timezone.utc),
            "finished_at": datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc)}


def test_local_day_window_prague_summer():
    # 2026-07-08 10:00 UTC is 12:00 CEST; Prague day = [2026-07-07 22:00, 2026-07-08 22:00) UTC
    start, end = watchdog.local_day_window(
        datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc), "Europe/Prague")
    assert start == datetime(2026, 7, 7, 22, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 8, 22, 0, tzinfo=timezone.utc)


def test_completed_and_sent_is_ok():
    repo = StubRepo(run=_completed_run(),
                    newsletters=[{"send_status": "sent"}, {"send_status": "sent"}])
    assert watchdog.check(repo, None, None)["ok"] is True


def test_completed_but_nothing_sent_alerts():
    v = watchdog.check(StubRepo(run=_completed_run(), newsletters=[]), None, None)
    assert v["ok"] is False and "sent no newsletter" in v["subject"]


def test_failed_run_alerts_with_stage_error():
    run = dict(_completed_run(), status="failed")
    v = watchdog.check(StubRepo(run=run, fails=[{"stage_name": "gate", "error": "boom"}]),
                       None, None)
    assert v["ok"] is False
    assert "latest: failed" in v["subject"]
    assert "gate: boom" in v["body"]


def test_no_run_alerts_never_started():
    v = watchdog.check(StubRepo(run=None), None, None)
    assert v["ok"] is False and "no pipeline run today" in v["subject"]


def test_run_sends_alert_only_when_unhealthy(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_alert",
                        lambda cfg, subject, body, **kw: sent.append(subject) or True)
    class Cfg: timezone = "Europe/Prague"
    now = datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc)
    ok_repo = StubRepo(run=_completed_run(), newsletters=[{"send_status": "sent"}])
    assert watchdog.run(Cfg(), ok_repo, now=now) == 0 and sent == []
    assert watchdog.run(Cfg(), StubRepo(run=None), now=now) == 0
    assert sent == ["watchdog: no pipeline run today"]


def test_cli_dispatch_runs_watchdog_without_migrations(monkeypatch):
    from geoecon import orchestrator
    calls = {}
    class FakeCfg: db_dsn = "postgresql://x"; timezone = "Europe/Prague"
    monkeypatch.setattr(orchestrator.Config, "from_env", classmethod(lambda cls, env: FakeCfg()))
    from geoecon.state import db as dbmod
    monkeypatch.setattr(dbmod, "connect", lambda dsn: calls.setdefault("connect", dsn))
    monkeypatch.setattr(dbmod, "apply_migrations",
                        lambda conn: calls.setdefault("migrated", True))
    monkeypatch.setattr(watchdog, "run", lambda cfg, repo, **kw: calls.update(ran=True) or 0)
    assert orchestrator.main(["watchdog"]) == 0
    assert calls.get("ran") is True and "migrated" not in calls
