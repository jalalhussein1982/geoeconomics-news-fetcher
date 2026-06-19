# Geoeconomic Newsletter Pipeline — Phase 1 (Walking Skeleton) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a daily Kubernetes CronJob that discovers geoeconomic news from live GDELT, extracts it, synthesizes per-article structured records through the `validator.py` gate, renders `en_only` dual HTML, emails it, and persists a fully resumable run in a dedicated Postgres — coexisting cleanly with the existing SIEM app.

**Architecture:** Single Python 3.12 package (`geoecon/`), one Docker image, one staged resumable orchestrator driven by the Postgres run-state schema (`schema.sql`). Synthesis models are reached as API calls to the e-INFRA LLM proxy. Deployed as a `geoecon-*`-prefixed CronJob + CNPG Postgres in the `hussein-ns` namespace on the CERIT-SC cluster.

**Tech Stack:** Python 3.12, psycopg 3 (Postgres), requests, PyYAML, trafilatura, Jinja2; pytest + aiosmtpd for tests; CloudNativePG for the database; Docker + kubectl for deploy.

## Global Constraints

- **Runtime:** Python **3.12**. Package name `geoecon`.
- **Source of truth:** behavior per `PIPELINE_BLUEPRINT.md`; DB per `schema.sql`; deploy per `DEPLOYMENT_ENVIRONMENT.md`. Do not alter `schema.sql` semantics.
- **Vendored, unchanged:** `geoecon/llm/client.py` and `geoecon/llm/validator.py` are copied verbatim from `~/Desktop/metacentrum/eval/` — do not modify their logic.
- **Synthesis:** model `deepseek-v3.2`, prompt id `task_a_cov`, `temperature=0`, `seed=12345`, `max_tokens=8192`. Endpoint `https://llm.ai.e-infra.cz/v1`, key env `MAETCENTRUM_KEY` (fallback `METACENTRUM_KEY`).
- **Naming/isolation:** every K8s object is prefixed `geoecon-` in namespace `hussein-ns`; never touch `siem-*` / `acled-*` objects.
- **Schedule:** `0 7 * * *`, `timeZone: Europe/Prague`.
- **Only `ok` records ship.** Non-`ok` `synthesis_records` are persisted, flagged, and excluded from render.
- **Times stored UTC** (`timestamptz`). Discovery window is fixed `[T-24h, T]`.
- **Discipline:** DRY, YAGNI, TDD (failing test first), frequent commits.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | package metadata + deps + pytest config |
| `.env.example` | config template (no secret values) |
| `geoecon/config.py` | `Config` dataclass + `from_env()` |
| `geoecon/state/db.py` | psycopg connection + `apply_migrations` |
| `geoecon/state/migrations/001_init.sql` | copy of `schema.sql` |
| `geoecon/state/repo.py` | typed CRUD over the schema (`Repo` class) |
| `geoecon/llm/client.py` | vendored e-INFRA proxy client |
| `geoecon/llm/validator.py` | vendored production guard |
| `geoecon/stages/discovery.py` | GDELT GKG discovery |
| `geoecon/stages/extract.py` | trafilatura + headline-only fallback |
| `geoecon/stages/cluster.py` | P1 trivial clustering |
| `geoecon/stages/synth.py` | per-cluster synthesis + validation + persist |
| `geoecon/stages/render.py` | `en_only` dual HTML render |
| `geoecon/stages/send.py` | SMTP send |
| `geoecon/prompts/task_a_cov.txt` | synthesis prompt (from eval) |
| `geoecon/render/templates/*.j2` | Jinja2 templates |
| `geoecon/orchestrator.py` | stage sequencing + resume + CLI |
| `Dockerfile` | image build |
| `deploy/*.yaml` | K8s manifests |
| `tests/**` | per-stage tests + fixtures |

**Local dev Postgres** (Docker-optional; used by DB tests via `TEST_DATABASE_URL`):
```bash
docker run -d --name geoecon-pg -e POSTGRES_PASSWORD=dev -p 5433:5432 postgres:17
export TEST_DATABASE_URL="postgresql://postgres:dev@localhost:5433/postgres"
```
DB-touching tests are marked `@pytest.mark.requires_db` and skip when `TEST_DATABASE_URL` is unset.

---

## Task 1: Project scaffold + config

**Files:**
- Create: `pyproject.toml`, `.env.example`, `geoecon/__init__.py`, `geoecon/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `geoecon.config.Config` (frozen dataclass) and `Config.from_env(env: Mapping[str,str]) -> Config`. Fields: `db_dsn: str`, `llm_base_url: str`, `llm_api_key: str`, `synthesis_model: str`, `synthesis_prompt_id: str`, `embedding_model: str | None`, `regions: tuple[str, ...]`, `window_hours: int`, `recipients: tuple[str, ...]`, `smtp_host: str`, `smtp_port: int`, `smtp_user: str`, `smtp_password: str`, `from_addr: str`, `timezone: str`, `archive_dir: str`, `send_heartbeat: bool`, `dry_run: bool`, `limit: int | None`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "geoecon"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "requests>=2.31",
    "PyYAML>=6.0",
    "psycopg[binary]>=3.1",
    "trafilatura>=1.8",
    "Jinja2>=3.1",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "aiosmtpd>=1.4"]

[project.scripts]
geoecon = "geoecon.orchestrator:main"

[tool.pytest.ini_options]
markers = ["requires_db: needs TEST_DATABASE_URL", "requires_net: hits live network"]
testpaths = ["tests"]
```

- [ ] **Step 2: Write the failing test** — `tests/unit/test_config.py`

```python
from geoecon.config import Config

BASE_ENV = {
    "DB_DSN": "postgresql://u:p@h:5432/geoeconomics",
    "MAETCENTRUM_KEY": "sk-test",
    "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587",
    "SMTP_USER": "me@gmail.com", "SMTP_PASSWORD": "pw",
    "FROM_ADDR": "me@gmail.com", "RECIPIENTS": "a@x.com, b@y.com",
}

def test_from_env_defaults():
    cfg = Config.from_env(BASE_ENV)
    assert cfg.synthesis_model == "deepseek-v3.2"
    assert cfg.synthesis_prompt_id == "task_a_cov"
    assert cfg.llm_base_url == "https://llm.ai.e-infra.cz/v1"
    assert cfg.window_hours == 24
    assert cfg.regions == ("MENA", "LatAm")
    assert cfg.recipients == ("a@x.com", "b@y.com")
    assert cfg.timezone == "Europe/Prague"
    assert cfg.dry_run is False and cfg.limit is None

def test_api_key_fallback():
    env = {k: v for k, v in BASE_ENV.items() if k != "MAETCENTRUM_KEY"}
    env["METACENTRUM_KEY"] = "sk-fallback"
    assert Config.from_env(env).llm_api_key == "sk-fallback"

def test_missing_required_raises():
    import pytest
    with pytest.raises(KeyError):
        Config.from_env({})
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError: geoecon.config`).

- [ ] **Step 4: Implement `geoecon/config.py`**

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping

def _split(csv: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in csv.split(",") if x.strip())

@dataclass(frozen=True)
class Config:
    db_dsn: str
    llm_api_key: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    from_addr: str
    recipients: tuple[str, ...]
    llm_base_url: str = "https://llm.ai.e-infra.cz/v1"
    synthesis_model: str = "deepseek-v3.2"
    synthesis_prompt_id: str = "task_a_cov"
    embedding_model: str | None = None
    regions: tuple[str, ...] = ("MENA", "LatAm")
    window_hours: int = 24
    timezone: str = "Europe/Prague"
    archive_dir: str = "/data/archive"
    send_heartbeat: bool = True
    dry_run: bool = False
    limit: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        api_key = env.get("MAETCENTRUM_KEY") or env.get("METACENTRUM_KEY")
        if not api_key:
            raise KeyError("MAETCENTRUM_KEY (or METACENTRUM_KEY) is required")
        limit = env.get("LIMIT")
        return cls(
            db_dsn=env["DB_DSN"],
            llm_api_key=api_key,
            smtp_host=env["SMTP_HOST"],
            smtp_port=int(env.get("SMTP_PORT", "587")),
            smtp_user=env["SMTP_USER"],
            smtp_password=env["SMTP_PASSWORD"],
            from_addr=env["FROM_ADDR"],
            recipients=_split(env["RECIPIENTS"]),
            llm_base_url=env.get("LLM_BASE_URL", "https://llm.ai.e-infra.cz/v1"),
            synthesis_model=env.get("SYNTHESIS_MODEL", "deepseek-v3.2"),
            synthesis_prompt_id=env.get("SYNTHESIS_PROMPT_ID", "task_a_cov"),
            embedding_model=env.get("EMBEDDING_MODEL") or None,
            regions=_split(env.get("REGIONS", "MENA,LatAm")),
            window_hours=int(env.get("WINDOW_HOURS", "24")),
            timezone=env.get("TIMEZONE", "Europe/Prague"),
            archive_dir=env.get("ARCHIVE_DIR", "/data/archive"),
            send_heartbeat=env.get("SEND_HEARTBEAT", "true").lower() == "true",
            dry_run=env.get("DRY_RUN", "false").lower() == "true",
            limit=int(limit) if limit else None,
        )
