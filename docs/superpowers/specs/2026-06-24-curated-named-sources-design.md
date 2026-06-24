# Curated named sources + economy-first preference — design

**Date:** 2026-06-24
**Status:** Draft for review
**Author:** pipeline work (geoeconomics news fetcher)

## 1. Goal

Add a fixed set of user-specified news outlets as **first-class curated discovery
sources**, fetched on every run **in addition to** the existing GDELT search and
the current registry sources. The existing news-fetcher criteria (GDELT discovery,
the geoeconomic gate, clustering, synthesis, render, send) stay the same. Two small,
explicitly-requested ranking refinements are added behind a reversible flag:

1. **Economy-first, politics-only-if-economic** ordering (request Q1).
2. A **mild curated-source preference** in ranking (request Q3).

The requested outlets:

- **MENA:** Al Jazeera, Reuters, Arab News.
- **LATAM:** Milenio (MX), La Nación (AR), Clarín (AR), La Izquierda Diario (AR),
  La Razón (BO), Revista Anfibia (AR), Viento Sur, El País América, El Universo (EC),
  Seúl (AR), La Política Online (AR).

## 2. Non-goals

- No change to the GDELT discovery stage, the gate prompt/criteria, clustering,
  synthesis, rendering, or sending.
- No headless browser / JS rendering (still unavailable in this environment).
- No gate-prompt redesign and no new per-article "category" column — the
  economy-vs-politics signal is derived from **which feed an item came from**, not
  from a new LLM classification.

## 3. Background (current architecture)

- Discovery runs in two stages: GDELT firehose (`stages/discovery.py`) then the
  registry-driven stage (`stages/discovery_registry.py`), which reads
  `config/sources.yaml` and dispatches each source to an rss/sitemap/scrape/api
  fetcher (`geoecon/fetchers/`).
- A source only fetches if `is_fetchable` — i.e. `fetch_type ∉ {unverified, dead}`
  **and** `status != needs_review` (`registry.py:48`). JS-rendered/no-feed sources
  are parked as `needs_review` and skipped.
- The gate (`stages/gate.py` + `prompts/geoecon_gate.txt`) is a binary keep/drop
  with a 0–1 `score`. It **keeps** "economy × geopolitics" and nationally-significant
  economic policy and **drops** generic markets and pure politics. So "politics
  related to economy" is exactly what survives a general feed; pure politics is
  already dropped.
- Clustering aggregates per-cluster provenance (`cluster.py:143-155`:
  `tier_min`, `has_primary`, `distinct_state_alignments`, `source_count`,
  `corroboration_score`) and stores them on `clusters`.
- Ranking (`ranking.make_score_of`) sorts clusters by `rank_score` (the cluster's
  `gate_score` by default) and demotes US/global market roundups by a large penalty.
  The record tuple is `(SynthesisRecord, country_fips, source_links, rank_score)`,
  built in `repo.records_for_buckets`.

## 4. Feed-probe findings (empirical, 2026-06-24)

Most requested outlets expose a usable RSS/sitemap feed and go live immediately.
Two have no feed and are hard bot-gated (even a non-JS scrape returns a challenge
page) — they are parked. Four serve valid RSS but reject a non-browser User-Agent.

| Outlet | Bucket | country_fips | Mechanism | Economy feed | Notes |
|---|---|---|---|---|---|
| Al Jazeera English | middle_east | — | rss `…/xml/rss/all.xml` | none (site-wide only) | static HTML; gate filters |
| Arab News | middle_east | — | rss `…/rss.xml` | rss `…/cat/4/rss.xml` | state_linked SA |
| Reuters | middle_east | — | **none** | — | **park** — DataDome bot-gate, needs headless |
| Milenio | latam | MX | sitemap (no RSS) | — | google-news + articles sitemap indexes |
| La Nación | latam | AR | rss (Arc) | rss `…/category/economia/…` | — |
| Clarín | latam | AR | rss `/rss/lo-ultimo/` | rss `/rss/economia/` | **needs browser UA** |
| La Izquierda Diario | latam | AR | **none** | — | **park** — Cloudflare JS challenge |
| La Razón | latam | BL | rss `/feed/` (WP) | try `/economia/feed/` (verify) | **needs UA**; `<link>` points to internal IP → rewrite |
| Revista Anfibia | latam | AR | rss `/feed/` (WP) | n/a (magazine) | low geoecon yield (accepted) |
| Viento Sur | latam | — | rss `/feed/` (WP) | n/a | low yield (accepted) |
| El País América | latam | — | rss MRSS `…/section/america/portada` | rss `…/section/economia/portada` | **needs UA** |
| El Universo | latam | EC | rss (Arc) | economia variant (verify) | — |
| Seúl | latam | AR | rss `/feed/` (WP) | n/a (niche) | **needs UA**; low yield (accepted) |
| La Política Online | latam | AR | rss `…/files/rss/economia.xml` | (economia is the feed) | — |

