# Design: Three Regional Emails with Executive Summaries + Source Links

**Date:** 2026-06-21
**Status:** Approved (design); pending spec review
**Topic:** Split the single daily Geoeconomic Brief into three region-scoped emails
(Middle East, North Africa, Latin America), each opening with an LLM-generated
executive summary, and add a verified source link to every story.

---

## 1. Problem & Goal

Today the pipeline sends **one** email per daily run containing all qualifying
stories across the combined `MENA` + `LatAm` regions. We want to deliver **three
separate emails**, one per region, each:

1. Scoped to a specific set of countries (regions redefined below).
2. Opening with an **executive summary** synthesizing that region's stories for
   the day (analyst/desk tone, lede + key takeaways).
3. Rendering a **verified source link** on every story.

This is a behavior change to the **discovery → summary → render → send** tail of
the pipeline plus a schema migration. Upstream extraction/cluster/synthesis logic
is unchanged except for carrying a country code through.

## 2. Decisions (locked)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Egypt (listed in both Middle East and North Africa) | Appears in **both** emails. A country may belong to multiple region buckets. |
| D2 | Coverage scope | Use the explicit lists; **add** Israel/Turkey/Cyprus to Middle East; **keep** West Bank + Gaza under Middle East; **drop** Sudan. |
| D3 | Recipients | **Same** recipient list for all three emails (one shared `RECIPIENTS`). |
| D4 | Empty region (no qualifying stories) | **Skip** — no summary, no newsletter, no email for that region. |
| D5 | Fully quiet day (all regions empty) | **Send nothing** (honors D4; supersedes the current single heartbeat email). |
| D6 | Executive summary format | **Lede + key takeaways**: 1–2 sentence throughline, then 3–5 single-sentence takeaways. |
| D7 | Source link | Link the **verified GDELT `articles.source_url`**, never the LLM-generated `sources` attribution field. |
| D8 | Architecture | **Approach A**: dedicated `summary` stage + `region_summaries` table + per-bucket render/send. |

## 3. Region Taxonomy — `geoecon/regions.py` (new)

Region buckets keyed off **FIPS 10-4** country codes (GDELT GKG emits FIPS, not
ISO — see existing note in `gdelt.py`). A country may map to multiple buckets.

```
BUCKETS = {
  "middle_east": {  # display: "Middle East"
    "BA",  # Bahrain
    "IR",  # Iran
    "IZ",  # Iraq
    "IS",  # Israel        (NEW)
    "JO",  # Jordan
    "KU",  # Kuwait
    "LE",  # Lebanon
    "MU",  # Oman
    "QA",  # Qatar
    "SA",  # Saudi Arabia
    "SY",  # Syria
    "AE",  # United Arab Emirates
    "YM",  # Yemen
    "EG",  # Egypt          (also in north_africa)
    "TU",  # Turkey         (NEW)
    "CY",  # Cyprus         (NEW)
    "WE",  # West Bank      (kept)
    "GZ",  # Gaza Strip     (kept)
  },
  "north_africa": {  # display: "North Africa"
    "EG",  # Egypt          (also in middle_east)
    "LY",  # Libya
    "TS",  # Tunisia
    "AG",  # Algeria
    "MO",  # Morocco
  },
  "latam": {  # display: "Latin America" — existing LATAM set, unchanged
    "BR","AR","MX","CI","CO","PE","VE","EC","BL",
    "PA","UY","GT","CU","DR","HO","NU","CS","PM","ES",
  },
}

BUCKET_DISPLAY = {
  "middle_east": "Middle East",
  "north_africa": "North Africa",
  "latam": "Latin America",
}

# Stable display/iteration order for stages and emails.
BUCKET_ORDER = ("middle_east", "north_africa", "latam")
```

API:

