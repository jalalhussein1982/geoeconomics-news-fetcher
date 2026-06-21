# Per-Country LLM Event Grouping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the failed embedding clustering with per-country LLM event grouping: bucket gated articles by gate-assigned country, then have the LLM partition each country's articles into distinct events.

**Architecture:** In `cluster.run`, group gated articles by `country_fips`; for each country with ≥2 articles, call a new `group_events()` (batched LLM, mirroring `gate.py`) that returns an event partition; on LLM failure fall back to the existing lexical same-country title-Jaccard merge for that country. Fully removes the embedding code (`LLMClient.embed`, cosine merge, `cluster_cosine_threshold`, tuning script, `insert_cluster` centroid).

**Tech Stack:** Python 3.12, the existing OpenAI-compatible `LLMClient.complete` (e-INFRA gateway), psycopg 3, pytest (LLM mocked offline).

## Global Constraints

- Grouping is **per gate-assigned `country_fips`** (cross-country same-events are out of scope). The gate has already set `country_fips` on every kept article.
- `group_events(client, model, country, articles, *, prompt=None) -> list[list[Article]] | None`: deterministic LLM call (`temperature=0, seed=12345`), returns an event partition (every input article in exactly one group) or `None` on parse/validation failure after one retry.
- On `group_events` returning `None` for a country, fall back to the lexical same-country title-Jaccard merge **for that country only**, WARN-logged. The run never breaks.
- Grouping model is config: `dedup_model` default `"deepseek-v3.2"` (env `DEDUP_MODEL`). `dedup_jaccard` is kept (fallback).
- Full embedding revert: remove `LLMClient.embed()` + `tests/unit/test_client.py`, the embedding merge in `cluster.py`, `cluster_cosine_threshold`, `scripts/tune_cluster_threshold.py`, and the `centroid` parameter of `repo.insert_cluster`. Revert `embedding_model` default to `None` (run metadata only).
- Downstream unchanged: per event group, primary = `max(gate_score)` sets `country_fips`/`gate_score`/bucket; `insert_cluster` + `add_membership` per member. Egypt dual-membership preserved.

---

## File Structure

- `geoecon/stages/cluster.py` — add `group_events`/`_parse_groups`/`_lead` (Task 1); rewrite `run`, remove embedding code (Task 2).
- `geoecon/prompts/event_grouping.txt` — new grouping prompt (Task 1).
- `geoecon/config.py` — add `dedup_model` (Task 1); remove `cluster_cosine_threshold`, revert `embedding_model` (Task 2).
- `geoecon/llm/client.py` — remove `embed()` (Task 2).
- `geoecon/state/repo.py` — remove `insert_cluster` `centroid` (Task 2).
- `scripts/tune_cluster_threshold.py` — delete (Task 2).
- Tests: `tests/unit/test_cluster.py` (group_events in Task 1; run in Task 2), `tests/unit/test_config.py` (Tasks 1–2), `tests/unit/test_client.py` (delete, Task 2), `tests/unit/test_repo.py` (Task 2).
- `PIPELINE_BLUEPRINT.md` — doc note (Task 3).

---

## Task 1: `group_events` + prompt + `dedup_model` config (additive)