## 5. Design

The work splits into two independently-landable units. **Unit A changes no
criteria** and delivers the core request. **Unit B** is the requested ranking
refinement, flag-gated and reversible.

### Unit A — Curated sources + fetcher hardening (no criteria change)

**A1. Source entries in `config/sources.yaml`.**
Add the outlets as Tier-2 registry sources (Reuters stays Tier-1 wire). For each
outlet that exposes **both** an economy-section feed and a general feed, register
**both** as separate `Source` entries:

- the **economy-section feed** → high-signal economy content ("economy first");
- the **general feed** → the gate keeps only economy×geopolitics from it, i.e.
  "politics related to economy" ("second").

Outlets with only a general feed (the WordPress ones, Milenio via sitemap, Al
Jazeera English, El Universo) register that one feed; the gate filters it.
Reuters and La Izquierda Diario are added with `status: needs_review` and notes
documenting the headless requirement, so they auto-enable when headless lands.

Two **new optional `Source` fields** drive Unit B (default-safe, librarian-layer):

- `curated: bool = False` — set `true` on every outlet added here.
- `section: str | None = None` — set `"economy"` on the economy-section feeds;
  left null/general otherwise.

The registry loader (`registry.py` `_coerce`) defaults both fields so existing
entries are unaffected. `is_fetchable` semantics are unchanged.

Approximate entry count: ~18 `Source` entries (several outlets contribute an
economy + a general feed). Exact `feed_url`s per §4; the three "verify" feeds
(La Razón economia, El Universo economia, Milenio sitemap URL) are confirmed with
a one-shot fetch during implementation and registered `live` only if they return
valid XML — otherwise only the verified general feed is registered.

**A2. Fetcher User-Agent hardening (`geoecon/fetchers/`).**
Introduce a shared realistic browser User-Agent and apply it across fetchers:

- New `geoecon/fetchers/http.py` exporting `BROWSER_UA` (a current Chrome UA
  string) and a small `get(url) -> requests.Response` helper that sets it.
- `rss.py`: pass the UA to feedparser (`feedparser.parse(target, agent=BROWSER_UA)`),
  which today sends feedparser's default UA and gets blocked.
- `scrape.py` and `sitemap.py`: replace the `geoecon/1.0` bot UA with `BROWSER_UA`.

This unblocks Clarín, La Razón, El País, and Seúl. It is a transport fix only — no
change to what gets fetched or how items are judged.

**A3. La Razón link rewrite (per-source quirk).**
La Razón's feed `<link>` elements point at an internal IP (`https://10.0.137.12/`).
Add a minimal, source-scoped normalization in the rss fetcher: when a discovered
item URL's host is a private/IP host, rewrite it to the source's public host
(derived from `source.url`). Guarded so it only rewrites private-network hosts;
public URLs pass through untouched.

### Unit B — Economy-first + curated preference (flag-gated, reversible)

**B1. Cluster aggregates (`cluster.py` + migration 007).**
At cluster creation, alongside the existing provenance aggregates, compute two
booleans from member articles' `registry_id` resolved against the registry:

- `has_curated` — any member is from a `curated: true` source.
- `has_economy_section` — any member is from a `section: "economy"` source.

`cluster.py` loads the registry once (cheap YAML parse, already loaded by the
discovery stage) to build an `id → Source` map. Migration `007_curated_flags.sql`
adds `has_curated BOOLEAN DEFAULT FALSE` and `has_economy_section BOOLEAN DEFAULT
FALSE` to `clusters` (additive, mirrors migration 005). `repo.insert_cluster` gains
the two parameters; `schema.sql` is updated to match.

**B2. Carry into the rank tuple (`repo.records_for_buckets`).**
Extend the record tuple to
`(SynthesisRecord, country_fips, source_links, rank_score, has_curated,
has_economy_section)`. Appending trailing elements is backward-compatible: existing
consumers index `t[0]`/`t[3]`; `summary.py` and `render.py` are unaffected.

**B3. Apply mild boosts (`ranking.make_score_of`).**
```
base = rank_score
if market_demote and is_market_roundup(rec):
    return base - MARKET_PENALTY        # market demote wins; not rescued by curation
if curated_preference:
    if has_economy_section: base += economy_section_boost
    if has_curated:         base += curated_boost
return base
```
Market roundups are still demoted first — a curated market roundup is not rescued
above country-subject stories. Boosts are small relative to `gate_score ∈ [0,1]`,
so they reorder ties and near-ties rather than dominate.

