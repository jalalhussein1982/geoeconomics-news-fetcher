# Design: Geoeconomic Capture-Quality Overhaul

**Date:** 2026-06-21
**Status:** Approved (design); pending spec review
**Topic:** Fix what the daily pipeline captures — make it genuinely *geoeconomic*
(economy × geopolitics), balanced across regions, and de-duplicated.

---

## 1. Problem

The first three-region run (run_id 6) exposed three capture-quality failures,
all rooted in the discovery layer:

1. **Noise / wrong content.** The Middle East email carried ~1/3 off-topic items
   (US Fed/markets, Bitcoin ETFs, US mortgages) and wrong-region items (a
   Philippines drug-rehab story, Sri Lanka fuel). The relevance gate
   (`theme_intersection_ok`) only checks for GDELT economic-theme *tags*
   (`ECON_`/`WB_`/`EPU_`), which any generic economic story carries. Nothing
   requires the **economy × geopolitics** intersection the product is about.

2. **Region misattribution.** Region is assigned from the most-mentioned FIPS
   country code, which misfires on wire/roundup articles (a US story that
   name-drops Iran lands in Middle East).

3. **Regional imbalance.** Discovery keeps a single **global top-150** ranked by
   economic-theme density. The Gulf's dense English coverage wins nearly every
   slot, so Latin America is crowded out (2 of 40 final records in run 6).

4. **Duplicates.** Phase-1 clustering hashes exact title/URL only, so the same
   event from three outlets becomes three cards (Saudi non-oil exports ×3,
   Bitcoin ETF ×2).

Plus a cosmetic render bug: the `key_figures` line shows `$318 billionUSD`,
`%%`, and duplicated figures.

## 2. Goal & definition

**Geoeconomic** = economic news that is *affected by or affecting the
geopolitical scene*. The capture pipeline should keep that and drop generic
economics, while giving each region its own budget and merging duplicate events.

## 3. Locked decisions

| # | Decision | Choice |
|---|----------|--------|
| D1 | Relevance mechanism | **Hybrid**: relaxed GDELT-theme prefilter (recall) → LLM gate (precision + region). |
| D2 | Region budget | **Per-region quotas** (not one global cap). |
| D3 | Dedup | **Lexical near-dup merge** (no embeddings). |
| D4 | Gate strictness | **Moderate**: keep economy×geopolitics + nationally-significant economic policy/structural moves; drop generic markets/crypto/foreign-domestic/standalone macro. |
| D5 | Per-region size | **Tight digest, ~8–12 stories/region** (configurable, default 12). |
| D6 | Region resolution | Gate returns a **primary country** (from the in-scope list); mapped to buckets via `regions.py` (preserves Egypt → both Middle East + North Africa). |
| D7 | LatAm supply | Per-region quotas + relaxed prefilter widen supply; if GDELT genuinely surfaces few LatAm geoeconomic items, **broader sourcing is a follow-on**, not silent under-delivery. A diagnostic measures the per-region funnel. |

## 4. New pipeline flow

```
discovery (relaxed prefilter, NO global cap)
  → extraction
  → gate          (NEW: LLM relevance + primary-country, batched)
  → cluster       (CHANGED: filter to gated+bucketed, lexical dedup → multi-article clusters)
  → synthesis     (unchanged; runs per cluster)
  → summary       (unchanged; per-region exec summary — now over capped set)
  → render        (CHANGED: per-region rank & cap ~12, + key_figures fix)
  → send          (unchanged)
```

The **per-region cap is applied at grouping time** (summary + render), ranked by
gate score, so an Egypt event can rank top-12 in Middle East yet fall outside
North Africa's top-12 — handled per bucket independently. A shared helper
guarantees summary and render cap identically.

## 5. Components

### 5.1 Relaxed prefilter — `geoecon/gdelt.py`
- Add a **geopolitics theme set** `GEOPOL_THEME_PREFIXES` covering conflict,
  sanctions, energy security, diplomacy, trade disputes, maritime/borders,
  military, political instability. Concrete starting candidates (to be confirmed
  and trimmed against live `articles.gkg_themes` per §8):
  `SANCTIONS`, `MILITARY`, `ARMEDCONFLICT`, `BLOCKADE`, `MARITIME`, `BORDER`,
  `ENERGY`, `EPU_CATS_GEOPOLITICAL`, `EPU_CATS_TRADE_POLICY`,
  `EPU_CATS_NATIONAL_SECURITY`, `EPU_CATS_SOVEREIGN_DEBT_CURRENCY_CRISES`,
  `WB_2433_CONFLICT_AND_FRAGILITY`, `CRISISLEX_`, `TAX_TERROR`. The §8 diagnostic
  finalizes the set from real theme frequencies before the gate is tuned.
