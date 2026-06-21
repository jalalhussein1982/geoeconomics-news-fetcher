# Geoeconomic Capture-Quality Overhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the daily pipeline capture genuinely *geoeconomic* (economy × geopolitics) news, balanced per region and de-duplicated, by inserting an LLM relevance/region gate, per-region quotas, and lexical near-dup merging — plus fix the garbled key-figures rendering.

**Architecture:** New flow `discovery (relaxed prefilter, no global cap) → extraction (now captures title) → gate (NEW LLM stage, batched: relevance + primary-country) → cluster (filter to gated, lexical dedup → multi-article clusters) → synthesis → summary → render (per-region cap ~12 + figures fix) → send`. The gate corrects region misattribution by overwriting `articles.country_fips` with the resolved in-scope country; Egypt still maps to both Middle East and North Africa.

**Tech Stack:** Python 3.12, psycopg 3 (autocommit), Jinja2, trafilatura, an OpenAI-compatible LLM via `LLMClient`, pytest.

## Global Constraints

- No new pip dependencies (psycopg, jinja2, requests, trafilatura; pytest for tests).
- Python 3.12; `from __future__ import annotations` at top of every module.
- snake_case; timestamps UTC; JSONB via `psycopg.types.json.Jsonb`.
- GDELT GKG emits **FIPS 10-4** codes; all country sets/resolution use FIPS.
- LLM calls deterministic: `temperature=0, seed=12345`.
- **Geoeconomic = economy × geopolitics**, Moderate rubric: KEEP economy×geopolitics (sanctions, energy security, trade disputes, political-risk capital flows, conflict/diplomacy with economic stakes, sovereign-wealth/strategic-industry moves) + nationally-significant economic policy/structural moves (IMF programs, major currency/budget/reserves shifts, large FDI, diversification like Vision 2030, capital-market reforms); DROP generic markets/crypto/corporate earnings/standalone macro prints/foreign-domestic.
- Bucket keys `middle_east`/`north_africa`/`latam`; **Egypt (EG) ∈ both** middle_east and north_africa.
- Gate output is strict JSON; fail-open on unparseable (never silently drop a whole batch).
- Per-region cap default 12, applied at grouping time (summary + render must cap identically).
- DB-gated tests use `@pytest.mark.requires_db` + `TEST_DATABASE_URL` (live throwaway Postgres at `postgresql://postgres:postgres@localhost:55432/postgres` during dev).

---

### Task 1: Config — new tuning fields

**Files:**
- Modify: `geoecon/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `Config.gate_model: str` (default `"deepseek-v3.2"`), `Config.gate_batch_size: int` (15), `Config.region_cap: int` (12), `Config.discovery_ceiling: int` (120), `Config.dedup_jaccard: float` (0.6), parsed from env `GATE_MODEL`/`GATE_BATCH_SIZE`/`REGION_CAP`/`DISCOVERY_CEILING`/`DEDUP_JACCARD`.

- [ ] **Step 1: Write the failing test** — add to `tests/unit/test_config.py`:

```python
def test_capture_overhaul_defaults():
    cfg = Config.from_env(dict(_ENV))
    assert cfg.gate_model == "deepseek-v3.2"
    assert cfg.gate_batch_size == 15
    assert cfg.region_cap == 12
    assert cfg.discovery_ceiling == 120
    assert cfg.dedup_jaccard == 0.6


def test_capture_overhaul_env_overrides():
    cfg = Config.from_env({**_ENV, "REGION_CAP": "8", "DEDUP_JACCARD": "0.7",
                           "GATE_BATCH_SIZE": "20", "DISCOVERY_CEILING": "200",
                           "GATE_MODEL": "qwen3"})
    assert cfg.region_cap == 8 and cfg.dedup_jaccard == 0.7
    assert cfg.gate_batch_size == 20 and cfg.discovery_ceiling == 200
    assert cfg.gate_model == "qwen3"
```

(If `_ENV` is not already a module constant in this file, reuse the existing minimal env dict the other tests use.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py -k capture_overhaul -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'gate_model'`

- [ ] **Step 3: Implement** — in the `Config` dataclass (after `buckets`):

```python
    gate_model: str = "deepseek-v3.2"
    gate_batch_size: int = 15
    region_cap: int = 12
    discovery_ceiling: int = 120
    dedup_jaccard: float = 0.6
```

In `from_env` (after the `buckets=` line):

```python
            gate_model=env.get("GATE_MODEL", "deepseek-v3.2"),
            gate_batch_size=int(env.get("GATE_BATCH_SIZE", "15")),
            region_cap=int(env.get("REGION_CAP", "12")),
            discovery_ceiling=int(env.get("DISCOVERY_CEILING", "120")),
            dedup_jaccard=float(env.get("DEDUP_JACCARD", "0.6")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add geoecon/config.py tests/unit/test_config.py
git commit -m "feat(config): gate/region-cap/dedup tuning fields"
```

---

### Task 2: regions — country-name resolution + per-bucket cap

**Files:**
- Modify: `geoecon/regions.py`
- Test: `tests/unit/test_regions.py`

**Interfaces:**
- Consumes: `BUCKETS`, `BUCKET_ORDER`, `buckets_for_country`, `group_by_bucket` (existing).
- Produces:
  - `country_to_fips(name: str | None) -> str | None` — maps an in-scope country NAME (case-insensitive, + aliases UAE/Türkiye/Palestine/Gaza Strip) to its FIPS code; `None` for `"other"`/unknown/empty.
  - `top_per_bucket(items, country_of, score_of, n, active=BUCKET_ORDER) -> dict[str, list]` — `group_by_bucket` then sort each bucket by `score_of` desc and keep the top `n`.

- [ ] **Step 1: Write the failing test** — add to `tests/unit/test_regions.py`:

```python
def test_country_to_fips_names_and_aliases():
    assert regions.country_to_fips("Egypt") == "EG"
    assert regions.country_to_fips("saudi arabia") == "SA"
    assert regions.country_to_fips("UAE") == "AE"
    assert regions.country_to_fips("United Arab Emirates") == "AE"
    assert regions.country_to_fips("Türkiye") == "TU"
    assert regions.country_to_fips("Brazil") == "BR"
    assert regions.country_to_fips("other") is None
    assert regions.country_to_fips("United States") is None
    assert regions.country_to_fips(None) is None


def test_top_per_bucket_ranks_caps_and_handles_egypt_dual():
    # tuples: (id, country_fips, score)
    items = [("eg", "EG", 0.9), ("sa", "SA", 0.8), ("ir", "IR", 0.7),
             ("br", "BR", 0.5), ("ly", "LY", 0.4)]
    out = regions.top_per_bucket(items, country_of=lambda t: t[1],
                                 score_of=lambda t: t[2], n=2)
    me = [t[0] for t in out["middle_east"]]
    na = [t[0] for t in out["north_africa"]]
    la = [t[0] for t in out["latam"]]
    assert me == ["eg", "sa"]          # top 2 of EG/SA/IR by score
    assert na == ["eg", "ly"]          # Egypt appears in NA too, independently capped
    assert la == ["br"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_regions.py -k "country_to_fips or top_per_bucket" -v`
Expected: FAIL — `AttributeError: module 'geoecon.regions' has no attribute 'country_to_fips'`

- [ ] **Step 3: Implement** — append to `geoecon/regions.py`:

