from __future__ import annotations
import json, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import trafilatura
from trafilatura.settings import use_config
from ..config import Config

log = logging.getLogger("geoecon.extract")
MIN_CHARS = 200  # below this, treat as headline-only (body dropped; title kept if any)


def _make_fetcher(timeout_s: int):
    """Build a fetch closure with its own trafilatura config, constructed once.

    use_config() returns a fresh ConfigParser instance (not trafilatura's shared
    global default), so mutating it here is safe and does not leak across calls
    or threads.
    """
    config = use_config()
    config.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(timeout_s))

    def _fetch(url: str) -> str | None:
        return trafilatura.fetch_url(url, config=config)

    return _fetch


def extract_one(url, *, fetcher):
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
            body, title, status, lang = fut.result()  # extract_one never raises
            repo.update_extraction(a.article_id, body, status, source_lang=lang,
                                   fetch_error=None if status != "failed" else "extraction failed",
                                   title=title)
            counts[status] += 1
    log.info("extraction: %s", counts)
    return counts
