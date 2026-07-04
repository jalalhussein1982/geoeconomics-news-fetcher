# Wave 1: Throughput + Gate Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the post-fetch pipeline safe for the new discovery volume before `SEARCH_DISCOVERY` flips on: concurrent extraction with timeouts, a gate that no longer fail-opens unjudged articles into briefs, and bounded synthesis inputs.

**Architecture:** Three surgical changes to existing stages — no new stages, no schema changes. (1) `extract.run` fans fetches out on a ThreadPoolExecutor, DB writes stay on the main thread (psycopg conn is not thread-safe). (2) `gate.run` replaces the keep-unjudged fail-open with a re-gate cascade (batch → sub-batches of 5 → per-article → drop with loud telemetry). (3) `synth.build_messages` gains per-article and per-cluster char caps.

**Tech Stack:** Python 3.12, concurrent.futures, trafilatura 2.1.0, pytest.

## Design decisions (the "spec" for this wave)

- **Extraction:** `EXTRACT_WORKERS` default 12 (network-bound; polite to any single host since articles span many domains). Per-fetch timeout via trafilatura's config (`DOWNLOAD_TIMEOUT = EXTRACT_TIMEOUT`, default 20s) — same fetch semantics as today otherwise. Worker does fetch+extract (pure, no DB); main thread drains `as_completed` and writes. Counts identical in meaning.
- **Gate cascade:** an unparsed batch (after the existing single retry) is no longer kept-unjudged. Cascade: sub-batches of 5 (each with 1 retry) → still-unparsed sub-batch goes per-article (1 call each, no retry) → a still-unparsed single article is **dropped** as `gate_unparsed_dropped` (`geoeconomic=False, score=0.0, country_fips=None`). Missing indices inside an otherwise-parsed batch go straight to the per-article step. Rationale: a parse failure at batch-size 1 means systemically broken model output; keeping such articles unjudged is how US stories reached the ME brief. Telemetry: `gate_unparsed` now counts articles that *entered* the cascade; new `gate_unparsed_dropped` counts drops; WARNING when drops > 5% of total. Cost bound: cascade only fires on misbehavior; worst case per 15-batch ≈ 2 + 3×2 + 15 = 23 calls.
- **Synthesis caps:** `SYNTH_BODY_CHARS` default 6000 (≈1.5k tokens) truncates each article body with an explicit `[… truncated]` marker; `SYNTH_TOTAL_CHARS` default 30000 — once cumulative rendered length exceeds it, remaining member articles render as `[HEADLINE-ONLY …]` lines. `0` disables either. Bodies arrive primary-first only insofar as repo ordering — cap is positional (later members demote first), which biases toward keeping earlier members' bodies; acceptable.
- All three ship ON (they are safety/perf hardening, not behavior experiments); each is env-tunable.

## Global Constraints

- Do not change: repo/DB layer, stage contracts (`run(cfg, repo, run_id, ...) -> dict` metric dicts keep all existing keys), prompt files, orchestrator wiring.
- DB writes only from the main thread in extract (psycopg connection is not thread-safe).
- Existing tests must pass unmodified unless a test asserts the old fail-open behavior (then update it to the new contract and say so in the report).
- Tests: `.venv/bin/python -m pytest tests/unit -q -m "not requires_net"` from repo root.
- Commit after every task; trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Config knobs

**Files:**
- Modify: `geoecon/config.py` (fields after `gdelt_mode`; `from_env` after `gdelt_mode=`)
- Test: `tests/unit/test_config.py` (append)

**Interfaces:**
- Produces: `cfg.extract_workers: int = 12` (EXTRACT_WORKERS), `cfg.extract_timeout: int = 20` (EXTRACT_TIMEOUT, seconds), `cfg.synth_body_chars: int = 6000` (SYNTH_BODY_CHARS, 0 disables), `cfg.synth_total_chars: int = 30000` (SYNTH_TOTAL_CHARS, 0 disables).