```python
# Country NAME -> FIPS for the in-scope set, with common aliases. Used by the
# geoeconomic gate, which returns a primary_country NAME chosen from this list.
_NAME_TO_FIPS: dict[str, str] = {
    "bahrain": "BA", "iran": "IR", "iraq": "IZ", "israel": "IS", "jordan": "JO",
    "kuwait": "KU", "lebanon": "LE", "oman": "MU", "qatar": "QA",
    "saudi arabia": "SA", "syria": "SY", "united arab emirates": "AE", "uae": "AE",
    "yemen": "YM", "egypt": "EG", "turkey": "TU", "türkiye": "TU", "cyprus": "CY",
    "west bank": "WE", "palestine": "WE", "gaza": "GZ", "gaza strip": "GZ",
    "libya": "LY", "tunisia": "TS", "algeria": "AG", "morocco": "MO",
    "brazil": "BR", "argentina": "AR", "mexico": "MX", "chile": "CI",
    "colombia": "CO", "peru": "PE", "venezuela": "VE", "ecuador": "EC",
    "bolivia": "BL", "paraguay": "PA", "uruguay": "UY", "guatemala": "GT",
    "cuba": "CU", "dominican republic": "DR", "honduras": "HO", "nicaragua": "NU",
    "costa rica": "CS", "panama": "PM", "el salvador": "ES",
}


def country_to_fips(name: str | None) -> str | None:
    """Resolve an in-scope country NAME (or alias) to its FIPS code; None otherwise."""
    if not name:
        return None
    return _NAME_TO_FIPS.get(name.strip().lower())


def top_per_bucket(items, country_of: Callable, score_of: Callable, n: int,
                   active: Iterable[str] = BUCKET_ORDER) -> dict[str, list]:
    """group_by_bucket, then sort each bucket by score_of desc and keep the top n."""
    grouped = group_by_bucket(items, country_of, active)
    return {b: sorted(v, key=score_of, reverse=True)[:n] for b, v in grouped.items()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_regions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add geoecon/regions.py tests/unit/test_regions.py
git commit -m "feat(regions): country-name->FIPS resolution + top_per_bucket cap"
```

---

### Task 3: gdelt — relaxed geoeconomic prefilter

**Files:**
- Modify: `geoecon/gdelt.py`
- Test: `tests/unit/test_discovery.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `GEOPOL_THEME_PREFIXES: tuple[str, ...]`; `prefilter_ok(themes, *, target_prefixes=TARGET_THEME_PREFIXES, geopol_prefixes=GEOPOL_THEME_PREFIXES) -> bool` — pass if `(≥1 economic theme)` OR `(≥1 geopolitics theme AND ≥1 economic theme)`. (Net: economic signal required; geopolitics widens nothing on its own but is documented for the gate's downstream use.) Keep `theme_intersection_ok`, `theme_salience` as-is.

Note: since the rule reduces to "≥1 economic theme", implement `prefilter_ok` as exactly that, but define `GEOPOL_THEME_PREFIXES` here (the gate stage imports it for prompt context / diagnostics). This raises recall vs. the old "≥2 economic + ≥1 core".

- [ ] **Step 1: Write the failing test** — add to `tests/unit/test_discovery.py`:

```python
def test_prefilter_ok_requires_one_economic_theme():
    # one economic theme is enough now (recall up; gate restores precision)
    assert gdelt.prefilter_ok(["ECON_INFLATION"]) is True
    assert gdelt.prefilter_ok(["WB_1428_TRADE"]) is True
    assert gdelt.prefilter_ok(["EPU_POLICY"]) is True
    # geopolitics-only, no economic theme -> still excluded
    assert gdelt.prefilter_ok(["MILITARY", "ARMEDCONFLICT"]) is False
    # pure non-economic -> excluded
    assert gdelt.prefilter_ok(["SPORTS", "WEATHER"]) is False
    assert gdelt.prefilter_ok([]) is False


def test_geopol_theme_prefixes_present():
    assert "SANCTIONS" in gdelt.GEOPOL_THEME_PREFIXES
    assert "EPU_CATS_GEOPOLITICAL" in gdelt.GEOPOL_THEME_PREFIXES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_discovery.py -k "prefilter or geopol" -v`
Expected: FAIL — `AttributeError: module 'geoecon.gdelt' has no attribute 'prefilter_ok'`

- [ ] **Step 3: Implement** — in `geoecon/gdelt.py`, after `CORE_THEME_PREFIXES`:

```python
# Geopolitics theme prefixes (GDELT GKG). Candidates; trimmed against live data
# per the diagnostic. The gate uses these as context; prefilter requires an
# economic theme (recall), gate does precision.
GEOPOL_THEME_PREFIXES: tuple[str, ...] = (
    "SANCTIONS", "MILITARY", "ARMEDCONFLICT", "BLOCKADE", "MARITIME", "BORDER",
    "ENERGY", "EPU_CATS_GEOPOLITICAL", "EPU_CATS_TRADE_POLICY",
    "EPU_CATS_NATIONAL_SECURITY", "EPU_CATS_SOVEREIGN_DEBT_CURRENCY_CRISES",
    "WB_2433_CONFLICT_AND_FRAGILITY", "CRISISLEX_", "TAX_TERROR",
)


