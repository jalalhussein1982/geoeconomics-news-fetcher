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
