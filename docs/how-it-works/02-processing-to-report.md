# 02 — Processing the news into a report

This covers the second half of a run: turning the fetched `articles` into delivered
newsletters. It spans **six** stages — **gate → cluster → synthesis → summary → render →
send** — plus the cross-day **seen-store** that prevents repeats.

```
articles (fetched, with bodies)                                  from file 01
   │
   ▼
gate       (geoecon/stages/gate.py)                  ── PRECISION FILTER
   │   batched LLM: is each article geoeconomic? which country is it about?
   │   keep iff geoeconomic AND country ∈ an active bucket → writes gate_* onto articles
   ▼
cluster    (geoecon/stages/cluster.py)
   │   per country: LLM groups the kept articles into events (lexical Jaccard fallback)
   │   1 event = 1 cluster → clusters, cluster_articles
   ▼
synthesis  (geoecon/stages/synth.py)                 ── the analytical core
   │   per cluster: skip-if-seen → LLM → structured JSON → QUALITY GATE → persist
   │   only records flagged `ok` will ship
   ▼
summary    (geoecon/stages/summary.py)
   │   per region bucket: LLM writes an executive lede + takeaways over the top records
   ▼
render     (geoecon/stages/render.py)
   │   per bucket: top-N `ok` records + that bucket's summary → archive HTML + email HTML
   ▼
send       (geoecon/stages/send.py + orchestrator `_send`)
   │   email one brief per region (multipart SMTP); on all-sent, write the seen-store
   ▼
📧 one "Geoeconomic Brief — <Region> — <date>" per region  +  🗄 per-region archives  +  📊 DB audit
```

The split into **per-region buckets** (`middle_east`, `north_africa`, `latam`) runs through
the whole second half: the gate pins each article's country, and cluster/summary/render/send
all group by the bucket that country belongs to (see `geoecon/regions.py`). The result is one
self-contained brief **per region**, not one global newsletter.

---

## Stage 3 — Gate: the geoeconomic precision filter

**File:** `geoecon/stages/gate.py` · **Prompt:** `geoecon/prompts/geoecon_gate.txt`
**Reads:** `articles` rows where `fetch_status != 'failed'`
**Writes:** `gate_geoeconomic`, `gate_score`, `gate_country`, `gate_reason`, `country_fips`
(on each `articles` row)
**Config:** `GATE_MODEL` (deepseek-v3.2), `GATE_BATCH_SIZE` (15), `BUCKETS`

Discovery (file 01) deliberately filtered for **recall** — it kept anything carrying a single
economic theme. The gate is where **precision** is decided. An LLM reads each article and
answers two questions: *is this genuinely geoeconomic?* and *which country is it really
about?*

`gate.run()` processes articles in batches of `GATE_BATCH_SIZE` (15). For each batch,
`build_gate_messages` lays out one block per article — `COUNTRY_HINT` (the GDELT country),
`TITLE`, up to 12 `THEMES`, and a 400-char `LEAD` of the body — under the
`geoecon_gate.txt` system prompt. The call is deterministic (`temperature=0`, `seed=12345`,
`max_tokens=4096`); the reply is a JSON array of verdicts, each
`{i, geoeconomic, primary_country, score, reason}`. A parse failure triggers **one retry**.

The **keep decision** per article (`upsert_gate`):

```
keep  =  geoeconomic  AND  country_to_fips(primary_country) resolves
                       AND  that FIPS country ∈ an active bucket
```

| Verdict | `gate_geoeconomic` | `country_fips` written |
|---|---|---|
| kept | `true` | the **LLM's** resolved country (overwrites the GDELT guess) |
| dropped | `false` | `NULL` |
| no verdict returned for that index | `true` (kept defensively) | unchanged (GDELT value), `reason='gate_unparsed'` |

> **The country the gate writes is the one everything downstream buckets on.** When an article
> is kept, `country_fips` is replaced with the LLM's `primary_country` — so a story datelined
> in one country but *about* another lands in the right regional brief. Articles with no
> returned verdict are kept rather than dropped (fail-open), so a flaky batch never silently
> loses coverage.

**Metrics emitted:** `{gate_total, gate_kept, gate_dropped, gate_unparsed}`.

