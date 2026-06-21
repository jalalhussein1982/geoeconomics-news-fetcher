# Embedding-Based Semantic Event Clustering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `cluster.py`'s lexical title-Jaccard de-dup with `multilingual-e5-large-instruct` cosine clustering so same-event articles that differ in headline and/or country collapse into one cluster.

**Architecture:** Add an `embed()` method to the existing OpenAI-compatible `LLMClient`. In `cluster.run`, embed each gated article (title + body lead, e5-prefixed), L2-normalize, and union-find merge on cosine ≥ a tuned threshold with the same-country constraint dropped. Keep all downstream logic (primary selection, bucket assignment, persistence). The lexical merge survives only as the fallback when the embedding endpoint fails.

**Tech Stack:** Python 3.12, psycopg 3, `requests` (existing LLM client), e-INFRA LiteLLM gateway (`https://llm.ai.e-infra.cz/v1/embeddings`), Postgres (CNPG). Tests with pytest; embedding/HTTP mocked offline.

## Global Constraints

- Embedding model: `multilingual-e5-large-instruct` (1024-dim), reached via the existing `Config.llm_base_url` + `MAETCENTRUM_KEY` auth. **BGE-M3 is NOT available on the gateway.**
- e5 is asymmetric by default: every embed text is prefixed with the module constant `EMBED_PREFIX = "Instruct: Identify news articles reporting the same event\nQuery: "`, applied uniformly.
- Embed input per article: `EMBED_PREFIX + title + "\n" + body[:500]` (`EMBED_CHARS = 500`); headline-only/no-body → prefix + title; no title → prefix + body lead; both empty → standalone cluster (not embedded).
- The same-country constraint is DROPPED for the embedding merge (cross-country same-events must merge). It remains ONLY in the lexical fallback.
- Embedding-endpoint failure must degrade to the lexical merge with a WARNING — the run never breaks or empties.
- `clusters.centroid` (JSONB) already exists (`001_init.sql:124`); no migration. Store the mean member vector there only when the embedding path is used.
- Egypt dual-membership preserved: an Egypt story still appears in BOTH the Middle East and North Africa briefs. No cross-email de-dup.
- Out of scope: cross-run embedding de-dup, pgvector, reranker refinement, framing-divergence.

---

## File Structure

- `geoecon/config.py` — add `cluster_cosine_threshold`; flip `embedding_model` default to the e5 model. (Task 1)
- `geoecon/llm/client.py` — add `embed()` to `LLMClient`. (Task 2)
- `geoecon/stages/cluster.py` — rewrite the merge (embedding cosine + lexical fallback); add `embed_text`, cosine/union-find helpers. (Task 3)
- `geoecon/state/repo.py` — `insert_cluster` gains a trailing `centroid=None` param. (Task 3)
- `geoecon/orchestrator.py` — `_cluster` closure passes `client`. (Task 3)
- `scripts/tune_cluster_threshold.py` — offline threshold sweep over a completed run. (Task 4)
- `tests/unit/test_config.py`, `tests/unit/test_client.py`, `tests/unit/test_cluster.py` — coverage. (Tasks 1–3)
- `PIPELINE_BLUEPRINT.md` — doc note. (Task 5)

---

## Task 1: Config — cosine threshold + embedding model default

**Files:**
- Modify: `geoecon/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `cfg.embedding_model` (str, default `"multilingual-e5-large-instruct"`), `cfg.cluster_cosine_threshold` (float, default `0.85`). `cfg.dedup_jaccard` unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_config.py`:
```python
def test_embedding_and_cosine_defaults_and_overrides():
    from geoecon.config import Config
    base = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
            "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"}
    cfg = Config.from_env(base)
    assert cfg.embedding_model == "multilingual-e5-large-instruct"
    assert cfg.cluster_cosine_threshold == 0.85
    over = Config.from_env({**base, "EMBEDDING_MODEL": "qwen3-embedding-4b",
                            "CLUSTER_COSINE_THRESHOLD": "0.9"})
    assert over.embedding_model == "qwen3-embedding-4b"
    assert over.cluster_cosine_threshold == 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py::test_embedding_and_cosine_defaults_and_overrides -v`
