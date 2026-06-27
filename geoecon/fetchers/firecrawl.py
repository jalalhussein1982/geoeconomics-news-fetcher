"""Firecrawl-backed fetch for JS-rendered sources.

Several Tier-0 primaries (central banks, finance ministries, IMF/World Bank) serve
an empty HTML shell and build their article list client-side, so the no-JS
`ScrapeFetcher` sees zero links and the source stays `needs_review`. For those, the
registry sets `render: firecrawl`; `get_fetcher` then wires the ScrapeFetcher to
pull the *rendered* DOM through Firecrawl's /scrape API before applying the same
`extract_hint` CSS selector — the extraction logic is unchanged, only the fetch
differs. The key comes from FIRECRAWL_API_KEY (a cluster secret, like
MAETCENTRUM_KEY); it is read at call time so the rest of the pipeline runs without
it when no source needs rendering.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("geoecon.fetchers.firecrawl")

FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v1/scrape"
WAIT_FOR_MS = 5000  # let the SPA hydrate its article list before we read the DOM


class FirecrawlError(RuntimeError):
    """Key missing or the API errored. Raised (not swallowed) so the discovery
    stage's per-source try/except logs it and moves on, instead of a source
    silently yielding zero articles and looking 'covered'."""


def firecrawl_fetch(url: str, *, _post=None) -> str | None:
    """Return the JS-rendered HTML for `url` via Firecrawl, or None if absent.

    `_post` is injectable so tests exercise the contract with no network.
    """
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        raise FirecrawlError("FIRECRAWL_API_KEY not set (needed for render: firecrawl sources)")
    post = _post or _default_post
    data = post(url, key)
    if not data.get("success"):
        raise FirecrawlError(f"firecrawl scrape failed for {url}: {data.get('error')!r}")
    return (data.get("data") or {}).get("rawHtml")


def _default_post(url: str, key: str) -> dict:
    resp = requests.post(
        FIRECRAWL_ENDPOINT,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "url": url,
            "formats": ["rawHtml"],
            "waitFor": WAIT_FOR_MS,
            "onlyMainContent": False,
            "timeout": 60000,
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()
