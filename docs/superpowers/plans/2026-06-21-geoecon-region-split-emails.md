# Three Regional Emails with Executive Summaries + Source Links — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single daily Geoeconomic Brief into three region-scoped emails (Middle East, North Africa, Latin America), each opening with an LLM-generated executive summary and rendering a verified source link on every story.

**Architecture:** A new region-bucket module classifies articles by FIPS country code (a country may map to multiple buckets — Egypt → both Middle East and North Africa). A migration persists the country code and makes newsletters per-region. A new `summary` pipeline stage generates one executive summary per non-empty region; render emits one newsletter per region; send dispatches each with a region-specific subject. Empty regions are skipped.

**Tech Stack:** Python 3.12, psycopg 3 (autocommit), Jinja2, an OpenAI-compatible LLM via the existing `LLMClient`, pytest.

## Global Constraints

- No new pip dependencies — use only what `pyproject.toml` already declares (psycopg, jinja2, requests; pytest for tests).
- Python 3.12; `from __future__ import annotations` at the top of every module (match existing files).
- snake_case; store all timestamps UTC; JSONB via `psycopg.types.json.Jsonb` for variable-shape payloads.
- GDELT GKG emits **FIPS 10-4** country codes, not ISO 3166 — all country sets use FIPS.
- LLM calls are deterministic: `temperature=0, seed=12345` (match `synth.py`).
- Executive summary is **strictly grounded** in the provided stories (no invented facts/figures); output is strict JSON `{"lede": str, "takeaways": [str]}`; ≤ ~180 words; 3–5 takeaways.
- Source links use the **verified `articles.source_url`** only — never the LLM `synthesis_records.sources` field.
- Bucket keys are exactly `"middle_east"`, `"north_africa"`, `"latam"`; display names `"Middle East"`, `"North Africa"`, `"Latin America"`; iteration order `("middle_east","north_africa","latam")`.
- Same recipient list for all three emails. Empty region ⇒ no email; all-empty day ⇒ no email at all.
- DB-backed repo tests are gated behind `@pytest.mark.requires_db` + `TEST_DATABASE_URL` (skip when unset), matching `tests/unit/test_repo.py`.

---

### Task 1: Region taxonomy module

**Files:**
- Create: `geoecon/regions.py`
- Test: `tests/unit/test_regions.py`

**Interfaces:**
- Produces:
  - `BUCKETS: dict[str, set[str]]` — bucket key → FIPS code set.
  - `BUCKET_DISPLAY: dict[str, str]` — bucket key → display name.
  - `BUCKET_ORDER: tuple[str, ...]` — `("middle_east","north_africa","latam")`.
  - `buckets_for_country(cc: str | None, active=BUCKET_ORDER) -> list[str]` — every active bucket containing `cc`, in `BUCKET_ORDER`; `[]` if uncovered.
  - `group_by_bucket(items, country_of, active=BUCKET_ORDER) -> dict[str, list]` — groups items by each item's country (via the `country_of` callable); every active bucket present (possibly empty); an item whose country is in multiple buckets appears in each.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_regions.py
from geoecon import regions


def test_egypt_in_both_middle_east_and_north_africa():
    bs = regions.buckets_for_country("EG")
    assert "middle_east" in bs and "north_africa" in bs


def test_new_middle_east_countries_present():
    for cc in ("IS", "TU", "CY"):  # Israel, Turkey, Cyprus (FIPS)
        assert "middle_east" in regions.buckets_for_country(cc)


def test_palestine_kept_sudan_dropped():
    assert "middle_east" in regions.buckets_for_country("WE")  # West Bank
    assert "middle_east" in regions.buckets_for_country("GZ")  # Gaza
    assert regions.buckets_for_country("SU") == []             # Sudan dropped


def test_latam_unchanged():
    assert regions.buckets_for_country("BR") == ["latam"]


def test_uncovered_country_is_empty():
    assert regions.buckets_for_country("US") == []
    assert regions.buckets_for_country(None) == []


def test_order_follows_bucket_order():
    assert regions.buckets_for_country("EG") == ["middle_east", "north_africa"]


def test_active_filter_restricts_buckets():
    assert regions.buckets_for_country("EG", active=("north_africa",)) == ["north_africa"]


def test_group_by_bucket_duplicates_overlap():
    items = [("a", "EG"), ("b", "BR"), ("c", "SU")]
    grouped = regions.group_by_bucket(items, country_of=lambda t: t[1])
    assert ("a", "EG") in grouped["middle_east"]
    assert ("a", "EG") in grouped["north_africa"]
    assert grouped["latam"] == [("b", "BR")]
    assert all(("c", "SU") not in v for v in grouped.values())  # Sudan dropped everywhere
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_regions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'geoecon.regions'`

- [ ] **Step 3: Write minimal implementation**

```python
# geoecon/regions.py
from __future__ import annotations

from typing import Callable, Iterable

# FIPS 10-4 country codes (GDELT GKG emits FIPS, not ISO). A country may belong
# to more than one bucket (Egypt -> middle_east AND north_africa).
BUCKETS: dict[str, set[str]] = {
    "middle_east": {
        "BA",  # Bahrain
        "IR",  # Iran
        "IZ",  # Iraq
        "IS",  # Israel
        "JO",  # Jordan
        "KU",  # Kuwait
        "LE",  # Lebanon
        "MU",  # Oman
        "QA",  # Qatar
        "SA",  # Saudi Arabia
        "SY",  # Syria
        "AE",  # United Arab Emirates
        "YM",  # Yemen
        "EG",  # Egypt (also north_africa)
        "TU",  # Turkey
        "CY",  # Cyprus
        "WE",  # West Bank
        "GZ",  # Gaza Strip
    },
    "north_africa": {
        "EG",  # Egypt (also middle_east)
        "LY",  # Libya
        "TS",  # Tunisia
        "AG",  # Algeria
        "MO",  # Morocco
    },
    "latam": {
        "BR", "AR", "MX", "CI", "CO", "PE", "VE", "EC", "BL",
        "PA", "UY", "GT", "CU", "DR", "HO", "NU", "CS", "PM", "ES",
    },
}

BUCKET_DISPLAY: dict[str, str] = {
    "middle_east": "Middle East",
    "north_africa": "North Africa",
    "latam": "Latin America",
}

BUCKET_ORDER: tuple[str, ...] = ("middle_east", "north_africa", "latam")


def buckets_for_country(cc: str | None, active: Iterable[str] = BUCKET_ORDER) -> list[str]:
    """Return every ACTIVE bucket whose FIPS set contains *cc*, in BUCKET_ORDER.

    Empty list means the country is not covered (article is dropped at discovery).
    """
    if not cc:
        return []
    active_set = set(active)
    return [b for b in BUCKET_ORDER if b in active_set and cc in BUCKETS[b]]


def group_by_bucket(items, country_of: Callable, active: Iterable[str] = BUCKET_ORDER) -> dict[str, list]:
    """Group *items* into {bucket: [items]} by each item's country (via *country_of*).

    Every active bucket is present (possibly empty). An item whose country maps to
    multiple buckets is placed in each.
    """
    active_list = [b for b in BUCKET_ORDER if b in set(active)]
    out: dict[str, list] = {b: [] for b in active_list}
    for it in items:
        for b in buckets_for_country(country_of(it), active_list):
            out[b].append(it)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_regions.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add geoecon/regions.py tests/unit/test_regions.py
