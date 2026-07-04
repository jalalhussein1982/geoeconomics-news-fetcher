from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from .. import gdelt, regions, urlnorm
from ..config import Config

log = logging.getLogger("geoecon.discovery")

MASTER_LATEST = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"


def _default_fetch(window_start: datetime, window_end: datetime) -> list[str]:
    """Fetch GKG lines across [window_start, window_end] from the 15-min files.

    Uses the master file list, keeping .gkg.csv.zip URLs whose timestamp is
    in-window.
    """
    master = requests.get(
        "http://data.gdeltproject.org/gdeltv2/masterfilelist.txt",
        timeout=120,
    )
    master.raise_for_status()
    urls = []
    for row in master.text.splitlines():
        parts = row.split()
        if len(parts) == 3 and parts[2].endswith(".gkg.csv.zip"):
            stamp = parts[2].rsplit("/", 1)[-1][:14]
            try:
                ts = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            if window_start <= ts <= window_end:
                urls.append(parts[2])
    lines: list[str] = []
    for u in urls:
        try:
            blob = requests.get(u, timeout=120).content
            lines.extend(gdelt.unzip_gkg_bytes(blob))
        except Exception as e:  # one bad file must not kill discovery
            log.warning("gkg file failed %s: %s", u, e)
    return lines


def run(
    cfg: Config,
    repo,
    run_id: int,
    *,
    fetch=_default_fetch,
    doc_fetch=None,
    countries=None,
    now=None,
) -> dict:
    if getattr(cfg, "gdelt_mode", "gkg") == "docapi":
        return _run_docapi(cfg, repo, run_id, doc_fetch=doc_fetch,
                            countries=countries, now=now)

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=cfg.window_hours)
    lines = fetch(window_start, window_end)

    kept: list[tuple] = []
    skipped = 0
    seen_hashes: set[str] = set()

    for line in lines:
        rec = gdelt.parse_gkg_line(line)
        if rec is None:
            skipped += 1
            continue
        if not gdelt.prefilter_ok(rec.themes):
            continue
        region = gdelt.region_for_country(rec.country)
        if not regions.buckets_for_country(rec.country, cfg.buckets):
            continue
        h = urlnorm.normalized_url_hash(rec.url)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        kept.append((rec, region, h))

    # Per-bucket cap by salience proxy; LLM gate makes the final selection.
    kept.sort(key=lambda x: gdelt.theme_salience(x[0].themes), reverse=True)
    ceiling = cfg.limit or cfg.discovery_ceiling
    per_bucket: dict[str, int] = {}
    capped: list[tuple] = []
    for rec, region, h in kept:
        bs = regions.buckets_for_country(rec.country, cfg.buckets)
        if any(per_bucket.get(b, 0) < ceiling for b in bs):
            for b in bs:
                per_bucket[b] = per_bucket.get(b, 0) + 1
            capped.append((rec, region, h))
    kept = capped

    for rec, region, h in kept:
        repo.insert_article(
            run_id,
            source_url=rec.url,
            url_hash=h,
            domain=rec.domain,
            region=region,
            country_fips=rec.country,
            gkg_themes=rec.themes,
            gkg_tone=rec.tone,
            source_lang=rec.lang,
            title=None,
            gdelt_gkg_id=rec.gkg_id,
            discovery_source="gdelt",
        )

    metrics = {
        "discovered": len(kept),
        "discovery_skipped": skipped,
        "raw_lines": len(lines),
    }
    log.info("discovery: %s", metrics)
    return metrics


def _run_docapi(cfg, repo, run_id, *, doc_fetch=None, countries=None, now=None) -> dict:
    """DOC 2.0 path: one sourcecountry query per matrix country. English
    vocabulary rides GDELT's machine translation across all source languages;
    attribution comes from the query (clean), not most-mentioned-FIPS (noisy)."""
    from .. import gdelt_doc
    from ..search import matrix

    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(hours=cfg.window_hours)
    doc_fetch = doc_fetch or gdelt_doc.fetch_articles
    countries = countries if countries is not None else matrix.load_countries(cfg.countries_path)
    kw = matrix.load_keywords(cfg.keywords_path)
    en_terms = kw["family1"]["en"] + kw["family2"]["en"]

    ceiling = cfg.limit or cfg.discovery_ceiling
    discovered = queries = failed = 0
    per_bucket: dict[str, int] = {}
    seen_hashes: set[str] = set()

    for spec in countries:
        buckets = regions.buckets_for_country(spec.fips, cfg.buckets)
        if not buckets:
            continue
        query = gdelt_doc.build_query(spec.gdelt_sc, en_terms)
        queries += 1
        try:
            arts = doc_fetch(query, cfg.window_hours, max_records=ceiling)
        except Exception as e:  # one bad country must not kill the stage
            failed += 1
            log.warning("gdelt-doc %s failed: %s", spec.fips, e)
            continue
        region = gdelt.region_for_country(spec.fips)
        for a in arts:
            if a.seendate is not None and a.seendate < window_start:
                continue
            if not any(per_bucket.get(b, 0) < ceiling for b in buckets):
                break
            h = urlnorm.normalized_url_hash(a.url)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            repo.insert_article(
                run_id, source_url=a.url, url_hash=h, domain=a.domain,
                region=region, country_fips=spec.fips, source_lang=a.language,
                title=a.title, published_at=a.seendate, discovery_source="gdelt",
            )
            discovered += 1
            for b in buckets:
                per_bucket[b] = per_bucket.get(b, 0) + 1

    metrics = {"discovered": discovered, "per_bucket": per_bucket,
               "gdelt_doc_queries": queries, "gdelt_doc_failed": failed}
    log.info("discovery(docapi): %s", metrics)
    return metrics
