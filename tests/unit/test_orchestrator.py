from datetime import datetime, timezone
import pytest
from geoecon import orchestrator, alerts

class FakeRepo:
    def __init__(self, completed=()):
        self._completed = set(completed); self.calls = []; self.status = None; self.metrics = {}
    def find_resumable_run(self, ws, we): return 7 if self._completed else None
    def create_run(self, *a, **k): return 7
    def completed_stages(self, rid): return set(self._completed)
    def start_stage(self, rid, s): self.calls.append(("start", s))
    def complete_stage(self, rid, s, checkpoint=None): self.calls.append(("done", s))
    def fail_stage(self, rid, s, e): self.calls.append(("fail", s))
    def skip_stage(self, rid, s): self.calls.append(("skip", s))
    def set_run_status(self, rid, st): self.status = st
    def merge_run_metrics(self, rid, m): self.metrics.update(m)

def _cfg():
    from geoecon.config import Config
    return Config.from_env({"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h",
        "SMTP_USER": "u", "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"})

def test_runs_all_stages_in_order():
    repo = FakeRepo()
    stages = [("discovery", lambda c, r, rid: {"discovered": 1}),
              ("synthesis", lambda c, r, rid: {"records_ok": 1})]
    orchestrator.run_pipeline(_cfg(), repo, now=datetime(2026,6,19,5,tzinfo=timezone.utc), stages=stages)
    assert [c for c in repo.calls if c[0] == "done"] == [("done","discovery"), ("done","synthesis")]
    assert repo.status == "completed"

def test_resume_skips_completed_stages():
    repo = FakeRepo(completed=["discovery"])
    ran = []
    stages = [("discovery", lambda c,r,rid: ran.append("discovery")),
              ("synthesis", lambda c,r,rid: ran.append("synthesis"))]
    orchestrator.run_pipeline(_cfg(), repo, now=datetime(2026,6,19,5,tzinfo=timezone.utc), stages=stages)
    assert ran == ["synthesis"]  # discovery skipped

def test_stage_failure_marks_failed_and_stops():
    repo = FakeRepo()
    def boom(c, r, rid): raise RuntimeError("x")
    stages = [("discovery", boom), ("synthesis", lambda c,r,rid: None)]
    with pytest.raises(RuntimeError):
        orchestrator.run_pipeline(_cfg(), repo, now=datetime(2026,6,19,5,tzinfo=timezone.utc), stages=stages)
    assert ("fail", "discovery") in repo.calls and repo.status == "failed"

def test_systemic_llm_failure_fails_the_stage_and_records_the_cause():
    """The breaker only pays off if it reaches fail_stage. Nothing on the LLM path
    catches broad exceptions (only JSONDecodeError), so SystemicLLMFailure must
    travel from complete() through fut.result() to run_pipeline's `except
    Exception` — producing the failed stage row the watchdog reports on, which on
    2026-07-28 read '(no failed stage recorded)'."""
    from geoecon.llm.client import SystemicLLMFailure
    recorded = {}

    class _Repo(FakeRepo):
        def fail_stage(self, rid, s, e):
            recorded["stage"] = s; recorded["error"] = str(e)
            super().fail_stage(rid, s, e)

    repo = _Repo()
    def systemic(c, r, rid):
        raise SystemicLLMFailure("gemma4: 5 consecutive complete() calls exhausted")
    with pytest.raises(SystemicLLMFailure):
        orchestrator.run_pipeline(_cfg(), repo, now=datetime(2026, 7, 30, 5, tzinfo=timezone.utc),
                                  stages=[("gate", systemic)])

    assert recorded["stage"] == "gate"
    assert "gemma4" in recorded["error"] and "exhausted" in recorded["error"]
    assert repo.status == "failed"


def test_default_stages_are_all_in_stage_runs_check():
    # Guard: every orchestrator stage must be permitted by the stage_runs.stage_name
    # CHECK constraint, or start_stage() raises a CheckViolation at runtime — and
    # because start_stage is outside the per-stage try/except, that escapes as a
    # silent exit 1. This is exactly how the discovery_registry stage broke the
    # first live run; this test fails without a DB if a stage is ever missing.
    import re, pathlib
    schema = pathlib.Path("schema.sql").read_text()
    m = re.search(r"stage_name\s+TEXT NOT NULL\s*CHECK \(stage_name IN\s*\((.*?)\)\)", schema, re.S)
    allowed = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    names = {name for name, _ in orchestrator._default_stages(_cfg(), client=None)}
    assert names <= allowed, f"stages missing from stage_runs CHECK: {names - allowed}"


