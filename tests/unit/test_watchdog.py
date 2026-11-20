from datetime import datetime, timezone
from geoecon import alerts, watchdog


class StubRepo:
    """`run=` is shorthand for a day with exactly one run."""
    def __init__(self, run=None, newsletters=(), fails=(), runs=None,
                 newsletters_by_run=None, fails_by_run=None):
        self._runs = list(runs) if runs is not None else ([run] if run else [])
        self._nls = newsletters_by_run or {r["run_id"]: list(newsletters) for r in self._runs}
        self._fails = fails_by_run or {r["run_id"]: list(fails) for r in self._runs}
    def runs_for_window(self, start, end): return self._runs
    def newsletters_for_run(self, rid): return self._nls.get(rid, [])
    def failed_stages(self, rid): return self._fails.get(rid, [])


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


def test_run_exits_nonzero_when_alert_send_fails(monkeypatch):
    monkeypatch.setattr(alerts, "send_alert", lambda cfg, subject, body, **kw: False)
    class Cfg: timezone = "Europe/Prague"
    now = datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc)
    assert watchdog.run(Cfg(), StubRepo(run=None), now=now) == 1


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


# ---- the watchdog judges the whole day, not just the newest run row ----
# 2026-11-06: run 198 delivered 2 of 3 briefs; the Job's last retry (run 199)
# segfaulted and stayed 'running'. The watchdog looked only at 199 and reported
# "no completed run today (latest: running)" — nothing was running, and it said
# nothing about the two briefs that had arrived or the one that had not.

def _run(run_id, status, hour=0, minute=0):
    return {"run_id": run_id, "status": status,
            "started_at": datetime(2026, 11, 6, hour, minute, tzinfo=timezone.utc),
            "finished_at": None}


def _nl(bucket, status, error=None):
    return {"bucket": bucket, "send_status": status, "send_error": error}


def _night_of_nov_6():
    return StubRepo(
        runs=[_run(197, "running", 0, 0), _run(198, "failed", 0, 3), _run(199, "running", 0, 43)],
        newsletters_by_run={198: [
            _nl("latam", "failed", "Connection unexpectedly closed: timed out"),
            _nl("middle_east", "sent"), _nl("north_africa", "sent")]},
        fails_by_run={198: [{"stage_name": "send", "error": "send: some regions failed to send"}]})


def test_partial_delivery_reports_how_many_arrived_and_which_did_not():
    v = watchdog.check(_night_of_nov_6(), None, None)
    assert v["ok"] is False
    assert v["subject"] == "only 2 of 3 briefs sent today (missing: latam)"
    assert "running" not in v["subject"]


def test_partial_delivery_body_gives_the_send_error_and_every_attempt():
    body = watchdog.check(_night_of_nov_6(), None, None)["body"]
    assert "Connection unexpectedly closed: timed out" in body
    for rid in ("197", "198", "199"):
        assert f"run {rid}" in body
    assert "01:03" in body          # 00:03Z shown in Prague time, matching the schedule


def test_briefs_delivered_by_an_earlier_run_are_ok_despite_a_later_dead_attempt():
    repo = StubRepo(runs=[dict(_completed_run(), run_id=9), _run(10, "running", 4)],
                    newsletters_by_run={9: [_nl("latam", "sent"), _nl("middle_east", "sent")]})
    assert watchdog.check(repo, None, None)["ok"] is True


def test_all_briefs_sent_is_ok_even_if_the_run_row_says_failed():
    # e.g. the missing brief was re-sent by hand: the reader has everything.
    repo = StubRepo(run=_run(198, "failed"),
                    newsletters=[_nl("latam", "sent"), _nl("middle_east", "sent")])
    assert watchdog.check(repo, None, None)["ok"] is True


def test_nothing_sent_lists_every_attempt_and_explains_a_running_row():
    repo = StubRepo(runs=[_run(197, "running", 0, 0), _run(199, "running", 0, 43)])
    v = watchdog.check(repo, None, None)
    assert v["ok"] is False and v["subject"] == "no completed run today (latest: running)"
    assert "run 197" in v["body"] and "run 199" in v["body"]
    assert "not proof" in v["body"]   # 'running' may be a dead attempt's leftover row
