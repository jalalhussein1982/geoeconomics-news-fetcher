# Geoeconomic Newsletter Pipeline — Comprehensive Blueprint

**Purpose:** A daily automated pipeline that discovers geoeconomic news across **MENA** and **Latin America** in multiple source languages (Arabic, Spanish, Portuguese, French, + Chinese/English where relevant), clusters it into events, synthesizes grounded structured records, and delivers a newsletter as both a standalone HTML archive and an email.

**Status:** Architecture fully locked. Per-cluster model selected and validated. Output-language strategy decided. Postgres schema written and validated. Three evals remain queued (non-blocking).

**Last updated:** working reference — update as queued items close.

---

## 1. Pipeline at a glance

```
07:00 cron (daily)
  → 1a  DISCOVERY    GDELT 2.0, window [T-24h, T], GKG theme-intersection ≥2,
                     NumMentions tiebreak, extraction ceiling 150/day
  → 1b  EXTRACTION   trafilatura → GDELT-metadata fallback (headline-only) on fail
  → 1c  RSS DEPTH    curated regional feeds (deferred config; empty for now)
  → 2   CLUSTER      multilingual event clustering (BGE-M3 provisional) + framing-divergence flag
  → 3   SYNTHESIS    per-cluster JSON record (deepseek-v3.2 + coverage prompt)
                     + throughline pass (stronger model, null-permitted)
                     + production guard (validator.py)
  → 4   RENDER+SEND  dual HTML (standalone archive + email-safe) → SMTP direct
  STATE: Postgres — runs, stage checkpoints, article-level provenance,
         seen-store (non-overlap), synthesis quality flags, resumable
```

---

## 2. Stage 0 — Scope & trigger

| Parameter | Decision |
|---|---|
| Trigger | Scheduled batch (not on-demand / event-driven) |
| Cadence | Daily |
| Fire time | **07:00 AM** |
| Window | Fixed **[T-24h, T]** — the 24 hours prior to the run |
| Overlap rule | **No overlap unless necessary** — enforced by the seen-store (cluster fingerprints) across the 07:00 boundary |
| Domain | Geoeconomics |
| Regions | MENA + Latin America |

---

## 3. Stage 1 — Ingestion

**Architecture:** Hybrid, discovery-weighted. GDELT is the spine (discovery), extraction is a separate operation, curated RSS is a deferred depth layer.

### 1a — Discovery (GDELT 2.0)
- Source: GDELT 2.0 — multilingual, geo-tagged, theme-tagged; 15-min update files sliced to the window.
- Filter: **GKG themes only** (keyword/actor layer deferred as fallback if recall is thin).
- Filter logic: **theme intersection** — require **≥2** geoeconomic/region themes co-tagged (`ECON_*`, `WB_*`, region themes). Raises precision without a lexicon.
- Tiebreaker: **NumMentions / NumSources** (salience proxy) when volume exceeds the ceiling.
- Extraction ceiling: **150 articles/day pre-dedup** (tunable).
- Known caveat: GDELT geo/theme tagging is noisy (false positives). Accepted for breadth; relevance is further handled downstream.

### 1b — Extraction
- Primary: **trafilatura** (fetch + parse URL).
- Failure/paywall handling: **tiered fallback** → on failure (paywall, 403, JS-render, empty parse) fall back to **GDELT metadata** (title + GKG themes/entities/tone + source link) as a **headline-only** record. Nothing high-salience silently vanishes; synthesis flags it as headline-only.
- Aggressive recovery (Playwright/headless, archive.org) NOT built now — reserved as an optional per-outlet whitelist later.

### 1c — RSS depth layer
- Curated native RSS/Atom feeds for high-signal regional business outlets (Arabic + LatAm) to capture full text and editorial framing GDELT under-weights.
- **Outlet list deferred by design** → populated iteratively in a config file after observing GDELT output. Empty for now.

---

## 4. Stage 2 — Clustering

