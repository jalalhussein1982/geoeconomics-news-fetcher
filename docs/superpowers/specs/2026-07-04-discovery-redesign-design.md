# Discovery Layer Redesign — Four-Layer Query-Driven Ingestion

**Date:** 2026-07-04
**Status:** Approved (design); Phase 0 probe RUN — TheNewsAPI **NO-GO**, Google
News RSS promoted to Layer-1 spine (see Phase-0 results addendum at bottom)
**Scope:** Replaces the discovery stages only. Extract → gate → cluster → rank → synth → send are untouched.

## Problem

Current discovery is supply-driven: a GDELT raw-GKG firehose (96 zip files/run,
hundreds of MB in memory, blunt `ECON_/WB_/EPU_` theme prefilter, noisy
most-mentioned-FIPS country attribution) plus a 66-source curated registry of
which 29 are `needs_review`/skipped and 34 are brittle no-JS scrapes. There is
no way to *ask* for news ("everything geoeconomic in Egypt in the last 24h"),
so coverage gaps can only be fixed by adding more brittle sources. Result:
poor recall, uneven per-country coverage, high maintenance, ~2h runs.

## Goal

Demand-driven discovery: explicit per-country, per-language, per-topic queries
across redundant recall layers, with precision still owned by the existing LLM
gate. Time window unchanged: `WINDOW_HOURS` (default 24h) up to run start.

## Decisions made (with user)

| Decision | Choice |
|---|---|
| Budget | ≤ ~$50/mo on top of existing Firecrawl |
| Backbone vendor | TheNewsAPI (account created; free tier for probe, Basic $19/mo for production). World News API was first choice but signup is broken; GNews Essential (€39.99/mo annual) is the named fallback — its free tier has a 12h article delay and is production-unusable. |
| Country coverage | Explicit full list, all ~33 countries (MENA + North Africa + LATAM); ranking decides who makes the brief |
| Existing layers | Keep both, demoted: GDELT moves to DOC 2.0 API as corroboration net; registry shrinks to tier-0 primaries |
| Topic definition | Two-family keyword vocabulary (below); recall in queries, precision in the gate |

### Vendor research summary (July 2026)

Disqualified: NewsAPI.org (24h delay, $449 floor), Newsdata.io (12h delay,
$199 floor), APITube (12h delay, $99 floor), Event Registry/NewsAPI.ai ($90
floor), Perigon ($250 floor), NewsCatcher (~600 articles/mo at $50).
Viable at budget: **TheNewsAPI** ($19–49, real-time all tiers, ar/es/pt/en/tr/he/fr,
boolean search, snippet-only — fine because our extraction stage fetches
bodies anyway), **GNews Essential** (real-time, full content, but no SA/AE/CL
country codes and ~€50/mo monthly), World News API ($39, full text — blocked
on signup). Free layers: Google News RSS (per-country/language editions +
`when:1d`, ~100 items/feed cap, unofficial), GDELT DOC 2.0 (free,
`timespan`/`sourcecountry:`/`sourcelang:`/themes, ~1 req/5s pacing).

## Architecture

Four independent recall layers, all converging into the existing `articles`
table via `repo.insert_article` and `url_hash` normalization dedup:

```
Layer 1  stages/discovery_search  TheNewsAPI adapter        discovery_source='search:thenewsapi'
Layer 2  stages/discovery_search  Google News RSS adapter   discovery_source='search:googlenews'
Layer 3  stages/discovery         GDELT DOC 2.0 API         discovery_source='gdelt'
Layer 4  stages/discovery_registry Tier-0 primaries only    discovery_source='registry'
```

Failure isolation: each layer catches its own errors, logs WARNING + telemetry
counts, and the run continues on the remaining layers.

Firecrawl `/search` (`sources=news`, `tbs=qdr:d`, ~2 credits/query) is a
reserved gap-filler for countries that come back thin — not wired in v1.

### New: `config/countries.yaml`

The coverage definition. One entry per country (~33): FIPS code, bucket(s)
(Egypt maps to both `middle_east` and `north_africa`), language(s),
TheNewsAPI locale, Google News edition (`hl`/`gl`/`ceid`), GDELT
`sourcecountry` name. Regions: MENA (Gulf, Levant, Iraq, Iran, Turkey,
Israel, Yemen), North Africa (Egypt, Libya, Tunisia, Algeria, Morocco, Sudan,
Mauritania), LATAM (Mexico, Central/South America, Caribbean majors).

### New: `config/keywords.yaml`

Two families × seven languages (ar, es, pt, en, tr, he, **fr** — Maghreb
outlets publish heavily in French), shared by Layers 1–2.
Editable without code changes.

- **Family 1 — economy core:** economy, central bank, inflation, GDP,
  interest rate, currency, trade, exports, debt, IMF, investment, budget —
  translated per language.
