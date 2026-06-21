# Design: Embedding-based semantic event clustering

**Date:** 2026-06-21
**Status:** Approved (brainstorm) — pending implementation plan
**Branch base:** `main` @ `2229a63` (capture-quality overhaul, deployed as image `20260621-2`)

## 1. Problem

The deployed pipeline still ships heavily duplicated briefs. Run 7 (2026-06-21)
sent three region newsletters in which roughly half of each brief restated a
handful of events:

- **Middle East:** the Cairo quartet foreign-ministers meeting appeared **5×**
  (different headlines, sources tagged EG / TU / SA / PK); US–Iran Switzerland
  talks 2–3×.
- **North Africa:** the same Cairo cluster repeated 4–5×; Trans-Saharan pipeline 2×.
- **Latin America:** Bolivia state-of-emergency 3×, Colombia runoff 2×, Cuba
  currency record 2×.

### Why the current de-dup fails

Clustering (`geoecon/stages/cluster.py`) keys events on a **title fingerprint**
(`sha1` of the normalized title), then merges near-duplicates with a union-find
pass gated on **same `country_fips` AND title-token Jaccard ≥ `cfg.dedup_jaccard`
(0.6)**. Two failure modes dominate:

1. **Jaccard ≥ 0.6 is too strict for real coverage.** The three Bolivia
   headlines ("…to clear 50-day roadblock gridlock" / "…amid prolonged blockade
   crisis" / "…as road blockades choke fuel and food supplies") share well under
   60% of their tokens, so they never merge.
2. **The same-country constraint structurally blocks multi-country events.** The
   Cairo meeting is tagged EG / TU / SA / PK across sources, so those articles
   land in different `country_fips` and are barred from merging — producing 5
   copies.

Lexical similarity cannot see that differently-worded, differently-tagged
articles describe one event. The architecturally correct fix is **semantic
(embedding) clustering**, which the original blueprint named as the intended
Stage-2 approach (deferred Phase 2).

## 2. Goals / non-goals

**Goals**
- Collapse same-event articles that differ in headline and/or country into one
  cluster, using sentence-embedding cosine similarity.
- Keep the change to the smallest viable surface: rewrite the merge inside the
  existing `cluster` stage; do not add stages or schema migrations.
- Stay robust: an embedding-endpoint failure must not break or empty the run.

**Non-goals (explicitly out of scope — YAGNI)**
- Cross-run de-duplication. The seen-store stays title-fingerprint based.
- Changing Egypt's dual-membership: an Egypt/Cairo story still appears in BOTH
  the Middle East and North Africa briefs by design (each regional brief is
  self-contained). The complaint being fixed is *within-email* repetition.
- pgvector. Within-run clustering of ~191 articles is done in memory; the
  cluster centroid is persisted as JSONB in the existing `clusters.centroid`.
- Reranker (`qwen3-reranker-4b`) refinement of borderline merges.
- Framing-divergence detection.

## 3. Approach

Replace the lexical merge in `cluster.run` with an embedding cosine merge.
Embed each gated article (title + body lead) with
`multilingual-e5-large-instruct` via the e-INFRA gateway, union-find merge on
cosine ≥ a tuned threshold with the same-country constraint dropped, then keep
all existing downstream logic (primary selection, bucket assignment, fingerprint,
persistence). The lexical merge remains **only** as a fallback when the embedding
call fails.

**Infrastructure (verified 2026-06-21):** the e-INFRA endpoint
(`https://llm.ai.e-infra.cz/v1`) is an OpenAI-compatible LiteLLM proxy that
serves `multilingual-e5-large-instruct` (1024-dim, multilingual) via
`/v1/embeddings`. BGE-M3 (the blueprint's provisional name) is **not** available;
e5-large-instruct is the chosen multilingual embedder and its 1024 dims match the
schema's anticipated `vector(1024)` centroid note.

## 4. Components

### 4.1 `LLMClient.embed()` — `geoecon/llm/client.py`

New method alongside `complete()`, reusing the same base URL, Bearer auth,
retry/backoff, and timing path.

```
embed(model: str, texts: list[str], *, batch_size: int = 64) -> list[list[float]] | None
```

- POSTs `{"model": model, "input": batch}` to `{base_url}/embeddings`; concatenates
  per-batch results in input order; returns one vector per input text.
- Returns `None` if any batch ultimately fails after retries (so the caller can
  fall back). Never raises on transport/HTTP error.
- `embed()` is **model-agnostic**: it sends `input` verbatim. The e5 instruct
  prefix (below) is applied by the caller in §4.2, so the client stays reusable
  for any embedding model.

### 4.2 `cluster.run` rewrite — `geoecon/stages/cluster.py`

Signature gains the LLM client: `run(cfg, repo, run_id, client) -> dict`.

Per gated article, build the embed text, prefixed with the e5 instruct string so
cosine stays a meaningful same-event signal (`multilingual-e5-large-instruct` is
asymmetric by default):
- `EMBED_PREFIX + title + "\n" + body[:EMBED_CHARS]` where
  `EMBED_PREFIX = "Instruct: Identify news articles reporting the same event\nQuery: "`
  and `EMBED_CHARS = 500`;
- headline-only / empty body → prefix + title alone;
- empty title → prefix + body lead; both empty → the article forms a standalone
  cluster (not embedded).

The exact prefix wording is confirmed in the tuning step (§6); if the served
model ignores or double-applies it, the cosine distributions there will reveal it.

Then:
1. `vecs = client.embed(cfg.embedding_model, texts)`.
2. **If `vecs` is non-null:** L2-normalize each vector; union-find merge article
   pairs whose cosine ≥ `cfg.cluster_cosine_threshold`. **No same-country
   constraint** — cross-country same-events merge. Persist each cluster's centroid
   (mean of member vectors) to `clusters.centroid` (JSONB) for tuning/debugging.
3. **If `vecs` is null (endpoint failure):** fall back to the current lexical
   merge (same-country + title-token Jaccard ≥ `cfg.dedup_jaccard`), emitted at
   WARNING level so the degradation is visible. The run still produces clusters.

Downstream is unchanged: for each group, primary = `max(gate_score)` sets the
cluster's `country_fips`, `gate_score`, and (via `regions`) bucket membership;
`languages` from distinct member source langs; `event_fingerprint` from the
primary title/URL; `insert_cluster` + `add_membership` per member. The stage
return value (`{"clusters": n}`) and the metric line are unchanged.

The helpers `title_tokens` / `jaccard` are retained for the fallback path;
`fingerprint` is retained.

### 4.3 Config — `geoecon/config.py`

- `embedding_model`: default flips `None` → `"multilingual-e5-large-instruct"`
  (env `EMBEDDING_MODEL`).
- `cluster_cosine_threshold: float`: new, default `0.85` (env
  `CLUSTER_COSINE_THRESHOLD`), finalized in §6 against run-7 data.
- `dedup_jaccard` is **kept** (fallback path only).

`EMBED_CHARS` (body-lead length) and `EMBED_PREFIX` are module constants in
`cluster.py`, not config — they are implementation details, not operational knobs.

### 4.4 Orchestrator — `geoecon/orchestrator.py`

One line: the `_cluster` closure passes the client, mirroring `_gate` / `_synth`
/ `_summary`:

```
def _cluster(c, r, rid): return cluster.run(c, r, rid, client)
```

## 5. Data flow

```
gate (writes gate_geoeconomic, country_fips, gate_score)
  → cluster.run(cfg, repo, run_id, client)
       articles_for_run → filter (gate_geoeconomic, in-bucket, not failed)
       build embed texts (title + body[:500])
       client.embed(e5-large-instruct, texts)  ── null? ──► lexical fallback merge
          │ vectors
          ▼
       L2-normalize → pairwise cosine → union-find (cosine ≥ threshold, any country)
       per group: primary=max gate_score → insert_cluster(country_fips, gate_score,
                  centroid) + add_membership(*members)
  → synthesis → summary → render → send
```

## 6. Threshold tuning (part of the work)

The cosine threshold is set against real data, not guessed:

1. Offline (a script or notebook, not in the shipped run), embed run 7's 354
   gated articles (already in the deployed DB) with the e5 prefix.
2. Inspect the pairwise-cosine distribution and verify the known duplicate groups
   land **above** threshold while distinct events land **below**:
   - the ~5 Cairo-quartet articles,
   - the 3 Bolivia state-of-emergency articles,
   - the 2 Cuba currency-record articles.
3. Pick the threshold that collapses the known groups without chaining distinct
   events together (single-link union-find is chaining-prone; the threshold is
   the guard). Record the chosen value and the observed separation in the plan's
   implementation notes and as the `cluster_cosine_threshold` default.

## 7. Error handling / robustness

- **Embedding failure** (timeout, HTTP error, malformed payload) → `embed()`
  returns `None` → lexical fallback merge, WARNING-logged. The run never breaks
  or empties.
- **Empty embed text** (no title, no body) → the article forms its own cluster
  (as today).
- **Cost / latency:** ~191 gated articles = one (or few) `/embeddings` batches +
  O(n²) cosine over ≤~200 vectors (~40k pairs) — adds ~10–30 s, negligible
  against the gate (~18 min) and synthesis (~39 min).

## 8. Testing (offline; no live endpoint in unit tests)

- `tests/unit/test_client.py` (new or extended):
  - `embed()` returns one vector per input on a mocked HTTP 200 embeddings payload;
  - `embed()` returns `None` when the endpoint errors after retries;
  - batching concatenates in input order.
- `tests/unit/test_cluster.py` (extended):
  - injected synthetic vectors: same-event vectors merge; **cross-country**
    same-event (different `country_fips`) merges; dissimilar events stay separate;
  - embedding-failure (client returns `None`) → lexical fallback still produces
    clusters (repurpose the existing lexical-merge tests for this path);
  - primary selection, `country_fips`/`gate_score`, bucket membership, and
    `insert_cluster`/`add_membership` calls unchanged.
- The client is injected/mocked in cluster tests so no network is required.

## 9. Acceptance criteria

- `cluster.run` clusters by e5 cosine when embeddings are available; the same-
  country constraint is gone, so cross-country same-events merge.
- On a re-run of run-7-like input, the Cairo meeting, Bolivia emergency, and Cuba
  currency stories each collapse to a single cluster/record per region.
- Embedding-endpoint failure degrades cleanly to the lexical merge with a WARNING;
  the run completes and sends.
- Full unit suite green and pristine; new embedding/cluster behavior covered by
  offline tests.
- Egypt dual-membership preserved (Egypt stories still in both ME and NA briefs).