**Files:**
- Create: `geoecon/prompts/event_grouping.txt`
- Modify: `geoecon/stages/cluster.py` (add functions; leave existing code), `geoecon/config.py` (add `dedup_model`)
- Test: `tests/unit/test_cluster.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces: `cluster.group_events(client, model, country, articles, *, prompt=None) -> list[list[Article]] | None`; `cluster._parse_groups(text, n) -> list[list[int]] | None`; `cfg.dedup_model` (str, default `"deepseek-v3.2"`).
- Consumes: `LLMClient.complete(model, messages, **params)` returning an object with `.ok()` and `.final_answer`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_cluster.py` (top of file already imports `cluster as cluster_stage` and `Article`; add these):
```python
class _Resp:
    def __init__(self, fa, ok=True): self.final_answer = fa; self._ok = ok
    def ok(self): return self._ok

class _LLM:
    """complete() returns the next queued answer string."""
    def __init__(self, answers): self._answers = list(answers); self.calls = []
    def complete(self, model, messages, **kw):
        self.calls.append((model, messages, kw)); return _Resp(self._answers.pop(0))


def test_group_events_partitions_articles():
    arts = [_art(1, "Bolivia declares state of emergency", country="BL"),
            _art(2, "Bolivia emergency clears blockades", country="BL"),
            _art(3, "Bolivia central bank raises rates", country="BL")]
    llm = _LLM(['{"groups": [[0, 1], [2]]}'])
    out = cluster_stage.group_events(llm, "m", "BL", arts, prompt="SYS {COUNTRY}")
    assert [[a.article_id for a in g] for g in out] == [[1, 2], [3]]
    # determinism + country interpolation
    assert llm.calls[0][2]["temperature"] == 0 and llm.calls[0][2]["seed"] == 12345
    assert "BL" in llm.calls[0][1][0]["content"]


def test_group_events_returns_none_on_bad_json_after_retry():
    arts = [_art(1, "x", country="BL"), _art(2, "y", country="BL")]
    llm = _LLM(["not json", "still not json"])
    assert cluster_stage.group_events(llm, "m", "BL", arts, prompt="SYS") is None
    assert len(llm.calls) == 2          # retried once


def test_parse_groups_rejects_non_partition():
    assert cluster_stage._parse_groups('{"groups": [[0]]}', 2) is None     # missing index 1
    assert cluster_stage._parse_groups('{"groups": [[0,1],[1]]}', 2) is None  # dup index
    assert cluster_stage._parse_groups('{"groups": [[0],[1]]}', 2) == [[0], [1]]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_cluster.py -k "group_events or parse_groups" -v`
Expected: FAIL — `group_events`/`_parse_groups` not defined.

- [ ] **Step 3: Create the prompt**

Create `geoecon/prompts/event_grouping.txt`:
```
You are grouping news articles that all concern {COUNTRY}. Group them so that each group is exactly one real-world event or story: near-duplicate coverage, the same announcement or development reported by multiple outlets, and direct follow-ups on one event belong in the same group; genuinely different events or stories go in separate groups.

You are given numbered articles, each as "[N] headline" followed by a short lead. Return ONLY a JSON object of the form:
{"groups": [[0, 3, 5], [1], [2, 4]]}
where each inner list holds the numbers of the articles that are the same event. Every article number must appear in exactly one group. Output no text outside the JSON.
```

- [ ] **Step 4: Implement `group_events` + `_parse_groups`**

Add to `geoecon/stages/cluster.py` (add `import json` to the top import line; `re` is already imported):
```python
def _lead(art, n: int = 200) -> str:
    return (art.body or "")[:n]


_OBJ_RE = re.compile(r"\{.*\}", re.S)
_ARR_RE = re.compile(r"\[.*\]", re.S)


def _parse_groups(text, n):
    """Parse the LLM reply into a validated partition of range(n), or None."""
    if not text:
        return None
    raw = None
    for candidate in (text,
                      (_OBJ_RE.search(text).group(0) if _OBJ_RE.search(text) else None),
                      (_ARR_RE.search(text).group(0) if _ARR_RE.search(text) else None)):
        if candidate is None:
            continue
        try:
            raw = json.loads(candidate)
            break
        except json.JSONDecodeError:
            raw = None
    if isinstance(raw, dict):
        raw = raw.get("groups")
    if not isinstance(raw, list):
        return None
    groups, seen = [], set()
    for g in raw:
        if not isinstance(g, list):
            return None
        members = []
        for x in g:
            if not isinstance(x, int) or x < 0 or x >= n or x in seen:
                return None
            seen.add(x); members.append(x)
        if members:
            groups.append(members)
    if len(seen) != n:
        return None
    return groups


def group_events(client, model, country, articles, *, prompt=None):
    """Partition one country's articles into event groups via the LLM.
    Returns a list of Article groups, or None on failure (caller falls back)."""
    if prompt is None:
        from importlib.resources import files
        prompt = files("geoecon.prompts").joinpath("event_grouping.txt").read_text("utf-8")
    system = prompt.replace("{COUNTRY}", country or "this region")
    parts = [f"[{i}] {a.title or '(no title)'}\n{_lead(a)}" for i, a in enumerate(articles)]
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "\n\n".join(parts)}]
    for _ in range(2):
        resp = client.complete(model, messages, temperature=0, seed=12345, max_tokens=2048)
        groups = _parse_groups(getattr(resp, "final_answer", "") or "", len(articles)) if resp.ok() else None
        if groups is not None:
            return [[articles[i] for i in g] for g in groups]
    return None
```

- [ ] **Step 5: Add `dedup_model` config + test**

