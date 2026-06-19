from __future__ import annotations
import logging
import trafilatura
from ..config import Config

log = logging.getLogger("geoecon.extract")
MIN_CHARS = 200  # below this, treat as failed extraction -> headline-only

def _default_fetcher(url: str) -> str | None:
    return trafilatura.fetch_url(url)

def extract_one(url, *, fetcher=_default_fetcher):
    try:
        downloaded = fetcher(url)
    except Exception as e:
        log.warning("fetch failed %s: %s", url, e)
        return None, "failed", None
    if not downloaded:
        return None, "headline_only", None
    text = trafilatura.extract(downloaded, include_comments=False, favor_recall=True)
    if not text or len(text) < MIN_CHARS:
        return None, "headline_only", None
    return text, "extracted", None

def run(cfg: Config, repo, run_id: int, *, fetcher=_default_fetcher) -> dict:
    counts = {"extracted": 0, "headline_only": 0, "failed": 0}
    for art in repo.articles_for_run(run_id, fetch_status="pending"):
        body, status, lang = extract_one(art.source_url, fetcher=fetcher)
        repo.update_extraction(art.article_id, body, status, source_lang=lang,
                               fetch_error=None if status != "failed" else "extraction failed")
        counts[status] += 1
    log.info("extraction: %s", counts)
    return counts
