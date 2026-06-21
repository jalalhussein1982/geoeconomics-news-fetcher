# Design: Per-country LLM event grouping (replaces embedding clustering)

**Date:** 2026-06-22
**Status:** Approved (brainstorm) — pending implementation plan
**Branch base:** `main` @ `b451de3`

## 1. Problem

The embedding-based clustering merged to `main` (`b451de3`, `multilingual-e5-large-instruct`
cosine) **over-merges distinct events** and is unsafe to ship. The first real run
(run 8) produced a single-link chain of **70 articles mixing four unrelated events**
(Colombia runoff + Oman "Ministry of Finance" + Freddie Mac mortgages + USDA
fertilizer). Empirical investigation on that run's 201 gated articles showed the
failure is in the embedding signal, not the linkage:

- single-link @ 0.98 → one 70-article chain;
- **complete-link** @ 0.94–0.98 → still binds "Brazil Lula G7" + "Colombia runoff"
  into one clique (a complete-link clique requires *every* pair ≥ threshold);
- complete-link + **mean-centering/whitening** (which spread the distribution from
  p50 0.93 to −0.06) → the same Brazil+Colombia clique survives;
- **title-only** + centering → the same clique survives.

Root cause: e5 under-discriminates this syndication-heavy feed (the same wire story
appears 60–77× verbatim), and the long constant instruct prefix inflates similarity
on short texts. No threshold or linkage rule separates distinct events from
same events because the cosine signal itself does not.

The live cron has been rolled back to the safe lexical image `20260621-2`.

## 2. Decision

Replace embedding clustering with **per-country LLM event grouping**. The gate
already assigns each article one reliable `country_fips`. Group gated articles by
country; for each country with ≥2 articles, ask the LLM to partition that country's
articles into distinct events. The LLM reliably distinguishes events (it knows
"Bolivia state of emergency" ≠ "Bolivia FX move" and unifies three differently-worded
Bolivia headlines) — the judgment embeddings could not make.

**Out of scope (user decision):** cross-country same-events (e.g. the Cairo quartet
tagged EG/TU/SA/PK). Grouping is per gate-assigned country. A cross-country event may
still appear once per country tag — a small residual, far better than today's 5×, and
not worth the complexity of country-agnostic grouping.

This fully reverts the embedding experiment: `LLMClient.embed()`, the embedding merge,
and `cluster_cosine_threshold` are removed.

## 3. Components

### 3.1 `cluster.run` rewrite — `geoecon/stages/cluster.py`

Signature stays `run(cfg, repo, run_id, client)`.

1. Filter gated articles: `gate_geoeconomic` true, `fetch_status != "failed"`,
   `regions.buckets_for_country(country_fips, cfg.buckets)` non-empty (unchanged).
2. Bucket the articles by `country_fips`.
3. For each country bucket:
   - 1 article → it is its own event group (no LLM call).
   - ≥2 articles → `groups = group_events(client, cfg.dedup_model, country, arts) or
     _lexical_groups(arts, cfg.dedup_jaccard)` — LLM partition, or the lexical
     same-country fallback if the LLM call/parse fails (§3.3).
4. For each event group: primary = `max(gate_score)`, `country_fips` = the country,
   `languages` = distinct member source langs, `fingerprint` from the primary
   title/URL; `insert_cluster(...)` (no centroid) + `add_membership` per member.
5. Return `{"clusters": n, "max_cluster_size": <largest group size>}`; WARN-log if the
   largest exceeds 25% of gated articles (kept for visibility; chaining is now
   structurally impossible since grouping is per-country and LLM-bounded).

`insert_cluster`'s `centroid` parameter is removed (added by the embedding work; no
longer used). `title_tokens`, `jaccard`, `fingerprint` are retained.

### 3.2 `group_events` + prompt

```
group_events(client, model, country, articles, *, prompt=None) -> list[list[Article]] | None
```