```

- [ ] **Step 5: Write `.env.example`**

```bash
DB_DSN=postgresql://geoecon:CHANGEME@geoecon-db-rw:5432/geoeconomics
MAETCENTRUM_KEY=
LLM_BASE_URL=https://llm.ai.e-infra.cz/v1
SYNTHESIS_MODEL=deepseek-v3.2
SYNTHESIS_PROMPT_ID=task_a_cov
REGIONS=MENA,LatAm
WINDOW_HOURS=24
TIMEZONE=Europe/Prague
ARCHIVE_DIR=/data/archive
SEND_HEARTBEAT=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
FROM_ADDR=
RECIPIENTS=
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_config.py -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .env.example geoecon/__init__.py geoecon/config.py tests/unit/test_config.py
git commit -m "feat: project scaffold + config loader"
```

---

## Task 2: State — migrations + DB connection

**Files:**
- Create: `geoecon/state/__init__.py`, `geoecon/state/db.py`, `geoecon/state/migrations/001_init.sql`
- Test: `tests/unit/test_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `geoecon.state.db.connect(dsn: str) -> psycopg.Connection`; `apply_migrations(conn, migrations_dir: str | None = None) -> list[str]` (returns applied filenames, idempotent).

- [ ] **Step 1: Create the migration** — copy `schema.sql` verbatim

```bash
cp schema.sql geoecon/state/migrations/001_init.sql
```

- [ ] **Step 2: Write the failing test** — `tests/unit/test_db.py`

```python
import os, uuid, pytest, psycopg
from geoecon.state import db

requires_db = pytest.mark.requires_db
DSN = os.environ.get("TEST_DATABASE_URL")

@pytest.fixture
def fresh_db():
    if not DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f'CREATE DATABASE {name}')
    test_dsn = DSN.rsplit("/", 1)[0] + "/" + name
    yield test_dsn
    admin.execute(f'DROP DATABASE {name} WITH (FORCE)')
    admin.close()

@requires_db
def test_apply_migrations_creates_tables(fresh_db):
    conn = db.connect(fresh_db)
    applied = db.apply_migrations(conn)
    assert "001_init.sql" in applied
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {"runs", "stage_runs", "articles", "clusters", "cluster_articles",
            "seen_events", "synthesis_records", "throughlines", "newsletters"} <= names

@requires_db
def test_apply_migrations_idempotent(fresh_db):
    conn = db.connect(fresh_db)
    db.apply_migrations(conn)
    db.apply_migrations(conn)  # second run must not raise
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_db.py -v`
Expected: FAIL (`ModuleNotFoundError: geoecon.state.db`). (Skips if `TEST_DATABASE_URL` unset — set it per the local-dev snippet.)

- [ ] **Step 4: Implement `geoecon/state/db.py`**

```python
from __future__ import annotations
from pathlib import Path
import psycopg

_MIGRATIONS = Path(__file__).parent / "migrations"

def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True)

def apply_migrations(conn: psycopg.Connection, migrations_dir: str | None = None) -> list[str]:
    d = Path(migrations_dir) if migrations_dir else _MIGRATIONS
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
    done = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
    applied = []
    for sql_file in sorted(d.glob("*.sql")):
        if sql_file.name in done:
            continue
        conn.execute(sql_file.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations(filename) VALUES (%s)", (sql_file.name,))
        applied.append(sql_file.name)
    return applied
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_db.py -v`
Expected: PASS (2 tests) when `TEST_DATABASE_URL` is set.

- [ ] **Step 6: Commit**

```bash
git add geoecon/state/__init__.py geoecon/state/db.py geoecon/state/migrations/001_init.sql tests/unit/test_db.py
git commit -m "feat: state DB connection + idempotent migrations"
```

---

## Task 3: State — typed repository

**Files:**
- Create: `geoecon/state/repo.py`, `geoecon/state/models.py`
- Test: `tests/unit/test_repo.py`

**Interfaces:**
- Consumes: `geoecon.state.db.connect`, `apply_migrations`.
- Produces: `geoecon.state.models` dataclasses (`Article`, `Cluster`, `SynthesisRecord`) and `geoecon.state.repo.Repo(conn)` with methods:
  - `create_run(window_start, window_end, scheduled_for, synthesis_model, synthesis_prompt_id, embedding_model=None) -> int`
  - `find_resumable_run(window_start, window_end) -> int | None`
  - `completed_stages(run_id) -> set[str]`
  - `start_stage(run_id, stage)` / `complete_stage(run_id, stage, checkpoint=None)` / `fail_stage(run_id, stage, error)` / `skip_stage(run_id, stage)`
  - `set_run_status(run_id, status)` / `merge_run_metrics(run_id, metrics: dict)`
  - `insert_article(run_id, **fields) -> int` (dedup by `(run_id, url_hash)` → returns existing id)
  - `update_extraction(article_id, body, fetch_status, source_lang=None, fetch_error=None)`
  - `articles_for_run(run_id, fetch_status=None) -> list[Article]`
  - `insert_cluster(run_id, event_fingerprint, region, languages, article_count, framing_divergent=False) -> int`
  - `add_membership(cluster_id, article_id, frame_label=None)`
  - `clusters_for_run(run_id) -> list[Cluster]` / `articles_in_cluster(cluster_id) -> list[Article]`
  - `upsert_synthesis_record(run_id, cluster_id, fields: dict, quality_flag, quality_detail=None, is_headline_only=False) -> int`
  - `ok_records(run_id) -> list[SynthesisRecord]`
  - `get_seen(fingerprint) -> dict | None` / `upsert_seen(fingerprint, run_id, ttl_days=14)`
  - `insert_newsletter(run_id, render_mode, archive_html_path, email_html, record_count) -> int`
  - `update_newsletter_send(newsletter_id, send_status, send_error=None)`

- [ ] **Step 1: Write `geoecon/state/models.py`**

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

@dataclass
class Article:
    article_id: int
    source_url: str
    url_hash: str
    domain: str | None
    region: str | None
    gkg_themes: list[str]
    gkg_tone: float | None
    num_mentions: int | None
    source_lang: str | None
    title: str | None
    body: str | None
    fetch_status: str

@dataclass
class Cluster:
    cluster_id: int
    event_fingerprint: str
    region: str | None
    languages: list[str]
    article_count: int
    framing_divergent: bool

@dataclass
class SynthesisRecord:
    record_id: int
    cluster_id: int
    fields: dict[str, Any]
    quality_flag: str
    is_headline_only: bool
```

- [ ] **Step 2: Write the failing test** — `tests/unit/test_repo.py`

```python
import os, uuid, pytest, psycopg
from datetime import datetime, timezone, timedelta
from geoecon.state import db
from geoecon.state.repo import Repo

requires_db = pytest.mark.requires_db
DSN = os.environ.get("TEST_DATABASE_URL")

@pytest.fixture
def repo():
    if not DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f'CREATE DATABASE {name}')
    test_dsn = DSN.rsplit("/", 1)[0] + "/" + name
    conn = db.connect(test_dsn); db.apply_migrations(conn)
    yield Repo(conn)
    conn.close(); admin.execute(f'DROP DATABASE {name} WITH (FORCE)'); admin.close()

def _win():
    end = datetime(2026, 6, 19, 5, 0, tzinfo=timezone.utc)
    return end - timedelta(hours=24), end, end