def prefilter_ok(themes, *, target_prefixes=TARGET_THEME_PREFIXES,
                 geopol_prefixes=GEOPOL_THEME_PREFIXES) -> bool:
    """Cheap recall gate: keep if the article carries at least one economic theme.

    Precision (is it geoeconomic? which region?) is decided later by the LLM gate.
    geopol_prefixes is accepted for forward use/diagnostics but an economic theme
    is the binding requirement.
    """
    return any(any(t.startswith(p) for p in target_prefixes) for t in themes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_discovery.py -v`
Expected: PASS (existing theme tests still pass — `theme_intersection_ok` is unchanged)

- [ ] **Step 5: Commit**

```bash
git add geoecon/gdelt.py tests/unit/test_discovery.py
git commit -m "feat(gdelt): relaxed geoeconomic prefilter + geopolitics theme set"
```

---

### Task 4: Migration 003 + schema.sql + model gate fields

**Files:**
- Create: `geoecon/state/migrations/003_geoecon_gate.sql`
- Modify: `schema.sql`, `geoecon/state/models.py`
- Test: `tests/unit/test_repo.py`

**Interfaces:**
- Produces: columns `articles.gate_geoeconomic BOOLEAN`, `articles.gate_score REAL`, `articles.gate_country TEXT`, `articles.gate_reason TEXT`, `clusters.gate_score REAL`; `stage_runs` CHECK gains `'gate'`. Model fields `Article.gate_geoeconomic/gate_score/gate_country/gate_reason` (trailing optionals), `Cluster.gate_score` (trailing optional).

- [ ] **Step 1: Write the migration** — `geoecon/state/migrations/003_geoecon_gate.sql`:

```sql
-- Geoeconomic gate: per-article relevance verdict + per-cluster event score.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS gate_geoeconomic BOOLEAN;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS gate_score       REAL;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS gate_country     TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS gate_reason      TEXT;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS gate_score       REAL;

ALTER TABLE stage_runs DROP CONSTRAINT IF EXISTS stage_runs_stage_name_check;
ALTER TABLE stage_runs ADD CONSTRAINT stage_runs_stage_name_check
    CHECK (stage_name IN
        ('discovery','extraction','rss','gate','cluster','synthesis','summary','render','send'));
```

- [ ] **Step 2: Sync `schema.sql`**
- In `CREATE TABLE articles (...)` add (after `country_fips TEXT,`): `    gate_geoeconomic BOOLEAN,`, `    gate_score      REAL,`, `    gate_country    TEXT,`, `    gate_reason     TEXT,`.
- In `CREATE TABLE clusters (...)` add (after `country_fips ... ,`): `    gate_score          REAL,`.
- In the `stage_runs` CHECK, change the list to include `'gate'`: `('discovery','extraction','rss','gate','cluster','synthesis','summary','render','send')`.

- [ ] **Step 3: Add model fields** — in `geoecon/state/models.py`:

`Article` dataclass — append after `country_fips: str | None = None`:
```python
    gate_geoeconomic: bool | None = None
    gate_score: float | None = None
    gate_country: str | None = None
    gate_reason: str | None = None
```
`Cluster` dataclass — append after `country_fips: str | None = None`:
```python
    gate_score: float | None = None
```

- [ ] **Step 4: Write the failing DB-gated test** — add to `tests/unit/test_repo.py`:

```python
@requires_db
def test_migration_003_gate_columns_and_stage(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    repo.conn.execute(
        "INSERT INTO articles(run_id, source_url, url_hash, region, country_fips, "
        "gate_geoeconomic, gate_score, gate_country, gate_reason) "
        "VALUES (%s,'u','h','MENA','EG', true, 0.8, 'Egypt', 'why')", (rid,))
    row = repo.conn.execute(
        "SELECT gate_geoeconomic, gate_score, gate_country FROM articles WHERE run_id=%s",
        (rid,)).fetchone()
    assert row[0] is True and abs(row[1] - 0.8) < 1e-6 and row[2] == "Egypt"
    repo.conn.execute(
        "INSERT INTO clusters(run_id, event_fingerprint, region, article_count, gate_score) "
        "VALUES (%s,'fp','MENA',1,0.9)", (rid,))
    assert repo.conn.execute(
        "SELECT gate_score FROM clusters WHERE run_id=%s", (rid,)).fetchone()[0] is not None
    repo.start_stage(rid, "gate")  # must not raise (CHECK includes 'gate')
    assert "gate" not in repo.completed_stages(rid)
```

- [ ] **Step 5: Run test to verify it fails, then passes**

Run (DB up): `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres .venv/bin/python -m pytest tests/unit/test_repo.py -k migration_003 -v`
Expected: FAIL before the migration/model exist (UndefinedColumn / CHECK violation), PASS after Steps 1–3.

- [ ] **Step 6: Full suite + commit**

```bash
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres .venv/bin/python -m pytest tests/unit -q
git add geoecon/state/migrations/003_geoecon_gate.sql schema.sql geoecon/state/models.py tests/unit/test_repo.py
git commit -m "feat(schema): migration 003 — gate verdict columns + 'gate' stage"
```

---

### Task 5: repo — extraction title + gate persistence + articles_for_run gate fields

**Files:**
- Modify: `geoecon/state/repo.py`
- Test: `tests/unit/test_repo.py`

**Interfaces:**
- Consumes: gate columns (Task 4), `models.Article`.
- Produces:
  - `update_extraction(article_id, body, fetch_status, source_lang=None, fetch_error=None, title=None)` — sets `title=COALESCE(%s, title)`.
  - `articles_for_run` SELECT includes `title` (already), and appends `gate_geoeconomic, gate_score, gate_country, gate_reason`; `Article(*r)` maps them.
  - `upsert_gate(article_id, *, geoeconomic, score, country, reason, country_fips)` — writes the four gate fields AND overwrites `country_fips`.

- [ ] **Step 1: Write the failing test** — add to `tests/unit/test_repo.py`:

```python
@requires_db
def test_update_extraction_sets_title_and_upsert_gate(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    aid = repo.insert_article(rid, source_url="u", url_hash="h", region="MENA",
                              country_fips="EG")
    repo.update_extraction(aid, "body text here", "extracted", title="A Real Headline")
    arts = {a.article_id: a for a in repo.articles_for_run(rid)}
    assert arts[aid].title == "A Real Headline"
    assert arts[aid].body == "body text here"
    # gate overwrites country_fips + records verdict
    repo.upsert_gate(aid, geoeconomic=True, score=0.77, country="Egypt",
                     reason="econ x geopolitics", country_fips="EG")
    a = {x.article_id: x for x in repo.articles_for_run(rid)}[aid]
    assert a.gate_geoeconomic is True and abs(a.gate_score - 0.77) < 1e-6
    assert a.gate_country == "Egypt" and a.country_fips == "EG"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TEST_DATABASE_URL=... .venv/bin/python -m pytest tests/unit/test_repo.py -k update_extraction_sets_title -v`
Expected: FAIL — `update_extraction() got an unexpected keyword argument 'title'`

- [ ] **Step 3: Implement** — in `geoecon/state/repo.py`:

Replace `update_extraction`:
```python
    def update_extraction(self, article_id, body, fetch_status, source_lang=None,
                          fetch_error=None, title=None):
        self.conn.execute(
            "UPDATE articles SET body=%s, fetch_status=%s, "
            "source_lang=COALESCE(%s, source_lang), fetch_error=%s, "
            "title=COALESCE(%s, title) WHERE article_id=%s",
            (body, fetch_status, source_lang, fetch_error, title, article_id))
```

Update `articles_for_run` SELECT + construction (append the four gate columns last, matching the new trailing `Article` fields):
```python
    def articles_for_run(self, run_id, fetch_status=None) -> list[Article]:
        q = ("SELECT article_id, source_url, url_hash, domain, region, gkg_themes, gkg_tone, "
             "num_mentions, source_lang, title, body, fetch_status, country_fips, "
             "gate_geoeconomic, gate_score, gate_country, gate_reason "
             "FROM articles WHERE run_id=%s")
        params: tuple = (run_id,)
        if fetch_status:
            q += " AND fetch_status=%s"; params += (fetch_status,)
        return [Article(*r) for r in self.conn.execute(q + " ORDER BY article_id", params).fetchall()]
```

Add `upsert_gate` (after `update_extraction`):
```python
    def upsert_gate(self, article_id, *, geoeconomic, score, country, reason, country_fips):
        self.conn.execute(
            "UPDATE articles SET gate_geoeconomic=%s, gate_score=%s, gate_country=%s, "
            "gate_reason=%s, country_fips=%s WHERE article_id=%s",
            (geoeconomic, score, country, reason, country_fips, article_id))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TEST_DATABASE_URL=... .venv/bin/python -m pytest tests/unit/test_repo.py -k "update_extraction_sets_title or records_for_buckets or migration" -v`
Expected: PASS (existing `articles_for_run`-dependent tests still pass — new columns are trailing).

- [ ] **Step 5: Commit**

```bash
git add geoecon/state/repo.py tests/unit/test_repo.py
git commit -m "feat(repo): extraction title capture + upsert_gate + gate fields in articles_for_run"
```

---

### Task 6: extraction — capture the article title

**Files:**
- Modify: `geoecon/stages/extract.py`
- Test: `tests/unit/test_extract.py`

**Interfaces:**
- Consumes: `repo.update_extraction(..., title=...)` (Task 5).
- Produces: `extract_one(url, *, fetcher) -> (body|None, title|None, status, lang|None)` — captures the title from trafilatura metadata even for `headline_only`; `run` passes `title` to `update_extraction`.

- [ ] **Step 1: Write the failing test** — replace the body-only assertions in `tests/unit/test_extract.py` and add a title test. Use this (keep any unrelated existing tests):

```python
from geoecon.stages import extract

_HTML = ("<html><head><title>Saudi non-oil exports rise</title></head><body>"
         + "<article><h1>Saudi non-oil exports rise</h1><p>"
         + ("Riyadh reported a sharp rise in non-oil exports this quarter. " * 12)
         + "</p></article></body></html>")


def test_extract_one_returns_body_title_status():
    body, title, status, lang = extract.extract_one("http://x", fetcher=lambda u: _HTML)
    assert status == "extracted"
    assert body and len(body) >= 200
    assert title == "Saudi non-oil exports rise"


def test_extract_one_failed_when_fetch_raises():
    def boom(u): raise RuntimeError("net")
    body, title, status, lang = extract.extract_one("http://x", fetcher=boom)
    assert status == "failed" and body is None


def test_run_passes_title_to_repo():
    captured = {}
    class FakeRepo:
        def articles_for_run(self, rid, fetch_status=None):
            from geoecon.state.models import Article
            return [Article(1, "http://x", "h", "d", "MENA", [], None, None, None,
                            None, None, "pending")]
        def update_extraction(self, aid, body, status, source_lang=None,
                              fetch_error=None, title=None):
            captured.update(aid=aid, status=status, title=title)
    out = extract.run(object(), FakeRepo(), 1, fetcher=lambda u: _HTML)
    assert out["extracted"] == 1
    assert captured["title"] == "Saudi non-oil exports rise"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_extract.py -v`
Expected: FAIL — `extract_one(...)` returns 3 values, not 4 (ValueError on unpack).

- [ ] **Step 3: Implement** — rewrite `geoecon/stages/extract.py`:

```python
from __future__ import annotations
import json, logging
import trafilatura
from ..config import Config

log = logging.getLogger("geoecon.extract")
MIN_CHARS = 200  # below this, treat as headline-only (body dropped; title kept if any)


def _default_fetcher(url: str) -> str | None:
    return trafilatura.fetch_url(url)


def extract_one(url, *, fetcher=_default_fetcher):
    """Return (body|None, title|None, status, lang|None).

    Title is captured from trafilatura metadata when available, even when the
    body is too short (headline_only) — the gate uses it.
    """
    try:
        downloaded = fetcher(url)
    except Exception as e:
        log.warning("fetch failed %s: %s", url, e)
        return None, None, "failed", None
    if not downloaded:
        return None, None, "headline_only", None
    title = None
    text = None
    result = trafilatura.extract(downloaded, include_comments=False,
                                 favor_recall=True, output_format="json",
                                 with_metadata=True)
    if result:
        try:
            data = json.loads(result)
            title = data.get("title")
            text = data.get("text")
        except (json.JSONDecodeError, AttributeError):
            pass
    if not text or len(text) < MIN_CHARS:
        return None, title, "headline_only", None
    return text, title, "extracted", None


def run(cfg: Config, repo, run_id: int, *, fetcher=_default_fetcher) -> dict:
    counts = {"extracted": 0, "headline_only": 0, "failed": 0}
    for art in repo.articles_for_run(run_id, fetch_status="pending"):
        body, title, status, lang = extract_one(art.source_url, fetcher=fetcher)
        repo.update_extraction(art.article_id, body, status, source_lang=lang,
                               fetch_error=None if status != "failed" else "extraction failed",
                               title=title)
        counts[status] += 1
    log.info("extraction: %s", counts)
    return counts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_extract.py -v`
Expected: PASS. (If `data.get("text")` field name differs in the installed trafilatura, the first test will show it — trafilatura's JSON output uses `"text"` and `"title"`; do not change unless the test proves otherwise.)

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/extract.py tests/unit/test_extract.py
git commit -m "feat(extract): capture article title from trafilatura metadata"
```

---

### Task 7: discovery — relaxed prefilter, per-bucket ceiling, no global cap

**Files:**
- Modify: `geoecon/stages/discovery.py`
- Test: `tests/unit/test_discovery.py`

**Interfaces:**
- Consumes: `gdelt.prefilter_ok` (Task 3), `regions.buckets_for_country`, `cfg.buckets`, `cfg.discovery_ceiling`.
- Produces: discovery keeps via `prefilter_ok` + bucket membership; caps **per bucket** at `cfg.discovery_ceiling` (ranked by `theme_salience`) instead of one global 150; still persists `country_fips`.

- [ ] **Step 1: Write the failing test** — add to `tests/unit/test_discovery.py`:

```python
def test_discovery_caps_per_bucket(monkeypatch):
    # 3 distinct SA (middle_east) lines; ceiling per bucket = 2 -> keep 2
    lines = [_line("s1"), _line("s2"), _line("s3")]
    captured = []
    class FakeRepo:
        def insert_article(self, run_id, **kw):
            captured.append(kw); return len(captured)
    from geoecon.stages import discovery
    from geoecon.config import Config
    cfg = Config.from_env({
        "DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
        "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x",
        "DISCOVERY_CEILING": "2",
    })
    metrics = discovery.run(cfg, FakeRepo(), run_id=1, fetch=lambda ws, we: lines)
    assert metrics["discovered"] == 2  # capped at 2 for the middle_east bucket
    assert all(c["country_fips"] == "SA" for c in captured)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_discovery.py -k caps_per_bucket -v`
Expected: FAIL — discovered == 3 (global cap behavior) or KeyError.

- [ ] **Step 3: Implement** — in `geoecon/stages/discovery.py`, replace the filter + cap section of `run`:

Change the keep test to use `prefilter_ok`:
```python
        if not gdelt.prefilter_ok(rec.themes):
            continue
        region = gdelt.region_for_country(rec.country)
        if not regions.buckets_for_country(rec.country, cfg.buckets):
            continue
```

Replace the global sort+cap (`kept.sort(...)`, `ceiling = ...`, `kept = kept[:ceiling]`) with a **per-bucket** cap:
```python
    # Per-bucket cap by salience proxy; LLM gate makes the final selection.
    kept.sort(key=lambda x: gdelt.theme_salience(x[0].themes), reverse=True)
    ceiling = cfg.limit or cfg.discovery_ceiling
    per_bucket: dict[str, int] = {}
    capped: list[tuple] = []
    for rec, region, h in kept:
        bs = regions.buckets_for_country(rec.country, cfg.buckets)
        if any(per_bucket.get(b, 0) < ceiling for b in bs):
            for b in bs:
                per_bucket[b] = per_bucket.get(b, 0) + 1
            capped.append((rec, region, h))
    kept = capped
```

(The `insert_article(..., country_fips=rec.country, ...)` loop and metrics stay as-is.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_discovery.py -v`
Expected: PASS (the existing `test_discovery_run_inserts_and_caps` and `test_discovery_persists_country_and_keeps_bucketed` still pass — SA → middle_east, under the default ceiling).

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/discovery.py tests/unit/test_discovery.py
git commit -m "feat(discovery): relaxed prefilter + per-bucket ceiling (drop global cap)"
```

---

### Task 8: gate — LLM geoeconomic relevance + region stage

**Files:**
- Create: `geoecon/stages/gate.py`, `geoecon/prompts/geoecon_gate.txt`
- Test: `tests/unit/test_gate.py`

**Interfaces:**
- Consumes: `regions.country_to_fips`, `regions.buckets_for_country`, `cfg.gate_model/gate_batch_size/buckets`, `repo.articles_for_run`, `repo.upsert_gate`, LLM client (`.complete`, `.ok()`, `.final_answer`).
- Produces:
  - `build_gate_messages(prompt, batch) -> list[dict]`
  - `parse_verdicts(text) -> list | None`
  - `run(cfg, repo, run_id, client, *, prompt=None) -> dict` returning `{gate_total, gate_kept, gate_dropped, gate_unparsed}`. Per article: writes `upsert_gate(...)`. Keep iff `geoeconomic and country_to_fips(primary_country)` resolves to ≥1 active bucket; on keep, `country_fips` is overwritten with the resolved FIPS; on drop, `country_fips=None`. Fail-open (kept, score 0, reason `gate_unparsed`) when a batch is unparseable after one retry.

- [ ] **Step 1: Write the prompt** — `geoecon/prompts/geoecon_gate.txt`:

```
You filter a geoeconomic news desk. For EACH numbered article, decide whether it is GEOECONOMIC and which in-scope country it primarily concerns.

GEOECONOMIC = economic news affected by or affecting the geopolitical scene.
KEEP:
- economy x geopolitics: sanctions, trade wars/disputes, energy security, capital flows driven by political risk, conflict or diplomacy with economic stakes, currency moves tied to geopolitics, sovereign-wealth / strategic-industry / resource decisions, de-dollarization, maritime/trade chokepoints;
- nationally significant economic policy or structural moves: IMF programs, major currency/budget/reserves shifts, large foreign direct investment, diversification strategies (e.g. Vision 2030), capital-market reforms.
DROP:
- generic markets/stocks/crypto with no geopolitical dimension; routine corporate earnings; a standalone macro print (a lone CPI or rate number) with no geopolitical link; consumer/lifestyle; anything whose economic substance is foreign to the in-scope regions.

Choose primary_country from EXACTLY this list (else "other"):
Middle East: Bahrain, Iran, Iraq, Israel, Jordan, Kuwait, Lebanon, Oman, Qatar, Saudi Arabia, Syria, United Arab Emirates, Yemen, Egypt, Turkey, Cyprus, West Bank, Gaza.
North Africa: Egypt, Libya, Tunisia, Algeria, Morocco.
Latin America: Brazil, Argentina, Mexico, Chile, Colombia, Peru, Venezuela, Ecuador, Bolivia, Paraguay, Uruguay, Guatemala, Cuba, Dominican Republic, Honduras, Nicaragua, Costa Rica, Panama, El Salvador.
If the economic subject is some other country (United States, China, India, Philippines, Sri Lanka, ...) or no clear in-scope country, use "other".

Output STRICT JSON ONLY — an array with one object per input index, no prose:
[{"i": <index>, "geoeconomic": true|false, "primary_country": "<name or other>", "score": <0.0-1.0 importance>, "reason": "<= 15 words"}]
Return one object for EVERY index you were given.
```

- [ ] **Step 2: Write the failing test** — `tests/unit/test_gate.py`:

```python
from geoecon.stages import gate
from geoecon.state.models import Article


def _art(aid, country, title, body="econ body"):
    return Article(aid, f"http://x/{aid}", f"h{aid}", "d", "MENA", ["ECON_X"], None,
                   None, None, title, body, "extracted", country)


def _client(answer):
    class Resp:
        def __init__(self, fa): self.final_answer = fa
        def ok(self): return True
    class C:
        def __init__(self): self.calls = []
        def complete(self, model, messages, **kw):
            self.calls.append((model, messages, kw)); return Resp(answer)
    return C()


class _Repo:
    def __init__(self, arts): self._arts = arts; self.gates = {}
    def articles_for_run(self, rid, fetch_status=None): return self._arts
    def upsert_gate(self, aid, *, geoeconomic, score, country, reason, country_fips):
        self.gates[aid] = dict(geoeconomic=geoeconomic, score=score, country=country,
                               reason=reason, country_fips=country_fips)


_CFG = type("C", (), {"gate_model": "m", "gate_batch_size": 10,
                      "buckets": ("middle_east", "north_africa", "latam")})()


def test_gate_keeps_resolves_region_and_drops_other():
    arts = [_art(1, "SA", "Saudi sovereign fund shifts to Asia"),
            _art(2, "IR", "US Fed holds rates"),     # gate says other
            _art(3, "EG", "Egypt pound rallies on US-Iran deal")]
    answer = ('[{"i":0,"geoeconomic":true,"primary_country":"Saudi Arabia","score":0.9,"reason":"swf geopolitics"},'
              '{"i":1,"geoeconomic":false,"primary_country":"other","score":0.1,"reason":"us fed"},'
              '{"i":2,"geoeconomic":true,"primary_country":"Egypt","score":0.8,"reason":"oil geopolitics"}]')
    repo = _Repo(arts)
    counts = gate.run(_CFG, repo, 1, _client(answer), prompt="SYS")
    assert counts == {"gate_total": 3, "gate_kept": 2, "gate_dropped": 1, "gate_unparsed": 0}
    assert repo.gates[1]["country_fips"] == "SA" and repo.gates[1]["geoeconomic"] is True
    assert repo.gates[2]["geoeconomic"] is False and repo.gates[2]["country_fips"] is None
    assert repo.gates[3]["country_fips"] == "EG"  # Egypt resolves; dual-bucket handled downstream
    # determinism kwargs forwarded
    assert _client(answer).complete  # sanity


def test_gate_fail_open_on_unparseable():
    arts = [_art(1, "SA", "x")]
    repo = _Repo(arts)
    counts = gate.run(_CFG, repo, 1, _client("not json"), prompt="SYS")
    assert counts["gate_unparsed"] == 1 and counts["gate_kept"] == 1
    assert repo.gates[1]["geoeconomic"] is True and repo.gates[1]["reason"] == "gate_unparsed"


def test_build_messages_numbers_items():
    msgs = gate.build_gate_messages("SYS", [_art(1, "SA", "T1"), _art(2, "EG", "T2")])
    assert msgs[0]["content"] == "SYS"
    assert "[0]" in msgs[1]["content"] and "[1]" in msgs[1]["content"]
    assert "T1" in msgs[1]["content"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'geoecon.stages.gate'`

- [ ] **Step 4: Implement** — `geoecon/stages/gate.py`:

```python
"""Geoeconomic gate — LLM relevance + primary-country, batched."""
from __future__ import annotations

import json
import logging
import re

from .. import regions

log = logging.getLogger("geoecon.gate")

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.S)


def _lead(art, n: int = 400) -> str:
    return (art.body or "")[:n]


def build_gate_messages(prompt: str, batch) -> list[dict]:
    parts = []
    for i, a in enumerate(batch):
        themes = ", ".join((a.gkg_themes or [])[:12])
        parts.append(
            f"[{i}] COUNTRY_HINT={a.country_fips or '?'} | TITLE: {a.title or '(no title)'}\n"
            f"THEMES: {themes}\nLEAD: {_lead(a)}"
        )
    return [{"role": "system", "content": prompt},
            {"role": "user", "content": "\n\n".join(parts)}]


def parse_verdicts(text: str):
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_ARRAY_RE.search(text)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, list) else None


def run(cfg, repo, run_id, client, *, prompt: str | None = None) -> dict:
    if prompt is None:
        from importlib.resources import files
        prompt = files("geoecon.prompts").joinpath("geoecon_gate.txt").read_text("utf-8")

    arts = [a for a in repo.articles_for_run(run_id) if a.fetch_status != "failed"]
    counts = {"gate_total": 0, "gate_kept": 0, "gate_dropped": 0, "gate_unparsed": 0}
    bs = max(1, cfg.gate_batch_size)

    for start in range(0, len(arts), bs):
        batch = arts[start:start + bs]
        messages = build_gate_messages(prompt, batch)
        resp = client.complete(cfg.gate_model, messages,
                               temperature=0, seed=12345, max_tokens=4096)
        verdicts = parse_verdicts(getattr(resp, "final_answer", "") or "") if resp.ok() else None
        if verdicts is None:
            resp2 = client.complete(cfg.gate_model, messages,
                                    temperature=0, seed=12345, max_tokens=4096)
            verdicts = parse_verdicts(getattr(resp2, "final_answer", "") or "") if resp2.ok() else None

        by_i = {}
        if verdicts is not None:
            for v in verdicts:
                if isinstance(v, dict) and isinstance(v.get("i"), int):
                    by_i[v["i"]] = v

        for idx, art in enumerate(batch):
            counts["gate_total"] += 1
            v = by_i.get(idx)
            if v is None:
                repo.upsert_gate(art.article_id, geoeconomic=True, score=0.0,
                                 country=art.gate_country, reason="gate_unparsed",
                                 country_fips=art.country_fips)
                counts["gate_unparsed"] += 1
                counts["gate_kept"] += 1
                continue
            geo = bool(v.get("geoeconomic"))
            pc = (v.get("primary_country") or "").strip()
            fips = regions.country_to_fips(pc)
            try:
                score = float(v.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            reason = (v.get("reason") or "")[:200]
            keep = bool(geo and fips and regions.buckets_for_country(fips, cfg.buckets))
            repo.upsert_gate(art.article_id, geoeconomic=keep, score=score,
                             country=pc or None, reason=reason,
                             country_fips=fips if keep else None)
            counts["gate_kept" if keep else "gate_dropped"] += 1

    log.info("gate: %s", counts)
    return counts
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_gate.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add geoecon/stages/gate.py geoecon/prompts/geoecon_gate.txt tests/unit/test_gate.py
git commit -m "feat(gate): batched LLM geoeconomic relevance + region gate"
```

---

### Task 9: cluster — filter gated + lexical near-dup merge

**Files:**
- Modify: `geoecon/stages/cluster.py`, `geoecon/state/repo.py` (`insert_cluster` gains `gate_score`)
- Test: `tests/unit/test_cluster.py`

**Interfaces:**
- Consumes: `Article.gate_geoeconomic/gate_score/country_fips/title`, `regions.buckets_for_country`, `cfg.buckets/dedup_jaccard`.
- Produces:
  - `repo.insert_cluster(run_id, event_fingerprint, region, languages, article_count, framing_divergent=False, country_fips=None, gate_score=None)`.
  - `cluster.run`: only `gate_geoeconomic` + bucketed + non-failed articles; lexical near-dup merge (same `country_fips`, title-token Jaccard ≥ `cfg.dedup_jaccard`) → one multi-article cluster per event; `cluster.country_fips`/`gate_score` from the highest-score member. Helpers `title_tokens`, `jaccard`.

- [ ] **Step 1: Write the failing test** — replace `tests/unit/test_cluster.py` with (keeps the country_fips behavior, adds dedup + gating):

```python
import re
from geoecon.stages import cluster as cluster_stage
from geoecon.state.models import Article


def _art(aid, title, country="SA", geo=True, score=0.5):
    return Article(aid, f"http://x/{aid}", f"h{aid}", "d", "MENA", [], None, None,
                   None, title, "body", "extracted", country,
                   gate_geoeconomic=geo, gate_score=score)


class FakeRepo:
    def __init__(self, arts):
        self._arts = arts; self.clusters = []; self.memberships = []
    def articles_for_run(self, rid, fetch_status=None): return self._arts
    def insert_cluster(self, run_id, fp, region, langs, count,
                       framing_divergent=False, country_fips=None, gate_score=None):
        cid = len(self.clusters) + 1
        self.clusters.append(dict(cid=cid, count=count, country_fips=country_fips,
                                  gate_score=gate_score)); return cid
    def add_membership(self, cid, aid, frame_label=None):
        self.memberships.append((cid, aid))


_CFG = type("C", (), {"buckets": ("middle_east", "north_africa", "latam"),
                      "dedup_jaccard": 0.6})()


def test_jaccard_and_tokens():
    a = cluster_stage.title_tokens("Saudi non-oil exports rise 10.7 percent")
    b = cluster_stage.title_tokens("Saudi non-oil exports rise 10.7%, driven by chemicals")
    assert cluster_stage.jaccard(a, b) >= 0.6


def test_cluster_merges_near_dups_and_filters_gate():
    arts = [
        _art(1, "Saudi non-oil exports rise 10.7 percent", score=0.9),
        _art(2, "Saudi non-oil exports rise 10.7%, driven by chemicals", score=0.7),
        _art(3, "Egypt pound rallies below 50 per dollar", country="EG", score=0.8),
        _art(4, "Bitcoin ETFs shed billions", geo=False),  # dropped by gate
    ]
    repo = FakeRepo(arts)
    out = cluster_stage.run(_CFG, repo, 1)
    # art 4 excluded (geo False); arts 1&2 merge -> one cluster; art 3 own cluster
    assert out["clusters"] == 2
    sizes = sorted(c["count"] for c in repo.clusters)
    assert sizes == [1, 2]
    merged = [c for c in repo.clusters if c["count"] == 2][0]
    assert merged["country_fips"] == "SA" and abs(merged["gate_score"] - 0.9) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_cluster.py -v`
Expected: FAIL — `title_tokens`/`jaccard` not defined; gate filtering/merge absent.

- [ ] **Step 3: Implement** — rewrite `geoecon/stages/cluster.py`:

```python
from __future__ import annotations
import hashlib, logging, re
from .. import regions

log = logging.getLogger("geoecon.cluster")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def fingerprint(title: str | None, canonical_url: str | None) -> str:
    basis = _norm(title) if title else (canonical_url or "")
    return "p1:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()


def title_tokens(title: str | None) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(t) >= 3}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def run(cfg, repo, run_id: int) -> dict:
    arts = [a for a in repo.articles_for_run(run_id)
            if a.gate_geoeconomic and a.fetch_status != "failed"
            and regions.buckets_for_country(a.country_fips, cfg.buckets)]

    # Union-find near-dup merge: same country + title-token Jaccard >= threshold.
    parent = list(range(len(arts)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    def union(i, j):
        parent[find(i)] = find(j)

    toks = [title_tokens(a.title) for a in arts]
    for i in range(len(arts)):
        for j in range(i + 1, len(arts)):
            if (arts[i].country_fips == arts[j].country_fips and toks[i] and toks[j]
                    and jaccard(toks[i], toks[j]) >= cfg.dedup_jaccard):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(arts)):
        groups.setdefault(find(i), []).append(i)

    n = 0
    for members in groups.values():
        member_arts = [arts[m] for m in members]
        primary = max(member_arts, key=lambda a: a.gate_score or 0.0)
        langs = sorted({a.source_lang for a in member_arts if a.source_lang})
        fp = fingerprint(primary.title, primary.source_url)
        cid = repo.insert_cluster(run_id, fp, primary.region, langs, len(member_arts),
                                  country_fips=primary.country_fips,
                                  gate_score=primary.gate_score)
        for a in member_arts:
            repo.add_membership(cid, a.article_id)
        n += 1

    log.info("cluster: %d clusters from %d gated articles", n, len(arts))
    return {"clusters": n}
```

In `geoecon/state/repo.py`, update `insert_cluster` to persist `gate_score`:
```python
    def insert_cluster(self, run_id, event_fingerprint, region, languages, article_count,
                       framing_divergent=False, country_fips=None, gate_score=None) -> int:
        return self.conn.execute(
            """INSERT INTO clusters(run_id, event_fingerprint, region, country_fips,
                 languages, article_count, framing_divergent, gate_score)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING cluster_id""",
            (run_id, event_fingerprint, region, country_fips, languages, article_count,
             framing_divergent, gate_score)).fetchone()[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres .venv/bin/python -m pytest tests/unit/test_cluster.py tests/unit/test_repo.py -v`
Expected: PASS (cluster unit tests + repo DB tests; existing `insert_cluster` callers pass `gate_score` as a trailing default).

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/cluster.py geoecon/state/repo.py tests/unit/test_cluster.py
git commit -m "feat(cluster): gate-filtered lexical near-dup merge into multi-article clusters"
```

---

### Task 10: render + summary — per-region cap + key-figures fix

**Files:**
- Modify: `geoecon/stages/render.py`, `geoecon/stages/summary.py`, `geoecon/state/repo.py` (`records_for_buckets` returns `gate_score`), `geoecon/render/templates/email.html.j2`, `geoecon/render/templates/archive.html.j2`
- Test: `tests/unit/test_render.py`, `tests/unit/test_summary.py`

**Interfaces:**
- Consumes: `regions.top_per_bucket`, `cfg.region_cap/buckets`.
- Produces:
  - `repo.records_for_buckets(run_id)` returns 4-tuples `(SynthesisRecord, country_fips, sources, gate_score)`.
  - `render.format_figures(key_figures) -> str` (spaced, de-duped, no `gloss_en`); each record gets a transient `figures_text`.
  - `summary.run` and `render.run` both select per-region top-`cfg.region_cap` via `top_per_bucket(score_of=lambda t: t[3] or 0.0)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_render.py`:
```python
def test_format_figures_spacing_and_dedup():
    from geoecon.stages import render
    figs = [{"value": "$318 billion", "unit": "USD"},
            {"value": "$318 billion", "unit": "USD"},   # duplicate
            {"value": "1.10%", "unit": "%"},            # unit already a suffix
            {"value": "52,621.84", "unit": "points"}]
    out = render.format_figures(figs)
    assert "$318 billion USD" in out
    assert out.count("$318 billion USD") == 1          # de-duped
    assert "1.10%" in out and "%%" not in out
    assert "52,621.84 points" in out
```

Add to `tests/unit/test_summary.py` (the `_Repo` there returns `records_for_buckets`; update its tuples to 4-elements and assert capping):
```python
def test_summary_caps_per_region():
    # 3 latam records, region_cap 2 -> summary built from top 2 by score
    recs = [(_rec(i, f"H{i}"), "BR", [], s) for i, s in [(1, 0.9), (2, 0.8), (3, 0.1)]]
    class Repo:
        def __init__(self): self.saved = []
        def records_for_buckets(self, rid): return recs
        def upsert_region_summary(self, rid, bucket, lede, takeaways, model, count):
            self.saved.append((bucket, count))
    cfg = type("C", (), {"synthesis_model": "m", "buckets": ("middle_east","north_africa","latam"),
                         "region_cap": 2})()
    repo = Repo()
    summary.run(cfg, repo, 1, _client(GOOD), prompt="SYS {REGION}")
    latam = [s for s in repo.saved if s[0] == "latam"][0]
    assert latam[1] == 2  # capped to 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_render.py -k format_figures tests/unit/test_summary.py -k caps_per_region -v`
Expected: FAIL — `render.format_figures` missing; `_rec`/`_Repo` tuple arity / capping not yet present.

- [ ] **Step 3: Implement repo + format_figures + grouping**

In `geoecon/state/repo.py`, `records_for_buckets` — add `c.gate_score` to the SELECT and the tuple (4th element):
```python
               ... s.sources, s.is_headline_only, c.country_fips, c.gate_score, a.source_url, a.domain
```
Adjust the row-index mapping: `c.country_fips` is `r[13]`, `c.gate_score` is `r[14]`, `a.source_url` is `r[15]`, `a.domain` is `r[16]`. Build `by_id[rec_id] = [rec, r[13], [], r[14]]` and append sources from `r[15]/r[16]`; return `tuple(by_id[i])` (now 4-tuples `(record, country_fips, sources, gate_score)`).

In `geoecon/stages/render.py`, add the figures formatter and use `top_per_bucket`:
```python
def format_figures(key_figures) -> str:
    seen, out = set(), []
    for k in key_figures or []:
        val = str(k.get("value", "")).strip()
        if not val:
            continue
        unit = str(k.get("unit") or "").strip()
        disp = f"{val} {unit}" if unit and not val.lower().endswith(unit.lower()) else val
        if disp not in seen:
            seen.add(disp); out.append(disp)
    return " · ".join(out)
```
Update `render.run` grouping + per-record attachment:
```python
    from .. import regions
    grouped = regions.top_per_bucket(
        repo.records_for_buckets(run_id), country_of=lambda t: t[1],
        score_of=lambda t: t[3] or 0.0, n=cfg.region_cap, active=cfg.buckets)
    ...
    for bucket in cfg.buckets:
        items = grouped.get(bucket, [])
        if not items:
            continue
        records = []
        for rec, _country, sources, _score in items:
            rec.source_links = sources
            rec.figures_text = format_figures(rec.fields.get("key_figures"))
            records.append(rec)
        ...
```

In `geoecon/stages/summary.py`, replace `group_by_bucket(...)` with the capped selection:
```python
    grouped = regions.top_per_bucket(
        repo.records_for_buckets(run_id), country_of=lambda t: t[1],
        score_of=lambda t: t[3] or 0.0, n=cfg.region_cap, active=cfg.buckets)
```
(and `records = [t[0] for t in items]` as before.)

- [ ] **Step 4: Update templates** — in both `email.html.j2` and `archive.html.j2`, replace the inline `key_figures` loop with the precomputed text. Email:
```html
    {% if r.figures_text %}<div style="font-size:12px;color:#666;margin-top:6px;">{{ r.figures_text }}</div>{% endif %}
```
Archive:
```html
  {% if r.figures_text %}<p class="figs">{{ r.figures_text }}</p>{% endif %}
```
(Remove the old `{% for k in r.fields.key_figures %}...{% endfor %}` blocks. Leave summary block and source-link line unchanged.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres .venv/bin/python -m pytest tests/unit/test_render.py tests/unit/test_summary.py tests/unit/test_repo.py -v`
Expected: PASS (existing render/summary behavior intact; `records_for_buckets` DB tests updated to 4-tuples — also update the earlier `records_for_buckets` repo tests to unpack 4 values).

Note: the Task 4/5 `records_for_buckets` DB tests unpack 3 values — update those unpacks to `rec, country, sources, score` here.

- [ ] **Step 6: Commit**

```bash
git add geoecon/stages/render.py geoecon/stages/summary.py geoecon/state/repo.py geoecon/render/templates/email.html.j2 geoecon/render/templates/archive.html.j2 tests/unit/test_render.py tests/unit/test_summary.py tests/unit/test_repo.py
git commit -m "feat(render): per-region cap + clean key-figures formatting"
```

---

### Task 11: orchestrator — insert the gate stage

**Files:**
- Modify: `geoecon/orchestrator.py`
- Test: `tests/unit/test_orchestrator.py`

**Interfaces:**
- Consumes: `gate.run` (Task 8).
- Produces: stage order discovery → extraction → **gate** → cluster → synthesis → summary → render → send; `gate` receives `client`.

- [ ] **Step 1: Write the failing test** — add to `tests/unit/test_orchestrator.py`:

```python
def test_default_stages_include_gate_before_cluster():
    from geoecon import orchestrator
    names = [n for n, _ in orchestrator._default_stages(_cfg(), client=None)]
    assert names == ["discovery", "extraction", "gate", "cluster", "synthesis",
                     "summary", "render", "send"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -k gate_before_cluster -v`
Expected: FAIL — `gate` not in the stage list.

- [ ] **Step 3: Implement** — in `_default_stages`:
- Add `gate` to the import: `from .stages import discovery, extract, cluster, synth, gate, summary, render, send`.
- Add the closure: `def _gate(c, r, rid): return gate.run(c, r, rid, client)`.
- Insert `("gate", _gate)` between `("extraction", _extract)` and `("cluster", _cluster)`:
```python
    return [("discovery", _discovery), ("extraction", _extract), ("gate", _gate),
            ("cluster", _cluster), ("synthesis", _synth), ("summary", _summary),
            ("render", _render), ("send", _send)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_orchestrator.py -v`
Expected: PASS (existing orchestrator tests use explicit `stages=`, unaffected).

- [ ] **Step 5: Commit**

```bash
git add geoecon/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat(orchestrator): insert geoeconomic gate stage before cluster"
```

---

### Task 12: Diagnostic script + full-suite smoke + docs

**Files:**
- Create: `deploy/diagnostics/funnel_report.sql`
- Modify: `PIPELINE_BLUEPRINT.md`

**Interfaces:** none (operational).

- [ ] **Step 1: Write the funnel diagnostic** — `deploy/diagnostics/funnel_report.sql` (read-only; run for a given `:run_id`):

```sql
-- Per-bucket capture funnel for a run. Usage:
--   kubectl exec -i geoecon-db-1 -c postgres -- \
--     psql -U postgres -d geoeconomics -v run_id=<RID> -tA -f - < funnel_report.sql
\echo == discovered per coarse region ==
SELECT region, count(*) FROM articles WHERE run_id = :run_id GROUP BY region ORDER BY region;
\echo == gate verdicts ==
SELECT gate_geoeconomic, count(*) FROM articles WHERE run_id = :run_id GROUP BY gate_geoeconomic;
\echo == kept by resolved country ==
SELECT country_fips, count(*) FROM articles
  WHERE run_id = :run_id AND gate_geoeconomic GROUP BY country_fips ORDER BY 2 DESC;
\echo == top gate_reason among drops ==
SELECT gate_reason, count(*) FROM articles
  WHERE run_id = :run_id AND gate_geoeconomic = false GROUP BY gate_reason ORDER BY 2 DESC LIMIT 20;
\echo == clusters (post-dedup) per country ==
SELECT country_fips, count(*) FROM clusters WHERE run_id = :run_id GROUP BY country_fips ORDER BY 2 DESC;
\echo == ok records ==
SELECT count(*) FROM synthesis_records WHERE run_id = :run_id AND synthesis_quality_flag='ok';
```

- [ ] **Step 2: Full suite (with DB)**

Run: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres .venv/bin/python -m pytest tests/unit -q`
Expected: PASS, no skips.

- [ ] **Step 3: Import + stage-list smoke**

Run:
```bash
.venv/bin/python -c "from geoecon import orchestrator; from geoecon.config import Config; \
cfg=Config.from_env({'DB_DSN':'x','MAETCENTRUM_KEY':'k','SMTP_HOST':'h','SMTP_USER':'u','SMTP_PASSWORD':'p','FROM_ADDR':'f','RECIPIENTS':'r@x'}); \
print([n for n,_ in orchestrator._default_stages(cfg, client=None)])"
```
Expected: `['discovery', 'extraction', 'gate', 'cluster', 'synthesis', 'summary', 'render', 'send']`

- [ ] **Step 4: Docs note** — add a short section to `PIPELINE_BLUEPRINT.md`: the new `gate` stage (LLM relevance + region, Moderate rubric, fail-open), relaxed prefilter + per-bucket discovery ceiling, lexical near-dup merge → multi-article clusters, per-region cap (`region_cap`, default 12) at render, and the key-figures formatting fix. Note migration `003` auto-applies.

- [ ] **Step 5: Commit**

```bash
git add deploy/diagnostics/funnel_report.sql PIPELINE_BLUEPRINT.md
git commit -m "docs: capture-overhaul diagnostic + pipeline notes"
```

---

## Self-Review

**Spec coverage:**
- §5.1 relaxed prefilter → Task 3. ✓ §5.2 discovery per-bucket ceiling → Task 7. ✓
- §5.3 gate (batched, region resolution, fail-open) → Task 8 (+ prompt). ✓
- §5.4 cluster filter + lexical dedup → Task 9. ✓
- §5.5 per-region cap (summary + render share `top_per_bucket`) → Tasks 2, 10. ✓
- §5.6 key_figures fix → Task 10. ✓
- §6 migration 003 + schema sync → Task 4. ✓
- §7 config → Task 1. ✓
- §8 diagnostic → Task 12. ✓ §9 orchestrator → Task 11. ✓
- Enabler not in spec but required (titles are NULL today): extraction title capture → Task 6 (feeds gate §5.3 + dedup §5.4). ✓
- §10 testing → tests in every task.

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to" — full code in each step. The one judgment call (trafilatura JSON field names `title`/`text`) is named with a verification note in Task 6 Step 4.

**Type consistency:**
- `country_to_fips(name)->str|None` (T2) ↔ used in gate (T8). ✓
- `top_per_bucket(items, country_of, score_of, n, active)` (T2) ↔ called in summary + render (T10) with `score_of=lambda t: t[3] or 0.0`. ✓
- `records_for_buckets -> (record, country_fips, sources, gate_score)` 4-tuple (T10) ↔ consumed in summary/render (T10) and the DB tests updated to unpack 4 (T10 Step 5 note). ✓
- `upsert_gate(article_id, *, geoeconomic, score, country, reason, country_fips)` (T5) ↔ called in gate (T8). ✓
- `insert_cluster(..., gate_score=None)` (T9) ↔ called in cluster.run (T9). ✓
- `update_extraction(..., title=None)` (T5) ↔ called in extract.run (T6). ✓
- `extract_one -> (body, title, status, lang)` (T6) ↔ test unpacks 4 (T6). ✓
- `Article` trailing fields `gate_geoeconomic/gate_score/gate_country/gate_reason` (T4) ↔ `articles_for_run` SELECT order (T5) ↔ read in gate (T8) + cluster (T9). ✓
- `prefilter_ok(themes)` (T3) ↔ discovery (T7). ✓ `GEOPOL_THEME_PREFIXES` (T3) used in test (T3). ✓
- stage name `'gate'` in CHECK (T4) ↔ stage list (T11). ✓
