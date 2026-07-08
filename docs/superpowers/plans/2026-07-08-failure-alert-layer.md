# Failure Alert Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Email an alert (same SMTP, same `RECIPIENTS`) whenever the daily pipeline crashes, hangs, never runs, or completes without sending — so a missing newsletter is never silent.

**Architecture:** A never-raising `geoecon/alerts.py` module reused by three hooks: (1) the top-level `except` in `orchestrator.main()` (crash alerts with stage + traceback), (2) a SIGTERM handler (4h-deadline kills), and (3) a new `geoecon watchdog` subcommand run by a second, tiny CronJob at 12:00 Europe/Prague that checks the DB for a completed-and-sent run today (dead-man's switch).

**Tech Stack:** Python 3.12, stdlib `smtplib`/`email`/`signal`/`zoneinfo`, psycopg, pytest (transport-injection pattern from `tests/unit/test_send.py`), k8s CronJob on CERIT-SC.

**Spec:** `docs/superpowers/specs/2026-07-08-failure-alert-layer-design.md`

## Global Constraints

- Alert recipients = `cfg.recipients` (the newsletter `RECIPIENTS`). No new recipient env var.
- Alert subjects start with the literal prefix `[GEOECON ALERT] ` (applied inside `alerts.build_alert`, never by callers).
- `alerts.send_alert` must NEVER raise — an alerting failure must not mask or alter the original failure path.
- Job semantics unchanged: crash path still exits 1; k8s backoff/retry behavior untouched.
- The watchdog is read-only on the DB: it must NOT call `db.apply_migrations`.
- Namespace `hussein-ns`; image repo `jalalhussein/geoecon-pipeline`; timezone strings `Europe/Prague`.
- Run tests with `python -m pytest` from the repo root. DB-backed repo tests use the existing `@pytest.mark.requires_db` + `TEST_DATABASE_URL` fixture pattern and skip when unset.
- Commit after every task; work on local `main` (repo's existing convention — no feature branch unless the executor was told otherwise).

## File Structure

| File | Responsibility |
|---|---|
| `geoecon/alerts.py` (new) | Build + best-effort-send plain-text ops alerts over the existing SMTP config. |
| `geoecon/watchdog.py` (new) | Day-window math + pure "is today healthy?" decision + watchdog entrypoint. |
| `geoecon/orchestrator.py` (modify) | Attach failure context to raised exceptions; crash-alert in `main()`; SIGTERM handler; `watchdog` subcommand dispatch. |
| `geoecon/state/repo.py` (modify) | Two narrow read-only queries: `latest_run_for_window`, `failed_stages`. |
| `deploy/watchdog-cronjob.yaml` (new) | 12:00 Europe/Prague CronJob running `geoecon watchdog`. |
| `tests/unit/test_alerts.py` (new) | Alert formatting + never-raises. |
| `tests/unit/test_watchdog.py` (new) | Decision matrix against stub repo; day-window math. |
| `tests/unit/test_orchestrator.py` (modify) | Crash-hook context/alert; SIGTERM handler. |
| `tests/unit/test_repo.py` (modify) | The two new queries (requires_db). |

---

### Task 1: `geoecon/alerts.py` — never-raise SMTP alert

**Files:**
- Create: `geoecon/alerts.py`
- Test: `tests/unit/test_alerts.py`

**Interfaces:**
- Consumes: `cfg` duck-typed with `smtp_host, smtp_port, smtp_user, smtp_password, from_addr, recipients` (same attributes `stages/send.py` uses).
- Produces: `build_alert(cfg, subject, body) -> EmailMessage`; `send_alert(cfg, subject, body, *, transport=_smtp_transport) -> bool` (True = delivered, False = swallowed failure). Tasks 2, 3, 5 call `send_alert`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_alerts.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_alerts.py -v`
Expected: FAIL / errors with `ModuleNotFoundError: No module named 'geoecon.alerts'` (or ImportError).

- [ ] **Step 3: Write the implementation**

Create `geoecon/alerts.py`:

```python
from __future__ import annotations
import logging, smtplib, ssl
from email.message import EmailMessage

log = logging.getLogger("geoecon.alerts")

SUBJECT_PREFIX = "[GEOECON ALERT] "


def _smtp_transport(cfg, msg):
    ctx = ssl.create_default_context()
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(cfg.smtp_user, cfg.smtp_password)
        s.send_message(msg)


def build_alert(cfg, subject, body) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.recipients)
    msg["Subject"] = SUBJECT_PREFIX + subject
    msg.set_content(body)
    return msg


def send_alert(cfg, subject, body, *, transport=_smtp_transport) -> bool:
    """Best-effort ops alert. NEVER raises — an alerting failure must not
    mask or alter the original failure path it is reporting on."""
    try:
        transport(cfg, build_alert(cfg, subject, body))
        log.info("alert sent: %s", subject)
        return True
    except Exception as e:
        log.error("alert send failed (%s): %s", subject, e)
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_alerts.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add geoecon/alerts.py tests/unit/test_alerts.py
git commit -m "feat(alerts): never-raise plain-text ops alert over existing SMTP config"
```

---

### Task 2: Crash hook — alert from `main()` with stage + traceback

**Files:**
- Modify: `geoecon/orchestrator.py` (imports; `run_pipeline` per-stage `except` at ~line 64; `main()` `except` at ~line 97)
- Test: `tests/unit/test_orchestrator.py` (append)

**Interfaces:**
- Consumes: `alerts.send_alert(cfg, subject, body)` from Task 1.
- Produces: raised stage exceptions carry `e.geoecon_ctx = {"run_id": int, "stage": str}`; `main()` sends one alert `pipeline failed at stage <stage>` then returns 1. Task 3 reuses the same import.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_orchestrator.py` (reuse that file's existing fake-cfg/fake-repo helpers if equivalent ones exist; otherwise these are self-contained):

```python
import pytest
from geoecon import orchestrator, alerts


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

    assert orchestrator.main(["run"]) == 1
    assert sent["subject"] == "pipeline failed at stage gate"
    assert "run_id: 42" in sent["body"] and "gate exploded" in sent["body"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_orchestrator.py -v -k "geoecon_ctx or crash_alert"`
Expected: FAIL — `geoecon_ctx` attribute missing; `_bootstrap` not defined.

- [ ] **Step 3: Implement**

In `geoecon/orchestrator.py`:

(a) Add imports at the top (line 2 area):

```python
import argparse, logging, os, signal, sys, traceback
...
from . import alerts
```

(`signal` is used in Task 3; adding it now avoids touching the import line twice.)

(b) In `run_pipeline`'s per-stage handler, attach context before re-raising:

```python
        except Exception as e:
            repo.fail_stage(run_id, name, e)
            repo.set_run_status(run_id, "failed")
            log.exception("stage %s failed", name)
            e.geoecon_ctx = {"run_id": run_id, "stage": name}
            raise
```

(c) Refactor `main()`'s wiring into a `_bootstrap` helper (so tests can stub it) and alert in the `except`. Replace the body of `main()` from `cfg = Config.from_env(env)` down with:

```python
def _bootstrap(env):
    """Config + DB (with migrations) + LLM client for a pipeline run."""
    cfg = Config.from_env(env)
    from .state import db
    from .state.repo import Repo
    from .llm.client import LLMClient
    conn = db.connect(cfg.db_dsn); db.apply_migrations(conn)
    return cfg, Repo(conn), LLMClient(cfg.llm_base_url, cfg.llm_api_key)
```

and in `main()`:

```python
    cfg, repo, client = _bootstrap(env)
    try:
        out = run_pipeline(cfg, repo, client=client)
        log.info("pipeline done: %s", out)
        return 0
    except Exception as e:
        # Log before exiting — a bare `return 1` made a CheckViolation in
        # start_stage (outside the per-stage handler) a silent exit-1 with no trace.
        log.exception("pipeline failed")
        ctx = getattr(e, "geoecon_ctx", {})
        stage = ctx.get("stage", "unknown")
        alerts.send_alert(
            cfg, f"pipeline failed at stage {stage}",
            f"Daily pipeline failed.\n\n"
            f"stage:  {stage}\n"
            f"run_id: {ctx.get('run_id', 'unknown')}\n"
            f"error:  {e!r}\n\n"
            f"{traceback.format_exc()}\n"
            f"Logs: kubectl -n hussein-ns logs job/$(kubectl -n hussein-ns get jobs "
            f"-l app=geoecon --sort-by=.metadata.creationTimestamp -o name | tail -1)\n")
        return 1
```

Keep the existing `argparse` block and `env` mangling above it unchanged. Note the label selector `app=geoecon` matches `deploy/cronjob.yaml`'s pod template label.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_orchestrator.py -v`
Expected: new tests pass AND all pre-existing tests in the file still pass (the `_bootstrap` refactor must not break them — if any existing test stubs the old inline wiring, adapt that test to stub `_bootstrap`).

- [ ] **Step 5: Commit**

```bash
git add geoecon/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat(alerts): crash alert from main() with failed stage, run_id, traceback"
```

---

### Task 3: SIGTERM hook — alert when the 4h deadline kills a hung run

**Files:**
- Modify: `geoecon/orchestrator.py`
- Test: `tests/unit/test_orchestrator.py` (append)

**Interfaces:**
- Consumes: `alerts.send_alert` (Task 1); module-level `_active` dict updated by `run_pipeline` (this task adds both).
- Produces: `_install_sigterm_alert(cfg) -> handler` (returned for testing); handler raises `SystemExit(143)`. `main()` installs it only on the `run` path (Task 5 keeps the watchdog path free of it).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_orchestrator.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_orchestrator.py -v -k "active_stage or sigterm"`
Expected: FAIL — `_active` / `_install_sigterm_alert` not defined.

- [ ] **Step 3: Implement**

In `geoecon/orchestrator.py`:

(a) Module level, under `log = ...`:

```python
# What the pipeline is doing right now — read by the SIGTERM handler so a
# deadline-kill alert can name the hung stage.
_active = {"run_id": None, "stage": None}


def _install_sigterm_alert(cfg):
    """Alert on SIGTERM (k8s activeDeadlineSeconds kill or node drain).

    SystemExit(143) is a BaseException: it passes through both the per-stage
    `except Exception` (no fail_stage row) and main()'s crash handler (no
    duplicate alert), terminating the process with the conventional code.
    """
    def handler(signum, frame):
        stage = _active.get("stage") or "startup"
        alerts.send_alert(
            cfg, f"run killed (SIGTERM) during {stage}",
            f"The pipeline pod received SIGTERM — most likely the 4h "
            f"activeDeadlineSeconds hang-guard killing a stuck run "
            f"(or node maintenance).\n\n"
            f"run_id: {_active.get('run_id')}\n"
            f"stage:  {stage}\n\n"
            f"The run row stays 'running'/'failed'; the 12:00 watchdog will "
            f"also report if nothing completes today.\n")
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, handler)
    return handler
```

(b) In `run_pipeline`, keep `_active` current — after `run_id` is known (right after the create/find block):

```python
    _active["run_id"] = run_id
```

and as the first line inside the `for name, fn in stages:` loop:

```python
        _active["stage"] = name
```

(c) In `main()`, install the handler right after `_bootstrap` on the run path:

```python
    cfg, repo, client = _bootstrap(env)
    _install_sigterm_alert(cfg)
    try:
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_orchestrator.py -v`
Expected: all pass. (Note `test_main_sends_crash_alert` from Task 2 now also exercises `_install_sigterm_alert` via `main()`; it installs a real handler on the test process, which is harmless — no SIGTERM is delivered.)

- [ ] **Step 5: Commit**

```bash
git add geoecon/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat(alerts): SIGTERM handler alerts on 4h deadline kill with hung stage name"
```

---

### Task 4: Repo watchdog queries (read-only)

**Files:**
- Modify: `geoecon/state/repo.py` (append after the newsletters section, ~line 300)
- Test: `tests/unit/test_repo.py` (append)

**Interfaces:**
- Consumes: `runs` / `stage_runs` tables (schema.sql), existing `self.conn.execute` style.
- Produces (Task 5 depends on these exact shapes):
  - `latest_run_for_window(start, end) -> dict | None` — keys `run_id, status, started_at, finished_at`; latest run with `scheduled_for` in `[start, end)`.
  - `failed_stages(run_id) -> list[dict]` — keys `stage_name, error`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_repo.py` (the file already defines the `repo` fixture, `requires_db`, and `_win()`):

```python
@requires_db
def test_latest_run_for_window_and_failed_stages(repo):
    ws, we, sf = _win()
    assert repo.latest_run_for_window(sf - timedelta(hours=12), sf + timedelta(hours=12)) is None
    rid = repo.create_run(ws, we, sf, "deepseek", "task_a_cov")
    repo.start_stage(rid, "gate"); repo.fail_stage(rid, "gate", ValueError("gate exploded"))
    repo.set_run_status(rid, "failed")
    run = repo.latest_run_for_window(sf - timedelta(hours=12), sf + timedelta(hours=12))
    assert run["run_id"] == rid and run["status"] == "failed"
    assert run["started_at"] is not None
    fails = repo.failed_stages(rid)
    assert fails == [{"stage_name": "gate", "error": "gate exploded"}]
    # outside the window -> None
    assert repo.latest_run_for_window(sf + timedelta(days=2), sf + timedelta(days=3)) is None

@requires_db
def test_latest_run_for_window_returns_newest(repo):
    ws, we, sf = _win()
    repo.create_run(ws, we, sf, "deepseek", "task_a_cov")
    rid2 = repo.create_run(ws, we, sf, "deepseek", "task_a_cov")
    run = repo.latest_run_for_window(sf - timedelta(hours=1), sf + timedelta(hours=1))
    assert run["run_id"] == rid2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TEST_DATABASE_URL=<your local postgres DSN> python -m pytest tests/unit/test_repo.py -v -k "latest_run or failed_stages"`
Expected: FAIL with `AttributeError: 'Repo' object has no attribute 'latest_run_for_window'`.
(If no local postgres is available, they skip — then rely on Step 4's skip-aware check and the watchdog stub tests in Task 5.)

- [ ] **Step 3: Implement**

Append to `geoecon/state/repo.py` (inside the `Repo` class):

```python
    # ---- watchdog (read-only) ----
    def latest_run_for_window(self, start, end) -> dict | None:
        row = self.conn.execute(
            """SELECT run_id, status, started_at, finished_at FROM runs
               WHERE scheduled_for >= %s AND scheduled_for < %s
               ORDER BY run_id DESC LIMIT 1""", (start, end)).fetchone()
        if not row:
            return None
        return {"run_id": row[0], "status": row[1], "started_at": row[2], "finished_at": row[3]}

    def failed_stages(self, run_id) -> list[dict]:
        rows = self.conn.execute(
            "SELECT stage_name, error FROM stage_runs WHERE run_id=%s AND status='failed'",
            (run_id,)).fetchall()
        return [{"stage_name": r[0], "error": r[1]} for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass (or skip cleanly)**

Run: `python -m pytest tests/unit/test_repo.py -v`
Expected: new tests PASS with `TEST_DATABASE_URL` set, otherwise SKIP; zero failures either way.

- [ ] **Step 5: Commit**

```bash
git add geoecon/state/repo.py tests/unit/test_repo.py
git commit -m "feat(repo): read-only watchdog queries — latest run in window, failed stages"
```

---

### Task 5: `geoecon/watchdog.py` + CLI dispatch

**Files:**
- Create: `geoecon/watchdog.py`
- Modify: `geoecon/orchestrator.py` (`main()` dispatch)
- Test: `tests/unit/test_watchdog.py`

**Interfaces:**
- Consumes: `alerts.send_alert` (Task 1); `repo.latest_run_for_window`, `repo.failed_stages` (Task 4); existing `repo.newsletters_for_run(run_id)` (returns dicts with `send_status`); `cfg.timezone` (`"Europe/Prague"`).
- Produces: `local_day_window(now_utc, tz_name) -> (start_utc, end_utc)`; `check(repo, day_start, day_end) -> {"ok": bool, "subject": str|None, "body": str|None}`; `run(cfg, repo, *, now=None) -> int`; CLI `geoecon watchdog`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_watchdog.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_watchdog.py -v`
Expected: FAIL — `geoecon.watchdog` module not found.

- [ ] **Step 3: Implement the module**

Create `geoecon/watchdog.py`:

```python
"""Dead-man's switch: alert if today's run did not complete and send.

Runs as `geoecon watchdog` from a small 12:00 Europe/Prague CronJob — catches
the failures in-process hooks never see (job never scheduled, image pull
error, missing secret) and double-covers crashes and deadline-kills.
"""
from __future__ import annotations
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from . import alerts

log = logging.getLogger("geoecon.watchdog")


def local_day_window(now_utc, tz_name):
    """UTC instants for [00:00, 24:00) of `now_utc`'s date in tz_name."""
    tz = ZoneInfo(tz_name)
    start = datetime.combine(now_utc.astimezone(tz).date(), time(0), tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def check(repo, day_start, day_end) -> dict:
    """Pure decision. Returns {'ok': bool, 'subject': str|None, 'body': str|None}."""
    run = repo.latest_run_for_window(day_start, day_end)
    if run is None:
        return {"ok": False, "subject": "no pipeline run today",
                "body": ("No run row exists for today — the daily job likely never "
                         "started (image pull error, missing secret, scheduling "
                         "problem, cluster issue).\n"
                         "Check: kubectl -n hussein-ns get cronjob,jobs,pods\n")}
    if run["status"] == "completed":
        sent = [n for n in repo.newsletters_for_run(run["run_id"])
                if n.get("send_status") == "sent"]
        if sent:
            return {"ok": True, "subject": None, "body": None}
        return {"ok": False, "subject": "pipeline ran but sent no newsletter",
                "body": (f"Run {run['run_id']} completed but no region newsletter was "
                         f"sent (likely zero records survived gating/synthesis).\n"
                         f"started:  {run['started_at']}\n"
                         f"finished: {run['finished_at']}\n")}
    fails = repo.failed_stages(run["run_id"])
    detail = "\n".join(f"  {f['stage_name']}: {f['error']}" for f in fails) \
             or "  (no failed stage recorded — run may be hung or was killed)"
    return {"ok": False, "subject": f"no completed run today (latest: {run['status']})",
            "body": (f"Latest run {run['run_id']} has status={run['status']} "
                     f"(started {run['started_at']}).\n"
                     f"Failed stages:\n{detail}\n"
                     f"Logs: kubectl -n hussein-ns logs job/$(kubectl -n hussein-ns "
                     f"get jobs -l app=geoecon "
                     f"--sort-by=.metadata.creationTimestamp -o name | tail -1)\n")}


def run(cfg, repo, *, now=None) -> int:
    now = now or datetime.now(timezone.utc)
    day_start, day_end = local_day_window(now, cfg.timezone)
    verdict = check(repo, day_start, day_end)
    if verdict["ok"]:
        log.info("watchdog: today's run completed and sent — OK")
        return 0
    log.warning("watchdog: %s", verdict["subject"])
    alerts.send_alert(cfg, "watchdog: " + verdict["subject"], verdict["body"])
    return 0  # alert delivered == the watchdog did its job
```

- [ ] **Step 4: Wire the CLI dispatch**

In `geoecon/orchestrator.py` `main()`, insert the dispatch between `env` preparation and `_bootstrap` (the watchdog must not apply migrations, needs no LLM client, and must not install the SIGTERM alert):

```python
    if args.command == "watchdog":
        from . import watchdog
        from .state import db
        from .state.repo import Repo
        cfg = Config.from_env(env)
        try:
            conn = db.connect(cfg.db_dsn)
        except Exception as e:
            log.exception("watchdog: DB connect failed")
            alerts.send_alert(cfg, "watchdog could not reach DB",
                              f"DB connect failed: {e!r}\n"
                              f"Pipeline state unknown — check the cluster and DB pod.\n")
            return 1
        return watchdog.run(cfg, Repo(conn))
    cfg, repo, client = _bootstrap(env)
    ...
```

Add a CLI test to `tests/unit/test_watchdog.py`:

```python
def test_cli_dispatch_runs_watchdog_without_migrations(monkeypatch):
    from geoecon import orchestrator
    calls = {}
    class FakeCfg: db_dsn = "postgresql://x"; timezone = "Europe/Prague"
    monkeypatch.setattr(orchestrator.Config, "from_env", classmethod(lambda cls, env: FakeCfg()))
    from geoecon.state import db as dbmod
    monkeypatch.setattr(dbmod, "connect", lambda dsn: calls.setdefault("connect", dsn))
    monkeypatch.setattr(dbmod, "apply_migrations",
                        lambda conn: calls.setdefault("migrated", True))
    monkeypatch.setattr(watchdog, "run", lambda cfg, repo, **kw: calls.setdefault("ran", True) or 0)
    assert orchestrator.main(["watchdog"]) == 0
    assert calls.get("ran") is True and "migrated" not in calls
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_watchdog.py tests/unit/test_orchestrator.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add geoecon/watchdog.py geoecon/orchestrator.py tests/unit/test_watchdog.py
git commit -m "feat(watchdog): dead-man's switch subcommand — alert if today's run didn't complete and send"
```

---

### Task 6: `deploy/watchdog-cronjob.yaml`

**Files:**
- Create: `deploy/watchdog-cronjob.yaml`

**Interfaces:**
- Consumes: image `jalalhussein/geoecon-pipeline:<next tag>` (set at deploy time alongside `deploy/cronjob.yaml`'s bump — use the tag the deploy builds, e.g. `20260708-1`); secrets `geoecon-secrets`, `geoecon-db-app`; CLI `watchdog` (Task 5).
- Produces: CronJob `geoecon-watchdog` at 12:00 Europe/Prague.

- [ ] **Step 1: Write the manifest**

Create `deploy/watchdog-cronjob.yaml` (mirrors `deploy/cronjob.yaml` security/env conventions; no archive volume, no pipeline flags, no LLM env needed — `MAETCENTRUM_KEY` arrives via the same `envFrom` secret, which `Config.from_env` requires):

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: geoecon-watchdog
  namespace: hussein-ns
spec:
  # Dead-man's switch for geoecon-daily (07:00): by 12:00 a healthy run has
  # long finished (~1h; the 4h activeDeadlineSeconds bounds the worst case at
  # ~11:00). If no run completed AND sent today, this emails an alert.
  schedule: "0 12 * * *"
  timeZone: "Europe/Prague"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 300
      template:
        metadata:
          labels: { app: geoecon-watchdog }
        spec:
          restartPolicy: OnFailure
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            runAsGroup: 1000
            seccompProfile: { type: RuntimeDefault }
          containers:
            - name: watchdog
              image: jalalhussein/geoecon-pipeline:20260708-1
              args: ["watchdog"]
              securityContext:
                allowPrivilegeEscalation: false
                capabilities: { drop: ["ALL"] }
              envFrom:
                - secretRef: { name: geoecon-secrets }
              env:
                - name: DB_USER
                  valueFrom: { secretKeyRef: { name: geoecon-db-app, key: username } }
                - name: DB_PASS
                  valueFrom: { secretKeyRef: { name: geoecon-db-app, key: password } }
                - name: DB_DSN
                  value: "postgresql://$(DB_USER):$(DB_PASS)@geoecon-db-rw:5432/geoeconomics"
              resources:
                requests: { cpu: "100m", memory: "128Mi" }
                limits: { cpu: "250m", memory: "256Mi" }
```

- [ ] **Step 2: Validate the YAML parses**

Run: `python -c "import yaml; yaml.safe_load(open('deploy/watchdog-cronjob.yaml')); print('yaml ok')"`
Expected: `yaml ok`. (If cluster access is available, also `kubectl apply --dry-run=client -f deploy/watchdog-cronjob.yaml`; otherwise defer to the deploy task.)

- [ ] **Step 3: Commit**

```bash
git add deploy/watchdog-cronjob.yaml
git commit -m "deploy: watchdog CronJob manifest — 12:00 Europe/Prague dead-man's switch"
```

---

### Task 7: Full-suite verification

**Files:**
- No new files; fixes only if the suite surfaces regressions.

- [ ] **Step 1: Run the entire unit suite**

Run: `python -m pytest tests/ -v`
Expected: everything passes (DB-marked tests skip without `TEST_DATABASE_URL`). Pay attention to pre-existing `test_orchestrator.py` tests — the `_bootstrap` refactor and dispatch insertion are the likely regression points.

- [ ] **Step 2: Fix any regressions and commit**

```bash
git add -A && git commit -m "test: full-suite pass after alert layer" # only if fixes were needed
```

---

### Task 8: Deploy + live validation (manual — needs cluster access via skirit)

Not TDD; this is the operational checklist from the spec. Follow the ControlMaster-socket deploy mechanics documented in project memory (`firecrawl-render-path.md`).

- [ ] **Step 1: Build + push image** with the next tag (e.g. `20260708-1`); update `deploy/cronjob.yaml`'s image to the same tag (the pipeline image must contain the new alert code for the crash/SIGTERM hooks).
- [ ] **Step 2: Apply both manifests**: `kubectl apply -f deploy/cronjob.yaml -f deploy/watchdog-cronjob.yaml`. Diff the live cronjob env against the repo first (`kubectl -n hussein-ns get cronjob geoecon-daily -o yaml`) — memory records live drift here (the kimi-k2.6 incident).
- [ ] **Step 3: Healthy-day watchdog probe**: after a good daily run, `kubectl -n hussein-ns create job --from=cronjob/geoecon-watchdog watchdog-manual-1` → expect exit 0, log line `watchdog: today's run completed and sent — OK`, **no** email.
- [ ] **Step 4: Forced-failure alert probe**: run a one-off pipeline job with a poisoned env (e.g. `LLM_BASE_URL=https://invalid.invalid/v1`, `DRY_RUN` off is fine — it fails at gate long before send) → expect a `[GEOECON ALERT] pipeline failed at stage gate` email with traceback within minutes, job exits 1.
- [ ] **Step 5: Missing-run watchdog probe** (optional but cheap): run the manual watchdog job on a fresh day before the daily run has completed → expect a `[GEOECON ALERT] watchdog: no completed run today (...)` or `no pipeline run today` email.
- [ ] **Step 6: Commit any manifest tag bumps** and update project memory with deploy state.

---

## Self-Review Notes

- **Spec coverage:** crash alert (Task 2), SIGTERM (Task 3), watchdog queries/logic/CLI (Tasks 4–5), manifest (Task 6), test matrix (Tasks 1–5), live validation (Task 8), "no migrations in watchdog" + "DB-unreachable alert" (Task 5 dispatch). Accepted-limitation (SMTP down) needs no task.
- **Type consistency:** `send_alert(cfg, subject, body, *, transport=...) -> bool` used identically in Tasks 2, 3, 5; repo query dict shapes in Task 4 match Task 5's consumption; `newsletters_for_run` returns dicts with `send_status` (verified in `repo.py:275-280`).
- **Known judgment call:** `main(["run"])` in tests installs a real SIGTERM handler in the pytest process — harmless (no signal delivered), noted in Task 3 Step 4.