- [ ] Step 1: failing tests (follow the file's `{**_ENV, ...}` idiom):

```python
def test_wave1_hardening_defaults():
    cfg = Config.from_env(dict(_ENV))
    assert cfg.extract_workers == 12
    assert cfg.extract_timeout == 20
    assert cfg.synth_body_chars == 6000
    assert cfg.synth_total_chars == 30000


def test_wave1_hardening_env_overrides():
    cfg = Config.from_env({**_ENV, "EXTRACT_WORKERS": "4", "EXTRACT_TIMEOUT": "10",
                           "SYNTH_BODY_CHARS": "0", "SYNTH_TOTAL_CHARS": "5000"})
    assert cfg.extract_workers == 4
    assert cfg.extract_timeout == 10
    assert cfg.synth_body_chars == 0
    assert cfg.synth_total_chars == 5000
```

- [ ] Step 2: run → FAIL. Step 3: add fields with comments in the config style:

```python
    # Wave-1 hardening (2026-07-04): throughput + input-bounding knobs.
    extract_workers: int = 12         # EXTRACT_WORKERS; thread-pool width for body fetches
    extract_timeout: int = 20         # EXTRACT_TIMEOUT; per-fetch download timeout (s)
    synth_body_chars: int = 6000      # SYNTH_BODY_CHARS; per-article body cap into synthesis (0 = off)
    synth_total_chars: int = 30000    # SYNTH_TOTAL_CHARS; per-cluster input cap (0 = off)
```

and the four `from_env` lines (`int(env.get("EXTRACT_WORKERS", "12"))` etc.).

- [ ] Step 4: run → PASS. Step 5: commit `feat(config): wave-1 hardening knobs (extract workers/timeout, synth input caps)`.

---

### Task 2: Concurrent extraction with timeout

**Files:**
- Modify: `geoecon/stages/extract.py`
- Test: `tests/unit/test_extract.py` (check name — if extraction tests live elsewhere, append there)

**Interfaces:**
- Consumes: `cfg.extract_workers`, `cfg.extract_timeout` (Task 1).
- Produces: same `run(cfg, repo, run_id, *, fetcher=None) -> dict` counts. `_default_fetcher` becomes `_make_fetcher(timeout_s)` returning a closure that calls `trafilatura.fetch_url(url, config=...)` with `DOWNLOAD_TIMEOUT` set (trafilatura 2.1.0: `from trafilatura.settings import use_config`; `c = use_config(); c.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(timeout_s))` — verify API against installed version before assuming; if `use_config` differs in 2.1, use `copy(trafilatura.settings.DEFAULT_CONFIG)`).

- [ ] Step 1: failing tests:

```python
def test_extract_run_parallel_processes_all_and_counts():
    # 30 articles, fetcher records which thread it ran on; workers=4
    import threading
    seen_threads = set()
    def fetcher(url):
        seen_threads.add(threading.current_thread().name)
        return "<html><body>" + ("x" * 300) + "</body></html>"
    arts = [SimpleNamespace(article_id=i, source_url=f"https://x/{i}") for i in range(30)]
    repo = FakeRepo(arts)   # articles_for_run -> arts; update_extraction captures
    counts = extract.run(_cfg(EXTRACT_WORKERS="4"), repo, 1, fetcher=fetcher)
    assert sum(counts.values()) == 30
    assert len(repo.updates) == 30                      # every article written exactly once
    assert len(seen_threads) > 1                        # actually ran on multiple threads


def test_extract_one_bad_fetch_isolated():
    def fetcher(url):
        if url.endswith("/13"): raise RuntimeError("boom")
        return None
    arts = [SimpleNamespace(article_id=i, source_url=f"https://x/{i}") for i in (12, 13, 14)]
    repo = FakeRepo(arts)
    counts = extract.run(_cfg(EXTRACT_WORKERS="3"), repo, 1, fetcher=fetcher)
    assert counts["failed"] == 1 and counts["headline_only"] == 2
```

(Write `FakeRepo`/`_cfg` helpers in the test file if absent; `update_extraction(article_id, body, fetch_status, source_lang=None, fetch_error=None, title=None)` must be captured keyed by article_id. Also keep/adapt any existing extraction tests — serial behavior via `EXTRACT_WORKERS=1` must still work.)

- [ ] Step 2: run → FAIL. Step 3: implement:

```python
def run(cfg: Config, repo, run_id: int, *, fetcher=None) -> dict:
    if fetcher is None:
        fetcher = _make_fetcher(getattr(cfg, "extract_timeout", 20))
    arts = repo.articles_for_run(run_id, fetch_status="pending")
    counts = {"extracted": 0, "headline_only": 0, "failed": 0}
    workers = max(1, getattr(cfg, "extract_workers", 12))
    # Fetch+extract in threads (pure, no DB); write results on THIS thread only —
    # the psycopg connection is not safe for concurrent use.
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(extract_one, a.source_url, fetcher=fetcher): a for a in arts}
        for fut in as_completed(futs):
            a = futs[fut]
            body, title, status, lang = fut.result()   # extract_one never raises
            repo.update_extraction(a.article_id, body, status, source_lang=lang,
                                   fetch_error=None if status != "failed" else "extraction failed",
                                   title=title)
            counts[status] += 1
    log.info("extraction: %s", counts)
    return counts
```

plus `_make_fetcher(timeout_s)` with the trafilatura config (and module imports). `extract_one` unchanged.

- [ ] Step 4: run new + full suite → PASS. Step 5: commit `perf(extract): thread-pool body fetches (EXTRACT_WORKERS) + per-fetch timeout; DB writes stay main-thread`.

---

### Task 3: Gate re-gate cascade (no more unjudged keeps)

**Files:**
- Modify: `geoecon/stages/gate.py`
- Test: `tests/unit/test_gate.py` (append; UPDATE any test asserting the old fail-open keep — disclose in report)

**Interfaces:**
- Consumes: existing `client.complete`, `parse_verdicts`, `build_gate_messages`.
- Produces: same `run()` signature/metrics plus `gate_unparsed_dropped` key. Internal helper `_judge(client, cfg, prompt, batch, *, retries=1) -> dict[int, dict] | None` (call + parse, `retries` extra attempts; returns index→verdict map or None).

Behavior contract:
1. Batch parses → verdicts applied as today; indices MISSING from the parsed response are collected and sent to the per-article step (not kept unjudged).
2. Batch unparsed after retry → split into sub-batches of ≤5, each judged with 1 retry; a still-unparsed sub-batch → per-article (no retry); a still-unparsed single → `upsert_gate(geoeconomic=False, score=0.0, country=None, reason="gate_unparsed_dropped", country_fips=None)`, `gate_dropped`+1 and `gate_unparsed_dropped`+1.
3. Every article that entered any cascade step increments `gate_unparsed` (it means "the model failed to judge this article on the first pass").
4. After the loop: `if counts["gate_unparsed_dropped"] > 0.05 * max(1, counts["gate_total"]): log.warning("gate: %d articles dropped unjudged (unparsable model output) — systemic LLM issue?", ...)`.

- [ ] Step 1: failing tests with a scripted fake client, e.g.:

```python
class ScriptedClient:
    """Returns canned responses in order; records the batch sizes it was asked to judge."""
    def __init__(self, responses): self.responses = list(responses); self.batch_sizes = []
    def complete(self, model, messages, **kw):
        user = messages[1]["content"]
        self.batch_sizes.append(user.count("] COUNTRY_HINT=") or user.count("COUNTRY_HINT="))
        text = self.responses.pop(0) if self.responses else "garbage"
        return SimpleNamespace(final_answer=text, ok=lambda: True)
```

Tests to write (derive exact canned-response sequences from the contract above):
- `test_unparsed_batch_cascades_to_subbatches`: batch of 6 (bs=6), responses: garbage, garbage (full batch + retry) then a VALID verdict array for each sub-batch of 5 and 1 → all 6 judged normally, `gate_unparsed == 6`, `gate_unparsed_dropped == 0`.
- `test_totally_unparsable_drops_not_keeps`: all responses garbage → every article dropped with reason `gate_unparsed_dropped`; `gate_kept == 0`; `gate_unparsed_dropped == N`; captured upsert has `geoeconomic=False, country_fips=None`.
- `test_missing_index_goes_to_per_article_not_keep`: batch of 3 parses but response covers only i=0,1 → article 2 gets one per-article call; if that returns a valid verdict it is applied; assert no `gate_unparsed` keep with score 0.0/reason gate_unparsed exists anymore.

- [ ] Step 2: run → FAIL (and note which existing tests assert old fail-open; update them). Step 3: implement per the contract (restructure `run` around `_judge`; keep the happy path identical). Step 4: full suite → PASS. Step 5: commit `fix(gate): replace fail-open keeps with re-gate cascade (subbatch->per-article->drop) + drop telemetry`.

---

### Task 4: Synthesis input caps + full-suite gate

**Files:**
- Modify: `geoecon/stages/synth.py` (`build_messages` + its call site in `run`)
- Test: `tests/unit/test_synth.py` (append)

**Interfaces:**
- Consumes: `cfg.synth_body_chars`, `cfg.synth_total_chars` (Task 1).
- Produces: `build_messages(prompt, articles, *, body_chars: int = 0, total_chars: int = 0)` — 0 = today's uncapped behavior (back-compat default); `run` passes the cfg values.

Behavior:
- Per article with a body: if `body_chars > 0` and `len(body) > body_chars` → `body[:body_chars] + "\n[… truncated]"`.
- Cumulative: parts assembled in member order; once `sum(len(p) for p in parts_so_far) > total_chars` (and `total_chars > 0`), each REMAINING article renders as the existing headline-only line format regardless of body.
- Headline-only articles render exactly as today.

- [ ] Step 1: failing tests:

```python
def test_build_messages_caps_body_chars():
    arts = [SimpleNamespace(source_lang="es", title="T", body="x" * 10000, source_url="u")]
    msgs = synth.build_messages("P", arts, body_chars=6000)
    user = msgs[1]["content"]
    assert "[… truncated]" in user and len(user) < 7000


def test_build_messages_total_cap_demotes_tail_to_headline_only():
    arts = [SimpleNamespace(source_lang="es", title=f"T{i}", body="x" * 900, source_url=f"u{i}")
            for i in range(5)]
    msgs = synth.build_messages("P", arts, body_chars=0, total_chars=2000)
    user = msgs[1]["content"]
    assert user.count("HEADLINE-ONLY") >= 2          # tail demoted
    assert "T0" in user and "x" * 100 in user        # head keeps its body


def test_build_messages_zero_caps_is_uncapped():
    arts = [SimpleNamespace(source_lang="es", title="T", body="x" * 10000, source_url="u")]
    user = synth.build_messages("P", arts)[1]["content"]
    assert len(user) > 10000 and "truncated" not in user
```

- [ ] Step 2: run → FAIL. Step 3: implement (find `build_messages(...)` call in `run` and pass `body_chars=getattr(cfg, "synth_body_chars", 0), total_chars=getattr(cfg, "synth_total_chars", 0)`). Step 4: full suite `.venv/bin/python -m pytest tests/unit -q -m "not requires_net"` → ALL green. Step 5: commit `feat(synth): bound synthesis inputs (per-article + per-cluster caps) -- tames token cost and table-dump number-salad input`.