def test_default_stages_include_summary_in_order():
    from geoecon import orchestrator
    cfg = _cfg()
    names = [name for name, _fn in orchestrator._default_stages(cfg, client=None)]
    assert names == ["discovery", "discovery_registry", "extraction", "gate", "cluster",
                     "synthesis", "summary", "render", "send"]
    # both discovery sources run before extraction; gate/cluster/summary order preserved
    assert names.index("discovery") < names.index("discovery_registry") < names.index("extraction")
    assert names.index("extraction") < names.index("gate") < names.index("cluster")
    assert names.index("summary") < names.index("render") < names.index("send")


class _WatchCfg:
    # minimal duck-typed cfg for run_pipeline
    window_hours = 24
    synthesis_model = "deepseek"; synthesis_prompt_id = "task_a_cov"; embedding_model = None


class _StubRepo:
    def __init__(self): self.failed = []
    def find_resumable_run(self, ws, we): return None
    def create_run(self, *a, **k): return 42
    def completed_stages(self, rid): return set()
    def start_stage(self, rid, name): pass
    def fail_stage(self, rid, name, e): self.failed.append((rid, name))
    def set_run_status(self, rid, status): pass
    def complete_stage(self, rid, name, result): pass
    def merge_run_metrics(self, rid, m): pass


def test_stage_failure_attaches_geoecon_ctx():
    def boom(cfg, repo, rid): raise ValueError("gate exploded")
    with pytest.raises(ValueError) as ei:
        orchestrator.run_pipeline(_WatchCfg(), _StubRepo(), stages=[("gate", boom)])
    assert ei.value.geoecon_ctx == {"run_id": 42, "stage": "gate"}


def test_main_sends_crash_alert(monkeypatch):
    sent = {}
    monkeypatch.setattr(alerts, "send_alert",
                        lambda cfg, subject, body, **kw: sent.update(subject=subject, body=body) or True)

    class FakeCfg: dry_run = False
    def fake_run_pipeline(cfg, repo, client=None):
        e = RuntimeError("gate exploded")
        e.geoecon_ctx = {"run_id": 42, "stage": "gate"}
        raise e
    monkeypatch.setattr(orchestrator, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(orchestrator, "_bootstrap", lambda env: (FakeCfg(), object(), object()))
    monkeypatch.setattr(orchestrator, "_install_sigterm_alert", lambda cfg: None)

    assert orchestrator.main(["run"]) == 1
    assert sent["subject"] == "pipeline failed at stage gate"
    assert "run_id: 42" in sent["body"] and "gate exploded" in sent["body"]


def test_run_pipeline_tracks_active_stage():
    seen = []
    def probe(cfg, repo, rid):
        seen.append(dict(orchestrator._active)); return {}
    orchestrator.run_pipeline(_WatchCfg(), _StubRepo(), stages=[("gate", probe)])
    assert seen == [{"run_id": 42, "stage": "gate"}]


def test_sigterm_handler_alerts_and_exits_143(monkeypatch):
    sent = {}
    monkeypatch.setattr(alerts, "send_alert",
                        lambda cfg, subject, body, **kw: sent.update(subject=subject) or True)
    monkeypatch.setattr(orchestrator, "_active", {"run_id": 7, "stage": "synthesis"})
    installed = {}
    monkeypatch.setattr(orchestrator.signal, "signal",
                        lambda signum, h: installed.update(signum=signum))
    handler = orchestrator._install_sigterm_alert(object())
    assert installed["signum"] == orchestrator.signal.SIGTERM
    with pytest.raises(SystemExit) as ei:
        handler(15, None)
    assert ei.value.code == 143
    assert sent["subject"] == "run killed (SIGTERM) during synthesis"


class _ReconcileRepo:
    def __init__(self, stale=()):
        self._stale = list(stale); self.failed = []; self.cutoff = None
    def stale_running_runs(self, cutoff):
        self.cutoff = cutoff; return list(self._stale)
    def mark_run_failed(self, run_id, note):
        self.failed.append((run_id, note))


def test_reconcile_marks_abandoned_runs_and_alerts(monkeypatch):
    """2026-07-29: runs 69/70/71 were SIGKILLed mid-extraction. SIGKILL is
    uncatchable, so no handler ran: no stage error, no alert, and the run rows
    sat in 'running' forever (run 62 had been sitting there since 07-23,
    unnoticed). This is the only failure class with zero signal — the next
    process to start must notice and say so."""
    sent = {}
    monkeypatch.setattr(alerts, "send_alert",
                        lambda cfg, subject, body, **kw: sent.update(subject=subject, body=body) or True)
    repo = _ReconcileRepo(stale=[{"run_id": 69, "status": "running",
                                  "started_at": datetime(2026, 7, 28, 23, 0, tzinfo=timezone.utc)}])
    now = datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc)

    n = orchestrator.reconcile_abandoned_runs(_cfg(), repo, now=now, max_age_hours=6)

    assert n == 1
    assert repo.failed and repo.failed[0][0] == 69
    assert "69" in sent["body"]
    assert repo.cutoff == datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)


