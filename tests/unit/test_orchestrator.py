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