- New `prefilter_ok(themes)`: pass if **(≥1 economic theme)** OR
  **(≥1 geopolitics theme AND ≥1 economic theme)**. This raises recall vs.
  today's "≥2 economic + ≥1 core"; the LLM gate restores precision.
- `theme_salience` stays as a cheap tiebreaker only (final ranking is the gate
  score). Keep `region_for_country` for the legacy coarse tag.

### 5.2 Discovery — `geoecon/stages/discovery.py`
- Replace `theme_intersection_ok` with `prefilter_ok`.
- **Remove the global 150 cap.** Keep a generous per-region safety ceiling to
  bound extraction + gate cost: collect up to `cfg.discovery_ceiling` (default
  120) **per active bucket**, ranked by `theme_salience` as a cheap proxy
  (final selection is the gate). Persist `country_fips` (GKG country) as today.

### 5.3 Geoeconomic gate — `geoecon/stages/gate.py` (NEW), prompt `geoecon/prompts/geoecon_gate.txt` (NEW)
- Runs after extraction; receives the LLM client.
- Input per candidate: `title`, lead (~first 400 chars of `body` or the title if
  headline-only), `gkg_themes`, GKG-detected country.
- **Batched**: ~`cfg.gate_batch_size` (default 15) candidates per LLM call →
  ~15–25 calls/run. Deterministic (`temperature=0, seed=12345`).
- Output: strict-JSON **array**, one verdict per item:
  `{"i": <index>, "geoeconomic": bool, "primary_country": <name from the
  in-scope list | "other">, "score": 0.0–1.0, "reason": <≤15 words>}`.
- Prompt encodes the **Moderate** rubric (D4) and lists the in-scope countries
  grouped by region, instructing the model to choose `primary_country` from that
  list or `"other"`.
- Resolution: `regions.country_to_fips(primary_country)` → FIPS → buckets. If
  `geoeconomic` is false, or `primary_country` resolves to no active bucket
  (`"other"`/unknown), the article is **not selected** (dropped from clustering).
  Otherwise the gate **overwrites `articles.country_fips`** with the resolved
  FIPS (correcting misattribution) and records the verdict.
- Persist per article: `gate_geoeconomic`, `gate_score`, `gate_country`,
  `gate_reason` (§6). The gate does not delete rows — it annotates, for audit.
- **Fail-open** on unparseable output after one retry: mark the batch's items
  `gate_geoeconomic=true, gate_score=0.0, gate_reason="gate_unparsed"` so real
  content is never lost to an LLM hiccup (they sort last and are usually capped
  out). Log the count; surface `gate_unparsed` in run metrics.
- Returns metrics: `{gate_total, gate_kept, gate_dropped, gate_unparsed}`.

### 5.4 Cluster — `geoecon/stages/cluster.py` (CHANGED)
- **Candidate filter**: `gate_geoeconomic=true` AND `country_fips` resolves to ≥1
  active bucket AND `fetch_status != 'failed'`.
- **Lexical near-dup merge** → multi-article clusters:
  - Normalize each title: lowercase, strip non-alphanumerics, token set (drop
    1–2 char tokens).
  - Two candidates are the same event iff same `country_fips` AND
    `Jaccard(title_tokens) ≥ cfg.dedup_jaccard` (default 0.6). Titleless
    (headline-only with no extracted title) candidates stand alone.
  - Union-find groups them; each group is one cluster with all member articles.
  - `cluster.country_fips` = the highest-`gate_score` member's country.
  - `cluster.gate_score` = max member `gate_score` (event score for ranking).
- Synthesis already iterates `articles_in_cluster`, and render already collects
  multiple source links per cluster — multi-article clusters flow through
  unchanged, producing one card with multiple sources.