In `geoecon/config.py`, add the field (next to `gate_model`) and the `from_env` line:
```python
    gate_model: str = "deepseek-v3.2"
    dedup_model: str = "deepseek-v3.2"
```
```python
            gate_model=env.get("GATE_MODEL", "deepseek-v3.2"),
            dedup_model=env.get("DEDUP_MODEL", "deepseek-v3.2"),
```
Add to `tests/unit/test_config.py`:
```python
def test_dedup_model_default_and_override():
    from geoecon.config import Config
    base = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
            "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"}
    assert Config.from_env(base).dedup_model == "deepseek-v3.2"
    assert Config.from_env({**base, "DEDUP_MODEL": "kimi-k2.6"}).dedup_model == "kimi-k2.6"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_cluster.py -k "group_events or parse_groups" tests/unit/test_config.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add geoecon/stages/cluster.py geoecon/prompts/event_grouping.txt geoecon/config.py tests/unit/test_cluster.py tests/unit/test_config.py
git commit -m "feat(cluster): per-country LLM event-grouping function + dedup_model config"
```

---

## Task 2: Cut `cluster.run` over to LLM grouping + remove embedding code

**Files:**
- Modify: `geoecon/stages/cluster.py` (rewrite `run`, remove embedding helpers), `geoecon/state/repo.py` (`insert_cluster` no centroid), `geoecon/config.py` (remove `cluster_cosine_threshold`, revert `embedding_model`)
- Delete: `scripts/tune_cluster_threshold.py`, `tests/unit/test_client.py`
- Modify: `geoecon/llm/client.py` (remove `embed`), `tests/unit/test_cluster.py`, `tests/unit/test_repo.py`, `tests/unit/test_config.py`

**Interfaces:**
- Consumes: `cluster.group_events` (Task 1), `cfg.dedup_model`, `cfg.dedup_jaccard`, `cfg.buckets`; `regions.buckets_for_country`; `repo.articles_for_run`, `repo.insert_cluster` (no centroid), `repo.add_membership`.
- Produces: `cluster.run(cfg, repo, run_id, client) -> {"clusters": int, "max_cluster_size": int}`.

- [ ] **Step 1: Write/replace the failing tests**

In `tests/unit/test_cluster.py`, replace the embedding-era cluster.run tests (`test_embedding_merges_same_event_across_countries`, `test_embedding_failure_falls_back_to_lexical`, and the `FakeClient`/`_CFG` they use) with the following (keep `test_jaccard_and_tokens`, `_art`, `FakeRepo`, and the Task-1 `_LLM`/`_Resp`/group_events tests). Update `FakeRepo.insert_cluster` to drop `centroid`, and `_CFG` to use `dedup_model`/`dedup_jaccard`:
```python
class FakeRepo:
    def __init__(self, arts):
        self._arts = arts; self.clusters = []; self.memberships = []
    def articles_for_run(self, rid, fetch_status=None): return self._arts
    def insert_cluster(self, run_id, fp, region, langs, count,
                       framing_divergent=False, country_fips=None, gate_score=None):
        cid = len(self.clusters) + 1
        self.clusters.append(dict(cid=cid, count=count, country_fips=country_fips,
                                  gate_score=gate_score)); return cid
    def add_membership(self, cid, aid, frame_label=None):
        self.memberships.append((cid, aid))


_CFG = type("C", (), {"buckets": ("middle_east", "north_africa", "latam"),
                      "dedup_jaccard": 0.6, "dedup_model": "m"})()


def test_run_groups_per_country_via_llm():
    # SA has 2 articles the LLM groups as one event; EG has 1 (singleton, no LLM call).
    arts = [_art(1, "Saudi oil cut", country="SA", score=0.9),
            _art(2, "Saudi reduces output", country="SA", score=0.7),
            _art(3, "Egypt pound rallies", country="EG", score=0.8)]
    llm = _LLM(['{"groups": [[0, 1]]}'])     # one call, for SA (2 arts); EG is a singleton
    repo = FakeRepo(arts)
    out = cluster_stage.run(_CFG, repo, 1, llm)
    assert out["clusters"] == 2 and out["max_cluster_size"] == 2
    assert len(llm.calls) == 1               # only the multi-article country hit the LLM
    merged = [c for c in repo.clusters if c["count"] == 2][0]
    assert merged["country_fips"] == "SA" and abs(merged["gate_score"] - 0.9) < 1e-6


def test_run_falls_back_to_lexical_on_llm_failure():
    arts = [_art(1, "Saudi non-oil exports rise 10.7 percent", country="SA", score=0.9),
            _art(2, "Saudi non-oil exports rise 10.7%, driven by chemicals", country="SA", score=0.7),
            _art(3, "Saudi tourism numbers climb", country="SA", score=0.5)]
    llm = _LLM(["bad", "bad"])               # grouping fails -> lexical fallback for SA
    repo = FakeRepo(arts)
    out = cluster_stage.run(_CFG, repo, 1, llm)
    # arts 1&2 merge by title Jaccard; art 3 separate
    assert out["clusters"] == 2
    assert sorted(c["count"] for c in repo.clusters) == [1, 2]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_cluster.py -v`