```python
def buckets_for_country(cc: str | None, active: Iterable[str] = BUCKET_ORDER) -> list[str]:
    """Return every ACTIVE bucket whose FIPS set contains cc, in BUCKET_ORDER.
    Empty list => the article is not covered (dropped at discovery)."""
```

- `Sudan (SU)` is intentionally **not** in any bucket (dropped per D2).
- `region_for_country` in `gdelt.py` is retained only for the coarse legacy
  `region` column; the new bucketing logic lives in `regions.py`.

## 4. Schema Migration — `geoecon/state/migrations/002_region_split.sql` (new)

Applied automatically on next run by `db.apply_migrations` (file-per-migration,
tracked in `schema_migrations`). All statements are idempotent-safe / guarded.

1. **Country code on articles & clusters** (enables region bucketing downstream;
   the existing coarse `region` column is left in place and untouched):
   ```sql
   ALTER TABLE articles ADD COLUMN IF NOT EXISTS country_fips TEXT;
   ALTER TABLE clusters ADD COLUMN IF NOT EXISTS country_fips TEXT;
   ```
2. **Per-region newsletters** (replace one-per-run with one-per-region-per-run):
   ```sql
   ALTER TABLE newsletters DROP CONSTRAINT IF EXISTS newsletters_run_id_key;
   ALTER TABLE newsletters ADD COLUMN IF NOT EXISTS bucket TEXT;
   ALTER TABLE newsletters ADD CONSTRAINT newsletters_run_bucket_key
       UNIQUE (run_id, bucket);
   ```
   Existing rows have `bucket = NULL`; new rows always set a non-null bucket.
   (Postgres treats NULLs as distinct in UNIQUE, so legacy rows are unaffected.)
3. **Region executive summaries** (new table; per-run, per-bucket):
   ```sql
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
   ```
4. **New stage name** in the `stage_runs` CHECK:
   ```sql
   ALTER TABLE stage_runs DROP CONSTRAINT IF EXISTS stage_runs_stage_name_check;
   ALTER TABLE stage_runs ADD CONSTRAINT stage_runs_stage_name_check
       CHECK (stage_name IN
           ('discovery','extraction','rss','cluster','summary','render','send'));
   ```

`schema.sql` (the canonical reference dump) is updated to match for fresh installs.

## 5. Pipeline Stages

New order: **discovery → extraction → cluster → summary → render → send**.

### 5.1 Discovery (`stages/discovery.py`) — modified
- Replace `region = gdelt.region_for_country(rec.country)` + `region in cfg.regions`
  with `buckets = regions.buckets_for_country(rec.country, cfg.buckets)`; keep the
  article iff `buckets` is non-empty.
- Persist `country_fips = rec.country` on the article (via `insert_article`).
- Continue to set the coarse `region` (legacy column) for back-compat.
- Dedup by `url_hash` is unchanged — a story mapping to two buckets is still one
  article row; the fan-out into two emails happens at grouping time.

### 5.2 Cluster (`stages/cluster.py`) — modified
- Carry `art.country_fips` onto the cluster (alongside the existing `region`).

### 5.3 Summary (`stages/summary.py`) — NEW
- Input: `cfg`, `repo`, `run_id`, `client`.
- Fetch OK records with their cluster country: `repo.records_for_buckets(run_id)`
  → list of `(SynthesisRecord, country_fips, [sources])`.
- Group by bucket via `regions.buckets_for_country(country_fips, cfg.buckets)`.
- For each **non-empty** bucket: build the summary messages from that bucket's
  records, call `client.complete(cfg.synthesis_model, messages, temperature=0,
  seed=12345, max_tokens=2048)`, parse strict JSON `{lede, takeaways}`, and
  `repo.upsert_region_summary(run_id, bucket, lede, takeaways, model, record_count)`.
- Parse/guard: reuse a `parse_record`-style JSON extractor. On parse failure or
  missing keys, persist an empty summary (`lede=NULL, takeaways=[]`) so render
  degrades gracefully (story cards still ship without a summary block). Log it.