Expected: FAIL — `embedding_model` is `None`; `cluster_cosine_threshold` missing (AttributeError).

- [ ] **Step 3: Implement**

In `geoecon/config.py`, change the `embedding_model` field default and add the threshold field (place `cluster_cosine_threshold` next to `dedup_jaccard`):
```python
    embedding_model: str = "multilingual-e5-large-instruct"
```
```python
    dedup_jaccard: float = 0.6
    cluster_cosine_threshold: float = 0.85
```
In `from_env`, replace the `embedding_model=...` line and add the threshold parse:
```python
            embedding_model=env.get("EMBEDDING_MODEL", "multilingual-e5-large-instruct"),
```
```python
            dedup_jaccard=float(env.get("DEDUP_JACCARD", "0.6")),
            cluster_cosine_threshold=float(env.get("CLUSTER_COSINE_THRESHOLD", "0.85")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add geoecon/config.py tests/unit/test_config.py
git commit -m "feat(config): embedding model default + cluster cosine threshold"
```

---

## Task 2: `LLMClient.embed()`

**Files:**
- Modify: `geoecon/llm/client.py` (add method to `LLMClient`)
- Test: `tests/unit/test_client.py`

**Interfaces:**
- Consumes: existing `LLMClient.__init__(base_url, api_key, *, timeout_s, max_retries, session)`, `LLMClient.RETRYABLE`.
- Produces: `LLMClient.embed(model: str, texts: list[str], *, batch_size: int = 64) -> list[list[float]] | None`. Returns one vector per input text in input order, or `None` if the endpoint fails after retries.

- [ ] **Step 1: Write the failing test**

Create/extend `tests/unit/test_client.py`:
```python
from geoecon.llm.client import LLMClient


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status; self._payload = payload; self.text = text
    def json(self): return self._payload


class _Session:
    def __init__(self, responses): self._responses = list(responses); self.calls = []
    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(json); return self._responses.pop(0)


def _client(session, **kw):
    return LLMClient("http://x/v1", "key", session=session, **kw)


def test_embed_returns_vectors_in_input_order():
    payload = {"data": [{"index": 1, "embedding": [3.0, 4.0]},
                        {"index": 0, "embedding": [1.0, 2.0]}]}
    s = _Session([_Resp(200, payload)])
    out = _client(s).embed("m", ["a", "b"])
    assert out == [[1.0, 2.0], [3.0, 4.0]]          # sorted by index, input order
    assert s.calls[0] == {"model": "m", "input": ["a", "b"]}


def test_embed_batches_and_concatenates():
    s = _Session([_Resp(200, {"data": [{"index": 0, "embedding": [1.0]}]}),
                  _Resp(200, {"data": [{"index": 0, "embedding": [2.0]}]})])
    out = _client(s).embed("m", ["a", "b"], batch_size=1)
    assert out == [[1.0], [2.0]] and len(s.calls) == 2


def test_embed_returns_none_on_http_error():
    s = _Session([_Resp(500, text="boom")])
    out = _client(s, max_retries=0).embed("m", ["a"])
    assert out is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_client.py -k embed -v`
Expected: FAIL — `LLMClient` has no attribute `embed`.

- [ ] **Step 3: Implement**