Expected: FAIL — `cluster.run` still calls `client.embed`/`cfg.cluster_cosine_threshold`; `FakeRepo.insert_cluster` signature changed.

- [ ] **Step 3: Rewrite `geoecon/stages/cluster.py`**

Replace the file with (keeps `fingerprint`/`title_tokens`/`jaccard`/`_UnionFind` and the Task-1 `group_events`/`_parse_groups`/`_lead`; removes `embed_text`/`_l2norm`/`_group_embedding`/`_group_lexical`/`EMBED_*`):
```python
from __future__ import annotations
import hashlib, json, logging, re
from .. import regions

log = logging.getLogger("geoecon.cluster")


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


def _lead(art, n: int = 200) -> str:
    return (art.body or "")[:n]


_OBJ_RE = re.compile(r"\{.*\}", re.S)
_ARR_RE = re.compile(r"\[.*\]", re.S)


def _parse_groups(text, n):
    """Parse the LLM reply into a validated partition of range(n), or None."""
    if not text:
        return None
    raw = None
    for candidate in (text,
                      (_OBJ_RE.search(text).group(0) if _OBJ_RE.search(text) else None),
                      (_ARR_RE.search(text).group(0) if _ARR_RE.search(text) else None)):
        if candidate is None:
            continue
        try:
            raw = json.loads(candidate)
            break
        except json.JSONDecodeError:
            raw = None
    if isinstance(raw, dict):
        raw = raw.get("groups")
    if not isinstance(raw, list):
        return None
    groups, seen = [], set()
    for g in raw:
        if not isinstance(g, list):
            return None
        members = []
        for x in g:
            if not isinstance(x, int) or x < 0 or x >= n or x in seen:
                return None
            seen.add(x); members.append(x)
        if members:
            groups.append(members)
    if len(seen) != n:
        return None
    return groups


def group_events(client, model, country, articles, *, prompt=None):
    """Partition one country's articles into event groups via the LLM.
    Returns a list of Article groups, or None on failure (caller falls back)."""
    if prompt is None:
        from importlib.resources import files
        prompt = files("geoecon.prompts").joinpath("event_grouping.txt").read_text("utf-8")
    system = prompt.replace("{COUNTRY}", country or "this region")
    parts = [f"[{i}] {a.title or '(no title)'}\n{_lead(a)}" for i, a in enumerate(articles)]
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "\n\n".join(parts)}]
    for _ in range(2):
        resp = client.complete(model, messages, temperature=0, seed=12345, max_tokens=2048)
        groups = _parse_groups(getattr(resp, "final_answer", "") or "", len(articles)) if resp.ok() else None
        if groups is not None:
            return [[articles[i] for i in g] for g in groups]
    return None


def _lexical_groups(arts, threshold):
    """Per-country fallback: title-token Jaccard union-find -> list of Article groups."""
    toks = [title_tokens(a.title) for a in arts]
    uf = _UnionFind(len(arts))
    for i in range(len(arts)):
        for j in range(i + 1, len(arts)):
            if toks[i] and toks[j] and jaccard(toks[i], toks[j]) >= threshold:
                uf.union(i, j)
    return [[arts[m] for m in members] for members in uf.groups().values()]


def run(cfg, repo, run_id: int, client) -> dict:
    arts = [a for a in repo.articles_for_run(run_id)
            if a.gate_geoeconomic and a.fetch_status != "failed"
            and regions.buckets_for_country(a.country_fips, cfg.buckets)]
    if not arts:
        log.info("cluster: 0 clusters from 0 gated articles")
        return {"clusters": 0, "max_cluster_size": 0}

    by_country: dict = {}
    for a in arts:
        by_country.setdefault(a.country_fips, []).append(a)

    groups: list[list] = []
    for country, c_arts in by_country.items():
        if len(c_arts) == 1:
            groups.append(c_arts); continue
        g = group_events(client, cfg.dedup_model, country, c_arts)
        if g is None:
            log.warning("cluster[%s]: LLM grouping failed, lexical fallback", country)
            g = _lexical_groups(c_arts, cfg.dedup_jaccard)
        groups.extend(g)

    n = 0
    for member_arts in groups:
        primary = max(member_arts, key=lambda a: a.gate_score or 0.0)
        langs = sorted({a.source_lang for a in member_arts if a.source_lang})
        fp = fingerprint(primary.title, primary.source_url)
        cid = repo.insert_cluster(run_id, fp, primary.region, langs, len(member_arts),
                                  country_fips=primary.country_fips, gate_score=primary.gate_score)
        for a in member_arts:
            repo.add_membership(cid, a.article_id)
        n += 1

    max_cluster_size = max((len(g) for g in groups), default=0)
    if arts and max_cluster_size > 0.25 * len(arts):
        log.warning("cluster: largest cluster has %d/%d gated articles (%.0f%%) — verify it is one event",
                    max_cluster_size, len(arts), 100 * max_cluster_size / len(arts))
    log.info("cluster: %d clusters from %d gated articles", n, len(arts))
    return {"clusters": n, "max_cluster_size": max_cluster_size}
```