- Empty buckets: no row written.
- Returns counts: `{summaries_total, summaries_ok, summaries_empty}`.
- Resumable: upsert keyed `(run_id, bucket)`; re-running regenerates cleanly.

### 5.4 Render (`stages/render.py`) — modified (no LLM)
- For each bucket in `BUCKET_ORDER` that has records:
  - Pull that bucket's records (+ source links) and its `region_summaries` row.
  - Render `email.html.j2` and `archive.html.j2` with `bucket`, `bucket_display`,
    `summary` (lede + takeaways), `records`, `date_str`, `stats`.
  - Write archive to `geoecon-{date_str}-{bucket}.html`.
  - `repo.insert_newsletter(run_id, render_mode, archive_path, email_html,
    record_count, bucket=bucket)` (upsert on `(run_id, bucket)`).
- Returns `{newsletters: [{newsletter_id, bucket, record_count}, ...]}`.

### 5.5 Send (`stages/send.py` + orchestrator `_send`) — modified
- Fetch all newsletters for the run via `repo.newsletters_for_run(run_id)`.
- For each newsletter not already `sent`: subject
  `Geoeconomic Brief — {BUCKET_DISPLAY[bucket]} — {date_str}`; send to the shared
  recipient list; update send status per newsletter.
- After the send loop, if **any** newsletter sent, call `repo.mark_emitted(run_id)`
  once (per-cluster fingerprint logic unchanged — an Egypt story is marked once
  and is suppressed in both regions on subsequent days).
- D5: if there are no newsletters (all regions empty), send nothing.
- `dry_run` short-circuits sending as today.

## 6. Source Links (D7)

- The grouping query returns, per record, the verified `source_url` + `domain`
  from the cluster's member article(s). Phase 1 = one article per cluster ⇒ one
  link; the field is a list for forward-compat with multi-article clusters.
- `email.html.j2` and `archive.html.j2` add a source line per story:
  `Source: <a href="{source_url}">{domain}</a> →` (email-safe inline styles).
- The LLM `synthesis_records.sources` field is **not** used for links (it is
  in-article attribution text, frequently not a URL, and may be hallucinated).

## 7. Executive Summary Prompt — `geoecon/prompts/region_summary.txt` (new)

Run deterministically (temperature 0, fixed seed) like synthesis. Strict-JSON
output. Draft system prompt:

```
You are the desk editor for a daily geoeconomic intelligence brief, writing the
executive summary that opens the {REGION} edition. Readers are analysts and
policymakers who will read the individual stories printed below your summary;
your job is to give them the day's throughline and the threads that matter,
grounded STRICTLY in the stories provided.

You receive the day's {REGION} stories — each with a headline, what-happened,
geoeconomic significance, and key figures. Synthesize them.

Output STRICT JSON only, no markdown, exactly:
{
  "lede": "<1-2 sentences naming the single dominant geoeconomic throughline of
           the day for this region, and why it matters>",
  "takeaways": ["<distinct thread>", "..."]
}

Rules:
- Ground everything ONLY in the provided stories. Never introduce a fact, figure,
  name, or event not present in them. Every figure or proper noun you cite must
  appear in the stories.
- Provide 3-5 takeaways when the material supports them; fewer if there are few
  stories. Each takeaway is one distinct thread (~25 words, one sentence), leading
  with the concrete development and its driving actor or figure.
- The lede must name a REAL common thread if one exists. If the stories do NOT
  share a throughline, say so plainly (e.g. "No single thread dominated; the day's
  {REGION} developments span unrelated fronts:") rather than manufacture a link.
- Geoeconomic lens only: trade, capital flows, energy, currency, sanctions, debt,
  investment, supply chains, fiscal/monetary policy. Omit purely political or
  human-interest angles unless they carry a geoeconomic consequence.
- Neutral, precise, non-sensational analyst register. No hype, no first person,
  no calls to action. English. Whole summary under ~180 words.

Return ONLY the JSON object.
```

