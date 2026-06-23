from datetime import datetime, timezone
from geoecon import orchestrator

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
    import pytest
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