### 5.5 Per-region rank & cap — shared helper (CHANGED in summary + render)
- `regions.top_per_bucket(items, score_of, n)`: group by bucket
  (`group_by_bucket`), sort each bucket's items by `score_of(item)` desc, keep
  the top `n` (`cfg.region_cap`, default 12). Returns `{bucket: [items]}`.
- `records_for_buckets` also returns each record's `cluster.gate_score`.
- **Summary** and **render** both call `top_per_bucket` so the executive summary
  describes exactly the stories shown. Stories render in score order.

### 5.6 Render `key_figures` fix — `geoecon/render/templates/*.j2`
- Synthesis stores figures as `{value, unit, source_span}` (eval shape). The
  templates currently render `{{ k.value }}{{ k.unit }} {{ k.gloss_en }}` →
  `$318 billionUSD`, `%%`, blank gloss. Fix:
  - Render `{{ k.value }}` then `{{ k.unit }}` **only when** `unit` is non-empty
    and not already a suffix of `value` (avoids `$318 billion` + `USD` →
    `$318 billionUSD` and `1.10%` + `%` → `1.10%%`).
  - Join distinct figures with ` · `; de-duplicate identical `value+unit` pairs
    (the `$318 billionUSD · $318 billionUSD` repeat).
  - Drop the `gloss_en` reference (not produced).

## 6. Schema — migration `003_geoecon_gate.sql` (NEW) + `schema.sql` sync
```sql
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
Idempotent; applied automatically by `db.apply_migrations`. `001`/`002` are not
modified. Legacy rows keep `NULL` gate fields.

## 7. Config — `geoecon/config.py`
- `gate_model: str` (default `deepseek-v3.2`) — gate LLM, swappable.
- `gate_batch_size: int` (default 15).
- `region_cap: int` (default 12).
- `discovery_ceiling: int` (default 120) — per-bucket pre-gate safety cap.
- `dedup_jaccard: float` (default 0.6).
All parsed from env with these defaults.

## 8. Diagnostic & tuning (first implementation step)
Before/while tuning, capture the per-region funnel for a run against the live DB:
`discovered → gated-kept → clustered (post-dedup) → shown` per bucket, plus the
distribution of `gate_reason` for drops and the top `gkg_themes` among kept vs
dropped items. This (a) confirms whether LatAm is supply-limited vs
allocation-limited (D7), and (b) grounds the `GEOPOL_THEME_PREFIXES` set in real
theme codes rather than guesses. A small read-only SQL script under `deploy/` or
a `--report` path; run via the existing skirit/kubectl access.

## 9. Orchestrator — `geoecon/orchestrator.py`
- Insert `gate` between `extraction` and `cluster`; it receives `client`.
- Stage list: discovery → extraction → gate → cluster → synthesis → summary →
  render → send.
- Per-stage resume preserved (gate upserts gate fields idempotently; cluster is
  rebuilt from gated articles each run).

## 10. Testing (TDD)
- `gdelt`: `prefilter_ok` truth table (economic-only passes; geopol+economic
  passes; pure non-economic fails; geopol-only fails).
- `gate` (fake LLM client): batched array parse; drop on `geoeconomic:false`;
  `primary_country` → bucket resolution incl. **Egypt → both**; `"other"` →
  dropped; malformed-JSON **fail-open** path; `country_fips` overwrite.
- `regions`: `country_to_fips` (names + common aliases: UAE, Türkiye, etc.) and
  `top_per_bucket` ranking/cap (incl. an Egypt item counted in both buckets,
  capped independently).
- `cluster`: lexical dedup groups near-dup titles into one multi-article cluster;
  distinct events stay separate; titleless stands alone; non-gated excluded;
  `cluster.country_fips`/`gate_score` from primary member.
- `render`: `key_figures` renders `$318 billion`, `1.10%` (no `%%`), de-duped;
  per-region cap applied; existing summary/source-link behavior intact.
- `repo` (DB-gated): gate-field persistence + `records_for_buckets` returns
  `gate_score`.
- `orchestrator`: `gate` present and ordered before `cluster`.

## 11. Out of scope (follow-ons)
- Embedding/semantic clustering (Phase-2 BGE-M3) — lexical dedup now.
- Broader LatAm **sourcing** (extra feeds, non-English emphasis) — revisit only
  if §8 shows supply, not allocation, is the bottleneck.
- A deeper relevance model or per-source reputation weighting.
- Changing the synthesis prompt or seen-store TTL.
