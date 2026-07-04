"""Query-driven discovery -- the search spine (spec 2026-07-04).

Fans out (country x lang x family) queries per provider; converges into the
same `articles` table as GDELT/registry discovery via url_hash dedup. Contract
mirrors discovery_registry: window-filter, one-bad-query-never-aborts,
insert_article(fetch_status='pending'). Precision is the LLM gate's job."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .. import gdelt, urlnorm
from ..search import matrix

log = logging.getLogger("geoecon.discovery_search")


def _host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _default_providers():
    from ..search.googlenews import GoogleNewsProvider
    return [GoogleNewsProvider()]


def run(cfg, repo, run_id: int, *, providers=None, countries=None,
        keywords=None, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(hours=cfg.window_hours)
    providers = providers if providers is not None else _default_providers()
    countries = countries if countries is not None else matrix.load_countries(cfg.countries_path)
    keywords = keywords if keywords is not None else matrix.load_keywords(cfg.keywords_path)

    feed_limit = cfg.search_country_cap
    if cfg.limit:
        feed_limit = min(feed_limit, cfg.limit)

    discovered = decode_failures = feeds_failed = feeds_empty = 0
    per_provider: dict[str, int] = {}
    seen_hashes: set[str] = set()

    for prov in providers:
        per_provider.setdefault(prov.name, 0)
        for spec in countries:
            langs = spec.langs or (("en",) if spec.global_en else ())
            for lang in langs:
                for family in ("family1", "family2"):
                    terms = keywords[family].get(lang)
                    if not terms:
                        continue
                    expr = matrix.or_expr(terms)
                    try:
                        items = prov.search(expr, spec, lang, cfg.window_hours, feed_limit)
                    except Exception as e:  # one bad feed must not kill the stage
                        feeds_failed += 1
                        log.warning("search %s %s/%s/%s failed: %s",
                                    prov.name, spec.fips, lang, family, e)
                        continue
                    if not items:
                        feeds_empty += 1
                        continue
                    region = gdelt.region_for_country(spec.fips)
                    for it in items:
                        if it.ts is not None and it.ts < window_start:
                            continue  # out of window; null ts kept (recall)
                        h = urlnorm.normalized_url_hash(it.url)
                        if h in seen_hashes:
                            continue
                        seen_hashes.add(h)
                        if not it.decoded:
                            decode_failures += 1
                        repo.insert_article(
                            run_id,
                            source_url=it.url,
                            url_hash=h,
                            domain=_host(it.url),
                            region=region,
                            country_fips=spec.fips,
                            source_lang=it.lang,
                            title=it.title,
                            published_at=it.ts,
                            discovery_source=f"search:{prov.name}",
                        )
                        discovered += 1
                        per_provider[prov.name] += 1

    if discovered == 0 and providers:
        # The spine returning nothing is a recall-floor hole -- make it LOUD
        # (mirrors the tier-1 wire guard in discovery_registry).
        log.warning("recall-floor: discovery_search found 0 items across all "
                    "providers/countries -- endpoint change or network issue?")

    metrics = {
        "search_discovered": discovered,
        "search_per_provider": per_provider,
        "search_decode_failures": decode_failures,
        "search_feeds_failed": feeds_failed,
        "search_feeds_empty": feeds_empty,
    }
    log.info("discovery_search: %s", metrics)
    return metrics