- **Approach:** event-level, language-agnostic clustering with framing-divergence retention.
- Embed all articles in a shared multilingual vector space; cluster by semantic similarity so the **same event across languages collapses into one cluster** carrying its multilingual sources.
- **Framing-divergence retention:** preserve divergent framing *within* a cluster as a synthesis signal (e.g. Gulf press vs. Brazilian press on one BRICS/de-dollarization story). This is the pipeline's core analytical differentiator — not a dedup nuisance.
- Tunables/guards: similarity threshold + min/max cluster-size guards.
- **Embedding model:** provisionally **BGE-M3** (already in use) — **pending the embedding/reranker eval**, which may replace it with a MetaCentrum-local model if it separates better cross-language.

---

## 5. Stage 3 — Synthesis

### Per-cluster output contract (structured JSON, paired-language)
Surface forms stored in **both** source-language original and English; synthesis prose English-only.

| Field | Language | Notes |
|---|---|---|
| `headline_en` / `headline_original` (+lang) | paired | |
| `what_happened_en` | EN-only | grounded, 2–3 sentences (generated prose) |
| `geoeconomic_significance_en` | EN-only | trade/currency/sanctions/debt/commodity vector |
| `geoeconomic_vector` | — | tag array |
| `entities` | paired | `[{canonical_en, surface_original, lang, type}]` |
| `key_figures` | paired | `[{value, unit, source_span_original, gloss_en}]` |
| `framing_divergence` | paired | only when cluster is flagged divergent: `[{source_lang, frame_en, frame_original}]` |
| `sources` | — | multilingual, linked |

### Throughline (cross-cluster pass)
- A second pass identifies connective tissue across the day's clusters and writes a short editorial throughline at the top.
- **Volume-gated** and **permitted to return null** ("no significant throughline today") — must frequently do so to avoid fabricated trends.

### Non-negotiables (built in regardless)
1. **Grounding constraint** — every quantitative/factual claim must trace to source text; the prompt forbids supplying facts not in the cluster; **derived numbers** (computed, no stated span) are a grounding fail; unverifiable specifics are hedged or dropped.
2. **Structured JSON output** — deterministic render; malformed item fails loudly.

### Translation
- **No separate translation stage.** Synthesis reads source-language text directly (AR/ES/PT/FR/ZH) to keep grounded claims tied to original wording and avoid a lossy MT relay.

### Models
- **Per-cluster synthesis = `deepseek-v3.2` (non-thinking)** + coverage-focused prompt (`task_a_cov`), temperature 0, seed 12345, max_tokens 8192, concurrency ≥4.
  - `deepseek-v4-pro` is an accepted ≈tie (pick v3.2 on latency).
  - **Contingency:** `kimi-k2.6` (+0.09 fact coverage, ~10× tokens) — switch **if** the held-out human-read or week-1 live output shows material coverage gaps.
  - Do NOT use thinking variants (Pareto-dominated) or qwen3.5-122b (lower coverage at same speed).
- **Throughline model = pending TASK_B** (the four refusal-passers are the candidate pool; null-discipline is the deciding behavior). Stronger tier than per-cluster.

### Production guard (validator.py) — build requirement
- Post-synthesis validator flags: **empty key_figures / zero entities / output-language mismatch / pair-incomplete (an `_original` lacking its `_en` partner or vice versa) / invalid JSON** → retry → headline-only fallback.
- Emits `synthesis_quality_flag` persisted to Postgres. `ok` ships; anything else is held/flagged.
- Motivation: the eval's near-miss was a **silent** scorer failure; real models will produce hollow records on live long-tail input. The 8-fixture corpus does not clear any model on that tail.

---

## 6. Stage 4 — Render & Send

- **Render = dual render from one data model (the Stage-3 paired JSON):**
  1. **Standalone HTML archive** — rich modern CSS, RTL/LTR bilingual, proper Arabic typography. Saved to disk/archive.
  2. **Email-safe HTML** — table-based, inline CSS, ~600px, renders across clients incl. Outlook.