git commit -m "feat(regions): FIPS region buckets with Egypt overlap + grouping"
```

---

### Task 2: Config — active buckets field

**Files:**
- Modify: `geoecon/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: `geoecon.regions.BUCKET_ORDER` (default value).
- Produces: `Config.buckets: tuple[str, ...]` (default `("middle_east","north_africa","latam")`), parsed from env `BUCKETS` (comma-separated). `Config.regions` retained unchanged for back-compat.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_config.py  — ADD these tests (keep existing ones)
from geoecon.config import Config

_ENV = {
    "DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h",
    "SMTP_USER": "u", "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x",
}


def test_buckets_default_is_three_regions():
    cfg = Config.from_env(dict(_ENV))
    assert cfg.buckets == ("middle_east", "north_africa", "latam")


def test_buckets_overridable_from_env():
    cfg = Config.from_env({**_ENV, "BUCKETS": "middle_east, latam"})
    assert cfg.buckets == ("middle_east", "latam")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_config.py -k buckets -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'buckets'`

- [ ] **Step 3: Write minimal implementation**

In `geoecon/config.py`, add the field to the dataclass (after `regions`):

```python
    regions: tuple[str, ...] = ("MENA", "LatAm")
    buckets: tuple[str, ...] = ("middle_east", "north_africa", "latam")
```

And in `from_env`, add (after the `regions=...` line):

```python
            regions=_split(env.get("REGIONS", "MENA,LatAm")),
            buckets=_split(env.get("BUCKETS", "middle_east,north_africa,latam")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_config.py -v`
Expected: PASS (existing + 2 new)

- [ ] **Step 5: Commit**

```bash
git add geoecon/config.py tests/unit/test_config.py
git commit -m "feat(config): add active buckets field (default three regions)"
```

---

### Task 3: Migration + schema.sql + model fields

**Files:**
- Create: `geoecon/state/migrations/002_region_split.sql`
- Modify: `schema.sql`
- Modify: `geoecon/state/models.py`
- Test: `tests/unit/test_repo.py` (one new DB-gated test)

**Interfaces:**
- Produces:
  - DB columns `articles.country_fips TEXT`, `clusters.country_fips TEXT`.
  - `newsletters`: `UNIQUE(run_id)` dropped; `bucket TEXT` added; `UNIQUE(run_id, bucket)`.
  - Table `region_summaries(run_id, bucket, lede_en, takeaways JSONB, model, record_count, PK(run_id,bucket))`.
  - `stage_runs` CHECK includes `'summary'`.
  - `models.Article.country_fips: str | None = None` (trailing optional field).
  - `models.Cluster.country_fips: str | None = None` (trailing optional field).

- [ ] **Step 1: Write the migration**

```sql
-- geoecon/state/migrations/002_region_split.sql
-- Region split: per-country bucketing, per-region newsletters, region summaries.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS country_fips TEXT;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS country_fips TEXT;

-- newsletters: one row per (run, region) instead of one per run
ALTER TABLE newsletters DROP CONSTRAINT IF EXISTS newsletters_run_id_key;
ALTER TABLE newsletters ADD COLUMN IF NOT EXISTS bucket TEXT;
ALTER TABLE newsletters ADD CONSTRAINT newsletters_run_bucket_key UNIQUE (run_id, bucket);

-- per-run, per-region executive summary
CREATE TABLE IF NOT EXISTS region_summaries (
    run_id        BIGINT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    bucket        TEXT   NOT NULL,
    lede_en       TEXT,
    takeaways     JSONB  NOT NULL DEFAULT '[]'::jsonb,
    model         TEXT,
    record_count  INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, bucket)
);

-- add the new 'summary' stage to the stage_runs CHECK
ALTER TABLE stage_runs DROP CONSTRAINT IF EXISTS stage_runs_stage_name_check;
ALTER TABLE stage_runs ADD CONSTRAINT stage_runs_stage_name_check
    CHECK (stage_name IN
        ('discovery','extraction','rss','cluster','summary','render','send'));
```

- [ ] **Step 2: Sync `schema.sql` (the canonical fresh-install dump)**

In `schema.sql`, make the same structural changes so a fresh install matches:
- In `CREATE TABLE articles (...)`, add after `discovered_at ...`: `country_fips TEXT,` (a nullable column; place it among the GDELT provenance columns, e.g. right after `region TEXT CHECK (...)` add `    country_fips    TEXT,`).
- In `CREATE TABLE clusters (...)`, add `    country_fips        TEXT,` after the `region` line.
- In `stage_runs` CHECK, change the list to include `'summary'`:
  `CHECK (stage_name IN ('discovery','extraction','rss','cluster','summary','render','send'))`.
- In `CREATE TABLE newsletters (...)`, add `    bucket          TEXT,` and replace `UNIQUE (run_id)` with `UNIQUE (run_id, bucket)`.
- After the `newsletters` table, add the `region_summaries` table definition (copy the `CREATE TABLE region_summaries (...)` block from Step 1, without `IF NOT EXISTS`).

- [ ] **Step 3: Add model fields**

In `geoecon/state/models.py`, add a trailing optional field to each dataclass:

```python
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
    country_fips: str | None = None
```

```python
@dataclass
class Cluster:
    cluster_id: int
    event_fingerprint: str
    region: str | None
    languages: list[str]
    article_count: int
    framing_divergent: bool
    country_fips: str | None = None
