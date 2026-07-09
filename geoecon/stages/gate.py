"""Geoeconomic gate — LLM relevance + primary-country, batched."""
from __future__ import annotations

import json
import logging
import re

from .. import regions

log = logging.getLogger("geoecon.gate")

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.S)


def _lead(art, n: int = 400) -> str:
    return (art.body or "")[:n]


def build_gate_messages(prompt: str, batch) -> list[dict]:
    parts = []
    for i, a in enumerate(batch):
        themes = ", ".join((a.gkg_themes or [])[:12])
        parts.append(
            f"[{i}] COUNTRY_HINT={a.country_fips or '?'} | TITLE: {a.title or '(no title)'}\n"
            f"THEMES: {themes}\nLEAD: {_lead(a)}"
        )
    return [{"role": "system", "content": prompt},
            {"role": "user", "content": "\n\n".join(parts)}]


def parse_verdicts(text: str):
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_ARRAY_RE.search(text)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, list) else None


def _judge(client, cfg, prompt, batch, *, retries: int = 1) -> dict[int, dict] | None:
    """Call the gate model for `batch`, parsing the response into an index->verdict
    map. Retries the SAME messages up to `retries` extra times while the response
    is unparsable. Returns None if every attempt fails to parse."""
    messages = build_gate_messages(prompt, batch)
    for _ in range(1 + retries):
        resp = client.complete(cfg.gate_model, messages,
                               temperature=0, seed=12345,
                               max_tokens=getattr(cfg, "gate_max_tokens", 4096))
        verdicts = parse_verdicts(getattr(resp, "final_answer", "") or "") if resp.ok() else None
        if verdicts is not None:
            by_i = {}
            for v in verdicts:
                if isinstance(v, dict) and isinstance(v.get("i"), int):
                    by_i[v["i"]] = v
            return by_i
    return None


def _apply_verdict(repo, cfg, counts, art, v: dict) -> None:
    """Apply one parsed verdict to `art` via the standard keep/drop/score/fips
    logic. This is the single code path used by both the happy path and every
    step of the re-gate cascade."""
    geo = bool(v.get("geoeconomic"))
    pc = (v.get("primary_country") or "").strip()
    fips = regions.country_to_fips(pc)
    try:
        score = float(v.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    reason = (v.get("reason") or "")[:200]
    keep = bool(geo and fips and regions.buckets_for_country(fips, cfg.buckets))
    repo.upsert_gate(art.article_id, geoeconomic=keep, score=score,
                     country=pc or None, reason=reason,
                     country_fips=fips if keep else None)
    counts["gate_kept" if keep else "gate_dropped"] += 1


def _drop_unparsed(repo, counts, art) -> None:
    """Terminal cascade step: the model never produced a usable verdict for this
    article, even alone. Drop it rather than fail open."""
    repo.upsert_gate(art.article_id, geoeconomic=False, score=0.0, country=None,
                     reason="gate_unparsed_dropped", country_fips=None)
    counts["gate_dropped"] += 1
    counts["gate_unparsed_dropped"] += 1


def _judge_per_article(client, cfg, prompt, arts) -> list:
    """Cascade floor: judge each article alone, no retry. Pure (LLM only —
    thread-safe); returns [(article, verdict|None)] where None means the model
    never produced a usable verdict, even alone."""
    out = []
    for art in arts:
        by_i = _judge(client, cfg, prompt, [art], retries=0)
        out.append((art, by_i.get(0) if by_i else None))
    return out


def _judge_subbatches(client, cfg, prompt, batch) -> list:
    """A full batch was unparsable after its retry. Split into contiguous
    sub-batches of <=5 (order preserved), each judged with one retry; any
    sub-batch (or index within one) still unresolved falls to per-article.
    Pure (LLM only); returns [(article, verdict|None)]."""
    out = []
    for start in range(0, len(batch), 5):
        chunk = batch[start:start + 5]
        by_i = _judge(client, cfg, prompt, chunk, retries=1)
        if by_i is None:
            out.extend(_judge_per_article(client, cfg, prompt, chunk))
            continue
        missing = []
        for idx, art in enumerate(chunk):
            v = by_i.get(idx)
            if v is None:
                missing.append(art)
            else:
                out.append((art, v))
        if missing:
            out.extend(_judge_per_article(client, cfg, prompt, missing))
    return out


def _gate_batch(client, cfg, prompt, batch) -> tuple[list, int]:
    """Judge one top-level batch through the full re-gate cascade. Pure — LLM
    calls only, safe to run in a worker thread. Returns
    ([(article, verdict|None)], unparsed_count) where unparsed_count preserves
    the sequential counting semantics: the whole batch when its top-level judge
    never parsed, else just the indices the model skipped."""
    by_i = _judge(client, cfg, prompt, batch, retries=1)
    if by_i is None:
        # Whole batch unparsable even after retry -> re-gate cascade.
        return _judge_subbatches(client, cfg, prompt, batch), len(batch)
    results, missing = [], []
    for idx, art in enumerate(batch):
        v = by_i.get(idx)
        if v is None:
            missing.append(art)
        else:
            results.append((art, v))
    if missing:
        # Batch parsed, but the model skipped some indices -> per-article,
        # never kept unjudged.
        results.extend(_judge_per_article(client, cfg, prompt, missing))
    return results, len(missing)


def _apply_batch(repo, cfg, counts, batch, outcome) -> None:
    """Apply one batch's cascade outcome. Main thread only — this is the sole
    place gate writes to the repo."""
    results, unparsed = outcome
    counts["gate_total"] += len(batch)
    counts["gate_unparsed"] += unparsed
    for art, v in results:
        if v is None:
            _drop_unparsed(repo, counts, art)
        else:
            _apply_verdict(repo, cfg, counts, art, v)


def run(cfg, repo, run_id, client, *, prompt: str | None = None) -> dict:
    if prompt is None:
        from importlib.resources import files
        prompt = files("geoecon.prompts").joinpath("geoecon_gate.txt").read_text("utf-8")

    arts = [a for a in repo.articles_for_run(run_id) if a.fetch_status != "failed"]
    counts = {"gate_total": 0, "gate_kept": 0, "gate_dropped": 0,
              "gate_unparsed": 0, "gate_unparsed_dropped": 0}
    bs = max(1, cfg.gate_batch_size)
    batches = [arts[start:start + bs] for start in range(0, len(arts), bs)]
    workers = max(1, getattr(cfg, "gate_workers", 4))

    if workers == 1 or len(batches) <= 1:
        for batch in batches:
            _apply_batch(repo, cfg, counts, batch,
                         _gate_batch(client, cfg, prompt, batch))
    else:
        # Batches fan out to threads (LLM round-trips only); every repo write
        # happens on THIS thread — the psycopg connection is not safe for
        # concurrent use (same pattern as extract.py).
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_gate_batch, client, cfg, prompt, b): b
                    for b in batches}
            for fut in as_completed(futs):
                _apply_batch(repo, cfg, counts, futs[fut], fut.result())

    if counts["gate_unparsed_dropped"] > 0.05 * max(1, counts["gate_total"]):
        log.warning("gate: %d articles dropped unjudged (unparsable model output) "
                    "— systemic LLM issue?", counts["gate_unparsed_dropped"])

    log.info("gate: %s", counts)
    return counts