---

## Stage 4 — Cluster: grouping articles into events (per-country LLM grouping)

**File:** `geoecon/stages/cluster.py` · **Prompt:** `geoecon/prompts/event_grouping.txt`
**Reads:** `articles` that are `gate_geoeconomic`, not `failed`, and map to an active bucket
**Writes:** `clusters`, `cluster_articles`
**Config:** `DEDUP_MODEL` (deepseek-v3.2), `DEDUP_JACCARD` (0.6), `BUCKETS`

A "cluster" is **one real-world event** — potentially reported by several sources in several
languages. Collapsing duplicates into a single cluster is what stops the brief from running the
same story three times under three outlets.

`cluster.run()`:

1. **Group the gated articles by `country_fips`.** All grouping happens *within* a country —
   which bounds the LLM's combinatorial work and keeps unrelated countries from being merged.
2. **Singletons pass through** — a country with one article becomes a one-article cluster.
3. **Multi-article countries go to the LLM** (`group_events`): each article is presented as
   `[i] TITLE` + a 200-char lead, and the model partitions the indices into event groups
   (`event_grouping.txt`, deterministic, 2 attempts). The reply is validated as a strict
   partition of `range(n)` — every article assigned to exactly one group — or it's rejected.
4. **Fallback** (`_lexical_groups`): if the LLM fails or returns an invalid partition, the
   country falls back to **title-token Jaccard union-find** at `DEDUP_JACCARD` (0.6). So the
   stage never hard-fails.

For each resulting group, the **primary article** is the one with the highest `gate_score`. Its
title (normalized) — or its URL if it has no title — becomes the **event fingerprint**
(`fingerprint() = "p1:" + sha1(...)`), the stable, language-agnostic key the seen-store uses to
recognize the same event across days. `insert_cluster(...)` records the cluster (region,
`country_fips`, `gate_score`, distinct `languages`, `article_count`) and `add_membership` links
each member article.

> **Provenance aggregates + corroboration (Changes 2 & 4).** `cluster.py` also computes, from
> the members' provenance, `tier_min` / `has_primary` / `distinct_state_alignments` /
> `source_count`, and a `corroboration_score` (`geoecon/corroboration.py`). These are
> **populated but not yet used for ranking** — the primary is still `max(gate_score)`. The
> ranking switch is dormant behind `RANK_BY_CORROBORATION` (see render/summary below and
> **[03-registry-redesign.md](03-registry-redesign.md)**).

> **Sanity check.** If the largest cluster swallows more than **25%** of the gated articles, the
> stage logs a warning to verify it's really one event (a symptom of the grouping over-merging).

> **History.** This replaced an embedding-clustering approach (BGE-M3 centroids) that was built,
> deployed, and reverted. The `clusters` schema still carries the embedding-era columns
> (`centroid`, `framing_divergent`) for forward use; per-country LLM grouping is what runs today.

**Metrics emitted:** `{clusters, max_cluster_size}`.

---

## Stage 5 — Synthesis: the analytical core (with a quality gate)

**File:** `geoecon/stages/synth.py`
**Reads:** `clusters`, `cluster_articles`, `articles`, `seen_events`
**Writes:** `synthesis_records`
**Uses:** the vendored LLM client (`geoecon/llm/client.py`), the vendored validator
(`geoecon/llm/validator.py`), and the prompt `geoecon/prompts/<SYNTHESIS_PROMPT_ID>.txt`
(default `task_a_cov.txt`)

For each cluster, `synth.run()` does the following:

### 1. Skip if already emitted (non-overlap)
`repo.get_seen(fingerprint)` — if this event was already emitted on a prior day
(`last_emitted_at` is set), the cluster is **skipped**. This is the "no overlap unless
necessary" rule. (The store is only *written* after a successful send — see Stage 7.)

### 2. Build the prompt
`build_messages(prompt, articles)` assembles a chat request: the **system** message is the
synthesis prompt (grounding rules + the exact JSON schema + a completeness directive); the
**user** message is the concatenated member articles (`[lang] title + body`, or a
`[HEADLINE-ONLY · lang]` note when the body is `NULL`).