**B4. Config knobs (`config.py`, env-toggleable like `market_demote`).**

| Field | Default | Env | Purpose |
|---|---|---|---|
| `curated_preference` | `True` | `CURATED_PREFERENCE` | master on/off for B |
| `curated_boost` | `0.15` | `CURATED_BOOST` | nudge for curated clusters |
| `economy_section_boost` | `0.10` | `ECONOMY_SECTION_BOOST` | nudge for economy-section clusters |

Boost defaults are provisional (like the corroboration weights) and tuned after a
live before/after brief diff. Setting `CURATED_PREFERENCE=false` reverts ranking to
exactly today's behavior.

## 6. Data flow

```
sources.yaml (+curated/+section)
   │  discovery_registry → articles (registry_id, tier, lang, …) [+ fetcher UA fix]
   ▼
gate (unchanged: drops pure politics + generic markets)
   ▼
cluster → clusters (+ has_curated, has_economy_section)
   ▼
records_for_buckets → tuple (+2 booleans)
   ▼
ranking.make_score_of → economy-section first, curated nudge, market demote intact
   ▼
render / summary / send (unchanged)
```

## 7. Files touched

- `config/sources.yaml` — new outlet entries + `curated`/`section` flags; meta notes.
- `geoecon/registry.py` — `Source.curated`, `Source.section`; `_coerce` defaults.
- `geoecon/fetchers/http.py` — **new**: `BROWSER_UA` + helper.
- `geoecon/fetchers/rss.py`, `scrape.py`, `sitemap.py` — use `BROWSER_UA`; rss link rewrite.
- `geoecon/state/migrations/007_curated_flags.sql` — **new**: two cluster columns.
- `schema.sql` — `clusters` columns to match.
- `geoecon/stages/cluster.py` — registry map + compute/persist the two booleans.
- `geoecon/state/repo.py` — `insert_cluster` params; `records_for_buckets` tuple.
- `geoecon/ranking.py` — apply boosts.
- `geoecon/config.py` — three new fields + env loading.
- Tests under `tests/` for each unit (see §8).

## 8. Testing

- **Fetchers:** `rss`/`scrape`/`sitemap` send `BROWSER_UA` (assert via injected
  fetch/parse); rss link-rewrite turns a private-IP `<link>` into the public host
  and leaves public URLs untouched.
- **Registry:** new YAML entries load; `curated`/`section` default correctly on
  existing entries; the curated outlets parse with expected fields; Reuters and
  La Izquierda load as `needs_review` and are not `is_fetchable`.
- **Cluster:** a cluster with a curated/economy-section member sets the booleans;
  one without leaves them `False`.
- **Ranking:** with `curated_preference` on, an economy-section cluster outranks an
  equal-`gate_score` general cluster, and a curated cluster outranks an equal
  non-curated one; a curated **market roundup** still sorts last; with the flag off,
  ordering is byte-identical to today.
- **YAML lint / full `pytest`** green before deploy.

## 9. Rollout

1. Land **Unit A** first (sources + UA fix). Validate with `python -m geoecon run
   --dry-run` (or `--limit`): the curated outlets appear as `discovery_source=registry`
   with non-empty article counts; the UA-gated four return items.
2. Land **Unit B**. Run a before/after brief diff (flag off vs on) on a live-ish run;
   tune `curated_boost` / `economy_section_boost` if the nudge over/under-shoots.
   Option to ship Unit B with `CURATED_PREFERENCE=false` initially (like
   `RANK_BY_CORROBORATION`) and flip after reviewing the diff.
3. Bump the deploy image and update `deploy/cronjob.yaml` env if any knob is set
   non-default.

## 10. Risks & mitigations

- **Verify-feeds may 404** (La Razón economia, El Universo economia, Milenio
  sitemap): registered `live` only after a one-shot fetch confirms valid XML;
  otherwise fall back to the verified general feed.
- **Volume up** from registering economy+general feeds: bounded by the existing
  window filter, url-hash dedup, and `discovery_ceiling`; gate filters the rest.
- **Boost mis-tuning:** flag-gated and reversible; defaults small; tuned on a real
  diff before being trusted.
- **Niche outlets (Seúl, Viento Sur, Anfibia)** low yield: accepted per request;
  the gate drops low-relevance items, so worst case is negligible contribution.
- **Reuters/La Izquierda stay dark** until headless: documented in their notes;
  no scrape workaround exists (hard bot-gate).
```
