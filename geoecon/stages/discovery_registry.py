"""Registry-driven discovery — a second discovery source that converges into the
same `articles` table alongside GDELT (file 01).

Contract mirrors GDELT discovery: window-filter, per-source dispatch, idempotent
`insert_article(... fetch_status='pending')`, one-bad-source-never-aborts. GDELT
stays the firehose tripwire; this stage adds registry-curated recall. Precision
is still the LLM gate's job — Tier-0 sources are NOT gate-bypassed here (that is
Change 5).

`url_hash` uses `gdelt.url_hash` for now; Change 3 swaps both discovery stages to
the normalized hash so cross-source URL variants collapse to one row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .. import gdelt, registry
from ..fetchers import base as fetchers_base

log = logging.getLogger("geoecon.discovery_registry")


def _host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def run(cfg, repo, run_id: int, *, sources=None, get_fetcher=None, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(hours=cfg.window_hours)
    if sources is None:
        sources = registry.load_sources(cfg.sources_path)
    if get_fetcher is None:
        get_fetcher = fetchers_base.get_fetcher

    active_buckets = set(cfg.buckets)
    discovered = 0
    skipped_unverified = skipped_needs_review = skipped_dead = 0
    per_tier: dict[int, int] = {}
    seen_hashes: set[str] = set()

    for src in sources:
        if src.fetch_type == "unverified":
            skipped_unverified += 1
            continue
        if src.fetch_type == "dead":
            skipped_dead += 1
            continue
        if src.status == "needs_review":
            skipped_needs_review += 1
            continue
        # Country-keyed sources must sit in an active bucket; cross-cutting wire/
        # actor sources (bucket=None) always run and let the gate resolve country.
        if src.bucket is not None and src.bucket not in active_buckets:
            continue

        try:
            items = get_fetcher(src).discover(src)
        except Exception as e:  # one bad source must not kill the stage
            log.warning("registry source %s failed: %s", src.id, e)
            continue

        region = gdelt.region_for_country(src.country_fips)
        for it in items:
            if it.ts is not None and it.ts < window_start:
                continue  # out of window; null ts is kept (better recall)
            h = gdelt.url_hash(it.url)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            repo.insert_article(
                run_id,
                source_url=it.url,
                url_hash=h,
                domain=_host(it.url),
                region=region,
                country_fips=src.country_fips,
                source_lang=it.source_lang or src.lang,
                title=it.title,
                discovery_source="registry",
                registry_id=src.id,
                source_tier=src.tier,
                origin_class=src.origin_class or None,
                ownership=src.ownership,
                state_alignment=src.state_alignment,
            )
            discovered += 1
            per_tier[src.tier] = per_tier.get(src.tier, 0) + 1

    if skipped_unverified or skipped_dead:
        log.warning(
            "registry: skipped %d unverified + %d dead source(s) — run Task 0 to resolve mechanics",
            skipped_unverified, skipped_dead,
        )

    metrics = {
        "registry_discovered": discovered,
        "registry_skipped_unverified": skipped_unverified,
        "registry_skipped_needs_review": skipped_needs_review,
        "registry_per_tier": per_tier,
    }
    log.info("discovery_registry: %s", metrics)
    return metrics