- **Family 2 — geopolitics×economy intersection** (the "don't miss economic
  news framed politically" insurance): sanctions, export controls, embargo,
  asset freeze, tariffs, trade war/agreement, boycott, OPEC/oil-gas deals,
  pipelines, energy security, IMF bailout, sovereign debt
  restructuring/default, currency swap, de-dollarization, BRICS,
  nationalization/expropriation, sovereign wealth fund deals,
  port/infrastructure concessions, remittances, trade-route closures —
  translated per language.

Layer 3 covers the same intersection language-independently via GDELT themes
(`SANCTIONS`, `ECON_TRADE_DISPUTE`, `ECON_BOYCOTT`, `EPU_*`, …).

Queries are recall-biased by design; over-capture is cheap (a few extra gate
calls), under-capture is unrecoverable. Precision is the gate's job.

### New: `geoecon/search/` package

`SearchProvider` interface:

```
search(query: str, country: CountrySpec, lang: str, window: (start, end))
    -> list[SearchItem]   # url, title, ts, snippet, source_name
```

Adapters: `thenewsapi.py`, `googlenews_rss.py` (~50 lines each; future:
`gnews.py`, `worldnews.py`). Vendor swap = config change, not a rewrite.
Google News adapter must handle the news.google.com redirect-URL decoding to
publisher URLs; on decode failure keep the redirect URL (extraction follows
redirects anyway) and count it in telemetry.

New stage `stages/discovery_search.py`: fan out country × family per
provider, window-filter (`published_after` / `when:1d`), per-layer
per-country insert cap (default 25), in-memory `seen_hashes` intra-stage
dedup, insert as `fetch_status='pending'` with region/country provenance from
`countries.yaml`.

### Rewrite: `stages/discovery.py` (GDELT)

DOC 2.0 API queries (per country: `sourcecountry:X` + theme filters,
`timespan` = window, JSON artlist) replace the masterfilelist + GKG zip
download path. ~1 request / 5s pacing, backoff on throttle. Kills the
multi-hundred-MB memory spike. Country attribution from `sourcecountry`
(source-based) rather than most-mentioned-FIPS — expected to also reduce
misattribution (e.g. the US-story-in-ME-brief incident). Old GKG path kept
behind `GDELT_MODE=gkg` for one release, then deleted.

### Trim: `config/sources.yaml`

Tier-0 primaries + actor sources (IMF/WB/etc.) stay (~40 entries). Tier-1/2
generic news outlets get `status: retired` (kept in file for history; skipped
at runtime like `dead`). The curated-preference ranking nudge is rewired to
match **by domain** (curated domain list derived from the retired entries) so
the editorial preference applies to search-layer items from those same
outlets.

## Volume & window

- TheNewsAPI: 33 countries × 2 families ≈ 66–130 req/day (Basic 2,500 ✓;
  free-tier probe uses a ~10-country subset under 100 req/day).
- Google News RSS: ≈66 feeds/day, paced, stable UA.
- GDELT DOC: ≈40 queries/day, paced 5s.
- `WINDOW_HOURS` enforced identically on all layers; null-`ts` items kept
  (recall bias), as today.
- Per-layer per-country cap (config `SEARCH_COUNTRY_CAP`, default 25)
  prevents any single query flooding the funnel; downstream caps
  (`REGION_CAP`, `COUNTRY_CAP`) unchanged.

## Error handling

- Per-layer try/except with WARNING + per-layer telemetry (queries run, items
  returned, items inserted, dedup collisions, failures) added to run funnel
  metrics.
- HTTP: existing retry/timeout conventions from `fetchers/`; GDELT backoff on
  429/throttle; Google News decode failures counted, not fatal.
- Missing `THENEWSAPI_KEY`: Layer 1 skips with a loud WARNING (recall floor
  hole, mirroring the existing tier-1 wire guard), run continues.

## Testing

- Unit tests per adapter with recorded fixture responses (JSON/RSS), matching
  the existing `tests/unit/test_fetchers.py` pattern.
- Stage test for `discovery_search` (fan-out, window filter, cap, dedup) with
  a fake provider.
- GDELT DOC: fixture-based parse test + `GDELT_MODE` flag test.
- Probe + live-run validation are the integration tests (below).

## Rollout phases

- **Phase 0 — probe (go/no-go, before any pipeline code):** throwaway
  `tools/search_probe.py` runs Family 1+2 queries for
  EG/SA/AE/TR/IQ/BR/MX/AR/CL/CO on TheNewsAPI free tier + Google News RSS;
  reports per-country article volume, source diversity, publish-ts freshness.
  Decides TheNewsAPI vs GNews-fallback. **Blocked on: user provides
  `THENEWSAPI_KEY`.**
- **Phase 1:** `geoecon/search/` + `discovery_search` behind
  `SEARCH_DISCOVERY` flag (default off) + `countries.yaml`/`keywords.yaml` +
  unit tests.
- **Phase 2:** GDELT DOC rewrite behind `GDELT_MODE=docapi|gkg`.
- **Phase 3:** registry trim + curated-domain matching.
- **Phase 4:** deploy flags-on to CERIT-SC, validate one live run
  (per-layer funnel telemetry, brief quality), flip defaults, upgrade
  TheNewsAPI to Basic ($19/mo).

Expected side benefit: no GKG downloads and ~25 fewer scrape sources should
cut the ~2h run time substantially.

## Out of scope

- Second-order political stories with economic implications (LLM pass over
  political headlines) — future enhancement.
- Gate/cluster/rank/synth changes.
- GNews integration (fallback path only; decided by probe).
- Firecrawl `/search` gap-filler wiring.

## Phase-0 probe results (2026-07-04, `tools/search_probe.py`)

10 countries (EG SA AE TR IQ / BR MX AR CL CO) × Family 1+2, 31h window.
Raw data: `tools/search_probe_results.json`.

**TheNewsAPI — NO-GO as backbone.** Two disqualifying findings:

1. **`locale` filter is ignored** (free tier at minimum): identical
   `meta.found` and identical top articles with `locale=mx` vs no locale
   (1141), and MX/AR/CL/CO all returned the same 1422/503 counts. No
   per-country filtering → country attribution would fall entirely on us.
2. **Arabic/Turkish index is near-empty:** ~250 Arabic articles per 31h in
   the ENTIRE index (all locales); keyword `اقتصاد` → 1 hit; Turkish 125/31h,
   top sources are tech blogs. Search relevance also poor (an
   `Egypt + (economy|inflation|debt)` query returned a US July-4th piece).
   Paying $19/mo does not fix an empty index. es/pt volume exists
   (1141–1422/31h) but is Spain-mixed and un-filterable per finding 1.

**Google News RSS — promoted to Layer-1 spine.** Every country hit the
100-item feed cap on Family 1; 35–83 unique sources per feed; strong
freshness (fresh-under-6h per country, both families: MX 117, AR 113, CO 113,
BR 103, EG 65, SA 66, IQ 66, AE 53, TR 20). Arabic editions work well.
**Known implementation cost:** feed links are `news.google.com/rss/articles/
CBMi...` encoded URLs; a plain GET lands on consent.google.com (verified).
The adapter must decode them (googlenewsdecoder-style batchexecute approach);
fallback for undecodable links: Firecrawl scrape of the redirect URL.
Telemetry guard: if GN returns ~0 across many countries, loud recall-floor
WARNING (endpoint is unofficial).

**GDELT DOC 2.0 — probe inconclusive from dev IP** (429 penalty box after
probe burst; needs ≥5s spacing *and* cooldown). Re-probe from cluster IP
during Phase 2; role (corroboration net) unchanged.

**Revised layer table:**

```
Layer 1  Google News RSS      spine (free)         search:googlenews
Layer 2  <paid slot: EMPTY>   optional, deferred   — GNews census may fill it
Layer 3  GDELT DOC 2.0        corroboration net    gdelt
Layer 4  Registry tier-0      primaries            registry
+ Firecrawl /search           gap-filler (bench)
```

The paid backbone slot stays open behind the `SearchProvider` interface.
Budget spent: $0/mo.

**GNews census (2026-07-04, `tools/gnews_census_results.json`) — NO-GO.**
Free-tier census, 22 requests:

1. **Unsupported countries fail silently to unfiltered results:** SA, AE, CL,
   IQ are not in GNews's country list; `country=sa|ae|iq` quietly returned
   the identical language-wide result set (total=3 = `lang=ar` no-country),
   `country=cl` = 101 = `lang=es` no-country. Silent no-filter fallback on
   Saudi/UAE/Iraq is structurally disqualifying for a MENA pipeline.
2. **Visible index is microscopic:** language-wide Arabic Family-1 total = 3
   articles/31h; Mexico = 7; Argentina = 2; Colombia = 2 (vs Google News
   hitting the 100-item cap everywhere). Caveat: the free tier may search a
   reduced source pool vs paid — but we cannot verify paid depth without
   paying, and finding 1 stands regardless of tier.
3. 12h delay confirmed empirically (min article age 12.1–25.4h in every
   query).

**Final Phase-0 outcome:** both candidate paid backbones failed on evidence.
Layer-2 paid slot stays empty; proceed to Phase 1 with Google News RSS spine
+ GDELT DOC + tier-0 registry + Firecrawl gap-filler. Revisit a paid layer
only if live-run telemetry shows per-country recall gaps (World News API
remains the best-researched candidate if its signup gets fixed).