- [ ] **Step 4: Remove `centroid` from `repo.insert_cluster`**

In `geoecon/state/repo.py`, replace `insert_cluster` with (drop the `centroid` param + column):
```python
    def insert_cluster(self, run_id, event_fingerprint, region, languages, article_count,
                       framing_divergent=False, country_fips=None, gate_score=None) -> int:
        return self.conn.execute(
            """INSERT INTO clusters(run_id, event_fingerprint, region, country_fips,
                 languages, article_count, framing_divergent, gate_score)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING cluster_id""",
            (run_id, event_fingerprint, region, country_fips, languages, article_count,
             framing_divergent, gate_score)).fetchone()[0]
```

- [ ] **Step 5: Remove `LLMClient.embed` + its tests + config + tuning script**

- In `geoecon/llm/client.py`, delete the entire `embed(self, model, texts, *, batch_size=64)` method.
- Delete `tests/unit/test_client.py` (it contained only `embed` tests): `git rm tests/unit/test_client.py`.
- Delete `scripts/tune_cluster_threshold.py`: `git rm scripts/tune_cluster_threshold.py`.
- In `geoecon/config.py`: remove the `cluster_cosine_threshold` field and its `from_env` line; revert `embedding_model` to `embedding_model: str | None = None` and `embedding_model=env.get("EMBEDDING_MODEL") or None`.
- In `tests/unit/test_config.py`, update `test_embedding_and_cosine_defaults_and_overrides`: drop the `cluster_cosine_threshold` assertions and change the embedding-model default assertion:
```python
def test_embedding_and_cosine_defaults_and_overrides():
    from geoecon.config import Config
    base = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
            "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"}
    cfg = Config.from_env(base)
    assert cfg.embedding_model is None
    over = Config.from_env({**base, "EMBEDDING_MODEL": "qwen3-embedding-4b"})
    assert over.embedding_model == "qwen3-embedding-4b"
```

- [ ] **Step 6: Update the `test_repo.py` cluster integration test**