- **Output-language decision = STORE BOTH, DECIDE AT RENDER.** Three render modes are pure template logic over the same stored pairs:
  - `en_only` — reads `*_en`, ignores originals (ship this for v1).
  - `en_plus_original` — EN body + original-language headlines/quotes/framing (surfaces the framing-divergence differentiator).
  - `bilingual_rtl` — full dual-language RTL/LTR.
  - Switching modes requires **no re-synthesis** — the data holds both.
- **Transport = SMTP direct.**
  - Flag on record: deliverability + auth (SPF/DKIM/DMARC) are self-managed; consumer SMTP (Gmail) throttles/flags programmatic HTML mail. Fine for daily single-recipient; revisit before any list fan-out.

---

## 7. State & persistence (PostgreSQL)

**Decision: full run-state DB (not stateless, not lightweight seen-store).** Run tracking, stage checkpoints, article-level provenance, resumable mid-pipeline, seen-store for non-overlap.

### Tables (schema.sql — validated)
| Table | Role |
|---|---|
| `runs` | One row per daily batch; audit + resume anchor; records models/prompts used |
| `stage_runs` | Per-stage checkpoint (discovery→…→send); enables resume from last completed stage |
| `articles` | Every discovered item + extraction provenance; `fetch_status` incl. `headline_only` |
| `clusters` | Multilingual event clusters; `event_fingerprint` (seen-store key); `framing_divergent` flag |
| `cluster_articles` | Cluster↔article membership + per-article frame label |
| `seen_events` | Cross-run dedup; suppress emitted fingerprints unless materially updated; TTL via `expires_at` |
| `synthesis_records` | Per-cluster paired-language output + `synthesis_quality_flag` |
| `throughlines` | Cross-cluster thread; NULL body = pass ran and found no thread |
| `newsletters` | Rendered artifact + SMTP outcome; `render_mode` = the deferred language decision |

### Provenance
- **Article-level**, with a **B-compatible schema**: claim-level provenance attaches later as a `claim_sources` table FK'd to `synthesis_records` — **no migration** of existing tables.

### Deferred schema upgrades (no migration cost)
- **pgvector**: migrate `clusters.centroid` + an `articles.embedding` column to `vector(1024)` for BGE-M3 if the extension is available.
- **Partitioning**: at scale, partition `articles` + `synthesis_records` BY RANGE on `window_start`, monthly. Not needed at v1 (~150 articles/day).

---

## 8. The evaluation that selected the per-cluster model (summary)

**Method:** GATES → DIFFERENTIATORS, not a blended score. Built on an 8-fixture adversarial corpus (AR/ES/PT/EN/ZH) grounded in verified real geoeconomic events, with a closed-system `ground_truth.json` answer key (planted facts multi-source-verified only; derived + single-sourced figures in `must_not_claim`).

- **Gates (eliminate on fail):** valid JSON; refusal on headline-only (no fabrication); contradiction/framing handling; must-not-claim (derived deterministic + referent/single-source/fabrication by judge panel); span fidelity. Judge panel = 3 families, self-recused, `thinker` excluded.
- **Differentiators (survivors):** fact coverage, entity F1, latency, token cost.
- **Outcome:** `deepseek-v3.2` on the cost-efficiency frontier (top fact coverage + entity recall at lowest latency/cost); kimi-k2.6 the max-coverage contingency.

**Critical lesson (recorded):** two deterministic-scorer bugs (fact_cov, refusal) were found late, both biasing the same direction. **Eval self-reported metrics are NOT the authority** — out-of-sample, **human-judged** output is. The held-out test (a real Asharq Al-Awsat Syria–ConocoPhillips/Novaterra article, never a fixture) passed human read: complete, verbatim-grounded, no fabrication. Week-1 live output is the final validator and the kimi trigger.

---

## 9. Open items (none blocking the schema/build)