### 3. Call the model
`client.complete("deepseek-v3.2", messages, temperature=0, seed=12345, max_tokens=8192)`
via the e-INFRA LLM proxy. Deterministic settings (`temperature=0`, fixed `seed`) make runs
reproducible. The client strips any `<think>` reasoning trace and returns `final_answer`.

### 4. Validate — the production guard (non-negotiable)
`parse_record()` extracts the JSON, then it is checked by the **vendored validator**
(`validator.validate_synthesis`). The validator catches the failure modes that real models
produce on the long tail: empty/short prose, wrong output language, no figures, no entities,
non-JSON. It returns an action; if not `ok`, synthesis **retries once**, and if still not
`ok` the record is **held** (persisted but flagged, never shipped).

`source_has_figures` is set to `not headline_only` (a cluster is headline-only when *every*
member article has a `NULL` body), so a headline-only cluster isn't penalized for lacking
figures.

### 5. The adapter (the important glue — read this before editing synthesis)
The vendored prompt and validator were built for the model **eval**, and they speak a
slightly different "eval schema" than the pipeline's database. `synth.py` bridges the two so
the prompt and validator stay **verbatim** (unmodified) and `schema.sql` stays intact:

- **Flag-vocabulary map** (`adapt_flag_vocab`): the validator's flags → the DB's
  `synthesis_quality_flag` CHECK values:
  `ok→ok`, `not_json→invalid_json`, `empty→retry_fallback`, `lang_mismatch→lang_mismatch`,
  `no_figures→empty_figures`, `no_entities→zero_entities`. (Persisting a raw validator flag
  would violate the DB constraint.)
- **Field map** (`map_record`): eval fields → pipeline columns —
  `headline→headline_en`, `what_happened→what_happened_en`,
  `geoeconomic_significance→geoeconomic_significance_en`; `entities`, `key_figures`, and
  `sources` are stored as-is in their JSONB columns; the paired `*_original` fields and
  `framing_divergence` are left `NULL` because the pipeline currently ships English-only
  (`en_only`).

> If you change the prompt's output shape or the DB columns, **update these two adapters
> together** — they are the single reconciliation point. The validator (`validate_synthesis`)
> reads the *eval* shape (`what_happened`, dict `entities`), so it must run on the **raw
> parsed record**, before `map_record`.

### 6. Persist
`repo.upsert_synthesis_record(run_id, cluster_id, fields, quality_flag, quality_detail,
is_headline_only)` — upserts on `(run_id, cluster_id)` so a re-run replaces cleanly.

**Output contract of a synthesis record** (`synthesis_records`): `headline_en`,
`what_happened_en` (2–4 grounded sentences), `geoeconomic_significance_en` (trade/currency/
sanctions/debt/commodity implications), `entities`, `key_figures` (each with a verbatim
source span), `sources`, and the `synthesis_quality_flag`.

**Metrics emitted:** `{records_total, records_ok, records_held}`.

**The cardinal rule: only `ok` records ship.** Held records are kept for audit/monitoring but
are excluded from the report.

---

## Stage 6 — Summary: one executive summary per region

**File:** `geoecon/stages/summary.py` · **Prompt:** `geoecon/prompts/region_summary.txt`
**Reads:** the `ok` synthesis records (`repo.records_for_buckets`)
**Writes:** `region_summaries` (one row per `(run_id, bucket)`)
**Config:** `SYNTHESIS_MODEL`, `REGION_CAP` (12), `BUCKETS`, `RANK_BY_CORROBORATION`, `MARKET_DEMOTE`

> **Ranking score (Change 4).** Summary and render both call
> `records_for_buckets(run_id, cfg.rank_by_corroboration)`. The 4th tuple element is the
> ranking score: `gate_score` by default, or `corroboration_score` when
> `RANK_BY_CORROBORATION` is ON (ships **OFF**). The flag is the *only* behavioural switch;
> with it off, ranking is unchanged. See **[03-registry-redesign.md](03-registry-redesign.md)**.

