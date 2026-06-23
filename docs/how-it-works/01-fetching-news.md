# 01 — Fetching the news (search & fetch)

This covers **Phase 1** of a run: **finding** candidate news and **fetching** its text.
It spans the first two pipeline stages — **discovery** and **extraction** — plus the GDELT
parsing helpers they rely on.

```
run starts
   │
   ▼
discovery   (geoecon/stages/discovery.py, geoecon/gdelt.py, geoecon/regions.py)   ── "SEARCH" (firehose)
   │   pull GDELT GKG files for the 24h window
   │   keep rows that carry an economic theme AND map to an active region bucket
   │   dedup, rank by theme density, cap per bucket → write `articles` rows (status = pending)
   ▼
discovery_registry  (geoecon/stages/discovery_registry.py, geoecon/registry.py, fetchers/)  ── "SEARCH" (curated)
   │   fetch the curated source registry (rss / sitemap / scrape) → same `articles` table
   │   (see 03-registry-redesign.md — this is the registry-driven multi-source layer)
   ▼
extraction  (geoecon/stages/extract.py)                                           ── "FETCH"
   │   for each pending article: download the page, extract title + body text
   │   success → extracted; paywall/empty/thin → headline_only; fetch error → failed
   ▼
articles table now holds bodies (or a flagged fallback) ready for the LLM gate + processing
```

