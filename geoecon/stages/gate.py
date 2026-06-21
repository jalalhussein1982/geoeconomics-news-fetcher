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


def run(cfg, repo, run_id, client, *, prompt: str | None = None) -> dict:
    if prompt is None:
        from importlib.resources import files
        prompt = files("geoecon.prompts").joinpath("geoecon_gate.txt").read_text("utf-8")

    arts = [a for a in repo.articles_for_run(run_id) if a.fetch_status != "failed"]
    counts = {"gate_total": 0, "gate_kept": 0, "gate_dropped": 0, "gate_unparsed": 0}
    bs = max(1, cfg.gate_batch_size)

    for start in range(0, len(arts), bs):
        batch = arts[start:start + bs]
        messages = build_gate_messages(prompt, batch)
        resp = client.complete(cfg.gate_model, messages,
                               temperature=0, seed=12345, max_tokens=4096)
        verdicts = parse_verdicts(getattr(resp, "final_answer", "") or "") if resp.ok() else None
        if verdicts is None:
            resp2 = client.complete(cfg.gate_model, messages,
                                    temperature=0, seed=12345, max_tokens=4096)
            verdicts = parse_verdicts(getattr(resp2, "final_answer", "") or "") if resp2.ok() else None

        by_i = {}
        if verdicts is not None:
            for v in verdicts:
                if isinstance(v, dict) and isinstance(v.get("i"), int):
                    by_i[v["i"]] = v

        for idx, art in enumerate(batch):
            counts["gate_total"] += 1
            v = by_i.get(idx)
            if v is None:
                repo.upsert_gate(art.article_id, geoeconomic=True, score=0.0,
                                 country=art.gate_country, reason="gate_unparsed",
                                 country_fips=art.country_fips)
                counts["gate_unparsed"] += 1
                counts["gate_kept"] += 1
                continue
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

    log.info("gate: %s", counts)
    return counts