- Builds a deterministic LLM request (temperature 0, seed 12345) mirroring `gate.py`:
  a numbered list of `title + short snippet` (first ~200 chars of body, or title-only
  for headline-only), plus the country.
- Prompt (`geoecon/prompts/event_grouping.txt`): "These articles all concern
  {COUNTRY}. Group them so each group is exactly one real-world event/story
  (near-duplicate coverage, the same announcement across outlets, or direct
  follow-ups belong together; different events go in different groups). Return ONLY
  JSON `{"groups": [[1,3],[2], ...]}` of the article numbers; every number appears in
  exactly one group."
- Parses the JSON, maps numbers back to articles, and **validates the partition**:
  every input article appears in exactly one group. On success returns the list of
  groups. On parse failure or invalid partition, retries once; if still bad, returns
  `None`.

### 3.3 Lexical fallback

`_lexical_groups(articles, threshold)` — the existing same-country title-token Jaccard
union-find, returning groups (list of lists). Used per-country when `group_events`
returns `None`. WARN-logged so the degradation is visible. The run never breaks; worst
case is the old under-merge for that one country.

### 3.4 Config — `geoecon/config.py`

- Add `dedup_model: str = "deepseek-v3.2"` (env `DEDUP_MODEL`) — the grouping model.
- Remove `cluster_cosine_threshold` (and its env). Keep `dedup_jaccard` (fallback).
- Revert `embedding_model` default to `None` (it is run metadata only — passed to
  `create_run`; nothing computes embeddings now).

### 3.5 Removals (full embedding revert)

- `geoecon/llm/client.py`: remove `embed()` and its tests in `tests/unit/test_client.py`.
- `geoecon/stages/cluster.py`: remove `embed_text`, `_l2norm`, `_group_embedding`,
  `EMBED_PREFIX`, `EMBED_CHARS`, the cosine merge path.
- `scripts/tune_cluster_threshold.py`: remove (embedding-specific).
- `geoecon/state/repo.py`: remove the `centroid` parameter/column write from
  `insert_cluster` (the `clusters.centroid` column may remain in the schema, unused).

## 4. Data flow

```
gate (writes gate_geoeconomic, country_fips, gate_score)
  → cluster.run(cfg, repo, run_id, client)
       filter gated articles → bucket by country_fips
       per country: 1 → singleton; >=2 → group_events(LLM) or _lexical_groups (fallback)
       per event group: primary=max gate_score → insert_cluster + add_membership
  → synthesis → summary → render → send
```

## 5. Testing (offline; mock the client)

- `tests/unit/test_cluster.py`:
  - `group_events` partitions a country's articles per a mocked LLM JSON response
    (3 differently-worded same-country headlines → one group; two distinct same-country
    events → two groups).
  - invalid/garbled LLM JSON → `group_events` returns `None` → `cluster.run` uses the
    lexical fallback for that country (assert clusters still form).
  - a country with 1 article → singleton cluster, no client call.
  - primary = max `gate_score`, `country_fips`, bucket membership, `insert_cluster` +
    `add_membership` calls unchanged; no centroid written.
- `tests/unit/test_repo.py`: the cluster integration test updated for the no-centroid
  `insert_cluster` and a stub client whose grouping returns one-group-per-country.

## 6. Cost / latency

~10–15 small LLM calls per run (one per multi-article country; most countries have
few articles), cheaper than the gate's ~24 batches. Sequential is acceptable;
per-country concurrency is a later optimization.

## 7. Acceptance criteria

- `cluster.run` groups gated articles into events per country via the LLM, with the
  lexical same-country merge only as the per-country fallback on LLM failure.
- Distinct same-country events stay separate; differently-worded same-country coverage
  merges. No cross-event mega-cluster (the run-8 70-chain cannot recur).
- Embedding code (`embed()`, embedding merge, cosine threshold, tuning script) removed;
  full unit suite green and pristine.
- Egypt dual-membership preserved (per-country grouping doesn't change bucket logic).