| Item | Status |
|---|---|
| **TASK_B** (throughline / null-discipline) | Queued. Candidate pool = refusal-passers. **Human-read its out-of-sample output** — null-discipline can fail silently like refusal did. |
| **Embedding + reranker eval** (Stage 2) | Queued. Confirms or replaces BGE-M3 (vs e5-large-instruct, qwen3-embedding-4b, nomic-v2-moe; reranker qwen3-reranker-4b). |
| **GLM family** (4.7 / 5 / 5.1) | Not evaluated — backend HTTP 500. Re-run when e-infra healthy. |
| **Non-Arabic held-out** | Run `task_a_cov` on one real ES or PT article to confirm the paired-field prompt generalizes across source languages. |
| **English-entities/values prompt clause** | Superseded by store-both: prompt now requires paired fields, not English-only. Re-confirm on the non-Arabic held-out. |
| **RSS depth outlet list** (1c) | Deferred by design — populate after observing GDELT output. |
| **Keyword/actor discovery layer** | Deferred fallback if GKG-theme recall is thin. |
| **Per-outlet aggressive paywall recovery** | Deferred — whitelistable later. |
| **Claim-level provenance** | Deferred — schema forward-compatible. |

---

## 10. Build artifacts produced

| Artifact | What it is |
|---|---|
| `schema.sql` | The full Postgres schema (validated) — the pipeline spine |
| `validator.py` | Model-agnostic production guard (built in eval; includes Postgres DDL for the quality flag) |
| `ground_truth.json` + `corpus/` | The eval answer key + 8 adversarial fixtures (closed system) |
| `DECISION_MEMO.md` | Per-cluster model decision record (incl. the scorer-bug history + authority caveat) |
| `README_corpus_integration.md` | How the corpus/ground-truth wire into the harness |

---

## 11. Decision ledger (the spine of why this is shaped as it is)

1. Scheduled daily batch, 07:00, fixed [T-24h, T], non-overlap enforced.
2. GDELT discovery spine + separate extraction + deferred curated RSS.
3. GKG themes only; theme-intersection ≥2; NumMentions tiebreak; ceiling 150/day.
4. trafilatura → GDELT-metadata headline-only fallback.
5. Multilingual event clustering + framing-divergence retention.
6. Per-cluster structured JSON record + null-permitted throughline; grounding + JSON non-negotiables; no separate translation.
7. Dual HTML render (archive + email-safe); SMTP direct.
8. Full Postgres run-state DB; article-level provenance (B-compatible); seen-store for non-overlap.
9. Per-cluster model = deepseek-v3.2 (non-thinking) + coverage prompt; kimi-k2.6 contingency.
10. Output language = store both, decide at render.
11. Production guard mandatory; eval metrics are not the authority — human-judged out-of-sample output is.
```

---

## 12. Region split, summary stage, and per-region delivery (implemented)

**Three region buckets** (FIPS country-code based, defined in `geoecon/regions.py`): `middle_east`, `north_africa`, `latam`. Egypt (`EG`) maps to both `middle_east` and `north_africa`; Israel, Turkey, and Cyprus are included in `middle_east`; West Bank (`WE`) and Gaza (`GZ`) are kept; Sudan is not in any bucket. Active buckets are configurable via `cfg.buckets` (env `BUCKETS`).

**Summary stage** (between synthesis and render): a `summary` stage (`geoecon/stages/summary.py`) groups synthesis records by bucket and calls the LLM once per non-empty bucket to produce a short editorial lede + takeaways; result is stored in `region_summaries`. The stage list is: `discovery → extraction → cluster → synthesis → summary → render → send`.

**Per-region newsletters**: render produces one newsletter per non-empty bucket, stored with a `bucket` label in `newsletters`. Source links in the rendered HTML come from `articles.source_url` (the verified fetched URL), not from the LLM `sources` field. Empty buckets are silently skipped; a run with no content produces no newsletters and sends nothing.

**Delivery**: `send` iterates `newsletters_for_run`; if there are no newsletters it returns `skipped_empty`. All newsletters go to the same `cfg.recipients` list (no per-region address splitting).