@requires_db
def test_run_and_stage_lifecycle(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    assert repo.find_resumable_run(ws, we) == rid
    repo.start_stage(rid, "discovery"); repo.complete_stage(rid, "discovery", {"n": 3})
    assert "discovery" in repo.completed_stages(rid)

@requires_db
def test_article_dedup_by_url_hash(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    a1 = repo.insert_article(rid, source_url="http://x/1", url_hash="h1", domain="x",
                             region="MENA", gkg_themes=["ECON_X"], title="t")
    a2 = repo.insert_article(rid, source_url="http://x/1", url_hash="h1", domain="x",
                             region="MENA", gkg_themes=["ECON_X"], title="t")
    assert a1 == a2  # dedup

@requires_db
def test_only_ok_records_returned(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    aid = repo.insert_article(rid, source_url="u", url_hash="h", domain="d",
                              region="MENA", gkg_themes=[], title="t")
    cid = repo.insert_cluster(rid, "fp1", "MENA", ["en"], 1)
    repo.add_membership(cid, aid)
    repo.upsert_synthesis_record(rid, cid, {"headline_en": "A"}, "ok")
    bad = repo.insert_cluster(rid, "fp2", "MENA", ["en"], 1)
    repo.upsert_synthesis_record(rid, bad, {"headline_en": "B"}, "zero_entities")
    recs = repo.ok_records(rid)
    assert len(recs) == 1 and recs[0].fields["headline_en"] == "A"

@requires_db
def test_seen_store_roundtrip(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    assert repo.get_seen("fpX") is None
    repo.upsert_seen("fpX", rid)
    assert repo.get_seen("fpX")["times_seen"] == 1
    repo.upsert_seen("fpX", rid)
    assert repo.get_seen("fpX")["times_seen"] == 2
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_repo.py -v`
Expected: FAIL (`ModuleNotFoundError: geoecon.state.repo`).

- [ ] **Step 4: Implement `geoecon/state/repo.py`**

```python
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from typing import Any
import psycopg
from psycopg.types.json import Jsonb
from .models import Article, Cluster, SynthesisRecord

class Repo:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # ---- runs / stages ----
    def create_run(self, window_start, window_end, scheduled_for,
                   synthesis_model, synthesis_prompt_id, embedding_model=None) -> int:
        return self.conn.execute(
            """INSERT INTO runs(window_start, window_end, scheduled_for,
                 synthesis_model, synthesis_prompt_id, embedding_model)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING run_id""",
            (window_start, window_end, scheduled_for, synthesis_model,
             synthesis_prompt_id, embedding_model)).fetchone()[0]

    def find_resumable_run(self, window_start, window_end) -> int | None:
        row = self.conn.execute(
            """SELECT run_id FROM runs
               WHERE window_start=%s AND window_end=%s AND status IN ('running','failed','partial')
               ORDER BY run_id DESC LIMIT 1""", (window_start, window_end)).fetchone()
        return row[0] if row else None

    def completed_stages(self, run_id) -> set[str]:
        rows = self.conn.execute(
            "SELECT stage_name FROM stage_runs WHERE run_id=%s AND status='completed'",
            (run_id,)).fetchall()
        return {r[0] for r in rows}

    def _upsert_stage(self, run_id, stage, status, checkpoint=None, error=None):
        self.conn.execute(
            """INSERT INTO stage_runs(run_id, stage_name, status, started_at, finished_at, checkpoint, error)
               VALUES (%s,%s,%s, now(), %s, %s, %s)
               ON CONFLICT (run_id, stage_name) DO UPDATE SET
                 status=EXCLUDED.status,
                 finished_at=COALESCE(EXCLUDED.finished_at, stage_runs.finished_at),
                 checkpoint=COALESCE(EXCLUDED.checkpoint, stage_runs.checkpoint),
                 error=EXCLUDED.error""",
            (run_id, stage, status,
             datetime.now(timezone.utc) if status in ("completed", "failed", "skipped") else None,
             Jsonb(checkpoint) if checkpoint is not None else None, error))

    def start_stage(self, run_id, stage): self._upsert_stage(run_id, stage, "running")
    def complete_stage(self, run_id, stage, checkpoint=None): self._upsert_stage(run_id, stage, "completed", checkpoint)
    def fail_stage(self, run_id, stage, error): self._upsert_stage(run_id, stage, "failed", error=str(error)[:4000])
    def skip_stage(self, run_id, stage): self._upsert_stage(run_id, stage, "skipped")

    def set_run_status(self, run_id, status):
        self.conn.execute(
            "UPDATE runs SET status=%s, finished_at=CASE WHEN %s IN ('completed','failed','partial') THEN now() ELSE finished_at END WHERE run_id=%s",
            (status, status, run_id))

    def merge_run_metrics(self, run_id, metrics: dict):
        self.conn.execute("UPDATE runs SET metrics = metrics || %s WHERE run_id=%s",
                          (Jsonb(metrics), run_id))

    # ---- articles ----
    def insert_article(self, run_id, *, source_url, url_hash, domain=None, region=None,
                       gkg_themes=None, gkg_tone=None, num_mentions=None, source_lang=None,
                       title=None, gdelt_gkg_id=None, canonical_url=None, published_at=None,
                       discovered_via="gdelt") -> int:
        existing = self.conn.execute(
            "SELECT article_id FROM articles WHERE run_id=%s AND url_hash=%s",
            (run_id, url_hash)).fetchone()
        if existing:
            return existing[0]
        return self.conn.execute(
            """INSERT INTO articles(run_id, gdelt_gkg_id, source_url, canonical_url, url_hash,
                 domain, discovered_via, region, gkg_themes, gkg_tone, num_mentions,
                 source_lang, title, published_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING article_id""",
            (run_id, gdelt_gkg_id, source_url, canonical_url, url_hash, domain, discovered_via,
             region, gkg_themes or [], gkg_tone, num_mentions, source_lang, title, published_at)
        ).fetchone()[0]

    def update_extraction(self, article_id, body, fetch_status, source_lang=None, fetch_error=None):
        self.conn.execute(
            "UPDATE articles SET body=%s, fetch_status=%s, source_lang=COALESCE(%s, source_lang), fetch_error=%s WHERE article_id=%s",
            (body, fetch_status, source_lang, fetch_error, article_id))

    def articles_for_run(self, run_id, fetch_status=None) -> list[Article]:
        q = ("SELECT article_id, source_url, url_hash, domain, region, gkg_themes, gkg_tone, "
             "num_mentions, source_lang, title, body, fetch_status FROM articles WHERE run_id=%s")
        params: tuple = (run_id,)
        if fetch_status:
            q += " AND fetch_status=%s"; params += (fetch_status,)
        return [Article(*r) for r in self.conn.execute(q + " ORDER BY article_id", params).fetchall()]

    # ---- clusters ----
    def insert_cluster(self, run_id, event_fingerprint, region, languages, article_count,
                       framing_divergent=False) -> int:
        return self.conn.execute(
            """INSERT INTO clusters(run_id, event_fingerprint, region, languages, article_count, framing_divergent)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING cluster_id""",
            (run_id, event_fingerprint, region, languages, article_count, framing_divergent)).fetchone()[0]

    def add_membership(self, cluster_id, article_id, frame_label=None):
        self.conn.execute(
            "INSERT INTO cluster_articles(cluster_id, article_id, frame_label) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (cluster_id, article_id, frame_label))

    def clusters_for_run(self, run_id) -> list[Cluster]:
        rows = self.conn.execute(
            "SELECT cluster_id, event_fingerprint, region, languages, article_count, framing_divergent FROM clusters WHERE run_id=%s ORDER BY cluster_id",
            (run_id,)).fetchall()
        return [Cluster(*r) for r in rows]

    def articles_in_cluster(self, cluster_id) -> list[Article]:
        rows = self.conn.execute(
            """SELECT a.article_id, a.source_url, a.url_hash, a.domain, a.region, a.gkg_themes,
                 a.gkg_tone, a.num_mentions, a.source_lang, a.title, a.body, a.fetch_status
               FROM articles a JOIN cluster_articles ca ON ca.article_id=a.article_id
               WHERE ca.cluster_id=%s ORDER BY a.article_id""", (cluster_id,)).fetchall()
        return [Article(*r) for r in rows]

    # ---- synthesis ----
    def upsert_synthesis_record(self, run_id, cluster_id, fields: dict, quality_flag,
                                quality_detail=None, is_headline_only=False) -> int:
        f = fields
        return self.conn.execute(
            """INSERT INTO synthesis_records(run_id, cluster_id, headline_en, headline_original,
                 headline_original_lang, what_happened_en, geoeconomic_significance_en,
                 geoeconomic_vector, entities, key_figures, framing_divergence, sources,
                 synthesis_quality_flag, quality_detail, is_headline_only)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, cluster_id) DO UPDATE SET
                 headline_en=EXCLUDED.headline_en, headline_original=EXCLUDED.headline_original,
                 headline_original_lang=EXCLUDED.headline_original_lang,
                 what_happened_en=EXCLUDED.what_happened_en,
                 geoeconomic_significance_en=EXCLUDED.geoeconomic_significance_en,
                 geoeconomic_vector=EXCLUDED.geoeconomic_vector, entities=EXCLUDED.entities,
                 key_figures=EXCLUDED.key_figures, framing_divergence=EXCLUDED.framing_divergence,
                 sources=EXCLUDED.sources, synthesis_quality_flag=EXCLUDED.synthesis_quality_flag,
                 quality_detail=EXCLUDED.quality_detail, is_headline_only=EXCLUDED.is_headline_only
               RETURNING record_id""",
            (run_id, cluster_id, f.get("headline_en"), f.get("headline_original"),
             f.get("headline_original_lang"), f.get("what_happened_en"),
             f.get("geoeconomic_significance_en"), f.get("geoeconomic_vector") or [],
             Jsonb(f.get("entities") or []), Jsonb(f.get("key_figures") or []),
             Jsonb(f.get("framing_divergence")) if f.get("framing_divergence") is not None else None,
             Jsonb(f.get("sources") or []), quality_flag,
             Jsonb(quality_detail) if quality_detail is not None else None, is_headline_only)
        ).fetchone()[0]

    def ok_records(self, run_id) -> list[SynthesisRecord]:
        rows = self.conn.execute(
            """SELECT record_id, cluster_id, headline_en, headline_original, headline_original_lang,
                 what_happened_en, geoeconomic_significance_en, geoeconomic_vector, entities,
                 key_figures, framing_divergence, sources, is_headline_only
               FROM synthesis_records WHERE run_id=%s AND synthesis_quality_flag='ok'
               ORDER BY record_id""", (run_id,)).fetchall()
        out = []
        for r in rows:
            out.append(SynthesisRecord(
                record_id=r[0], cluster_id=r[1],
                fields={"headline_en": r[2], "headline_original": r[3],
                        "headline_original_lang": r[4], "what_happened_en": r[5],
                        "geoeconomic_significance_en": r[6], "geoeconomic_vector": r[7],
                        "entities": r[8], "key_figures": r[9], "framing_divergence": r[10],
                        "sources": r[11]},
                quality_flag="ok", is_headline_only=r[12]))
        return out

    # ---- seen-store ----
    def get_seen(self, fingerprint) -> dict | None:
        row = self.conn.execute(
            "SELECT event_fingerprint, times_seen, last_emitted_at, expires_at FROM seen_events WHERE event_fingerprint=%s",
            (fingerprint,)).fetchone()
        if not row:
            return None
        return {"event_fingerprint": row[0], "times_seen": row[1],
                "last_emitted_at": row[2], "expires_at": row[3]}

    def upsert_seen(self, fingerprint, run_id, ttl_days=14):
        expires = datetime.now(timezone.utc) + timedelta(days=ttl_days)
        self.conn.execute(
            """INSERT INTO seen_events(event_fingerprint, first_seen_run, last_seen_run,
                 last_emitted_at, times_seen, expires_at)
               VALUES (%s,%s,%s, now(), 1, %s)
               ON CONFLICT (event_fingerprint) DO UPDATE SET
                 last_seen_run=EXCLUDED.last_seen_run, last_emitted_at=now(),
                 times_seen=seen_events.times_seen+1, expires_at=EXCLUDED.expires_at""",
            (fingerprint, run_id, run_id, expires))

    # ---- newsletters ----
    def insert_newsletter(self, run_id, render_mode, archive_html_path, email_html, record_count) -> int:
        return self.conn.execute(
            """INSERT INTO newsletters(run_id, render_mode, archive_html_path, email_html, record_count)
               VALUES (%s,%s,%s,%s,%s) RETURNING newsletter_id""",
            (run_id, render_mode, archive_html_path, email_html, record_count)).fetchone()[0]

    def update_newsletter_send(self, newsletter_id, send_status, send_error=None):
        self.conn.execute(
            "UPDATE newsletters SET send_status=%s, send_error=%s, sent_at=CASE WHEN %s='sent' THEN now() ELSE sent_at END WHERE newsletter_id=%s",
            (send_status, send_error, send_status, newsletter_id))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_repo.py -v`
Expected: PASS (4 tests) with `TEST_DATABASE_URL` set.

- [ ] **Step 6: Commit**

```bash
git add geoecon/state/models.py geoecon/state/repo.py tests/unit/test_repo.py
git commit -m "feat: typed state repository over the run-state schema"
```

---

## Task 4: Vendor the LLM client + validator

**Files:**
- Create: `geoecon/llm/__init__.py`, `geoecon/llm/client.py`, `geoecon/llm/validator.py`, `geoecon/prompts/task_a_cov.txt`
- Test: `tests/unit/test_llm_vendored.py`

**Interfaces:**
- Produces: `geoecon.llm.client.LLMClient` (with `.complete(model, messages, **params) -> LLMResponse`, `LLMResponse.final_answer`, `.ok()`), `geoecon.llm.client.split_reasoning`, and `geoecon.llm.validator.<validate fn>`.

- [ ] **Step 1: Copy the proven files verbatim**

```bash
cp ~/Desktop/metacentrum/eval/client.py geoecon/llm/client.py
cp ~/Desktop/metacentrum/eval/validator.py geoecon/llm/validator.py
cp ~/Desktop/metacentrum/eval/prompts/task_a_cov.txt geoecon/prompts/task_a_cov.txt 2>/dev/null || \
  grep -rl "task_a_cov" ~/Desktop/metacentrum/eval/ | head
touch geoecon/llm/__init__.py geoecon/prompts/.gitkeep
```
(If `task_a_cov` lives inside `tasks.py` rather than a `.txt`, extract its text into `geoecon/prompts/task_a_cov.txt`. Inspect `~/Desktop/metacentrum/eval/tasks.py` to confirm where the prompt string is defined and copy it exactly.)

- [ ] **Step 2: Write the failing test** — `tests/unit/test_llm_vendored.py`

```python
from geoecon.llm.client import split_reasoning, LLMClient, LLMResponse

def test_split_reasoning_strips_think_block():
    final, reasoning = split_reasoning("<think>secret</think>ANSWER")
    assert final == "ANSWER" and "secret" in reasoning

def test_complete_parses_mocked_200(monkeypatch):
    client = LLMClient("https://x/v1", "key")
    class R:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": '{"ok": true}'},
                                "finish_reason": "stop"}], "usage": {"prompt_tokens": 5}}
    monkeypatch.setattr(client.session, "post", lambda *a, **k: R())
    resp = client.complete("deepseek-v3.2", [{"role": "user", "content": "hi"}])
    assert resp.ok() and resp.final_answer == '{"ok": true}'

def test_validator_importable():
    import geoecon.llm.validator as v
    assert v is not None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_llm_vendored.py -v`
Expected: FAIL (module not found) before the copy; after copy, PASS.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_llm_vendored.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add geoecon/llm/ geoecon/prompts/ tests/unit/test_llm_vendored.py
git commit -m "feat: vendor proven LLM client + validator + task_a_cov prompt"
```

---

## Task 5: Discovery stage (GDELT GKG)

**Files:**
- Create: `geoecon/stages/__init__.py`, `geoecon/stages/discovery.py`, `geoecon/gdelt.py`
- Test: `tests/unit/test_discovery.py`, `tests/fixtures/gkg_sample.tsv`

**Interfaces:**
- Consumes: `Config`, `Repo`.
- Produces: `geoecon.gdelt.parse_gkg_line(line: str) -> GkgRecord | None`, `geoecon.gdelt.GkgRecord` (fields: `gkg_id, ts, domain, url, themes, country, tone, lang`), `geoecon.gdelt.theme_intersection_ok(themes, target_prefixes, min_count=2) -> bool`, `geoecon.gdelt.url_hash(url) -> str`, and `geoecon.stages.discovery.run(cfg, repo, run_id, *, fetch=...) -> dict` (metrics).

> **Pre-step (verify live format):** before locking field indices, download one real GKG file and confirm the column layout against the GDELT 2.1 GKG codebook:
> `curl -s http://data.gdeltproject.org/gdeltv2/lastupdate.txt` → take the `.gkg.csv.zip` URL → unzip → inspect first line. The indices below follow the published GKG 2.1 spec (tab-delimited): `1`=DATE, `3`=SourceCommonName(domain), `4`=DocumentIdentifier(URL), `7`=V1Themes(`;`-sep), `9`=V1Locations(`;`-sep records of `#`-fields, index 2 = country code), `15`=V1.5Tone(`,`-sep, first = tone). The translation stream adds a final `TranslationInfo` field (`srclc:<lang>;...`).

- [ ] **Step 1: Create the fixture** — `tests/fixtures/gkg_sample.tsv` (one real-ish GKG line; tab-delimited; trimmed)

```
20260619051500-1	20260619051500	1	alarabiya.net	https://www.alarabiya.net/x	WB_1428_TRADE;ECON_SUBSIDIES;EPU_ECONOMY	WB_1428_TRADE,120;ECON_SUBSIDIES,300	4#Saudi Arabia#SA#SA#24#45#SA	4#Saudi Arabia#SA	-3.5,2.1,5.6,7.7,0,0,21
```
(Tabs between the 16 columns; the line above shows the columns this stage reads — pad missing intermediate columns with empty tabs to reach index 15 when authoring the fixture.)

- [ ] **Step 2: Write the failing test** — `tests/unit/test_discovery.py`

```python
from geoecon import gdelt

def _line():
    with open("tests/fixtures/gkg_sample.tsv", encoding="utf-8") as f:
        return f.readline().rstrip("\n")

def test_parse_gkg_line_extracts_fields():
    rec = gdelt.parse_gkg_line(_line())
    assert rec is not None
    assert rec.url.startswith("https://www.alarabiya.net")
    assert rec.domain == "alarabiya.net"
    assert "WB_1428_TRADE" in rec.themes and "ECON_SUBSIDIES" in rec.themes
    assert rec.country == "SA"
    assert rec.tone == -3.5

def test_theme_intersection_requires_two():
    assert gdelt.theme_intersection_ok(["WB_1428_TRADE", "ECON_SUBSIDIES"], ("ECON_", "WB_")) is True
    assert gdelt.theme_intersection_ok(["ECON_SUBSIDIES"], ("ECON_", "WB_")) is False
    assert gdelt.theme_intersection_ok(["SPORTS", "WEATHER"], ("ECON_", "WB_")) is False

def test_url_hash_stable():
    assert gdelt.url_hash("http://a/b") == gdelt.url_hash("http://a/b")

def test_discovery_run_inserts_and_caps(monkeypatch):
    # fake fetch returns 3 identical-theme lines across the window
    lines = [_line()] * 3
    captured = []
    class FakeRepo:
        def insert_article(self, run_id, **kw): captured.append(kw); return len(captured)
    from geoecon.stages import discovery
    from geoecon.config import Config
    cfg = Config.from_env({"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h",
        "SMTP_USER": "u", "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"})
    metrics = discovery.run(cfg, FakeRepo(), run_id=1, fetch=lambda ws, we: lines)
    assert metrics["discovered"] == 3
    assert all(c["region"] == "MENA" for c in captured)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_discovery.py -v`
Expected: FAIL (`ModuleNotFoundError: geoecon.gdelt`).

- [ ] **Step 4: Implement `geoecon/gdelt.py`**

```python
from __future__ import annotations
import gzip, hashlib, io, zipfile
from dataclasses import dataclass
from datetime import datetime, timezone

# GKG 2.1 tab-delimited column indices (per published codebook)
_DATE, _DOMAIN, _URL, _THEMES, _LOCATIONS, _TONE = 1, 3, 4, 7, 9, 15
# Country code -> region (extend as needed)
MENA = {"SA","AE","EG","IQ","IR","SY","JO","LB","QA","KW","OM","BH","YE","DZ","MA","TN","LY","SD","PS"}
LATAM = {"BR","AR","MX","CL","CO","PE","VE","EC","BO","PY","UY","GT","CU","DO","HN","NI","CR","PA","SV"}
TARGET_THEME_PREFIXES = ("ECON_", "WB_", "EPU_")

@dataclass
class GkgRecord:
    gkg_id: str
    ts: datetime | None
    domain: str
    url: str
    themes: list[str]
    country: str | None
    tone: float | None
    lang: str | None

def url_hash(url: str) -> str:
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()

def region_for_country(cc: str | None) -> str:
    if cc in MENA: return "MENA"
    if cc in LATAM: return "LatAm"
    return "other"

def theme_intersection_ok(themes, target_prefixes=TARGET_THEME_PREFIXES, min_count=2) -> bool:
    hits = sum(1 for t in themes if any(t.startswith(p) for p in target_prefixes))
    return hits >= min_count

def parse_gkg_line(line: str) -> GkgRecord | None:
    f = line.rstrip("\n").split("\t")
    if len(f) <= _TONE:
        return None
    url = f[_URL].strip()
    if not url:
        return None
    themes = [t.split(",")[0] for t in f[_THEMES].split(";") if t]
    country = None
    if f[_LOCATIONS]:
        first = f[_LOCATIONS].split(";")[0].split("#")
        if len(first) > 2:
            country = first[2] or None
    tone = None
    if f[_TONE]:
        try: tone = float(f[_TONE].split(",")[0])
        except ValueError: tone = None
    try:
        ts = datetime.strptime(f[_DATE], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        ts = None
    return GkgRecord(gkg_id=f[0], ts=ts, domain=f[_DOMAIN].strip(), url=url,
                     themes=themes, country=country, tone=tone, lang=None)

def unzip_gkg_bytes(blob: bytes) -> list[str]:
    """Decompress a .gkg.csv.zip (or .gz) payload into text lines."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = z.namelist()[0]
            return z.read(name).decode("utf-8", "replace").splitlines()
    except zipfile.BadZipFile:
        return gzip.decompress(blob).decode("utf-8", "replace").splitlines()
```

- [ ] **Step 5: Implement `geoecon/stages/discovery.py`**

```python
from __future__ import annotations
import logging
from datetime import timedelta
import requests
from .. import gdelt
from ..config import Config

log = logging.getLogger("geoecon.discovery")
MASTER_LATEST = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
CEILING = 150

def _default_fetch(window_start, window_end) -> list[str]:
    """Fetch GKG lines across [window_start, window_end] from the 15-min files.
    Uses the master file list, keeping .gkg.csv.zip URLs whose timestamp is in-window."""
    master = requests.get("http://data.gdeltproject.org/gdeltv2/masterfilelist.txt", timeout=120)
    master.raise_for_status()
    urls = []
    for row in master.text.splitlines():
        parts = row.split()
        if len(parts) == 3 and parts[2].endswith(".gkg.csv.zip"):
            stamp = parts[2].rsplit("/", 1)[-1][:14]
            try:
                from datetime import datetime, timezone
                ts = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if window_start <= ts <= window_end:
                urls.append(parts[2])
    lines: list[str] = []
    for u in urls:
        try:
            blob = requests.get(u, timeout=120).content
            lines.extend(gdelt.unzip_gkg_bytes(blob))
        except Exception as e:  # one bad file must not kill discovery
            log.warning("gkg file failed %s: %s", u, e)
    return lines

def run(cfg: Config, repo, run_id: int, *, fetch=_default_fetch) -> dict:
    from datetime import datetime, timezone
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=cfg.window_hours)
    lines = fetch(window_start, window_end)
    kept, skipped, seen_hashes = [], 0, set()
    for line in lines:
        rec = gdelt.parse_gkg_line(line)
        if rec is None:
            skipped += 1; continue
        if not gdelt.theme_intersection_ok(rec.themes):
            continue
        region = gdelt.region_for_country(rec.country)
        if region not in cfg.regions:
            continue
        h = gdelt.url_hash(rec.url)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        kept.append((rec, region, h))
    # salience proxy: theme-hit count desc; cap at CEILING (or cfg.limit)
    kept.sort(key=lambda x: len(x[0].themes), reverse=True)
    ceiling = cfg.limit or CEILING
    kept = kept[:ceiling]
    for rec, region, h in kept:
        repo.insert_article(run_id, source_url=rec.url, url_hash=h, domain=rec.domain,
                            region=region, gkg_themes=rec.themes, gkg_tone=rec.tone,
                            source_lang=rec.lang, title=None, gdelt_gkg_id=rec.gkg_id)
    metrics = {"discovered": len(kept), "discovery_skipped": skipped, "raw_lines": len(lines)}
    log.info("discovery: %s", metrics)
    return metrics
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_discovery.py -v`
Expected: PASS (4 tests).

- [ ] **Step 7: Commit**

```bash
git add geoecon/stages/__init__.py geoecon/stages/discovery.py geoecon/gdelt.py tests/unit/test_discovery.py tests/fixtures/gkg_sample.tsv
git commit -m "feat: GDELT GKG discovery stage (theme-intersection >=2, 150 ceiling)"
```

---

## Task 6: Extraction stage

**Files:**
- Create: `geoecon/stages/extract.py`
- Test: `tests/unit/test_extract.py`

**Interfaces:**
- Consumes: `Repo.articles_for_run`, `Repo.update_extraction`.
- Produces: `geoecon.stages.extract.run(cfg, repo, run_id, *, fetcher=...) -> dict`; `extract.extract_one(url, *, fetcher) -> tuple[str | None, str, str | None]` returning `(body, fetch_status, source_lang)` where `fetch_status ∈ {'extracted','headline_only','failed'}`.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_extract.py`

```python
from geoecon.stages import extract

def test_extract_one_success():
    body, status, lang = extract.extract_one(
        "http://x", fetcher=lambda u: ("<html><body>" + "word " * 80 + "</body></html>"))
    assert status == "extracted" and body and "word" in body

def test_extract_one_empty_falls_back_to_headline_only():
    body, status, lang = extract.extract_one("http://x", fetcher=lambda u: "")
    assert status == "headline_only" and body is None

def test_extract_one_error_marks_failed():
    def boom(u): raise RuntimeError("timeout")
    body, status, lang = extract.extract_one("http://x", fetcher=boom)
    assert status == "failed" and body is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_extract.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `geoecon/stages/extract.py`**

```python
from __future__ import annotations
import logging
import trafilatura
from ..config import Config

log = logging.getLogger("geoecon.extract")
MIN_CHARS = 200  # below this, treat as failed extraction -> headline-only

def _default_fetcher(url: str) -> str | None:
    return trafilatura.fetch_url(url)

def extract_one(url, *, fetcher=_default_fetcher):
    try:
        downloaded = fetcher(url)
    except Exception as e:
        log.warning("fetch failed %s: %s", url, e)
        return None, "failed", None
    if not downloaded:
        return None, "headline_only", None
    text = trafilatura.extract(downloaded, include_comments=False, favor_recall=True)
    if not text or len(text) < MIN_CHARS:
        return None, "headline_only", None
    return text, "extracted", None

def run(cfg: Config, repo, run_id: int, *, fetcher=_default_fetcher) -> dict:
    counts = {"extracted": 0, "headline_only": 0, "failed": 0}
    for art in repo.articles_for_run(run_id, fetch_status="pending"):
        body, status, lang = extract_one(art.source_url, fetcher=fetcher)
        repo.update_extraction(art.article_id, body, status, source_lang=lang,
                               fetch_error=None if status != "failed" else "extraction failed")
        counts[status] += 1
    log.info("extraction: %s", counts)
    return counts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_extract.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/extract.py tests/unit/test_extract.py
git commit -m "feat: extraction stage (trafilatura -> headline-only fallback)"
```

---

## Task 7: Cluster stage (Phase 1 trivial)

**Files:**
- Create: `geoecon/stages/cluster.py`
- Test: `tests/unit/test_cluster.py`

**Interfaces:**
- Consumes: `Repo.articles_for_run`, `Repo.insert_cluster`, `Repo.add_membership`.
- Produces: `geoecon.stages.cluster.fingerprint(title, canonical_url) -> str`; `cluster.run(cfg, repo, run_id) -> dict` — creates one cluster per non-`failed` article.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_cluster.py`

```python
from geoecon.stages import cluster

def test_fingerprint_prefers_title_then_url():
    assert cluster.fingerprint("Saudi Trade Deal", None) == cluster.fingerprint("  saudi   trade deal ", None)
    assert cluster.fingerprint(None, "http://a/b") == cluster.fingerprint(None, "http://a/b")

def test_run_creates_one_cluster_per_article():
    from geoecon.state.models import Article
    arts = [Article(1, "u1", "h1", "d", "MENA", [], None, None, "ar", "T1", "body", "extracted"),
            Article(2, "u2", "h2", "d", "MENA", [], None, None, "es", None, None, "failed")]
    made = []
    class FakeRepo:
        def articles_for_run(self, rid): return arts
        def insert_cluster(self, rid, fp, region, langs, n, framing_divergent=False):
            made.append((fp, region, langs, n)); return len(made)
        def add_membership(self, cid, aid, frame_label=None): pass
    metrics = cluster.run(cfg=None, repo=FakeRepo(), run_id=1)
    assert metrics["clusters"] == 1  # failed article excluded
    assert made[0][1] == "MENA" and made[0][2] == ["ar"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_cluster.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `geoecon/stages/cluster.py`**

```python
from __future__ import annotations
import hashlib, logging, re

log = logging.getLogger("geoecon.cluster")

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())

def fingerprint(title: str | None, canonical_url: str | None) -> str:
    basis = _norm(title) if title else (canonical_url or "")
    return "p1:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()

def run(cfg, repo, run_id: int) -> dict:
    n = 0
    for art in repo.articles_for_run(run_id):
        if art.fetch_status == "failed":
            continue
        fp = fingerprint(art.title, art.source_url)
        langs = [art.source_lang] if art.source_lang else []
        cid = repo.insert_cluster(run_id, fp, art.region, langs, 1)
        repo.add_membership(cid, art.article_id)
        n += 1
    log.info("cluster(P1): %d clusters", n)
    return {"clusters": n}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_cluster.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/cluster.py tests/unit/test_cluster.py
git commit -m "feat: Phase-1 trivial cluster stage (1 article = 1 cluster)"
```

---

## Task 8: Synthesis stage

**Files:**
- Create: `geoecon/stages/synth.py`
- Test: `tests/unit/test_synth.py`

**Interfaces:**
- Consumes: `LLMClient.complete`, the `validator`, `Repo.clusters_for_run`, `Repo.articles_in_cluster`, `Repo.get_seen`, `Repo.upsert_synthesis_record`.
- Produces: `geoecon.stages.synth.build_messages(prompt, articles) -> list[dict]`; `synth.parse_record(text) -> dict | None`; `synth.run(cfg, repo, run_id, client, *, validate=...) -> dict`.

> The validator's exact function name/signature comes from the vendored `validator.py`. Inspect it and bind `validate` to it. The expected contract: `validate(record: dict) -> (flag: str, detail: dict | None)` where `flag == "ok"` passes. If the vendored signature differs, adapt `synth.run`'s `validate=` default accordingly in Step 3 (keep the `(flag, detail)` shape via a thin adapter).

- [ ] **Step 1: Write the failing test** — `tests/unit/test_synth.py`

```python
from geoecon.stages import synth
from geoecon.state.models import Article, Cluster

def _client(answer):
    class Resp:
        def __init__(self, fa): self.final_answer = fa
        def ok(self): return True
    class C:
        def complete(self, model, messages, **kw): return Resp(answer)
    return C()

GOOD = '{"headline_en":"X","what_happened_en":"Y","entities":[{"canonical_en":"E"}],"key_figures":[{"value":1}]}'

def test_parse_record_extracts_json():
    assert synth.parse_record("noise " + GOOD + " trailer")["headline_en"] == "X"

def test_run_persists_ok_record():
    arts = [Article(1, "u", "h", "d", "MENA", [], None, None, "ar", "T", "body text", "extracted")]
    saved = []
    class FakeRepo:
        def clusters_for_run(self, rid): return [Cluster(10, "fp", "MENA", ["ar"], 1, False)]
        def articles_in_cluster(self, cid): return arts
        def get_seen(self, fp): return None
        def upsert_synthesis_record(self, rid, cid, fields, flag, quality_detail=None, is_headline_only=False):
            saved.append((cid, flag, fields)); return 1
    cfg = type("C", (), {"synthesis_model": "deepseek-v3.2", "synthesis_prompt_id": "task_a_cov"})()
    metrics = synth.run(cfg, FakeRepo(), 1, _client(GOOD),
                        prompt="PROMPT", validate=lambda rec: ("ok", None))
    assert metrics["records_ok"] == 1 and saved[0][1] == "ok"

def test_run_holds_non_ok_record():
    arts = [Article(1, "u", "h", "d", "MENA", [], None, None, "ar", "T", "b", "extracted")]
    saved = []
    class FakeRepo:
        def clusters_for_run(self, rid): return [Cluster(10, "fp", "MENA", ["ar"], 1, False)]
        def articles_in_cluster(self, cid): return arts
        def get_seen(self, fp): return None
        def upsert_synthesis_record(self, rid, cid, fields, flag, quality_detail=None, is_headline_only=False):
            saved.append(flag); return 1
    cfg = type("C", (), {"synthesis_model": "m", "synthesis_prompt_id": "p"})()
    synth.run(cfg, FakeRepo(), 1, _client("not json"),
              prompt="P", validate=lambda rec: ("invalid_json", {"why": "x"}))
    assert saved == ["invalid_json"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_synth.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `geoecon/stages/synth.py`**

```python
from __future__ import annotations
import json, logging, re
log = logging.getLogger("geoecon.synth")

_JSON_RE = re.compile(r"\{.*\}", re.S)

def parse_record(text: str) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

def build_messages(prompt: str, articles) -> list[dict]:
    parts = []
    for a in articles:
        if a.body:
            parts.append(f"[{a.source_lang or '?'}] {a.title or ''}\n{a.body}")
        else:
            parts.append(f"[HEADLINE-ONLY · {a.source_lang or '?'}] {a.title or a.source_url}")
    return [{"role": "system", "content": prompt},
            {"role": "user", "content": "\n\n---\n\n".join(parts)}]

def _default_validate(record: dict):
    # Bound at wire-up to the vendored validator; see Task 8 interface note.
    from ..llm import validator as v
    return v.validate(record)  # adapt name if the vendored API differs

def run(cfg, repo, run_id, client, *, prompt=None, validate=_default_validate) -> dict:
    if prompt is None:
        from importlib.resources import files
        prompt = files("geoecon.prompts").joinpath(f"{cfg.synthesis_prompt_id}.txt").read_text("utf-8")
    counts = {"records_total": 0, "records_ok": 0, "records_held": 0}
    for cl in repo.clusters_for_run(run_id):
        seen = repo.get_seen(cl.event_fingerprint)
        if seen and seen.get("last_emitted_at"):
            continue  # non-overlap: already emitted (P1 = simple suppression)
        articles = repo.articles_in_cluster(cl.cluster_id)
        headline_only = all(a.body is None for a in articles)
        messages = build_messages(prompt, articles)
        resp = client.complete(cfg.synthesis_model, messages,
                               temperature=0, seed=12345, max_tokens=8192)
        record = parse_record(getattr(resp, "final_answer", "") or "") if resp.ok() else None
        if record is None:
            flag, detail = "invalid_json", {"error": getattr(resp, "error", "parse failed")}
        else:
            flag, detail = validate(record)
            if flag != "ok":  # one retry
                resp = client.complete(cfg.synthesis_model, messages,
                                       temperature=0, seed=12345, max_tokens=8192)
                record2 = parse_record(getattr(resp, "final_answer", "") or "") if resp.ok() else None
                if record2 is not None:
                    f2, d2 = validate(record2)
                    if f2 == "ok":
                        record, flag, detail = record2, f2, d2
        repo.upsert_synthesis_record(run_id, cl.cluster_id, record or {}, flag,
                                     quality_detail=detail, is_headline_only=headline_only)
        counts["records_total"] += 1
        counts["records_ok" if flag == "ok" else "records_held"] += 1
    log.info("synthesis: %s", counts)
    return counts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_synth.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/synth.py tests/unit/test_synth.py
git commit -m "feat: synthesis stage (deepseek-v3.2 + validator gate + retry/hold)"
```

---

## Task 9: Render stage (en_only)

**Files:**
- Create: `geoecon/stages/render.py`, `geoecon/render/templates/email.html.j2`, `geoecon/render/templates/archive.html.j2`
- Test: `tests/unit/test_render.py`

**Interfaces:**
- Consumes: `Repo.ok_records`, `SynthesisRecord.fields`.
- Produces: `geoecon.stages.render.render_html(records, *, mode="en_only", date_str, throughline=None) -> tuple[str, str]` returning `(archive_html, email_html)`; `render.run(cfg, repo, run_id, date_str) -> dict` (writes archive file + `newsletters` row, returns `{newsletter_id, record_count, email_html, archive_path}`).

- [ ] **Step 1: Write the failing test** — `tests/unit/test_render.py`

```python
from geoecon.stages import render
from geoecon.state.models import SynthesisRecord

def _rec():
    return SynthesisRecord(1, 10, {
        "headline_en": "Saudi cuts oil output",
        "what_happened_en": "Riyadh announced a 1mbd cut.",
        "geoeconomic_significance_en": "Pressures crude prices.",
        "entities": [{"canonical_en": "Saudi Arabia"}],
        "key_figures": [{"value": 1, "unit": "mbd", "gloss_en": "1 million barrels/day"}],
        "sources": [{"url": "http://x", "lang": "ar"}],
        "headline_original": "السعودية تخفض الإنتاج", "headline_original_lang": "ar",
    }, "ok", False)

def test_en_only_includes_english_excludes_original():
    archive, email = render.render_html([_rec()], mode="en_only", date_str="2026-06-19")
    assert "Saudi cuts oil output" in email and "Saudi cuts oil output" in archive
    assert "السعودية" not in email  # en_only ignores originals
    assert "<table" in email.lower()  # email is table-based

def test_zero_records_renders_heartbeat():
    archive, email = render.render_html([], mode="en_only", date_str="2026-06-19")
    assert "no qualifying" in email.lower() or "quiet" in email.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_render.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `geoecon/render/templates/email.html.j2`** (table-based, inline CSS, ~600px)

```jinja
<!doctype html><html><body style="margin:0;padding:0;background:#f4f4f4;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;background:#ffffff;font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;">
  <tr><td style="padding:24px 28px;border-bottom:3px solid #1a1a1a;">
    <div style="font-size:20px;font-weight:bold;">Geoeconomic Brief</div>
    <div style="font-size:13px;color:#777;">{{ date_str }}</div></td></tr>
  {% if throughline %}<tr><td style="padding:18px 28px;background:#fafafa;font-style:italic;font-size:14px;">{{ throughline }}</td></tr>{% endif %}
  {% if records %}{% for r in records %}
  <tr><td style="padding:20px 28px;border-bottom:1px solid #eee;">
    <div style="font-size:16px;font-weight:bold;">{{ r.fields.headline_en }}</div>
    <div style="font-size:14px;line-height:1.5;margin-top:6px;">{{ r.fields.what_happened_en }}</div>
    {% if r.fields.geoeconomic_significance_en %}<div style="font-size:13px;color:#444;margin-top:6px;"><b>Significance:</b> {{ r.fields.geoeconomic_significance_en }}</div>{% endif %}
    {% if r.fields.key_figures %}<div style="font-size:12px;color:#666;margin-top:6px;">{% for k in r.fields.key_figures %}{{ k.value }}{{ k.unit or '' }} {{ k.gloss_en or '' }}{% if not loop.last %} · {% endif %}{% endfor %}</div>{% endif %}
  </td></tr>{% endfor %}
  {% else %}<tr><td style="padding:24px 28px;font-size:14px;color:#666;">A quiet day — no qualifying items passed the quality gate.</td></tr>{% endif %}
  <tr><td style="padding:16px 28px;font-size:11px;color:#999;">Discovered {{ stats.discovered or 0 }} · passed {{ records|length }}</td></tr>
</table></td></tr></table></body></html>
```

- [ ] **Step 4: Write `geoecon/render/templates/archive.html.j2`** (rich standalone)

```jinja
<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Geoeconomic Brief — {{ date_str }}</title><style>
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:820px;margin:40px auto;padding:0 20px;color:#16181d;line-height:1.55;}
  h1{border-bottom:3px solid #16181d;padding-bottom:8px;} .date{color:#888;} .item{padding:18px 0;border-bottom:1px solid #ececec;}
  .sig{color:#3a3a3a;} .figs{color:#666;font-size:.9em;} .through{font-style:italic;background:#fafafa;padding:14px;border-left:3px solid #16181d;}
</style></head><body>
<h1>Geoeconomic Brief</h1><div class="date">{{ date_str }}</div>
{% if throughline %}<p class="through">{{ throughline }}</p>{% endif %}
{% for r in records %}<div class="item">
  <h2>{{ r.fields.headline_en }}</h2>
  <p>{{ r.fields.what_happened_en }}</p>
  {% if r.fields.geoeconomic_significance_en %}<p class="sig"><b>Significance:</b> {{ r.fields.geoeconomic_significance_en }}</p>{% endif %}
  {% if r.fields.key_figures %}<p class="figs">{% for k in r.fields.key_figures %}{{ k.value }}{{ k.unit or '' }} — {{ k.gloss_en or '' }}{% if not loop.last %}; {% endif %}{% endfor %}</p>{% endif %}
</div>{% else %}<p>A quiet day — no qualifying items.</p>{% endfor %}
</body></html>
```

- [ ] **Step 5: Implement `geoecon/stages/render.py`**

```python
from __future__ import annotations
import logging, os
from jinja2 import Environment, PackageLoader, select_autoescape
log = logging.getLogger("geoecon.render")

_env = Environment(loader=PackageLoader("geoecon.render", "templates"),
                   autoescape=select_autoescape(["html", "j2"]))

def render_html(records, *, mode="en_only", date_str, throughline=None, stats=None):
    ctx = {"records": records, "date_str": date_str, "throughline": throughline,
           "stats": stats or {}, "mode": mode}
    archive = _env.get_template("archive.html.j2").render(**ctx)
    email = _env.get_template("email.html.j2").render(**ctx)
    return archive, email

def run(cfg, repo, run_id, date_str, *, stats=None) -> dict:
    records = repo.ok_records(run_id)
    archive, email = render_html(records, mode="en_only", date_str=date_str, stats=stats)
    os.makedirs(cfg.archive_dir, exist_ok=True)
    archive_path = os.path.join(cfg.archive_dir, f"geoecon-{date_str}.html")
    with open(archive_path, "w", encoding="utf-8") as fh:
        fh.write(archive)
    nid = repo.insert_newsletter(run_id, "en_only", archive_path, email, len(records))
    log.info("render: %d records -> %s", len(records), archive_path)
    return {"newsletter_id": nid, "record_count": len(records),
            "email_html": email, "archive_path": archive_path}
```

- [ ] **Step 6: Run tests to verify they pass** (add `geoecon/render/__init__.py`)

Run: `pytest tests/unit/test_render.py -v`
Expected: PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add geoecon/stages/render.py geoecon/render/ tests/unit/test_render.py
git commit -m "feat: en_only dual HTML render (archive + email-safe)"
```

---

## Task 10: Send stage (SMTP)

**Files:**
- Create: `geoecon/stages/send.py`
- Test: `tests/unit/test_send.py`

**Interfaces:**
- Consumes: `Config` SMTP fields, `Repo.update_newsletter_send`.
- Produces: `geoecon.stages.send.build_message(cfg, subject, html) -> EmailMessage`; `send.run(cfg, repo, newsletter_id, subject, html, *, transport=...) -> dict`.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_send.py`

```python
from geoecon.stages import send

class Cfg:
    from_addr = "me@gmail.com"; recipients = ("a@x.com", "b@y.com")
    smtp_host = "h"; smtp_port = 587; smtp_user = "me@gmail.com"; smtp_password = "pw"

def test_build_message_sets_headers_and_html():
    msg = send.build_message(Cfg(), "Subject X", "<b>hi</b>")
    assert msg["From"] == "me@gmail.com"
    assert msg["To"] == "a@x.com, b@y.com"
    assert msg["Subject"] == "Subject X"
    assert msg.get_content_type() == "text/html"

def test_run_marks_sent_on_success():
    sent = {}
    class FakeRepo:
        def update_newsletter_send(self, nid, status, send_error=None):
            sent["status"] = status
    def transport(cfg, msg): return None  # success
    out = send.run(Cfg(), FakeRepo(), 1, "S", "<b>h</b>", transport=transport)
    assert out["send_status"] == "sent" and sent["status"] == "sent"

def test_run_marks_failed_on_error():
    sent = {}
    class FakeRepo:
        def update_newsletter_send(self, nid, status, send_error=None):
            sent["status"] = status; sent["err"] = send_error
    def transport(cfg, msg): raise RuntimeError("smtp down")
    out = send.run(Cfg(), FakeRepo(), 1, "S", "<b>h</b>", transport=transport)
    assert out["send_status"] == "failed" and "smtp down" in sent["err"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_send.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `geoecon/stages/send.py`**

```python
from __future__ import annotations
import logging, smtplib, ssl
from email.message import EmailMessage
log = logging.getLogger("geoecon.send")

def build_message(cfg, subject, html) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.recipients)
    msg["Subject"] = subject
    msg.set_content("HTML email — please view in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")
    return msg

def _smtp_transport(cfg, msg):
    ctx = ssl.create_default_context()
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60) as s:
        s.starttls(context=ctx)
        s.login(cfg.smtp_user, cfg.smtp_password)
        s.send_message(msg)

def run(cfg, repo, newsletter_id, subject, html, *, transport=_smtp_transport) -> dict:
    msg = build_message(cfg, subject, html)
    try:
        transport(cfg, msg)
    except Exception as e:
        log.error("send failed: %s", e)
        repo.update_newsletter_send(newsletter_id, "failed", send_error=str(e)[:2000])
        return {"send_status": "failed", "error": str(e)}
    repo.update_newsletter_send(newsletter_id, "sent")
    log.info("send: ok -> %s", cfg.recipients)
    return {"send_status": "sent"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_send.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: (Optional) live-ish SMTP test with aiosmtpd** — `tests/unit/test_send_aiosmtpd.py`

```python
import asyncio, pytest
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
    sink = Sink(); ctrl = Controller(sink, hostname="127.0.0.1", port=8025); ctrl.start()
    try:
        def plain_transport(cfg, msg):
            import smtplib
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=10) as s:
                s.send_message(msg)
        out = send.run(Cfg(), type("R", (), {"update_newsletter_send": lambda *a, **k: None})(),
                       1, "Hi", "<b>x</b>", transport=plain_transport)
        assert out["send_status"] == "sent" and len(sink.messages) == 1
    finally:
        ctrl.stop()
```

- [ ] **Step 6: Commit**

```bash
git add geoecon/stages/send.py tests/unit/test_send.py tests/unit/test_send_aiosmtpd.py
git commit -m "feat: SMTP send stage with injectable transport"
```

---

## Task 11: Orchestrator (sequence + resume + CLI)

**Files:**
- Create: `geoecon/orchestrator.py`
- Test: `tests/unit/test_orchestrator.py`

**Interfaces:**
- Consumes: every stage's `run(...)`, `Config`, `Repo`, `db.connect`/`apply_migrations`.
- Produces: `geoecon.orchestrator.run_pipeline(cfg, repo, *, now, stages=...) -> dict`; `orchestrator.main(argv=None) -> int` (CLI entrypoint with `--dry-run`, `--limit N`).

- [ ] **Step 1: Write the failing test** — `tests/unit/test_orchestrator.py`

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_orchestrator.py -v`
Expected: FAIL (`AttributeError: run_pipeline`).

- [ ] **Step 3: Implement `geoecon/orchestrator.py`**

```python
from __future__ import annotations
import argparse, logging, os, sys
from datetime import datetime, timedelta, timezone
from .config import Config

log = logging.getLogger("geoecon.orchestrator")

def _default_stages(cfg, client):
    from .stages import discovery, extract, cluster, synth, render, send
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = {}
    def _discovery(c, r, rid): state["m"] = discovery.run(c, r, rid); return state["m"]
    def _extract(c, r, rid):  return extract.run(c, r, rid)
    def _cluster(c, r, rid):  return cluster.run(c, r, rid)
    def _synth(c, r, rid):    return synth.run(c, r, rid, client)
    def _render(c, r, rid):
        out = render.run(c, r, rid, date_str, stats=state.get("m"))
        state["render"] = out; return {"record_count": out["record_count"]}
    def _send(c, r, rid):
        out = state["render"]
        if c.dry_run:
            log.info("dry-run: skipping send"); return {"send_status": "skipped"}
        if out["record_count"] == 0 and not c.send_heartbeat:
            return {"send_status": "skipped_empty"}
        subject = f"Geoeconomic Brief — {date_str}"
        return send.run(c, r, out["newsletter_id"], subject, out["email_html"])
    return [("discovery", _discovery), ("extraction", _extract), ("cluster", _cluster),
            ("synthesis", _synth), ("render", _render), ("send", _send)]

def run_pipeline(cfg: Config, repo, *, now=None, stages=None, client=None) -> dict:
    now = now or datetime.now(timezone.utc)
    window_end = now
    window_start = now - timedelta(hours=cfg.window_hours)
    run_id = repo.find_resumable_run(window_start, window_end)
    if run_id is None:
        run_id = repo.create_run(window_start, window_end, now,
                                 cfg.synthesis_model, cfg.synthesis_prompt_id, cfg.embedding_model)
    done = repo.completed_stages(run_id)
    stages = stages if stages is not None else _default_stages(cfg, client)
    metrics = {}
    for name, fn in stages:
        if name in done:
            log.info("resume: skipping completed stage %s", name); continue
        repo.start_stage(run_id, name)
        try:
            result = fn(cfg, repo, run_id) or {}
        except Exception as e:
            repo.fail_stage(run_id, name, e)
            repo.set_run_status(run_id, "failed")
            log.exception("stage %s failed", name)
            raise
        repo.complete_stage(run_id, name, result)
        if isinstance(result, dict):
            metrics.update(result)
    repo.merge_run_metrics(run_id, metrics)
    repo.set_run_status(run_id, "completed")
    return {"run_id": run_id, "metrics": metrics}

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="geoecon")
    p.add_argument("command", nargs="?", default="run")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    env = dict(os.environ)
    if args.dry_run: env["DRY_RUN"] = "true"
    if args.limit: env["LIMIT"] = str(args.limit)
    cfg = Config.from_env(env)
    from .state import db
    from .state.repo import Repo
    from .llm.client import LLMClient
    conn = db.connect(cfg.db_dsn); db.apply_migrations(conn)
    repo = Repo(conn)
    client = LLMClient(cfg.llm_base_url, cfg.llm_api_key)
    try:
        out = run_pipeline(cfg, repo, client=client)
        log.info("pipeline done: %s", out)
        return 0
    except Exception:
        return 1

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_orchestrator.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the full unit suite**

Run: `pytest -q -m "not requires_db and not requires_net"`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add geoecon/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat: staged resumable orchestrator + CLI (--dry-run, --limit)"
```

---

## Task 12: Containerization

**Files:**
- Create: `Dockerfile`, `.dockerignore`
- Test: manual smoke (documented commands)

**Interfaces:**
- Produces: image `jalalhussein/geoecon-pipeline:<tag>` with entrypoint `python -m geoecon.orchestrator`.

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd -u 1000 -m app
WORKDIR /app
COPY pyproject.toml ./
COPY geoecon ./geoecon
RUN pip install --no-cache-dir . && mkdir -p /data/archive && chown -R app /data
USER 1000
ENTRYPOINT ["python", "-m", "geoecon.orchestrator"]
CMD ["run"]
```

- [ ] **Step 2: Write `.dockerignore`**

```
tests/
docs/
.git/
__pycache__/
*.pyc
.env
```

- [ ] **Step 3: Build and smoke-test locally**

Run:
```bash
docker build -t jalalhussein/geoecon-pipeline:dev .
docker run --rm jalalhussein/geoecon-pipeline:dev --help
```
Expected: argparse help prints (entrypoint works, package importable).

- [ ] **Step 4: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "build: container image for the pipeline"
```

---

## Task 13: Kubernetes deploy manifests

**Files:**
- Create: `deploy/db-cluster.yaml`, `deploy/db-init-configmap.yaml`, `deploy/secret.example.yaml`, `deploy/pvc-archive.yaml`, `deploy/networkpolicy.yaml`, `deploy/cronjob.yaml`
- Test: client-side validation + one-shot Job (documented)

**Interfaces:**
- Consumes: the image from Task 12; `schema.sql`.
- Produces: deployable `geoecon-*` objects in `hussein-ns`.

- [ ] **Step 1: `deploy/db-cluster.yaml`** — CNPG Postgres, schema applied at bootstrap

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: geoecon-db
  namespace: hussein-ns
spec:
  instances: 1
  imageName: ghcr.io/cloudnative-pg/postgresql:17
  storage:
    size: 5Gi
    storageClass: nfs-csi
  bootstrap:
    initdb:
      database: geoeconomics
      owner: geoecon
      postInitApplicationSQLRefs:
        configMapRefs:
          - name: geoecon-db-init
            key: 001_init.sql
  resources:
    requests: { cpu: "100m", memory: "256Mi" }
    limits: { cpu: "500m", memory: "1Gi" }
```

- [ ] **Step 2: `deploy/db-init-configmap.yaml`** — generated from `schema.sql`

Run (generates the ConfigMap from the schema so the DB bootstraps with all tables):
```bash
kubectl create configmap geoecon-db-init -n hussein-ns \
  --from-file=001_init.sql=schema.sql --dry-run=client -o yaml > deploy/db-init-configmap.yaml
```

- [ ] **Step 3: `deploy/secret.example.yaml`** (no real values; copy to a real secret out-of-band)

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: geoecon-secrets
  namespace: hussein-ns
type: Opaque
stringData:
  MAETCENTRUM_KEY: "REPLACE"
  SMTP_HOST: "smtp.gmail.com"
  SMTP_PORT: "587"
  SMTP_USER: "REPLACE@gmail.com"
  SMTP_PASSWORD: "REPLACE_APP_PASSWORD"
  FROM_ADDR: "REPLACE@gmail.com"
  RECIPIENTS: "REPLACE@example.com"
  TIMEZONE: "Europe/Prague"
  ARCHIVE_DIR: "/data/archive"
  # DB_DSN is assembled from the CNPG-generated geoecon-db-app secret (see cronjob env)
```

- [ ] **Step 4: `deploy/pvc-archive.yaml`**

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: geoecon-archive
  namespace: hussein-ns
spec:
  accessModes: ["ReadWriteOnce"]
  resources: { requests: { storage: 2Gi } }
  storageClassName: nfs-csi
```

- [ ] **Step 5: `deploy/networkpolicy.yaml`** — allow only geoecon pods to reach geoecon-db

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: geoecon-db-netpol
  namespace: hussein-ns
spec:
  podSelector:
    matchLabels: { cnpg.io/cluster: geoecon-db }
  policyTypes: ["Ingress"]
  ingress:
    - from:
        - podSelector:
            matchLabels: { app: geoecon }
      ports:
        - { protocol: TCP, port: 5432 }
```

- [ ] **Step 6: `deploy/cronjob.yaml`** — 07:00 Europe/Prague, hardened, explicit resources

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: geoecon-daily
  namespace: hussein-ns
spec:
  schedule: "0 7 * * *"
  timeZone: "Europe/Prague"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 7
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 2
      template:
        metadata:
          labels: { app: geoecon }
        spec:
          restartPolicy: OnFailure
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            runAsGroup: 1000
            seccompProfile: { type: RuntimeDefault }
          containers:
            - name: pipeline
              image: jalalhussein/geoecon-pipeline:dev
              args: ["run"]
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
                requests: { cpu: "1", memory: "1Gi" }
                limits: { cpu: "2", memory: "2Gi" }
              volumeMounts:
                - { name: archive, mountPath: /data/archive }
          volumes:
            - name: archive
              persistentVolumeClaim: { claimName: geoecon-archive }
```

- [ ] **Step 7: Validate client-side**

Run:
```bash
for f in deploy/*.yaml; do kubectl --kubeconfig ~/.kube/kuba-cluster.yaml apply --dry-run=client -f "$f"; done
```
Expected: each prints `(dry run)` with no schema errors. (CRD `Cluster` validates against the cnpg.io CRD already installed.)

- [ ] **Step 8: Commit**

```bash
git add deploy/
git commit -m "deploy: geoecon CNPG db, secret template, pvc, networkpolicy, cronjob"
```

---

## Deployment runbook (after Task 13; executed against the live cluster on your go)

1. Push the image: `docker push jalalhussein/geoecon-pipeline:<date>-1` and set that tag in `cronjob.yaml`.
2. `kubectl apply -f deploy/db-init-configmap.yaml -f deploy/db-cluster.yaml` → wait for `geoecon-db-1` Ready.
3. Create the real secret from `secret.example.yaml` (fill values; use a Gmail **app password**).
4. `kubectl apply -f deploy/pvc-archive.yaml -f deploy/networkpolicy.yaml -f deploy/cronjob.yaml`.
5. **Smoke (one-shot, dry-run):** `kubectl create job -n hussein-ns --from=cronjob/geoecon-daily geoecon-smoke -- run --limit 10 --dry-run`; inspect logs + DB rows + archive file; confirm no `siem-*` object touched.
6. Remove smoke job; let the 07:00 schedule run. **Human-read** the first N daily outputs (blueprint authority); watch for the `kimi-k2.6` coverage-gap trigger.

---

## Self-Review

**Spec coverage:**
- Repo layout (§1) → Tasks 1–13. State/resumability (§2) → Tasks 2,3,11. Phase-1 stages (§3) → Tasks 5–10. Deploy (§5) → Tasks 12–13. Error handling (§6) → per-stage try/except + validator gate (Tasks 8,11) + send failure (Task 10). Testing (§7) → tests in every task + aiosmtpd + dry-run + smoke runbook. Phase-2 (§4) → explicitly deferred to its own plan.
- Gap noted & handled: the validator's exact function name is bound at Task 8 against the vendored file (interface note); the prompt location is confirmed at Task 4.

**Placeholder scan:** no `TBD`/`TODO`/"handle edge cases"; every code step shows real code. The two "inspect the vendored file" notes (Tasks 4, 8) are concrete verification actions, not deferred logic.

**Type consistency:** `Repo` method names/signatures match between definition (Task 3) and callers (Tasks 5–11); stage `run(cfg, repo, run_id)` signature is uniform; `render.run` returns `email_html`/`newsletter_id` consumed by the orchestrator's send step; `Config` field names match `.from_env` and all consumers.

**Known external-dependency risk (flagged, not a placeholder):** GDELT GKG exact column indices are per the published 2.1 codebook; Task 5's pre-step verifies them against a live file before locking — standard practice for an external feed.