Add this method to `LLMClient` in `geoecon/llm/client.py` (after `complete`):
```python
    def embed(self, model, texts, *, batch_size=64):
        """Return one embedding vector per input text (input order), or None if the
        endpoint fails after retries. Model-agnostic: sends `input` verbatim."""
        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        out = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            data = None
            for attempt in range(self.max_retries + 1):
                try:
                    r = self.session.post(url, headers=headers,
                                          json={"model": model, "input": batch},
                                          timeout=self.timeout_s)
                except requests.RequestException:
                    pass
                else:
                    if r.status_code == 200:
                        data = r.json(); break
                    if r.status_code not in self.RETRYABLE:
                        return None
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt + random.random(), 30))
            if data is None:
                return None
            items = data.get("data") or []
            if len(items) != len(batch):
                return None
            for it in sorted(items, key=lambda x: x.get("index", 0)):
                vec = it.get("embedding")
                if not vec:
                    return None
                out.append(vec)
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_client.py -k embed -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add geoecon/llm/client.py tests/unit/test_client.py
git commit -m "feat(llm): add embeddings endpoint support to LLMClient"
```

---

## Task 3: `cluster.run` embedding cosine merge + lexical fallback

**Files:**
- Modify: `geoecon/stages/cluster.py` (rewrite merge), `geoecon/state/repo.py` (`insert_cluster` centroid), `geoecon/orchestrator.py` (`_cluster` passes client)
- Test: `tests/unit/test_cluster.py`

**Interfaces:**
- Consumes: `LLMClient.embed` (Task 2); `cfg.embedding_model`, `cfg.cluster_cosine_threshold`, `cfg.dedup_jaccard`, `cfg.buckets`; `regions.buckets_for_country`; `repo.articles_for_run`, `repo.insert_cluster`, `repo.add_membership`.
- Produces: `cluster.run(cfg, repo, run_id, client) -> {"clusters": int}`. `repo.insert_cluster(..., centroid=None)`. Module constants `EMBED_PREFIX`, `EMBED_CHARS`; helpers `embed_text(art) -> str`, retained `title_tokens`/`jaccard`/`fingerprint`.

- [ ] **Step 1: Write the failing tests**