def test_reconcile_alert_does_not_assert_a_single_cause(monkeypatch):
    """An orphaned 'running' row does NOT prove SIGKILL. A SIGTERM deadline kill
    leaves one too: the handler raises SystemExit(143), a BaseException, which
    bypasses run_pipeline's `except Exception`, so fail_stage never runs. Run 43
    (2026-07-09) was exactly that — killed at 93% of synthesis by the old 4h
    deadline — yet the first version of this alert called all 8 runs SIGKILL.
    Nor does an abandoned run imply a missed brief: all 8 of those days were
    still delivered by a retry. State what is known; do not diagnose."""
    sent = {}
    monkeypatch.setattr(alerts, "send_alert",
                        lambda cfg, subject, body, **kw: sent.update(body=body) or True)
    repo = _ReconcileRepo(stale=[{"run_id": 43, "status": "running",
                                  "started_at": datetime(2026, 7, 9, 5, 4, tzinfo=timezone.utc)}])
    orchestrator.reconcile_abandoned_runs(_cfg(), repo,
                                          now=datetime(2026, 7, 29, tzinfo=timezone.utc))

    body = sent["body"]
    low = body.lower()
    # must not present one cause as fact...
    assert "uncatchable sigkill (" not in low
    # ...but must still name the candidates, including the SIGTERM/deadline case
    assert "sigterm" in low and "sigkill" in low
    # must not claim delivery was lost — a retry may well have covered the day
    assert "produced nothing and told no one" not in low
    assert "newsletters" in low or "brief" in low or "delivered" in low


def _capture_reconcile(monkeypatch, stale, now):
    sent = {}
    monkeypatch.setattr(alerts, "send_alert",
                        lambda cfg, subject, body, **kw:
                            sent.update(subject=subject, body=body, kw=kw) or True)
    orchestrator.reconcile_abandoned_runs(_cfg(), _ReconcileRepo(stale=stale), now=now)
    return sent


def test_reconcile_alert_is_labelled_cleanup_not_a_live_failure(monkeypatch):
    """This alert reports runs that died BEFORE the current process started and
    have already been closed out — there is nothing to act on. Sharing the
    '[GEOECON ALERT]' prefix with real outages is what made 2026-07-30's
    housekeeping email read as a third consecutive failure."""
    sent = _capture_reconcile(
        monkeypatch,
        [{"run_id": 69, "status": "running",
          "started_at": datetime(2026, 7, 28, 23, 0, 7, tzinfo=timezone.utc)}],
        now=datetime(2026, 7, 29, 23, 0, tzinfo=timezone.utc))

    assert sent["kw"].get("prefix") == alerts.CLEANUP_PREFIX
    assert "no action needed" in sent["subject"].lower()


def test_reconcile_subject_dates_the_runs_in_prague_not_utc(monkeypatch):
    """Run 69 started 2026-07-28 23:00:07Z — which is 01:00 on 07-29 in Prague,
    the night the operator actually lived through. Printing only the UTC date
    made the runs look like they came from an earlier, unrelated day."""
    sent = _capture_reconcile(
        monkeypatch,
        [{"run_id": 69, "status": "running",
          "started_at": datetime(2026, 7, 28, 23, 0, 7, tzinfo=timezone.utc)}],
        now=datetime(2026, 7, 29, 23, 0, tzinfo=timezone.utc))

    assert "2026-07-29" in sent["subject"]
    assert "2026-07-28" not in sent["subject"]