In `tests/unit/test_repo.py`, the test `test_cluster_run_persists_country_fips_via_repo` passes a stub client to `cluster.run`. Its stub `embed` is now unused; the stub must instead provide `complete` so `group_events` works (or force the lexical fallback). Simplest: make the stub's `complete` return an unparseable answer so the run uses the lexical fallback (which still persists the cluster with the right `country_fips`). Replace the stub client in that test with:
```python
    class _LLM:
        def complete(self, model, messages, **kw):
            class R:
                final_answer = "force-fallback"
                def ok(self): return True
            return R()
    cluster.run(cfg, repo, rid, _LLM())
```
Keep the test's existing assertion that the persisted cluster's `country_fips` is correct. (If the test's `cfg` stub lacks `dedup_model`/`dedup_jaccard`, add them.)

- [ ] **Step 7: Run the suite**

Run: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres .venv/bin/python -m pytest tests/unit -q`
Expected: PASS, pristine. (`test_client.py` is gone; cluster/run/config/repo updated.)

- [ ] **Step 8: Commit**

```bash
git add geoecon/stages/cluster.py geoecon/state/repo.py geoecon/llm/client.py geoecon/config.py tests/unit/test_cluster.py tests/unit/test_config.py tests/unit/test_repo.py
git rm tests/unit/test_client.py scripts/tune_cluster_threshold.py
git commit -m "feat(cluster): per-country LLM event grouping replaces embedding clustering"
```

---

## Task 3: End-to-end smoke check + docs

**Files:**
- Modify: `PIPELINE_BLUEPRINT.md`
- No new test; verifies wiring.

- [ ] **Step 1: Verify imports, stage list, and cluster signature**

Run:
```bash
.venv/bin/python -c "from geoecon import orchestrator; from geoecon.config import Config; \
import inspect; from geoecon.stages import cluster; \
cfg = Config.from_env({'DB_DSN':'x','MAETCENTRUM_KEY':'k','SMTP_HOST':'h','SMTP_USER':'u','SMTP_PASSWORD':'p','FROM_ADDR':'f','RECIPIENTS':'r@x'}); \
print([n for n,_ in orchestrator._default_stages(cfg, client=None)]); \
print('cluster.run params:', list(inspect.signature(cluster.run).parameters)); \
print('dedup_model:', cfg.dedup_model, '| embedding_model:', cfg.embedding_model); \
print('has embed:', hasattr(__import__('geoecon.llm.client', fromlist=['LLMClient']).LLMClient, 'embed')); \
print('has cosine cfg:', hasattr(cfg, 'cluster_cosine_threshold'))"
```
Expected output:
```
['discovery', 'extraction', 'gate', 'cluster', 'synthesis', 'summary', 'render', 'send']
cluster.run params: ['cfg', 'repo', 'run_id', 'client']
dedup_model: deepseek-v3.2 | embedding_model: None
has embed: False
has cosine cfg: False
```

- [ ] **Step 2: Full suite**

Run: `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres .venv/bin/python -m pytest tests/unit -q`
Expected: PASS, pristine.

- [ ] **Step 3: Doc note**

In `PIPELINE_BLUEPRINT.md`, update §4 (Stage 2 — Clustering) and the relevant line in §13 to state that event clustering now groups gated articles **per gate-assigned country** and uses the LLM (`dedup_model`) to partition each country's articles into distinct events, with the lexical title-Jaccard merge retained only as the per-country fallback on LLM failure; note that the embedding approach was tried and removed (it over-merged distinct events). Keep it terse (~6 lines), factually accurate to the code.

- [ ] **Step 4: Commit**

```bash
git add PIPELINE_BLUEPRINT.md
git commit -m "docs: per-country LLM event grouping replaces embedding clustering"
```

---

## Self-Review

**Spec coverage:**
- §2 per-country LLM grouping → Task 2 (`run`) using Task 1 `group_events`. ✓
- §3.1 `cluster.run` (bucket by country, singleton, group/fallback, primary/bucket, max_cluster_size) → Task 2. ✓
- §3.2 `group_events` + prompt + partition validation → Task 1. ✓
- §3.3 lexical per-country fallback → Task 2 (`_lexical_groups`). ✓
- §3.4 config (`dedup_model` add; remove `cluster_cosine_threshold`; revert `embedding_model`) → Task 1 (add) + Task 2 (remove/revert). ✓
- §3.5 removals (`embed()`, embedding merge, threshold, tuning script, centroid) → Task 2. ✓
- §5 tests (partition, fallback, singleton, primary/bucket, no centroid) → Tasks 1 & 2. ✓
- §7 acceptance (stage list, signature, no embed/cosine, green suite) → Task 3. ✓

**Placeholder scan:** every code step shows full code; the prompt file is complete; no TBD/TODO. ✓

**Type consistency:** `group_events(client, model, country, articles, *, prompt=None) -> list[list[Article]]|None` defined in Task 1, called in Task 2 `run` with `(client, cfg.dedup_model, country, c_arts)`. `_parse_groups(text, n)` defined + tested Task 1, used inside `group_events`. `_lexical_groups(arts, threshold) -> list[list[Article]]` defined + used in Task 2. `insert_cluster(...)` without `centroid` defined in Task 2 Step 4, called in `run` (Task 2 Step 3) and mirrored by `FakeRepo` (Task 2 Step 1). `cfg.dedup_model`/`cfg.dedup_jaccard` consistent across `_CFG` stub and config. ✓