Replace the body of `tests/unit/test_cluster.py` below the existing imports with this (keeps `test_jaccard_and_tokens`; updates `_art`/`FakeRepo`; adds a client and new cases):
```python
import math
from geoecon.stages import cluster as cluster_stage
from geoecon.state.models import Article


def _art(aid, title, country="SA", geo=True, score=0.5, body="body"):
    return Article(aid, f"http://x/{aid}", f"h{aid}", "d", "MENA", [], None, None,
                   "en", title, body, "extracted", country,
                   gate_geoeconomic=geo, gate_score=score)


class FakeRepo:
    def __init__(self, arts):
        self._arts = arts; self.clusters = []; self.memberships = []
    def articles_for_run(self, rid, fetch_status=None): return self._arts
    def insert_cluster(self, run_id, fp, region, langs, count,
                       framing_divergent=False, country_fips=None, gate_score=None,
                       centroid=None):
        cid = len(self.clusters) + 1
        self.clusters.append(dict(cid=cid, count=count, country_fips=country_fips,
                                  gate_score=gate_score, centroid=centroid)); return cid
    def add_membership(self, cid, aid, frame_label=None):
        self.memberships.append((cid, aid))


class FakeClient:
    """embed() returns a fixed vector list (aligned to article order), or None."""
    def __init__(self, vecs): self._vecs = vecs
    def embed(self, model, texts): return self._vecs


_CFG = type("C", (), {"buckets": ("middle_east", "north_africa", "latam"),
                      "dedup_jaccard": 0.6, "embedding_model": "e5",
                      "cluster_cosine_threshold": 0.85})()


def test_jaccard_and_tokens():
    a = cluster_stage.title_tokens("Saudi non-oil exports rise 10.7 percent")
    b = cluster_stage.title_tokens("Saudi non-oil exports rise 10.7%, driven by chemicals")
    assert cluster_stage.jaccard(a, b) >= 0.6


def test_embedding_merges_same_event_across_countries():
    # A(Egypt) & B(Turkey) describe one event -> near-identical vectors -> MERGE
    # despite different country_fips; C(Brazil) is orthogonal -> separate.
    arts = [_art(1, "Cairo summit on energy", country="EG", score=0.6),
            _art(2, "Foreign ministers meet in Cairo", country="TU", score=0.9),
            _art(3, "Brazil tariff shift", country="BR", score=0.7)]
    vecs = [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]]
    repo = FakeRepo(arts)
    out = cluster_stage.run(_CFG, repo, 1, FakeClient(vecs))
    assert out["clusters"] == 2
    merged = [c for c in repo.clusters if c["count"] == 2][0]
    assert merged["country_fips"] == "TU"          # primary = max gate_score (0.9)
    assert merged["centroid"] is not None and len(merged["centroid"]) == 2


def test_embedding_failure_falls_back_to_lexical():
    # client.embed returns None -> lexical same-country Jaccard merge path.
    arts = [_art(1, "Saudi non-oil exports rise 10.7 percent", score=0.9),
            _art(2, "Saudi non-oil exports rise 10.7%, driven by chemicals", score=0.7),
            _art(3, "Egypt pound rallies below 50 per dollar", country="EG", score=0.8),
            _art(4, "Bitcoin ETFs shed billions", geo=False)]  # dropped by gate
    repo = FakeRepo(arts)
    out = cluster_stage.run(_CFG, repo, 1, FakeClient(None))
    assert out["clusters"] == 2                     # 1&2 merge; 3 separate; 4 gated out
    sizes = sorted(c["count"] for c in repo.clusters)
    assert sizes == [1, 2]
    merged = [c for c in repo.clusters if c["count"] == 2][0]
    assert merged["country_fips"] == "SA" and abs(merged["gate_score"] - 0.9) < 1e-6
    assert merged["centroid"] is None               # lexical path stores no centroid
```
(Delete the old `test_cluster_merges_near_dups_and_filters_gate` — its behavior is now covered by `test_embedding_failure_falls_back_to_lexical`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_cluster.py -v`
Expected: FAIL — `cluster.run` takes 3 args (missing `client`); `insert_cluster` has no `centroid`; embedding path not implemented.

- [ ] **Step 3: Rewrite `geoecon/stages/cluster.py`**

Replace the whole file with:
```python
from __future__ import annotations
import hashlib, logging, math, re
from .. import regions

log = logging.getLogger("geoecon.cluster")

EMBED_PREFIX = "Instruct: Identify news articles reporting the same event\nQuery: "
EMBED_CHARS = 500


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def fingerprint(title: str | None, canonical_url: str | None) -> str:
    basis = _norm(title) if title else (canonical_url or "")
    return "p1:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()


def title_tokens(title: str | None) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(t) >= 3}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def embed_text(art) -> str:
    """e5-prefixed 'title + body lead' for one article; '' if it has neither."""
    title = (art.title or "").strip()
    body = (art.body or "").strip()
    if title and body:
        core = f"{title}\n{body[:EMBED_CHARS]}"
    elif title:
        core = title
    elif body:
        core = body[:EMBED_CHARS]
    else:
        return ""
    return EMBED_PREFIX + core


def _l2norm(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


class _UnionFind:
    def __init__(self, n): self.parent = list(range(n))
    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]; x = self.parent[x]
        return x
    def union(self, i, j): self.parent[self.find(i)] = self.find(j)
    def groups(self):
        g: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            g.setdefault(self.find(i), []).append(i)
        return g


def _group_embedding(arts, vecs, threshold):
    """Union-find on cosine >= threshold (any country). Returns (groups, normed_vecs).
    Articles with empty embed_text stay singletons."""
    norms = [_l2norm(v) for v in vecs]
    embeddable = [bool(embed_text(a)) for a in arts]
    uf = _UnionFind(len(arts))
    for i in range(len(arts)):
        if not embeddable[i]:
            continue
        for j in range(i + 1, len(arts)):
            if not embeddable[j]:
                continue
            if sum(x * y for x, y in zip(norms[i], norms[j])) >= threshold:
                uf.union(i, j)
    return uf.groups(), norms