> **Market-roundup demotion (2026-06-23).** Both stages now sort via
> `ranking.make_score_of(cfg)` (`geoecon/ranking.py`) rather than the raw `gate_score`. When
> `MARKET_DEMOTE` is on (default), any record whose headline/what-happened reads as a **US/global
> market-index or commodity roundup** (S&P 500, Nasdaq, Dow, Wall Street, gold futures, …) gets a
> large penalty so it sorts **below** every country-subject story — never dropped, just ranked
> last (and trimmed first when a bucket overflows `REGION_CAP`). Country-qualified markets
> ("Argentine stocks", "Colombian peso", Ibovespa, EGX) and index *reclassification* (MSCI) are
> deliberately **spared**. `MARKET_DEMOTE=false` disables it.

This stage writes the **lede** that opens each regional brief. `summary.run()`:

1. **Selects the top records per bucket.** `regions.top_per_bucket(records_for_buckets(run_id),
   …, n=REGION_CAP)` groups the `ok` records into buckets by their cluster's country and keeps
   the top `REGION_CAP` (12) by `gate_score` — the same selection render uses, so the summary
   describes exactly the items the brief will show.
2. For each **non-empty** bucket, `build_summary_messages` feeds those records' headline /
   what-happened / significance into `region_summary.txt` (with `{REGION}` set to
   the display name) and calls the LLM (`SYNTHESIS_MODEL`, deterministic, `max_tokens=2048`).
   *(As of 2026-06-23 the raw `key_figures` dump is **no longer** fed to the summary LLM —
   finance-table figure blobs were muddying the exec summary, and the prose already carries the
   salient numbers.)*
3. A valid reply yields a `lede` + a list of `takeaways`; an invalid/empty reply stores a
   `NULL` lede and `[]` takeaways (counted as `summaries_empty` — the brief still renders, just
   without an executive summary). Persisted via `upsert_region_summary`.

**Metrics emitted:** `{summaries_total, summaries_ok, summaries_empty}`.

---

## Stage 7a — Render: building the reports (one per region)

**File:** `geoecon/stages/render.py` (+ templates in `geoecon/render/templates/`)
**Reads:** the `ok` records (`repo.records_for_buckets`) and each bucket's summary
(`repo.get_region_summary`)
**Writes:** `newsletters` (one per non-empty bucket), and an archive HTML file per bucket to the
`geoecon-archive` PVC
**Config:** `REGION_CAP` (12), `BUCKETS`, `ARCHIVE_DIR`, `FIGURES_MAX` (8), `MARKET_DEMOTE`

`render.run()` **loops over the active buckets**. For each non-empty bucket it takes the top
`REGION_CAP` (12) `ok` records by the ranking score (`ranking.make_score_of(cfg)` — `gate_score`
with US/global market roundups demoted; see the Summary stage's market-demotion note), and for
each record attaches:

- **verified source links** — taken from `articles.source_url` (the real GDELT URLs), **not**
  the LLM's `sources` field; and
- a **figures string** (`format_figures`): figures whose value appears in the story prose are
  ranked first, duplicates are collapsed by a locale/currency-aware numeric key (so `49.66%` /
  `49,66%` and `US$72` / `72 USD` each count once), and the line is **capped at `FIGURES_MAX`
  (8)** — added 2026-06-23 to kill the 80–150-number "salad" that finance / feasibility-table
  sources produced (`FIGURES_MAX=0` disables the cap). Joined with " · ".

It pulls that bucket's `region_summary`, then produces **two** HTML documents from the same data
(`render_html(records, mode="en_only", date_str, summary, region_display, stats)`):

1. **`archive.html.j2`** — a rich, standalone page saved to disk as
   `{ARCHIVE_DIR}/geoecon-<date>-<bucket>.html` (the mounted PVC) — the durable archive.
2. **`email.html.j2`** — an email-safe version: table-based layout, inline CSS, no external
   assets, so it renders consistently across mail clients. Stored in the `newsletters` row.

`repo.insert_newsletter(run_id, "en_only", archive_path, email_html, record_count,
bucket=bucket)` records the artifact; it's **unique on `(run_id, bucket)`** and upserts, so a
re-render replaces cleanly. **Empty buckets are skipped** (no newsletter). The stage returns
`{newsletters: [...], record_count}`.

In `en_only` mode the templates read only the `*_en` fields and never emit original-language
text. The templates also carry a **quiet-day** fallback ("A quiet day — no qualifying items")
for a rendered brief with zero records; render passes the discovery `stats` so a heartbeat can
show the discovered/passed counts.

> `render_mode` is stored per-newsletter. Future `en_plus_original` / `bilingual_rtl` modes are
> pure template logic over the already-stored data — no re-synthesis.

---

## Stage 7b — Send: delivering the reports

**Files:** orchestrator `_send` closure + `geoecon/stages/send.py`
**Reads:** `repo.newsletters_for_run(run_id)`
**Writes:** updates each newsletter's `send_status`/`sent_at`/`send_error`; then `seen_events`
**Config:** `RECIPIENTS`, `SMTP_*`, `FROM_ADDR`, `DRY_RUN`

The orchestrator's `_send` step decides whether and what to send:

- **`DRY_RUN`** → skip sending (used by smoke runs).
- It loads **all** of the run's newsletters (`newsletters_for_run`). If there are none (a fully
  quiet day, every bucket empty) → `skipped_empty`.