```

- [ ] **Step 4: Write the failing DB-gated test**

```python
# tests/unit/test_repo.py — ADD (uses existing `repo` fixture + `requires_db`)
@requires_db
def test_migration_adds_country_and_per_region_newsletters(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    # country_fips column accepts a value
    aid = repo.insert_article(rid, source_url="u", url_hash="h", domain="d",
                              region="MENA", gkg_themes=[], title="t", country_fips="EG")
    row = repo.conn.execute(
        "SELECT country_fips FROM articles WHERE article_id=%s", (aid,)).fetchone()
    assert row[0] == "EG"
    # two newsletters for the same run, different bucket -> both allowed
    repo.conn.execute(
        "INSERT INTO newsletters(run_id, render_mode, record_count, bucket) "
        "VALUES (%s,'en_only',0,'middle_east'),(%s,'en_only',0,'north_africa')", (rid, rid))
    n = repo.conn.execute(
        "SELECT count(*) FROM newsletters WHERE run_id=%s", (rid,)).fetchone()[0]
    assert n == 2
```

This test requires `insert_article(..., country_fips=...)`, implemented in Task 4 — so it stays RED until then. To keep Task 3 self-contained, gate this step's verification on the migration applying cleanly instead:

- [ ] **Step 5: Verify migration applies (and the full suite still imports)**

Run: `python -m pytest tests/unit -v` (DB tests skip without `TEST_DATABASE_URL`)
Expected: PASS, with DB tests SKIPPED; no import or collection errors. If `TEST_DATABASE_URL` is set, the new `test_migration_adds_country_and_per_region_newsletters` will FAIL on `insert_article(... country_fips=...)` — that is expected and is fixed in Task 4.

- [ ] **Step 6: Commit**

```bash
git add geoecon/state/migrations/002_region_split.sql schema.sql geoecon/state/models.py tests/unit/test_repo.py
git commit -m "feat(schema): migration 002 — country_fips, per-region newsletters, region_summaries"
```

---

### Task 4: Repo — country_fips persistence + records_for_buckets

**Files:**
- Modify: `geoecon/state/repo.py`
- Test: `tests/unit/test_repo.py`

**Interfaces:**
- Consumes: `models.SynthesisRecord`, the `articles.country_fips` / `clusters.country_fips` columns from Task 3.
- Produces:
  - `insert_article(..., country_fips=None)` — persists the FIPS code (new keyword).
  - `insert_cluster(run_id, event_fingerprint, region, languages, article_count, framing_divergent=False, country_fips=None)` — persists FIPS code.
  - `records_for_buckets(run_id) -> list[tuple[SynthesisRecord, str | None, list[dict]]]` — OK records, each with its cluster `country_fips` and a `sources` list `[{"url": str, "domain": str}]` from the cluster's member article(s). One tuple per record.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_repo.py — ADD
@requires_db
def test_records_for_buckets_carries_country_and_source(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    aid = repo.insert_article(rid, source_url="https://reuters.com/x", url_hash="h",
                              domain="reuters.com", region="MENA", gkg_themes=[],
                              title="t", country_fips="EG")
    cid = repo.insert_cluster(rid, "fp1", "MENA", ["en"], 1, country_fips="EG")
    repo.add_membership(cid, aid)
    repo.upsert_synthesis_record(rid, cid, {"headline_en": "A"}, "ok")
    out = repo.records_for_buckets(rid)
    assert len(out) == 1
    rec, country, sources = out[0]
    assert rec.fields["headline_en"] == "A"
    assert country == "EG"
    assert sources == [{"url": "https://reuters.com/x", "domain": "reuters.com"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_repo.py -k records_for_buckets -v`
Expected: FAIL — `insert_article() got an unexpected keyword argument 'country_fips'` (or skip without DB; run with `TEST_DATABASE_URL` set).

- [ ] **Step 3: Write minimal implementation**

In `geoecon/state/repo.py`:

(a) `insert_article` — add `country_fips=None` to the signature and persist it. Replace the method's INSERT to include the new column:

```python
    def insert_article(self, run_id, *, source_url, url_hash, domain=None, region=None,
                       gkg_themes=None, gkg_tone=None, num_mentions=None, source_lang=None,
                       title=None, gdelt_gkg_id=None, canonical_url=None, published_at=None,
                       discovered_via="gdelt", country_fips=None) -> int:
        existing = self.conn.execute(
            "SELECT article_id FROM articles WHERE run_id=%s AND url_hash=%s",
            (run_id, url_hash)).fetchone()
        if existing:
            return existing[0]
        return self.conn.execute(
            """INSERT INTO articles(run_id, gdelt_gkg_id, source_url, canonical_url, url_hash,
                 domain, discovered_via, region, country_fips, gkg_themes, gkg_tone, num_mentions,
                 source_lang, title, published_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING article_id""",
            (run_id, gdelt_gkg_id, source_url, canonical_url, url_hash, domain, discovered_via,
             region, country_fips, gkg_themes or [], gkg_tone, num_mentions, source_lang, title,
             published_at)
        ).fetchone()[0]
```

(b) `insert_cluster` — add `country_fips=None` and persist it:

```python
    def insert_cluster(self, run_id, event_fingerprint, region, languages, article_count,
                       framing_divergent=False, country_fips=None) -> int:
        return self.conn.execute(
            """INSERT INTO clusters(run_id, event_fingerprint, region, country_fips,
                 languages, article_count, framing_divergent)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING cluster_id""",
            (run_id, event_fingerprint, region, country_fips, languages, article_count,
             framing_divergent)).fetchone()[0]
```

(c) Add `records_for_buckets` (place after `ok_records`):

```python
    def records_for_buckets(self, run_id) -> list:
        """OK records with their cluster country_fips and verified source link(s).

        Returns [(SynthesisRecord, country_fips, [{"url","domain"}, ...]), ...],
        one tuple per record. Source links come from articles.source_url (verified
        GDELT URLs), NOT the LLM 'sources' field.
        """
        rows = self.conn.execute(
            """SELECT s.record_id, s.cluster_id, s.headline_en, s.headline_original,
                 s.headline_original_lang, s.what_happened_en, s.geoeconomic_significance_en,
                 s.geoeconomic_vector, s.entities, s.key_figures, s.framing_divergence,
                 s.sources, s.is_headline_only, c.country_fips, a.source_url, a.domain
               FROM synthesis_records s
               JOIN clusters c ON c.cluster_id = s.cluster_id
               LEFT JOIN cluster_articles ca ON ca.cluster_id = c.cluster_id
               LEFT JOIN articles a ON a.article_id = ca.article_id
               WHERE s.run_id=%s AND s.synthesis_quality_flag='ok'
               ORDER BY s.record_id, a.article_id""", (run_id,)).fetchall()
        by_id: dict[int, list] = {}
        order: list[int] = []
        for r in rows:
            rec_id = r[0]
            if rec_id not in by_id:
                rec = SynthesisRecord(
                    record_id=r[0], cluster_id=r[1],
                    fields={"headline_en": r[2], "headline_original": r[3],
                            "headline_original_lang": r[4], "what_happened_en": r[5],
                            "geoeconomic_significance_en": r[6], "geoeconomic_vector": r[7],
                            "entities": r[8], "key_figures": r[9], "framing_divergence": r[10],
                            "sources": r[11]},
                    quality_flag="ok", is_headline_only=r[12])
                by_id[rec_id] = [rec, r[13], []]
                order.append(rec_id)
            if r[14]:  # source_url present
                src = {"url": r[14], "domain": r[15]}
                if src not in by_id[rec_id][2]:
                    by_id[rec_id][2].append(src)
        return [tuple(by_id[i]) for i in order]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_repo.py -k "records_for_buckets or migration_adds_country" -v` (with `TEST_DATABASE_URL` set)
Expected: PASS. Without a DB: the tests SKIP — run the full suite to confirm no import errors: `python -m pytest tests/unit -v`.

- [ ] **Step 5: Commit**

```bash
git add geoecon/state/repo.py tests/unit/test_repo.py
git commit -m "feat(repo): persist country_fips; records_for_buckets with verified source links"
```

---

### Task 5: Repo — region_summaries upsert/get

**Files:**
- Modify: `geoecon/state/repo.py`
- Test: `tests/unit/test_repo.py`

**Interfaces:**
- Produces:
  - `upsert_region_summary(run_id, bucket, lede_en, takeaways, model, record_count) -> None` — upsert keyed `(run_id, bucket)`.
  - `get_region_summary(run_id, bucket) -> dict | None` — `{"bucket","lede_en","takeaways","model","record_count"}` or `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_repo.py — ADD
@requires_db
def test_region_summary_upsert_and_get(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    repo.upsert_region_summary(rid, "middle_east", "Lede one.", ["a", "b"], "deepseek-v3.2", 2)
    got = repo.get_region_summary(rid, "middle_east")
    assert got["lede_en"] == "Lede one."
    assert got["takeaways"] == ["a", "b"]
    assert got["record_count"] == 2
    # upsert overwrites
    repo.upsert_region_summary(rid, "middle_east", "Lede two.", ["c"], "deepseek-v3.2", 1)
    got2 = repo.get_region_summary(rid, "middle_east")
    assert got2["lede_en"] == "Lede two." and got2["takeaways"] == ["c"]
    assert repo.get_region_summary(rid, "latam") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_repo.py -k region_summary -v` (with `TEST_DATABASE_URL`)
Expected: FAIL — `'Repo' object has no attribute 'upsert_region_summary'`

- [ ] **Step 3: Write minimal implementation**

Add to `geoecon/state/repo.py` (after the seen-store section):

```python
    # ---- region summaries ----
    def upsert_region_summary(self, run_id, bucket, lede_en, takeaways, model, record_count):
        self.conn.execute(
            """INSERT INTO region_summaries(run_id, bucket, lede_en, takeaways, model, record_count)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, bucket) DO UPDATE SET
                 lede_en=EXCLUDED.lede_en, takeaways=EXCLUDED.takeaways,
                 model=EXCLUDED.model, record_count=EXCLUDED.record_count, created_at=now()""",
            (run_id, bucket, lede_en, Jsonb(takeaways or []), model, record_count))

    def get_region_summary(self, run_id, bucket) -> dict | None:
        row = self.conn.execute(
            "SELECT bucket, lede_en, takeaways, model, record_count "
            "FROM region_summaries WHERE run_id=%s AND bucket=%s", (run_id, bucket)).fetchone()
        if not row:
            return None
        return {"bucket": row[0], "lede_en": row[1], "takeaways": row[2],
                "model": row[3], "record_count": row[4]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_repo.py -k region_summary -v` (with `TEST_DATABASE_URL`)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add geoecon/state/repo.py tests/unit/test_repo.py
git commit -m "feat(repo): region_summaries upsert/get"
```

---

### Task 6: Repo — per-region newsletters

**Files:**
- Modify: `geoecon/state/repo.py`
- Test: `tests/unit/test_repo.py`

**Interfaces:**
- Consumes: `newsletters.bucket` column + `UNIQUE(run_id, bucket)` from Task 3.
- Produces:
  - `insert_newsletter(run_id, render_mode, archive_html_path, email_html, record_count, bucket=None) -> int` — upsert on `(run_id, bucket)`; on conflict re-renders content and resets `send_status='pending'`, `send_error=NULL`.
  - `newsletters_for_run(run_id) -> list[dict]` — `[{"newsletter_id","bucket","record_count","email_html","send_status"}, ...]` ordered by bucket.
  - `update_newsletter_send` and `mark_emitted` unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_repo.py — ADD
@requires_db
def test_newsletter_per_bucket_upsert_and_list(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    n1 = repo.insert_newsletter(rid, "en_only", "/a/me.html", "<b>me</b>", 3, bucket="middle_east")
    n2 = repo.insert_newsletter(rid, "en_only", "/a/na.html", "<b>na</b>", 1, bucket="north_africa")
    assert n1 != n2
    # re-render same bucket -> same row, content updated, status reset
    repo.update_newsletter_send(n1, "sent")
    n1b = repo.insert_newsletter(rid, "en_only", "/a/me2.html", "<b>me2</b>", 4, bucket="middle_east")
    assert n1b == n1
    rows = {r["bucket"]: r for r in repo.newsletters_for_run(rid)}
    assert rows["middle_east"]["record_count"] == 4
    assert rows["middle_east"]["email_html"] == "<b>me2</b>"
    assert rows["middle_east"]["send_status"] == "pending"  # reset on re-render
    assert set(rows) == {"middle_east", "north_africa"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_repo.py -k newsletter_per_bucket -v` (with `TEST_DATABASE_URL`)
Expected: FAIL — `insert_newsletter() got an unexpected keyword argument 'bucket'`

- [ ] **Step 3: Write minimal implementation**

Replace `insert_newsletter` and add `newsletters_for_run` in `geoecon/state/repo.py`:

```python
    def insert_newsletter(self, run_id, render_mode, archive_html_path, email_html,
                          record_count, bucket=None) -> int:
        return self.conn.execute(
            """INSERT INTO newsletters(run_id, render_mode, archive_html_path, email_html,
                 record_count, bucket)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, bucket) DO UPDATE SET
                 render_mode=EXCLUDED.render_mode, archive_html_path=EXCLUDED.archive_html_path,
                 email_html=EXCLUDED.email_html, record_count=EXCLUDED.record_count,
                 send_status='pending', send_error=NULL
               RETURNING newsletter_id""",
            (run_id, render_mode, archive_html_path, email_html, record_count, bucket)).fetchone()[0]

    def newsletters_for_run(self, run_id) -> list[dict]:
        rows = self.conn.execute(
            "SELECT newsletter_id, bucket, record_count, email_html, send_status "
            "FROM newsletters WHERE run_id=%s ORDER BY bucket", (run_id,)).fetchall()
        return [{"newsletter_id": r[0], "bucket": r[1], "record_count": r[2],
                 "email_html": r[3], "send_status": r[4]} for r in rows]
```

Keep `get_newsletter` as-is (still used by tests / back-compat).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_repo.py -k newsletter_per_bucket -v` (with `TEST_DATABASE_URL`)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add geoecon/state/repo.py tests/unit/test_repo.py
git commit -m "feat(repo): per-region newsletter upsert + newsletters_for_run"
```

---

### Task 7: Discovery — bucket filter + persist country

**Files:**
- Modify: `geoecon/stages/discovery.py:62-100`
- Test: `tests/unit/test_discovery.py`

**Interfaces:**
- Consumes: `regions.buckets_for_country`, `cfg.buckets`, `repo.insert_article(..., country_fips=...)`.
- Produces: discovery keeps an article iff its country maps to ≥1 active bucket; persists `country_fips`; still sets the coarse legacy `region`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_discovery.py — ADD (keep existing tests)
def test_discovery_persists_country_and_keeps_bucketed(monkeypatch):
    lines = [_line("x1"), _line("x2")]  # fixture country = SA (Saudi -> middle_east)
    captured = []

    class FakeRepo:
        def insert_article(self, run_id, **kw):
            captured.append(kw)
            return len(captured)

    from geoecon.stages import discovery
    from geoecon.config import Config
    cfg = Config.from_env({
        "DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
        "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x",
    })
    metrics = discovery.run(cfg, FakeRepo(), run_id=1, fetch=lambda ws, we: lines)
    assert metrics["discovered"] == 2
    assert all(c["country_fips"] == "SA" for c in captured)
    assert all(c["region"] == "MENA" for c in captured)  # coarse legacy tag preserved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_discovery.py -k persists_country -v`
Expected: FAIL — `KeyError: 'country_fips'` (discovery does not yet pass it)

- [ ] **Step 3: Write minimal implementation**

In `geoecon/stages/discovery.py`, add the import near the top:

```python
from .. import gdelt, regions
```

Replace the per-line filter block (currently the `region = gdelt.region_for_country(...)` / `if region not in cfg.regions` lines) with:

```python
        region = gdelt.region_for_country(rec.country)
        if not regions.buckets_for_country(rec.country, cfg.buckets):
            continue
```

And in the `insert_article` call, pass the country code:

```python
    for rec, region, h in kept:
        repo.insert_article(
            run_id,
            source_url=rec.url,
            url_hash=h,
            domain=rec.domain,
            region=region,
            country_fips=rec.country,
            gkg_themes=rec.themes,
            gkg_tone=rec.tone,
            source_lang=rec.lang,
            title=None,
            gdelt_gkg_id=rec.gkg_id,
        )
```

(Note: `kept` tuples are `(rec, region, h)` as today; `rec.country` is the FIPS code. The `region` var stays the coarse legacy value for the `region` column.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_discovery.py -v`
Expected: PASS (existing `test_discovery_run_inserts_and_caps` still passes — SA → middle_east is active by default — plus the new test)

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/discovery.py tests/unit/test_discovery.py
git commit -m "feat(discovery): keep by bucket membership; persist country_fips"
```

---

### Task 8: Cluster — carry country through

**Files:**
- Modify: `geoecon/stages/cluster.py:16-27`
- Test: `tests/unit/test_cluster.py`

**Interfaces:**
- Consumes: `art.country_fips`, `repo.insert_cluster(..., country_fips=...)`.
- Produces: each cluster persists the article's `country_fips`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cluster.py — ADD (keep existing tests)
from geoecon.stages import cluster as cluster_stage
from geoecon.state.models import Article


def test_cluster_carries_country_fips():
    art = Article(1, "u", "h", "d", "MENA", [], None, None, "en", "Title", "body",
                  "extracted", country_fips="EG")
    captured = {}

    class FakeRepo:
        def articles_for_run(self, rid):
            return [art]

        def insert_cluster(self, run_id, fp, region, langs, count,
                           framing_divergent=False, country_fips=None):
            captured["country_fips"] = country_fips
            return 99

        def add_membership(self, cid, aid, frame_label=None):
            pass

    cluster_stage.run(object(), FakeRepo(), 1)
    assert captured["country_fips"] == "EG"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_cluster.py -k country_fips -v`
Expected: FAIL — `insert_cluster() got an unexpected keyword argument 'country_fips'` is NOT the failure (the FakeRepo accepts it); it FAILS because the real `cluster.run` does not pass `country_fips`, so `captured` stays empty → `KeyError`.

- [ ] **Step 3: Write minimal implementation**

In `geoecon/stages/cluster.py`, update the `insert_cluster` call inside `run`:

```python
        cid = repo.insert_cluster(run_id, fp, art.region, langs, 1,
                                  country_fips=art.country_fips)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_cluster.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/cluster.py tests/unit/test_cluster.py
git commit -m "feat(cluster): carry country_fips onto clusters"
```

---

### Task 9: Executive summary prompt + summary stage

**Files:**
- Create: `geoecon/prompts/region_summary.txt`
- Create: `geoecon/stages/summary.py`
- Test: `tests/unit/test_summary.py`

**Interfaces:**
- Consumes: `regions.group_by_bucket`, `regions.BUCKET_DISPLAY`, `cfg.buckets`, `cfg.synthesis_model`, `repo.records_for_buckets`, `repo.upsert_region_summary`, `synth.parse_record`, an LLM client with `.complete(model, messages, **kw) -> resp` where `resp.ok()` and `resp.final_answer`.
- Produces:
  - `build_summary_messages(prompt: str, region_display: str, records: list[SynthesisRecord]) -> list[dict]`
  - `run(cfg, repo, run_id, client, *, prompt=None) -> dict` returning `{"summaries_total","summaries_ok","summaries_empty"}`. Empty buckets are skipped (no row). Bad/unparseable JSON ⇒ a summary row with `lede=None, takeaways=[]` (graceful degrade) counted as empty.

- [ ] **Step 1: Write the prompt file**

```
geoecon/prompts/region_summary.txt
```

Content:

```
You are the desk editor for a daily geoeconomic intelligence brief, writing the executive summary that opens the {REGION} edition. Readers are analysts and policymakers who will read the individual stories printed below your summary; your job is to give them the day's throughline and the threads that matter, grounded STRICTLY in the stories provided.

You receive the day's {REGION} stories — each with a headline, what-happened, geoeconomic significance, and key figures. Synthesize them.

Output STRICT JSON only, no markdown, exactly this schema:
{"lede": "<1-2 sentences naming the single dominant geoeconomic throughline of the day for this region, and why it matters>", "takeaways": ["<distinct thread>", "..."]}

Rules:
- Ground everything ONLY in the provided stories. Never introduce a fact, figure, name, or event not present in them. Every figure or proper noun you cite must appear in the stories.
- Provide 3-5 takeaways when the material supports them; fewer if there are few stories. Each takeaway is one sentence (~25 words) leading with the concrete development and its driving actor or figure.
- The lede must name a REAL common thread if one exists. If the stories do NOT share a throughline, say so plainly (e.g. "No single thread dominated; the day's {REGION} developments span unrelated fronts:") rather than manufacture a connection.
- Geoeconomic lens only: trade, capital flows, energy, currency, sanctions, debt, investment, supply chains, fiscal/monetary policy. Omit purely political or human-interest angles unless they carry a geoeconomic consequence.
- Neutral, precise, non-sensational analyst register. No hype, no first person, no calls to action. English. Whole summary under ~180 words.

Return ONLY the JSON object.
```

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_summary.py
from geoecon.stages import summary
from geoecon.state.models import SynthesisRecord


def _rec(rid, headline):
    return SynthesisRecord(rid, rid * 10, {
        "headline_en": headline, "what_happened_en": "Something happened.",
        "geoeconomic_significance_en": "It matters.", "key_figures": [{"value": 5, "unit": "%"}],
    }, "ok", False)


def _client(answer):
    class Resp:
        def __init__(self, fa): self.final_answer = fa
        def ok(self): return True

    class C:
        def __init__(self): self.calls = []
        def complete(self, model, messages, **kw):
            self.calls.append((model, messages)); return Resp(answer)

    return C()


class _Repo:
    def __init__(self, tuples):
        self._tuples = tuples; self.saved = []
    def records_for_buckets(self, rid):
        return self._tuples
    def upsert_region_summary(self, rid, bucket, lede, takeaways, model, count):
        self.saved.append((bucket, lede, takeaways, count))


_CFG = type("C", (), {"synthesis_model": "deepseek-v3.2", "synthesis_prompt_id": "x",
                      "buckets": ("middle_east", "north_africa", "latam")})()

GOOD = '{"lede":"Gulf capital rotates east.","takeaways":["PIF shifts $15B.","UAE-Turkey swap widens."]}'


def test_groups_by_bucket_and_skips_empty():
    # one Egypt record (-> middle_east AND north_africa) + one Brazil record (-> latam)
    tuples = [(_rec(1, "Egypt FX move"), "EG", []), (_rec(2, "Brazil tariff"), "BR", [])]
    repo = _Repo(tuples)
    client = _client(GOOD)
    counts = summary.run(_CFG, repo, 1, client, prompt='SYS {REGION}')
    buckets_saved = {s[0] for s in repo.saved}
    assert buckets_saved == {"middle_east", "north_africa", "latam"}  # 3 non-empty
    assert counts["summaries_total"] == 3 and counts["summaries_ok"] == 3
    # region display interpolated into the system prompt
    me_system = client.calls[0][1][0]["content"]
    assert "Middle East" in me_system


def test_bad_json_degrades_to_empty_summary():
    tuples = [(_rec(1, "X"), "BR", [])]
    repo = _Repo(tuples)
    counts = summary.run(_CFG, repo, 1, _client("not json at all"), prompt='SYS {REGION}')
    assert counts["summaries_empty"] == 1
    bucket, lede, takeaways, count = repo.saved[0]
    assert bucket == "latam" and lede is None and takeaways == []


def test_no_records_writes_nothing():
    repo = _Repo([])
    counts = summary.run(_CFG, repo, 1, _client(GOOD), prompt='SYS {REGION}')
    assert repo.saved == [] and counts["summaries_total"] == 0
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_summary.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'geoecon.stages.summary'`

- [ ] **Step 4: Write minimal implementation**

```python
# geoecon/stages/summary.py
"""Summary stage — one executive summary per non-empty region bucket."""
from __future__ import annotations

import logging

from .. import regions
from .synth import parse_record

log = logging.getLogger("geoecon.summary")


def build_summary_messages(prompt: str, region_display: str, records) -> list[dict]:
    """Build [system, user] messages for the region executive summary."""
    parts = []
    for rec in records:
        f = rec.fields
        figs = "; ".join(
            f"{k.get('value', '')}{k.get('unit', '') or ''} {k.get('gloss_en', '') or ''}".strip()
            for k in (f.get("key_figures") or [])
        )
        block = "\n".join(x for x in [
            f"HEADLINE: {f.get('headline_en', '')}",
            f"WHAT HAPPENED: {f.get('what_happened_en', '')}",
            f"SIGNIFICANCE: {f.get('geoeconomic_significance_en', '')}",
            (f"KEY FIGURES: {figs}" if figs else ""),
        ] if x)
        parts.append(block)
    system = prompt.replace("{REGION}", region_display)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n---\n\n".join(parts)},
    ]


def run(cfg, repo, run_id, client, *, prompt: str | None = None) -> dict:
    if prompt is None:
        from importlib.resources import files
        prompt = files("geoecon.prompts").joinpath("region_summary.txt").read_text("utf-8")

    grouped = regions.group_by_bucket(
        repo.records_for_buckets(run_id), country_of=lambda t: t[1], active=cfg.buckets)

    counts = {"summaries_total": 0, "summaries_ok": 0, "summaries_empty": 0}
    for bucket in cfg.buckets:
        items = grouped.get(bucket, [])
        if not items:
            continue
        records = [t[0] for t in items]
        messages = build_summary_messages(prompt, regions.BUCKET_DISPLAY[bucket], records)
        resp = client.complete(cfg.synthesis_model, messages,
                               temperature=0, seed=12345, max_tokens=2048)
        data = parse_record(getattr(resp, "final_answer", "") or "") if resp.ok() else None
        if data and isinstance(data.get("takeaways"), list):
            lede, takeaways = data.get("lede"), data.get("takeaways")
            counts["summaries_ok"] += 1
        else:
            lede, takeaways = None, []
            counts["summaries_empty"] += 1
        repo.upsert_region_summary(run_id, bucket, lede, takeaways,
                                   cfg.synthesis_model, len(records))
        counts["summaries_total"] += 1

    log.info("summary: %s", counts)
    return counts
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_summary.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add geoecon/prompts/region_summary.txt geoecon/stages/summary.py tests/unit/test_summary.py
git commit -m "feat(summary): region executive-summary stage + grounded prompt"
```

---

### Task 10: Render — per-region emails with summary block + source links

**Files:**
- Modify: `geoecon/render/render.py` (the module at `geoecon/stages/render.py`)
- Modify: `geoecon/render/templates/email.html.j2`
- Modify: `geoecon/render/templates/archive.html.j2`
- Test: `tests/unit/test_render.py`

**Interfaces:**
- Consumes: `regions.group_by_bucket`, `regions.BUCKET_DISPLAY`, `cfg.buckets`, `cfg.archive_dir`, `repo.records_for_buckets`, `repo.get_region_summary`, `repo.insert_newsletter(..., bucket=...)`.
- Produces:
  - `render_html(records, *, mode="en_only", date_str, summary=None, region_display=None, stats=None) -> (archive, email)` — `summary` is a dict `{"lede_en","takeaways"}` or `None`; each record may carry a transient `source_links` attribute (`[{"url","domain"}]`).
  - `run(cfg, repo, run_id, date_str, *, stats=None) -> dict` returning `{"newsletters": [{"newsletter_id","bucket","record_count"}], "record_count": int}`. One newsletter per non-empty bucket; archive path `geoecon-{date_str}-{bucket}.html`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_render.py — REPLACE the file with this (keeps the two original
# behaviors and adds summary/source-link/per-bucket coverage)
from geoecon.stages import render
from geoecon.state.models import SynthesisRecord


def _rec(source_links=None):
    r = SynthesisRecord(1, 10, {
        "headline_en": "Saudi cuts oil output",
        "what_happened_en": "Riyadh announced a 1mbd cut.",
        "geoeconomic_significance_en": "Pressures crude prices.",
        "entities": [{"canonical_en": "Saudi Arabia"}],
        "key_figures": [{"value": 1, "unit": "mbd", "gloss_en": "1 million barrels/day"}],
        "sources": [{"url": "http://attribution-only", "lang": "ar"}],
        "headline_original": "السعودية تخفض الإنتاج", "headline_original_lang": "ar",
    }, "ok", False)
    if source_links is not None:
        r.source_links = source_links
    return r


def test_en_only_includes_english_excludes_original():
    archive, email = render.render_html([_rec()], mode="en_only", date_str="2026-06-19")
    assert "Saudi cuts oil output" in email and "Saudi cuts oil output" in archive
    assert "السعودية" not in email
    assert "<table" in email.lower()


def test_zero_records_renders_heartbeat():
    archive, email = render.render_html([], mode="en_only", date_str="2026-06-19")
    assert "no qualifying" in email.lower() or "quiet" in email.lower()


def test_summary_block_and_source_link_render():
    rec = _rec(source_links=[{"url": "https://reuters.com/x", "domain": "reuters.com"}])
    summary = {"lede_en": "Gulf capital rotates east.", "takeaways": ["PIF shifts $15B."]}
    archive, email = render.render_html([rec], mode="en_only", date_str="2026-06-19",
                                        summary=summary, region_display="Middle East")
    assert "Middle East" in email
    assert "Gulf capital rotates east." in email
    assert "PIF shifts $15B." in email
    assert 'href="https://reuters.com/x"' in email
    assert "reuters.com" in email
    # the LLM 'sources' attribution URL must NOT be used as a link
    assert "attribution-only" not in email
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_render.py -v`
Expected: FAIL — `test_summary_block_and_source_link_render` fails (no summary/link rendering yet); `render_html` does not accept `summary`/`region_display`.

- [ ] **Step 3: Update `render_html` signature**

In `geoecon/stages/render.py`, replace `render_html` and `run`:

```python
def render_html(records, *, mode="en_only", date_str, summary=None,
                region_display=None, throughline=None, stats=None):
    ctx = {"records": records, "date_str": date_str, "summary": summary,
           "region_display": region_display, "throughline": throughline,
           "stats": stats or {}, "mode": mode}
    archive = _env.get_template("archive.html.j2").render(**ctx)
    email = _env.get_template("email.html.j2").render(**ctx)
    return archive, email


def run(cfg, repo, run_id, date_str, *, stats=None) -> dict:
    from .. import regions
    grouped = regions.group_by_bucket(
        repo.records_for_buckets(run_id), country_of=lambda t: t[1], active=cfg.buckets)
    os.makedirs(cfg.archive_dir, exist_ok=True)
    newsletters = []
    for bucket in cfg.buckets:
        items = grouped.get(bucket, [])
        if not items:
            continue
        records = []
        for rec, _country, sources in items:
            rec.source_links = sources
            records.append(rec)
        summary = repo.get_region_summary(run_id, bucket)
        region_display = regions.BUCKET_DISPLAY[bucket]
        archive, email = render_html(records, mode="en_only", date_str=date_str,
                                     summary=summary, region_display=region_display, stats=stats)
        path = os.path.join(cfg.archive_dir, f"geoecon-{date_str}-{bucket}.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(archive)
        nid = repo.insert_newsletter(run_id, "en_only", path, email, len(records), bucket=bucket)
        newsletters.append({"newsletter_id": nid, "bucket": bucket, "record_count": len(records)})
        log.info("render[%s]: %d records -> %s", bucket, len(records), path)
    return {"newsletters": newsletters,
            "record_count": sum(n["record_count"] for n in newsletters)}
```

- [ ] **Step 4: Update the email template**

In `geoecon/render/templates/email.html.j2`, change the header line to use the region display name, add a summary block after the header, and add a source line to each story card. Replace the header `<tr>` and add the summary block right after it:

```html
  <tr><td style="padding:24px 28px;border-bottom:3px solid #1a1a1a;">
    <div style="font-size:20px;font-weight:bold;">Geoeconomic Brief{% if region_display %} — {{ region_display }}{% endif %}</div>
    <div style="font-size:13px;color:#777;">{{ date_str }}</div></td></tr>
  {% if summary and (summary.lede_en or summary.takeaways) %}
  <tr><td style="padding:18px 28px;background:#fafafa;border-bottom:1px solid #eee;">
    <div style="font-size:13px;font-weight:bold;text-transform:uppercase;color:#555;letter-spacing:.04em;">Executive Summary</div>
    {% if summary.lede_en %}<div style="font-size:14px;line-height:1.5;margin-top:8px;">{{ summary.lede_en }}</div>{% endif %}
    {% if summary.takeaways %}<ul style="font-size:13px;line-height:1.5;margin:10px 0 0 0;padding-left:18px;">{% for t in summary.takeaways %}<li style="margin-bottom:4px;">{{ t }}</li>{% endfor %}</ul>{% endif %}
  </td></tr>{% endif %}
```

Then, inside the per-record `<tr>`, add a source line after the key-figures `<div>` (just before the closing `</td></tr>` of the record loop):

```html
    {% if r.source_links %}<div style="font-size:12px;margin-top:8px;">{% for s in r.source_links %}Source: <a href="{{ s.url }}" style="color:#1a5fb4;text-decoration:underline;">{{ s.domain or s.url }}</a> →{% if not loop.last %} · {% endif %}{% endfor %}</div>{% endif %}
```

(Leave the existing `{% else %}` quiet-day branch and footer unchanged.)

- [ ] **Step 5: Update the archive template**

In `geoecon/render/templates/archive.html.j2`:
- Change the `<h1>` line to `<h1>Geoeconomic Brief{% if region_display %} — {{ region_display }}{% endif %}</h1>`.
- After the date `<div>`, add the summary block:

```html
{% if summary and (summary.lede_en or summary.takeaways) %}
<div class="through">
  {% if summary.lede_en %}<p>{{ summary.lede_en }}</p>{% endif %}
  {% if summary.takeaways %}<ul>{% for t in summary.takeaways %}<li>{{ t }}</li>{% endfor %}</ul>{% endif %}
</div>{% endif %}
```

- Inside the per-record `.item` block, after the figures `<p>`, add:

```html
  {% if r.source_links %}<p class="figs">{% for s in r.source_links %}Source: <a href="{{ s.url }}">{{ s.domain or s.url }}</a>{% if not loop.last %}; {% endif %}{% endfor %}</p>{% endif %}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_render.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
git add geoecon/stages/render.py geoecon/render/templates/email.html.j2 geoecon/render/templates/archive.html.j2 tests/unit/test_render.py
git commit -m "feat(render): per-region emails with executive summary + verified source links"
```

---

### Task 11: Send + orchestrator wiring (multi-newsletter, summary stage)

**Files:**
- Modify: `geoecon/orchestrator.py:8-33` (the `_default_stages` factory)
- Test: `tests/unit/test_orchestrator.py`, `tests/unit/test_send.py`

**Interfaces:**
- Consumes: `regions.BUCKET_DISPLAY`, `summary.run`, `render.run`, `repo.newsletters_for_run`, `repo.mark_emitted`, `send.run`.
- Produces: stage order `discovery → extraction → cluster → summary → render → send`; `_send` iterates `newsletters_for_run`, sends each pending one with subject `Geoeconomic Brief — {Region} — {date}`, calls `mark_emitted` once if any sent, returns `{"send_status": "sent"|"failed"|"skipped"|"skipped_empty", "sent": [...]}`. `send.run` signature unchanged.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_orchestrator.py — ADD
def test_default_stages_include_summary_in_order():
    from geoecon import orchestrator
    cfg = _cfg()
    names = [name for name, _fn in orchestrator._default_stages(cfg, client=None)]
    assert names == ["discovery", "extraction", "cluster", "synthesis", "render", "send"] or \
           names == ["discovery", "extraction", "cluster", "summary", "render", "send"]
    assert "summary" in names
    assert names.index("summary") < names.index("render") < names.index("send")
```

```python
# tests/unit/test_send.py — ADD
def test_send_run_still_sends_one_newsletter():
    # send.run contract is unchanged: one newsletter at a time
    sent = {}
    class FakeRepo:
        def update_newsletter_send(self, nid, status, send_error=None):
            sent["status"] = status
    out = send.run(Cfg(), FakeRepo(), 1, "Geoeconomic Brief — Middle East — 2026-06-21",
                   "<b>h</b>", transport=lambda cfg, msg: None)
    assert out["send_status"] == "sent" and sent["status"] == "sent"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_orchestrator.py -k summary tests/unit/test_send.py -v`
Expected: FAIL — `test_default_stages_include_summary_in_order` fails (`summary` not in stage list).

- [ ] **Step 3: Rewrite `_default_stages`**

Replace `_default_stages` in `geoecon/orchestrator.py` with:

```python
def _default_stages(cfg, client):
    from .stages import discovery, extract, cluster, synth, summary, render, send
    from . import regions
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = {}
    def _discovery(c, r, rid): state["m"] = discovery.run(c, r, rid); return state["m"]
    def _extract(c, r, rid):   return extract.run(c, r, rid)
    def _cluster(c, r, rid):   return cluster.run(c, r, rid)
    def _synth(c, r, rid):     return synth.run(c, r, rid, client)
    def _summary(c, r, rid):   return summary.run(c, r, rid, client)
    def _render(c, r, rid):
        out = render.run(c, r, rid, date_str, stats=state.get("m"))
        return {"record_count": out["record_count"], "newsletters": out["newsletters"]}
    def _send(c, r, rid):
        if c.dry_run:
            log.info("dry-run: skipping send"); return {"send_status": "skipped"}
        newsletters = r.newsletters_for_run(rid)
        if not newsletters:
            return {"send_status": "skipped_empty"}
        any_sent = False
        results = []
        for nl in newsletters:
            if nl["send_status"] == "sent":
                results.append((nl["bucket"], "already_sent")); continue
            display = regions.BUCKET_DISPLAY.get(nl["bucket"], nl["bucket"] or "")
            subject = f"Geoeconomic Brief — {display} — {date_str}"
            res = send.run(c, r, nl["newsletter_id"], subject, nl["email_html"])
            status = res.get("send_status")
            results.append((nl["bucket"], status))
            if status == "sent":
                any_sent = True
        if any_sent:
            r.mark_emitted(rid)
        return {"send_status": "sent" if any_sent else "failed", "sent": results}
    return [("discovery", _discovery), ("extraction", _extract), ("cluster", _cluster),
            ("synthesis", _synth), ("summary", _summary), ("render", _render), ("send", _send)]
```

This rewrite adds `summary` to the `from .stages import ...` line and inserts the
`("summary", _summary)` stage between `synthesis` and `render`; `render` no longer
needs the client, and `_send` now fans out over `newsletters_for_run`. The
module-level `log` and `from .config import Config` imports at the top of
`orchestrator.py` are unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_orchestrator.py tests/unit/test_send.py -v`
Expected: PASS (existing orchestrator/send tests unaffected — they pass explicit `stages=`; the new ones pass).

- [ ] **Step 5: Full suite**

Run: `python -m pytest tests/unit -v`
Expected: PASS (DB-gated tests SKIP without `TEST_DATABASE_URL`); no failures.

- [ ] **Step 6: Commit**

```bash
git add geoecon/orchestrator.py tests/unit/test_orchestrator.py tests/unit/test_send.py
git commit -m "feat(orchestrator): summary stage + per-region multi-newsletter send"
```

---

### Task 12: End-to-end dry-run smoke check + docs

**Files:**
- Modify: `PIPELINE_BLUEPRINT.md` (or `NEXT_SESSION.md`) — one short note on the region split + new stage.
- No new test; this task verifies the wired pipeline imports and the stage list is correct.

- [ ] **Step 1: Verify the package imports and stage list end-to-end**

Run:
```bash
python -c "from geoecon import orchestrator, regions; from geoecon.config import Config; \
cfg = Config.from_env({'DB_DSN':'x','MAETCENTRUM_KEY':'k','SMTP_HOST':'h','SMTP_USER':'u','SMTP_PASSWORD':'p','FROM_ADDR':'f','RECIPIENTS':'r@x'}); \
print([n for n,_ in orchestrator._default_stages(cfg, client=None)]); \
print(regions.buckets_for_country('EG'))"
```
Expected output:
```
['discovery', 'extraction', 'cluster', 'synthesis', 'summary', 'render', 'send']
['middle_east', 'north_africa']
```

- [ ] **Step 2: Run the complete suite once more**

Run: `python -m pytest tests/unit -v`
Expected: PASS / DB-tests SKIP. No failures.

- [ ] **Step 3: Add a short doc note**

In `PIPELINE_BLUEPRINT.md`, add a brief section noting: three region buckets (FIPS-based, Egypt in both Middle East and North Africa), the new `summary` stage, per-region newsletters, verified source links, and that empty regions (and fully quiet days) send nothing. Keep it to ~8 lines.

- [ ] **Step 4: Commit**

```bash
git add PIPELINE_BLUEPRINT.md
git commit -m "docs: note region split, summary stage, per-region delivery"
```

---

## Self-Review

**Spec coverage:**
- §3 region taxonomy → Task 1. ✓
- §4 migration (country_fips, newsletters bucket, region_summaries, stage CHECK) → Task 3. ✓ schema.sql sync → Task 3 Step 2. ✓
- §5.1 discovery → Task 7. ✓ §5.2 cluster → Task 8. ✓ §5.3 summary stage → Task 9. ✓ §5.4 render → Task 10. ✓ §5.5 send + orchestrator → Task 11. ✓
- §6 source links (verified source_url, not LLM sources) → Task 4 (`records_for_buckets`) + Task 10 (templates) + explicit negative assertion in `test_render`/`test_summary`. ✓
- §7 exec-summary prompt → Task 9 Step 1. ✓
- §8 repo additions → Tasks 4/5/6. ✓
- §9 config buckets → Task 2. ✓
- §10 orchestrator → Task 11. ✓
- §11 testing → tests in every task (`test_regions`, `test_summary`, modified `test_render`/`test_send`/`test_discovery`/`test_cluster`/`test_orchestrator`/`test_repo`). ✓
- §2 D4/D5 empty-region/quiet-day skip → render skips empty buckets (Task 10), `_send` returns `skipped_empty` when no newsletters (Task 11). ✓
- §2 D3 same recipients → `send.run` uses `cfg.recipients` unchanged; no per-region lists. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to" — every code step shows full code. ✓

**Type consistency:**
- `buckets_for_country(cc, active)` and `group_by_bucket(items, country_of, active)` — same signatures in Task 1, consumed identically in Tasks 7/9/10. ✓
- `records_for_buckets(run_id) -> [(SynthesisRecord, country_fips, [sources])]` — produced in Task 4; consumed via `country_of=lambda t: t[1]` and `t[0]`/`t[2]` in Tasks 9/10. ✓
- `get_region_summary` returns `{"lede_en","takeaways",...}`; templates read `summary.lede_en` / `summary.takeaways` (Task 10). ✓
- `insert_newsletter(..., bucket=)` (Task 6) ↔ called with `bucket=bucket` (Task 10). ✓
- `newsletters_for_run` dict keys `newsletter_id/bucket/record_count/email_html/send_status` (Task 6) ↔ read identically in `_send` (Task 11). ✓
- `upsert_region_summary(run_id, bucket, lede_en, takeaways, model, record_count)` (Task 5) ↔ called with those positional args in summary stage (Task 9). ✓
- Model field `country_fips` (Task 3) ↔ `art.country_fips` (Task 8), `insert_article(country_fips=)` (Task 4/7), `insert_cluster(country_fips=)` (Task 4/8). ✓
