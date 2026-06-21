from __future__ import annotations
import json, logging
import trafilatura
from ..config import Config

log = logging.getLogger("geoecon.extract")
MIN_CHARS = 200  # below this, treat as headline-only (body dropped; title kept if any)


def _default_fetcher(url: str) -> str | None:
    return trafilatura.fetch_url(url)


def extract_one(url, *, fetcher=_default_fetcher):
    """Return (body|None, title|None, status, lang|None).

    Title is captured from trafilatura metadata when available, even when the
    body is too short (headline_only) — the gate uses it.
    """
    try:
        downloaded = fetcher(url)
    except Exception as e:
        log.warning("fetch failed %s: %s", url, e)
        return None, None, "failed", None
    if not downloaded:
        return None, None, "headline_only", None
    title = None
    text = None
    result = trafilatura.extract(downloaded, include_comments=False,
                                 favor_recall=True, output_format="json",
                                 with_metadata=True)
    if result:
        try:
            data = json.loads(result)
            title = data.get("title")
            text = data.get("text")
        except (json.JSONDecodeError, AttributeError):
            pass
    if not text or len(text) < MIN_CHARS:
        return None, title, "headline_only", None
    return text, title, "extracted", None


def run(cfg: Config, repo, run_id: int, *, fetcher=_default_fetcher) -> dict:
    counts = {"extracted": 0, "headline_only": 0, "failed": 0}
    for art in repo.articles_for_run(run_id, fetch_status="pending"):
        body, title, status, lang = extract_one(art.source_url, fetcher=fetcher)
        repo.update_extraction(art.article_id, body, status, source_lang=lang,
                               fetch_error=None if status != "failed" else "extraction failed",
                               title=title)
        counts[status] += 1
    log.info("extraction: %s", counts)
    return counts