- Otherwise it builds a per-region subject — `subject_for(bucket) = "Geoeconomic Brief —
  <Region display> — <date>"` (e.g. "— Middle East —") — and calls `send.send_pending`.

`send_pending` iterates the newsletters and sends each one that isn't already `sent` (so a
send-retry on resume re-sends only the pending regions). Per newsletter, `send.run` →
`build_message` constructs a **multipart/alternative** email (a plain-text fallback plus the
HTML body) and `_smtp_transport` opens an SMTP connection (`SMTP_HOST:SMTP_PORT`, default 587),
upgrades with STARTTLS, authenticates (`SMTP_USER` + the Gmail **app password**), and sends. The
transport is injectable, so tests use a local `aiosmtpd` server instead of real mail. It returns
`{results, any_sent, all_sent}`.

- **All regions sent (`all_sent`):** the orchestrator calls `repo.mark_emitted(run_id)`, which
  writes every shipped (`ok`) cluster's fingerprint into `seen_events` (`upsert_seen`, 14-day
  TTL). This is what makes tomorrow's run *not* repeat today's stories.
- **Any region failed:** `_send` raises (the stage fails). The archive HTML was already written
  (so content is never lost), each newsletter's `send_status` reflects its own outcome, and
  crucially `seen_events` is **not** updated — so a failed send doesn't wrongly suppress those
  events, and a retry re-sends only the regions still `pending`.

---

## The final report (what you actually get)

1. **📧 Email** — one `Geoeconomic Brief — <Region> — <date>` per non-empty region, English-only
   HTML, to `RECIPIENTS`.
2. **🗄 Archive** — one standalone HTML file per region per day on the `geoecon-archive` PVC
   (`geoecon-<date>-<bucket>.html`).
3. **📊 Audit** — the full run is queryable in Postgres: `runs.metrics` (counts/timings), every
   `synthesis_record` (including *held* ones and *why* they were held), the `region_summaries`,
   and each `newsletters` send outcome.

---

## Why the run survives crashes (state & resumability)

Every stage records its status in `stage_runs`, and the run window is the key in `runs`. If a
pod dies mid-run, Kubernetes restarts it; `run_pipeline` finds the open run for the window and
**skips the stages already marked `completed`**, resuming from where it stopped. Idempotency
makes re-running safe at the data layer too:

- discovery: `articles` deduped by `(run_id, url_hash)`
- gate: `gate_*` columns upserted per `article_id`
- synthesis: `synthesis_records` upserted on `(run_id, cluster_id)`
- summary: `region_summaries` upserted on `(run_id, bucket)`
- render: `newsletters` upserted, unique per `(run_id, bucket)`
- send: only the still-`pending` newsletters are re-sent
- cross-day: `seen_events` written **only after all regions send**

(The cluster stage rebuilds its `clusters`/`cluster_articles` from scratch when it runs; its
safety comes from completed-stage skipping rather than row-level idempotency.) That combination
is what lets the daily job be both **auditable** and **safely retryable**.