def _group_lexical(arts, threshold):
    """Fallback: same-country + title-token Jaccard >= threshold."""
    toks = [title_tokens(a.title) for a in arts]
    uf = _UnionFind(len(arts))
    for i in range(len(arts)):
        for j in range(i + 1, len(arts)):
            if (arts[i].country_fips == arts[j].country_fips and toks[i] and toks[j]
                    and jaccard(toks[i], toks[j]) >= threshold):
                uf.union(i, j)
    return uf.groups()


def run(cfg, repo, run_id: int, client) -> dict:
    arts = [a for a in repo.articles_for_run(run_id)
            if a.gate_geoeconomic and a.fetch_status != "failed"
            and regions.buckets_for_country(a.country_fips, cfg.buckets)]
    if not arts:
        log.info("cluster: 0 clusters from 0 gated articles")
        return {"clusters": 0}

    texts = [embed_text(a) or " " for a in arts]   # " " placeholder: endpoints reject ""
    vecs = client.embed(cfg.embedding_model, texts)
    norms = None
    if vecs is not None and len(vecs) == len(arts):
        groups, norms = _group_embedding(arts, vecs, cfg.cluster_cosine_threshold)
        mode = "embedding"
    else:
        log.warning("cluster: embeddings unavailable, falling back to lexical merge")
        groups = _group_lexical(arts, cfg.dedup_jaccard)
        mode = "lexical"

    n = 0
    for members in groups.values():
        member_arts = [arts[m] for m in members]
        primary = max(member_arts, key=lambda a: a.gate_score or 0.0)
        langs = sorted({a.source_lang for a in member_arts if a.source_lang})
        fp = fingerprint(primary.title, primary.source_url)
        centroid = None
        if norms is not None:
            dim = len(norms[members[0]])
            centroid = [sum(norms[m][k] for m in members) / len(members) for k in range(dim)]
        cid = repo.insert_cluster(run_id, fp, primary.region, langs, len(member_arts),
                                  country_fips=primary.country_fips,
                                  gate_score=primary.gate_score, centroid=centroid)
        for a in member_arts:
            repo.add_membership(cid, a.article_id)
        n += 1

    log.info("cluster: %d clusters from %d gated articles (%s)", n, len(arts), mode)
    return {"clusters": n}
```

- [ ] **Step 4: Add `centroid` to `repo.insert_cluster`**

In `geoecon/state/repo.py`, replace `insert_cluster` with (adds a trailing `centroid=None`, writes the `centroid` column):
```python
    def insert_cluster(self, run_id, event_fingerprint, region, languages, article_count,
                       framing_divergent=False, country_fips=None, gate_score=None,
                       centroid=None) -> int:
        return self.conn.execute(
            """INSERT INTO clusters(run_id, event_fingerprint, region, country_fips,
                 languages, article_count, framing_divergent, gate_score, centroid)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING cluster_id""",
            (run_id, event_fingerprint, region, country_fips, languages, article_count,
             framing_divergent, gate_score,
             Jsonb(centroid) if centroid is not None else None)).fetchone()[0]
```
(`Jsonb` is already imported at the top of `repo.py`: `from psycopg.types.json import Jsonb`.)

- [ ] **Step 5: Wire the client into the orchestrator**

In `geoecon/orchestrator.py`, change the `_cluster` closure to pass `client`:
```python
    def _cluster(c, r, rid):   return cluster.run(c, r, rid, client)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres .venv/bin/python -m pytest tests/unit/test_cluster.py tests/unit/test_orchestrator.py tests/unit/test_repo.py -v`
Expected: PASS — embedding merge (incl. cross-country), lexical fallback, centroid stored only on the embedding path; orchestrator stage list unchanged; repo cluster tests still pass.

- [ ] **Step 7: Full suite**

Run: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres .venv/bin/python -m pytest tests/unit -q`
Expected: PASS, output pristine.