These are the first three of the pipeline's nine stages (`discovery → discovery_registry →
extraction → gate → cluster → synthesis → summary → render → send`, see
`geoecon/orchestrator.py`). Everything from the **gate** onward is the processing half —
see **[02-processing-to-report.md](02-processing-to-report.md)**.

> **Two discovery sources, one table.** GDELT discovery (below) is the firehose tripwire;
> **`discovery_registry`** adds a curated, better-provenanced second source that converges
> into the *same* `articles` table before extraction. This file details GDELT discovery +
> extraction; the registry layer (fetchers, the source registry, provenance, URL-normalized
> dedup) is **[03-registry-redesign.md](03-registry-redesign.md)**.

**Key design choice: discovery and extraction are separate, best-effort operations.**
GDELT is the *discovery spine* — it tells us *what* exists and *where*. Extraction is a
distinct step that tries to get the *full text*. Nothing high-signal silently disappears: if a
page can't be fetched, the article is kept as a "headline-only" record (with its title) and
flagged for the stages downstream.

One more thing to keep in mind throughout Phase 1: **discovery filters for recall, not
precision.** It deliberately keeps too much. The expensive judgement — *is this actually
geoeconomic, and which country is it really about?* — is made later by the LLM **gate**
(the processing half — file 02). Phase 1's job is to cast a wide-but-cheap net and pull
the text in.

---

## Stage 1a — Discovery: searching GDELT

**File:** `geoecon/stages/discovery.py` (orchestration) + `geoecon/gdelt.py` (parsing/filters)
 + `geoecon/regions.py` (bucket map)
**Writes:** `articles` (one row per kept URL, `fetch_status='pending'`)
**Reads config:** `WINDOW_HOURS`, `BUCKETS`, `DISCOVERY_CEILING`, `LIMIT`

### What GDELT is, and why we use it

[GDELT](https://www.gdeltproject.org/) (the Global Database of Events, Language, and Tone)
continuously monitors world news in many languages and publishes machine-readable extracts.
We use its **GKG (Global Knowledge Graph)** stream, which tags every monitored article with
**themes**, **locations**, and **tone**. GDELT publishes a new GKG file **every 15 minutes**.

GDELT is the discovery layer because it is multilingual and geo/theme-tagged out of the box,
so we can find geoeconomic stories across the Middle East, North Africa, and Latin America in
Arabic, Spanish, Portuguese, French, etc. — without maintaining our own crawler or keyword
lexicon. GDELT's tagging is noisy; that noise is intentionally tolerated here and tightened
downstream by the LLM gate.

### Step 1 — Pull the GKG files for the window

`discovery.run()` computes the window `[now − WINDOW_HOURS, now]` (default 24h) and calls a
`fetch` function (default `_default_fetch`) that:

1. Downloads GDELT's **`masterfilelist.txt`** (the index of every published update file).
2. Keeps the rows whose third column ends in **`.gkg.csv.zip`** and whose 14-digit filename
   timestamp falls inside the window. A 24h window ⇒ ~96 fifteen-minute files.
3. Downloads each `.gkg.csv.zip` and decompresses it to text lines
   (`gdelt.unzip_gkg_bytes`, which also tolerates `.gz`).

A single bad/unreachable file is logged and skipped — one failure never aborts discovery.

> The `fetch` function is **injectable** (`discovery.run(cfg, repo, run_id, *, fetch=...)`).
> Tests pass canned lines; production uses `_default_fetch`. This is why the stage is
> unit-testable without the network.

### Step 2 — Parse each GKG line

`gdelt.parse_gkg_line()` splits a tab-delimited GKG 2.1 row and pulls the fields we need
(0-based column indices, per the published GKG codebook):

| Field | Index | Used for |
|---|---|---|
| DATE | 1 | record timestamp (`ts`) |
| SourceCommonName | 3 | `domain` |
| DocumentIdentifier | 4 | the article `url` |
| V1Themes | 7 | `themes` (`;`-separated; a `,count` suffix is stripped) |
| V1Locations | 9 | `country` (see note below) |
| V1.5Tone | 15 | `tone` (first comma value) |

It returns a `GkgRecord(gkg_id, ts, domain, url, themes, country, tone, lang)`, or `None`
for malformed/short lines and rows with no URL (counted as `discovery_skipped`).

> **Country = most-mentioned, not first.** GDELT lists locations in order of appearance, so
> taking the *first* location tracked the dateline/wire origin (frequently the US) rather than
> what the story is about. Instead we count the FIPS country code across **all** location
> records and take the most frequent one (`Counter.most_common`, ties broken by first
> mention). This is the country the rest of the pipeline treats as the article's focus.
>
> **FIPS, not ISO.** GDELT GKG locations emit **FIPS 10-4** country codes, not ISO 3166-1.
> (Lebanon is `LE`, Iraq `IZ`, Chile `CI`, …) Mixing the two silently dropped real targets and
> mislabeled collisions, so every country set in the code is FIPS.

### Step 3 — Filter for relevance (two cheap gates)

Each parsed record must clear **both** filters to survive discovery:

1. **Has an economic theme** — `gdelt.prefilter_ok(themes)`. Keep the record if it carries at
   least **one** theme whose code starts with a geoeconomic prefix
   `TARGET_THEME_PREFIXES = ("ECON_", "WB_", "EPU_")` (economy, World-Bank topic taxonomy,
   economic-policy-uncertainty). This is a **recall gate** — one hit is enough on purpose.
   Whether the story is *genuinely* geoeconomic is decided later by the LLM gate, not here.

2. **Maps to an active region bucket** — `regions.buckets_for_country(country, cfg.buckets)`.
   The article's FIPS country is mapped to every **active** bucket it belongs to. There are
   three buckets (`geoecon/regions.py`):

   | Bucket | Display | Covers |
   |---|---|---|
   | `middle_east` | Middle East | Gulf states, Levant, Iran, Iraq, Egypt, Turkey, Cyprus, … |
   | `north_africa` | North Africa | Egypt, Libya, Tunisia, Algeria, Morocco |
   | `latam` | Latin America | Brazil, Argentina, Mexico, Chile, Colombia, … |

   If the country maps to **no** active bucket, the record is dropped. A country can belong to
   more than one bucket — e.g. **Egypt** (`EG`) is in both `middle_east` and `north_africa`,
   and is processed in both.

> The coarse `region` label (`MENA` / `LatAm` / `other`) is also computed
> (`gdelt.region_for_country`) and stored on the row for provenance, but the keep/drop
> decision is made by the **buckets**, which is the finer-grained grouping the brief is
> organized around.

### Step 4 — Dedup, rank, cap

- **Dedup** within the run by `url_hash = normalized_url_hash(url)` (`geoecon/urlnorm.py`,
  Change 3 — collapses www/AMP/tracking-param variants so GDELT and the registry dedup the
  same story to one row; both discovery stages use it) — the same URL
  appearing across multiple 15-minute files is kept once.
- **Rank** by a salience proxy: `gdelt.theme_salience(themes)` = the **density** of target
  themes among all of the record's themes (`hits / len(themes)`), highest first. Density
  rather than raw count — raw count promoted sprawling omnibus articles (news briefs, opinion
  roundups touching hundreds of shallow themes); density rewards focused economic coverage.
- **Cap, per bucket.** Walking the salience-sorted list, an article is kept if **any** of its
  buckets is still under the ceiling; keeping it then counts against **all** of its buckets.
  The ceiling is `cfg.limit or cfg.discovery_ceiling` (default **120** per bucket; smoke runs
  set a small `LIMIT` to cap the whole run cheaply).

### Step 5 — Persist

For each kept record, `repo.insert_article(...)` writes a row to **`articles`** with
`fetch_status='pending'`, carrying the GDELT provenance forward: `source_url`, `url_hash`,
`domain`, `region`, `country_fips`, `gkg_themes`, `gkg_tone`, `gdelt_gkg_id` (`title` and
`source_lang` are still empty at this point — extraction fills them). Inserts are **idempotent
on `(run_id, url_hash)`**, so re-running discovery for the same window never creates
duplicates — which is what makes a crashed run safely resumable.

**Metrics emitted:** `{discovered, discovery_skipped, raw_lines}`.

> **Operational note.** `_default_fetch` accumulates the window's GKG lines in memory before
> filtering. For a 24h window that's on the order of ~10⁵ lines (a few hundred MB) — fine for
> the pod, but it's the heaviest part of a real run and the natural first target if the window
> is ever widened (stream-per-file instead of accumulate).

---

## Stage 1b — Extraction: fetching the article text

**File:** `geoecon/stages/extract.py`
**Reads:** `articles` rows with `fetch_status='pending'`
**Writes:** updates each row's `title`, `body`, `fetch_status`, `source_lang`, `fetch_error`

Discovery gives us URLs; extraction turns them into text. For each pending article,
`extract.run()` calls `extract_one(url, fetcher=...)`, which returns
`(body, title, status, lang)`:

1. **Fetch** the page. The default `fetcher` is `trafilatura.fetch_url` (injectable for tests).
   If the fetch raises (timeout, DNS, 5xx, …) → `failed`.
2. **Empty download?** → `headline_only` (nothing to extract).
3. **Extract** the main content with
   `trafilatura.extract(html, include_comments=False, favor_recall=True,
   output_format="json", with_metadata=True)` —
   [trafilatura](https://trafilatura.readthedocs.io/) strips boilerplate (nav, ads, comments)
   and returns the article **title and body** as JSON, which we parse out.
4. **Classify** the outcome into one of three `fetch_status` values:

   | Outcome | When | `body` | `title` |
   |---|---|---|---|
   | `extracted` | text returned and ≥ `MIN_CHARS` (200) | the body text | kept |
   | `headline_only` | empty download, or extracted text < 200 chars (paywall, JS-only, thin page) | `NULL` | **kept if available** |
   | `failed` | the fetch raised | `NULL` | `NULL` (`fetch_error` set) |

`repo.update_extraction(...)` writes the result back to the `articles` row.

**Why the tiered fallback matters:** a paywalled or JS-rendered page still has a GDELT title +
theme/tone metadata, and trafilatura can often recover the headline even when the body is too
thin to use. Rather than dropping such an item, we keep it as `headline_only` **with its
title** — the LLM gate and synthesis stages downstream use that title and treat the record as
headline-only (it won't be penalized for lacking figures). Aggressive recovery (headless
browser, archive.org) is intentionally **not** built in Phase 1.

**Metrics emitted:** `{extracted, headline_only, failed}`.

---

## What "fetched" looks like at the end

After these two stages, the `articles` table for the run contains every relevant URL GDELT
surfaced, each tagged with bucket/region, country, themes, and tone, and carrying either a
full `body` (`extracted`), a flagged stub with a title (`headline_only`), or a `failed`
marker. That table is the input to the processing half of the pipeline, which opens with the
LLM **gate** that decides precision and final country — see
**[02-processing-to-report.md](02-processing-to-report.md)**.

---

### Phase 1 config knobs (quick reference)

| Env var | `Config` field | Default | Effect |
|---|---|---|---|
| `WINDOW_HOURS` | `window_hours` | `24` | How far back discovery looks |
| `BUCKETS` | `buckets` | `middle_east,north_africa,latam` | Which region buckets are active |
| `DISCOVERY_CEILING` | `discovery_ceiling` | `120` | Per-bucket cap on articles carried forward |
| `LIMIT` | `limit` | _unset_ | If set, overrides the per-bucket ceiling (smoke runs) |

`MIN_CHARS` (200) — the extracted-body length below which an article falls back to
`headline_only` — is a constant in `geoecon/stages/extract.py`.
