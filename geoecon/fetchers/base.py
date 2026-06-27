"""Fetcher protocol + the item shape every fetcher yields, plus the dispatcher.

A fetcher takes one `Source` and returns the article links it can see *right now*,
as `DiscoveredItem`s. It does NOT fetch article bodies — that is the existing
extraction stage's job. Keeping discovery (find the URL) and extraction (get the
text) separate is the same contract GDELT discovery follows (see file 01).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class DiscoveredItem:
    url: str
    title: str | None = None
    ts: datetime | None = None          # publish time if known; None => keep (gate judges)
    source_lang: str | None = None


class Fetcher(Protocol):
    def discover(self, source) -> list[DiscoveredItem]:
        ...


def get_fetcher(source) -> Fetcher:
    """Pick a fetcher for `source.fetch_type`. Imports are lazy so a registry that
    never uses scrape/api doesn't pay for lxml/adapters at import time."""
    ft = source.fetch_type
    if ft == "rss":
        from .rss import RssFetcher
        return RssFetcher()
    if ft == "scrape":
        from .scrape import ScrapeFetcher
        # JS-rendered sources (render: firecrawl) get the rendered DOM via
        # Firecrawl, then the same extract_hint selector logic applies.
        if getattr(source, "render", None) == "firecrawl":
            from .firecrawl import firecrawl_fetch
            return ScrapeFetcher(fetch=firecrawl_fetch)
        return ScrapeFetcher()
    if ft == "sitemap":
        from .sitemap import SitemapFetcher
        return SitemapFetcher()
    if ft == "api":
        from .api import ApiFetcher
        return ApiFetcher()
    raise ValueError(f"no fetcher for fetch_type={ft!r} (source {getattr(source, 'id', '?')})")