- [ ] **Step 8: Commit**

```bash
git add geoecon/stages/cluster.py geoecon/state/repo.py geoecon/orchestrator.py tests/unit/test_cluster.py
git commit -m "feat(cluster): embedding cosine event clustering with lexical fallback"
```

---

## Task 4: Tune the cosine threshold against real data

**Files:**
- Create: `scripts/tune_cluster_threshold.py`
- Modify: `geoecon/config.py` (only if the tuned value differs from 0.85)

**Interfaces:**
- Consumes: `cluster.embed_text`, `cluster._group_embedding`, `LLMClient.embed`, `Repo.articles_for_run`.

This task is empirical and run by the controller against the deployed DB (run 7), because it needs the live embedding endpoint + real article text. The script reuses the already-tested helpers from Task 3, so it carries no new unit logic.

- [ ] **Step 1: Write the sweep script**

Create `scripts/tune_cluster_threshold.py`:
```python
"""Sweep cluster_cosine_threshold over one completed run's gated articles.

Usage (inside the cluster, where the DB + MAETCENTRUM_KEY are reachable):
  python -m scripts.tune_cluster_threshold <run_id>
Prints, for each candidate threshold, the resulting cluster count and the
largest merged groups (by title) so a human can pick the value that collapses
known duplicates without chaining distinct events.
"""
import sys
from geoecon.config import Config
from geoecon.llm.client import LLMClient
from geoecon.state import db
from geoecon.state.repo import Repo
from geoecon.stages import cluster
from geoecon import regions
import os

def main(run_id: int):
    cfg = Config.from_env(dict(os.environ))
    conn = db.connect(cfg.db_dsn)
    repo = Repo(conn)
    client = LLMClient(cfg.llm_base_url, cfg.llm_api_key)
    arts = [a for a in repo.articles_for_run(run_id)
            if a.gate_geoeconomic and a.fetch_status != "failed"
            and regions.buckets_for_country(a.country_fips, cfg.buckets)]
    texts = [cluster.embed_text(a) or " " for a in arts]
    vecs = client.embed(cfg.embedding_model, texts)
    if vecs is None:
        print("embed failed"); return
    print(f"gated articles: {len(arts)}")
    for thr in (0.80, 0.82, 0.84, 0.85, 0.86, 0.88, 0.90):
        groups, _ = cluster._group_embedding(arts, vecs, thr)
        merged = sorted((g for g in groups.values() if len(g) > 1),
                        key=len, reverse=True)
        print(f"\n=== threshold {thr}: {len(groups)} clusters, {len(merged)} merged groups ===")
        for g in merged[:6]:
            print("  - " + " || ".join((arts[m].title or "?")[:60] for m in g[:5]))

if __name__ == "__main__":
    main(int(sys.argv[1]))
```

- [ ] **Step 2: Run the sweep against run 7 (controller step)**

Copy the repo (or the new image) into the cluster context and run, e.g. inside a one-off pod or via `kubectl exec` on a pod that has the package + secrets, against `run_id` 7:
```bash
python -m scripts.tune_cluster_threshold 7
```
Inspect the output: confirm the Cairo-quartet (~5), Bolivia (3), and Cuba currency (2) articles appear as single merged groups at the chosen threshold, and that distinct events are NOT chained into oversized groups.

- [ ] **Step 3: Record + set the threshold**

If the eyeballed best threshold differs from `0.85`, update the default in `geoecon/config.py` (`cluster_cosine_threshold`) and the `CLUSTER_COSINE_THRESHOLD` note. Record the chosen value and the observed separation as a comment in this plan's task and in the commit message.

- [ ] **Step 4: Commit**

```bash
git add scripts/tune_cluster_threshold.py geoecon/config.py
git commit -m "chore(cluster): threshold tuning script + tuned cosine default"
```

