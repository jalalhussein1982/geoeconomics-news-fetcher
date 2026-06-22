"""RSS/Atom fetcher — parses a feed into DiscoveredItems via feedparser."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import feedparser

from .base import DiscoveredItem


def _entry_ts(entry) -> datetime | None:
    """Best-effort publish time from an entry's parsed time struct, else None."""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    try:
        return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


class RssFetcher:
    """Discovers items from `source.feed_url` (falling back to `source.url`).

    `parse` is injectable so tests can feed a canned feedparser result with no
    network — mirroring how GDELT discovery injects its fetch.
    """

    def __init__(self, *, parse=feedparser.parse):
        self._parse = parse

    def discover(self, source) -> list[DiscoveredItem]:
        target = source.feed_url or source.url
        parsed = self._parse(target)
        items: list[DiscoveredItem] = []
        for entry in getattr(parsed, "entries", []) or []:
            url = entry.get("link")
            if not url:
                continue
            items.append(DiscoveredItem(
                url=url,
                title=entry.get("title"),
                ts=_entry_ts(entry),
                source_lang=source.lang,
            ))
        return items