def test_reconcile_subject_spans_a_date_range_when_runs_span_days(monkeypatch):
    """The 2026-07-29 sweep closed 8 runs spanning five weeks. One date would
    misrepresent that, so show the range in Prague terms."""
    sent = _capture_reconcile(
        monkeypatch,
        [{"run_id": 23, "status": "running",
          "started_at": datetime(2026, 6, 27, 1, 29, tzinfo=timezone.utc)},
         {"run_id": 62, "status": "running",
          "started_at": datetime(2026, 7, 23, 23, 0, tzinfo=timezone.utc)}],
        now=datetime(2026, 7, 29, 1, 41, tzinfo=timezone.utc))

    assert "2026-06-27" in sent["subject"]   # Prague 03:29 same day
    assert "2026-07-24" in sent["subject"]   # Prague 01:00 the NEXT day


def test_reconcile_body_shows_prague_time_beside_utc_for_each_run(monkeypatch):
    """The body listed bare UTC timestamps, so a reader comparing them against
    the nightly 01:00 Prague schedule saw a mismatch and assumed a different run."""
    sent = _capture_reconcile(
        monkeypatch,
        [{"run_id": 69, "status": "running",
          "started_at": datetime(2026, 7, 28, 23, 0, 7, tzinfo=timezone.utc)}],
        now=datetime(2026, 7, 29, 23, 0, tzinfo=timezone.utc))

    assert "2026-07-29 01:00:07 Prague" in sent["body"]
    assert "2026-07-28 23:00:07Z" in sent["body"]


def test_reconcile_body_says_plainly_this_is_not_a_new_failure(monkeypatch):
    sent = _capture_reconcile(
        monkeypatch,
        [{"run_id": 69, "status": "running",
          "started_at": datetime(2026, 7, 28, 23, 0, 7, tzinfo=timezone.utc)}],
        now=datetime(2026, 7, 29, 23, 0, tzinfo=timezone.utc))

    assert "not a new failure" in sent["body"].lower()


def test_sigterm_alert_is_not_labelled_as_cleanup(monkeypatch):
    """A SIGTERM kill IS a live failure — it must never be downgraded."""
    sent = {}
    monkeypatch.setattr(alerts, "send_alert",
                        lambda cfg, subject, body, **kw: sent.update(kw=kw) or True)
    monkeypatch.setattr(orchestrator, "_active", {"run_id": 7, "stage": "synthesis"})
    monkeypatch.setattr(orchestrator.signal, "signal", lambda signum, h: None)
    handler = orchestrator._install_sigterm_alert(object())
    with pytest.raises(SystemExit):
        handler(15, None)

    assert sent["kw"].get("prefix") in (None, alerts.SUBJECT_PREFIX)


def test_reconcile_is_quiet_and_sends_nothing_when_clean(monkeypatch):
    monkeypatch.setattr(alerts, "send_alert",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not alert")))
    repo = _ReconcileRepo(stale=[])
    assert orchestrator.reconcile_abandoned_runs(_cfg(), repo,
                                                 now=datetime(2026, 7, 29, tzinfo=timezone.utc)) == 0
    assert repo.failed == []


def test_reconcile_never_blocks_the_run(monkeypatch):
    """Best-effort: a reconciliation failure must not stop today's pipeline."""
    class Boom:
        def stale_running_runs(self, cutoff): raise RuntimeError("db hiccup")
    assert orchestrator.reconcile_abandoned_runs(_cfg(), Boom(),
                                                 now=datetime(2026, 7, 29, tzinfo=timezone.utc)) == 0


def test_sigterm_alert_reports_elapsed_and_does_not_guess_the_cause(monkeypatch):
    """2026-07-29: the handler asserted 'most likely the activeDeadlineSeconds
    hang-guard' on a job that had lived 5m52s against an 8h deadline, sending the
    reader after a hang that never happened. It cannot know why SIGTERM arrived;
    it must report elapsed wall-clock and let the reader compare."""
    sent = {}
    monkeypatch.setattr(alerts, "send_alert",
                        lambda cfg, subject, body, **kw: sent.update(body=body) or True)
    monkeypatch.setattr(orchestrator, "_active", {"run_id": 71, "stage": "discovery"})
    monkeypatch.setattr(orchestrator.signal, "signal", lambda signum, h: None)

    ticks = iter([1000.0, 1352.0])            # install, then handler -> 5m52s
    handler = orchestrator._install_sigterm_alert(object(), clock=lambda: next(ticks))
    with pytest.raises(SystemExit):
        handler(15, None)

    body = sent["body"]
    assert "0h05m52s" in body, body            # the fact that falsifies 'hang-guard'
    assert "most likely" not in body.lower(), body
    assert "71" in body and "discovery" in body