`{REGION}` is interpolated with the bucket display name. The user message is the
bucket's stories formatted as `headline / what_happened / significance / figures`
blocks separated by `---`.

## 8. Repo Additions / Changes (`state/repo.py`)

- `insert_article(..., country_fips=None)` — persist the FIPS code.
- `insert_cluster(..., country_fips=None)` — carry it through.
- `records_for_buckets(run_id) -> list[(SynthesisRecord, country_fips, sources)]`
  — OK records joined to clusters (country) and the cluster's article(s)
  (`source_url`, `domain`). `sources = [{"url":..., "domain":...}]`.
- `upsert_region_summary(run_id, bucket, lede, takeaways, model, record_count)`.
- `get_region_summary(run_id, bucket) -> dict | None`.
- `insert_newsletter(..., bucket=None)` — upsert on `(run_id, bucket)`.
- `newsletters_for_run(run_id) -> list[dict]` (replaces single `get_newsletter`
  in the orchestrator; keep `get_newsletter` if still referenced by tests).
- `mark_emitted` unchanged.

Models: `Cluster` gains `country_fips`; a small carrier for grouped records (or
reuse a tuple) surfaces `country_fips` + `sources` to the summary/render stages.

## 9. Config (`config.py`)

- Add `buckets: tuple[str, ...]` (active buckets; default `BUCKET_ORDER` =
  `("middle_east","north_africa","latam")`), parsed from env `BUCKETS`.
- `regions` field retained for back-compat but no longer drives discovery; may be
  removed once nothing references it.
- `recipients` unchanged (single shared list — D3).

## 10. Orchestrator (`orchestrator.py`)

- `_default_stages` adds `summary` between `synthesis` and `render`; `summary`
  receives the LLM `client`; `render` no longer needs the client.
- `_send` iterates `newsletters_for_run`, sends each pending one with a
  per-region subject, then `mark_emitted` once if anything sent.
- Per-stage resume semantics preserved (each stage idempotent / upserting).

## 11. Testing (TDD)

- `test_regions.py` (new): bucket membership; Egypt in both; Sudan dropped;
  Israel/Turkey/Cyprus present; West Bank/Gaza kept; `buckets_for_country` order
  and `active` filtering.
- `test_summary.py` (new): grouping by bucket from country; empty-bucket skip;
  strict-JSON parse + guardrail fallback (empty summary on bad JSON); fake LLM
  client.
- `test_render.py` (modified): one newsletter row per non-empty bucket; summary
  block present; source link rendered from `source_url`; quiet region omitted.
- `test_send.py` / `test_send_aiosmtpd.py` (modified): multiple newsletters,
  per-region subjects, skip already-sent, mark_emitted-once.
- `test_discovery.py` (modified): `country_fips` persisted; new-country capture;
  multi-bucket country kept once.
- `test_orchestrator.py` (modified): resume across the new `summary` stage;
  fully-quiet-day sends nothing.
- `test_repo.py` (modified): new repo methods; newsletter upsert on (run_id,bucket).
- Update existing fixtures/signatures as needed.

## 12. Deployment Notes

- Migration `002_region_split.sql` is applied automatically on the next CronJob
  run against the live CERIT-SC Postgres. No manual DDL required.
- Legacy rows (old runs) keep `country_fips = NULL` / `newsletters.bucket = NULL`;
  only new runs populate them — no backfill needed.
- New-country coverage (Israel/Turkey/Cyprus) takes effect on the next discovery
  run; no re-fetch of historical GDELT data.

## 13. Out of Scope

- Per-region recipient lists (D3 keeps one shared list; trivial to add later).
- Multi-article-per-cluster handling (Phase 1 remains one-per-cluster; the source
  link field is a list to accommodate it later).
- Any change to extraction, the synthesis prompt, or the seen-store TTL logic.
