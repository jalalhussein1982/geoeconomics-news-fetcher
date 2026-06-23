# geoecon/stages/summary.py
"""Summary stage — one executive summary per non-empty region bucket."""
from __future__ import annotations

import logging

from .. import regions, ranking
from .synth import parse_record

log = logging.getLogger("geoecon.summary")


def build_summary_messages(prompt: str, region_display: str, records) -> list[dict]:
    """Build [system, user] messages for the region executive summary."""
    parts = []
    for rec in records:
        f = rec.fields
        block = "\n".join(x for x in [
            f"HEADLINE: {f.get('headline_en', '')}",
            f"WHAT HAPPENED: {f.get('what_happened_en', '')}",
            f"SIGNIFICANCE: {f.get('geoeconomic_significance_en', '')}",
        ] if x)
        parts.append(block)
    system = prompt.replace("{REGION}", region_display)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n---\n\n".join(parts)},
    ]


def run(cfg, repo, run_id, client, *, prompt: str | None = None) -> dict:
    if prompt is None:
        from importlib.resources import files
        prompt = files("geoecon.prompts").joinpath("region_summary.txt").read_text("utf-8")

    grouped = regions.top_per_bucket(
        repo.records_for_buckets(run_id, cfg.rank_by_corroboration), country_of=lambda t: t[1],
        score_of=ranking.make_score_of(cfg), n=cfg.region_cap, active=cfg.buckets)

    counts = {"summaries_total": 0, "summaries_ok": 0, "summaries_empty": 0}
    for bucket in cfg.buckets:
        items = grouped.get(bucket, [])
        if not items:
            continue
        records = [t[0] for t in items]
        messages = build_summary_messages(prompt, regions.BUCKET_DISPLAY[bucket], records)
        resp = client.complete(cfg.synthesis_model, messages,
                               temperature=0, seed=12345, max_tokens=2048)
        data = parse_record(getattr(resp, "final_answer", "") or "") if resp.ok() else None
        if data and isinstance(data.get("takeaways"), list):
            lede, takeaways = data.get("lede"), data.get("takeaways")
            counts["summaries_ok"] += 1
        else:
            lede, takeaways = None, []
            counts["summaries_empty"] += 1
        repo.upsert_region_summary(run_id, bucket, lede, takeaways,
                                   cfg.synthesis_model, len(records))
        counts["summaries_total"] += 1

    log.info("summary: %s", counts)
    return counts