---

## Task 5: End-to-end smoke check + docs

**Files:**
- Modify: `PIPELINE_BLUEPRINT.md`
- No new test; verifies wiring + stage list.

- [ ] **Step 1: Verify imports, stage list, and cluster signature**

Run:
```bash
.venv/bin/python -c "from geoecon import orchestrator, regions; from geoecon.config import Config; \
import inspect; from geoecon.stages import cluster; \
cfg = Config.from_env({'DB_DSN':'x','MAETCENTRUM_KEY':'k','SMTP_HOST':'h','SMTP_USER':'u','SMTP_PASSWORD':'p','FROM_ADDR':'f','RECIPIENTS':'r@x'}); \
print([n for n,_ in orchestrator._default_stages(cfg, client=None)]); \
print('cluster.run params:', list(inspect.signature(cluster.run).parameters)); \
print('embedding_model:', cfg.embedding_model, '| cosine:', cfg.cluster_cosine_threshold)"
```
Expected output:
```
['discovery', 'extraction', 'gate', 'cluster', 'synthesis', 'summary', 'render', 'send']
cluster.run params: ['cfg', 'repo', 'run_id', 'client']
embedding_model: multilingual-e5-large-instruct | cosine: 0.85
```

- [ ] **Step 2: Full suite**

Run: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres .venv/bin/python -m pytest tests/unit -q`
Expected: PASS, pristine.

- [ ] **Step 3: Doc note**

In `PIPELINE_BLUEPRINT.md`, update §4 (Stage 2 — Clustering) to state that clustering now uses `multilingual-e5-large-instruct` cosine similarity over title + body-lead (cross-country same-events merge), with the lexical title-Jaccard merge retained only as a fallback when the embedding endpoint is unavailable; and add one line to §13 noting the embedding-clustering upgrade and that `cluster_cosine_threshold` governs merge strictness. Keep it terse (~6 lines total).

- [ ] **Step 4: Commit**

```bash
git add PIPELINE_BLUEPRINT.md
git commit -m "docs: embedding-based semantic clustering replaces lexical dedup"
```

---

## Self-Review

**Spec coverage:**
- §3 approach (embed → cosine union-find, drop same-country, lexical fallback) → Task 3. ✓
- §4.1 `LLMClient.embed()` → Task 2. ✓
- §4.2 `cluster.run` rewrite + centroid + empty-text handling → Task 3. ✓
- §4.3 config (`embedding_model` default, `cluster_cosine_threshold`) → Task 1. ✓
- §4.4 orchestrator wiring → Task 3 Step 5. ✓
- §6 threshold tuning against run-7 → Task 4. ✓
- §7 robustness (failure → lexical; empty-text → singleton) → Task 3 (`_group_embedding` embeddable guard, fallback branch) + test. ✓
- §8 tests (embed success/failure/batching; cross-country merge; fallback; centroid) → Tasks 2 & 3. ✓
- §9 acceptance (stage list, signature, defaults) → Task 5. ✓
- Egypt dual-membership untouched (primary→bucket logic unchanged). ✓

**Placeholder scan:** Every code step shows full code. Task 4 Step 2 is an explicit controller/operational step (run the sweep), not a placeholder. The `if False` scaffolding in the test (Task 3 Step 1) is called out for removal. ✓

**Type consistency:** `cluster.run(cfg, repo, run_id, client)` used in orchestrator (Task 3 Step 5), tests (Task 3 Step 1), and the smoke check (Task 5). `insert_cluster(..., centroid=None)` defined (Task 3 Step 4) and called (Task 3 Step 3) with the same trailing kwarg; FakeRepo mirrors it. `embed(model, texts, *, batch_size)` defined (Task 2) and called in cluster.run + tune script with `(cfg.embedding_model, texts)`. `embed_text` / `_group_embedding` defined in Task 3 and reused by Task 4. ✓
