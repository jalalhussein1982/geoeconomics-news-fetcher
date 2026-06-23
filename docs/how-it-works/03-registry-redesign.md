# 03 — The registry-driven multi-source redesign (Changes 1–4 + sitemap)

This is the single reference for the fetch redesign that replaced **GDELT-as-spine**
with a **tiered, registry-driven, multi-source** discovery layer, and added the
provenance + corroboration substrate the ranking will eventually use.

**Status (2026-06-23):** code-complete, deployed as image `20260623-2`, and
**validated in production** (run 16: GDELT 344 + registry 211 → gate → 36 records
shipped across 3 region briefs, all emails sent). The corroboration *ranking* ships
**dormant** (flag OFF). See [Follow-ups](#follow-ups) for what's left.

Read [01-fetching-news.md](01-fetching-news.md) and
[02-processing-to-report.md](02-processing-to-report.md) first — this doc layers on top.

---

## The shape of the change

GDELT is demoted from *the* discovery spine to a **firehose tripwire** that keeps
running. A second discovery stage, **`discovery_registry`**, fetches a curated
**source registry** and converges into the *same* `articles` table, before
extraction. Everything downstream (extraction → gate → cluster → …) is unchanged in
contract — it just sees more, better-provenanced articles.

```
discovery          (GDELT GKG firehose)            ┐
discovery_registry (registry: rss/sitemap/...)     ┘→ articles (pending, deduped) → extraction → gate → …
```

The two discovery stages run back-to-back (`geoecon/orchestrator.py`), both before
extraction. Idempotency on `(run_id, url_hash)` (now a *normalized* hash, Change 3)
collapses the same story surfaced by both sources into one row.

---

## Change 1 — registry-driven multi-strategy discovery

**Code:** `geoecon/registry.py`, `geoecon/fetchers/` (base, rss, scrape, sitemap, api),
`geoecon/stages/discovery_registry.py`. **Data:** `config/sources.yaml`.

- **`registry.py`** loads + validates `config/sources.yaml` into typed `Source`
  records (unique id; `tier ∈ {0,1,2}`; `fetch_type ∈ {rss,api,scrape,sitemap,dead,
  unverified}`). `default_path()` resolves the registry; in the container the path is
  pinned by `SOURCES_PATH` (see [Deploy](#deploy)).
- **`fetchers/`** — a dispatch package. Each `Fetcher.discover(source)` returns
  `DiscoveredItem{url, title|None, ts|None, source_lang|None}`; it finds URLs, it does
  *not* fetch bodies (extraction's job). All fetchers are injectable for no-network tests.
  - `rss.py` — feedparser; `ts` from `published_parsed`/`updated_parsed`.
  - `scrape.py` — lxml + cssselect on **served** HTML (no JS at runtime).
  - `sitemap.py` — see [the sitemap strategy](#the-sitemap-strategy).
  - `api.py` — per-source adapters keyed by id; **no speculative adapters** (none exist).
- **`discovery_registry.py`** — mirrors GDELT discovery's contract: window-filter
  (`ts` older than `now − WINDOW_HOURS` dropped; null `ts` kept for recall),
  per-source dispatch with try/except (one bad source never aborts the stage), bucket
  gate for country-keyed sources (cross-cutting wire/actor sources have `bucket=None`
  and always run — the LLM gate resolves their country), idempotent
  `insert_article(... fetch_status='pending')`.

**Tier-0 sources are NOT gate-bypassed** — precision is still the LLM gate's job.
(Gate-bypass was deliberately out of scope; it would be a later "Change 5".)

## The sitemap strategy

`fetchers/sitemap.py` is a third strategy alongside rss/scrape, added because XML
sitemaps are more common on government CMS than discoverable RSS, and JS-rendered
portals often still emit a static sitemap. It emits each `<url>` as a `DiscoveredItem`
with `ts = <lastmod>`, so the stage's existing window filter does the windowing for
free. **Entries without a parseable `<lastmod>` are dropped** — a non-windowable
sitemap would flood the gate with the whole archive. `<sitemapindex>` is followed into
child sitemaps (recent-first, capped). Titles are left None (extraction fills them).

---

## Change 2 — provenance through the schema (migration 004)

Additive, nullable columns only; **no behaviour change to what ships**.

- `articles`: `discovery_source` ('gdelt'|'registry'), `registry_id`, `source_tier`,
  `origin_class TEXT[]`, `ownership`, `state_alignment`. GDELT discovery sets
  `discovery_source='gdelt'` and leaves the registry columns null.
- `clusters`: aggregates over member articles — `tier_min`, `has_primary`,
  `distinct_state_alignments`, `source_count`. Computed in `cluster.py`,
  **populated but not used for ranking** (the substrate Change 4 reads). The cluster
  primary is still `max(gate_score)`.

## Change 3 — URL normalization for cross-source dedup

`geoecon/urlnorm.py`: `normalize_url` / `normalized_url_hash`, used by **both** discovery
stages for `url_hash`. Collapses known-equivalent variants (www/non-www, AMP, the
known-tracking param set, recoverable redirect wrappers) so GDELT and the registry
surfacing the same story dedup to one row — otherwise corroboration counts would be
corrupted. **Conservative**: only the known-tracking params are stripped; meaningful
params (`?id=`, `?p=`) are kept (over-normalizing merges distinct articles).

## Change 4 — corroboration scoring + ranking flag (migration 005)

`geoecon/corroboration.py`: `score(tier_min, has_primary, distinct_state_alignments,
source_count)` over the Change-2 aggregates. **Independence is keyed on
`distinct_state_alignments`, not raw `source_count`** — two co-state-aligned sources are
one witness, two cross-aligned are two. Weights are **named constants** with a
`# CALIBRATION TODO`; the *structure* is the deliverable, the *values* are provisional.

- `clusters.corroboration_score` is computed + persisted by `cluster.py`.
- **`Config.rank_by_corroboration`** (env `RANK_BY_CORROBORATION`) ships **OFF**. When ON,
  `repo.records_for_buckets(run_id, rank_by_corroboration=True)` returns
  `corroboration_score` as the ranking score instead of `gate_score`; summary/render
  only pass the flag (tuple shape unchanged). This is the single behavioural switch and
  it lands **tested-but-dormant** — flip it only after reviewing a real before/after diff.

---

## Handoff guardrails

- **Recall-floor warning** (`discovery_registry`): a held Tier-1 wire is a hole in the
  floor, so the stage logs a `recall-floor` WARNING naming each Tier-1 source not
  fetching. Currently fires for `wire_ap`, `wire_reuters_world`, `wire_efe`,
  `actor_china_mofcom`.
- **Coverage metrics** (run metrics): `core15_primaries_live` (6) / `core15_primaries_total`
  (15) / `tier1_live` (2) / `tier1_held` (4), plus `registry_discovered`,
  `registry_per_tier`, `registry_skipped_*`. The redesign's objective is now monitorable.
- **Strategy precedence** `rss/api > sitemap > scrape` — documented in `tools/README.md`
  and enforced in `registry_probe.classify` (order) + `merge` (`PRECEDENCE`), so
  re-resolution is deterministic, not last-writer-wins.

---

## Task 0 — registry mechanics resolution

The registry's *mechanics* layer (`fetch_type`/`feed_url`/`extract_hint`) ships
`unverified`; **Task 0** resolves it live. Tooling: `tools/registry_probe.py` (probe,
read-only) → `tools/registry_apply.py` (comment-preserving write-back) →
`config/sources.resolved.report.md`.

**Current resolution (2026-06-23):** 48 sources → **11 rss + 3 sitemap = 14 live**, 34
needs_review, 0 dead. **Core-15 primary coverage: 6/15** (UAE, Argentina, Brazil,
Colombia, Israel via BOI+TASE, Saudi via PIF). The other 34 are JS-rendered portals.

**Spike B (ruled out):** anonymous SharePoint REST (`_api/web/lists`) is hardened off on
all Gulf/MENA portals tested — **no API shortcut.** Headless rendering is the real lever
for the remaining 34 and is unavailable in this environment.

Classification policy is **conservative**: only a confirmed DNS failure is `dead`;
stale-path 404s / TLS-timeouts / robots blocks are `needs_review` ("verify from
production"), never retired. `wire_ap` is held manually (its only sitemap is `/hub/*`
section indexes, not articles).

---

## Deploy

- **Dockerfile** must `COPY config ./config` — the registry is data outside the package,
  so pip doesn't bundle it; without it `discovery_registry` raises `FileNotFoundError`.
- **CronJob** sets `SOURCES_PATH=/app/config/sources.yaml` (absolute, import-location
  independent) and `RANK_BY_CORROBORATION=false`.
- Images **must be built `--platform linux/amd64`** — the CERIT-SC nodes are amd64; an
  arm64 image fails with `no match for platform in manifest`.
- Migrations `004`/`005`/`006` apply on startup. **006** added `discovery_registry` to the
  `stage_runs.stage_name` CHECK — its absence caused the first live run to die with a
  silent `CheckViolation` (start_stage runs outside the per-stage try/except, and
  `main()` swallowed it). A no-DB test now asserts every orchestrator stage is in that
  CHECK, and `main()` logs the exception.

---

## Follow-ups

The single **live-env task** (Playwright-capable env with DB + LLM proxy + unblocked
GDELT), in order — also mirrored in agent memory `registry-coverage-state`:

1. Run the consolidated re-resolution (`tools/registry_probe.py`, applying the precedence
   rule) to **headless-resolve the 34 needs_review primaries**; run the API re-probe and
   sitemap probe in the same pass.
2. Re-run Task 0's report; confirm `core15_primaries_live` jumps from 6.
3. One full live pipeline run; **review the brief as a brief**.
4. Only then flip `RANK_BY_CORROBORATION` ON; review the before/after diff — confirm the
   clusters that sink below `REGION_CAP` are genuinely the weakly-sourced ones. **This
   diff review IS the calibration.**
5. Tune the named weights in `geoecon/corroboration.py` against that distribution; commit.

**Open editorial observations** (from the run-16 review, not bugs):
- **Egypt appears in both the Middle East and North Africa briefs** (`EG` maps to both
  buckets). Decide whether the dual-send is desirable.
- A dominant day's story (e.g. US–Iran) can take many of a region's slots via distinct
  angles; `region_cap` + dedup keep it from pure repetition, but topical-diversity
  tuning is a ranking question.
