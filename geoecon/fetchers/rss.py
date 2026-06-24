"""RSS/Atom fetcher — parses a feed into DiscoveredItems via feedparser."""
from __future__ import annotations

import ipaddress
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import feedparser

from .base import DiscoveredItem
from .http import BROWSER_UA


def _entry_ts(entry) -> datetime | None:
    """Best-effort publish time from an entry's parsed time struct, else None."""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    try:
        return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _is_ip_host(host: str | None) -> bool:
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _rewrite_ip_host(url: str, source_url: str) -> str:
    """If `url`'s host is a bare IP literal (e.g. La Razón's internal 10.0.137.12),
    swap scheme+host for the source's public host, preserving path/query."""
    u = urlparse(url)
    if not _is_ip_host(u.hostname):
        return url
    s = urlparse(source_url)
    return urlunparse((s.scheme or u.scheme, s.netloc, u.path, u.params, u.query, u.fragment))


class RssFetcher:
    """Discovers items from `source.feed_url` (falling back to `source.url`).

    `parse` is injectable so tests can feed a canned feedparser result with no
    network — mirroring how GDELT discovery injects its fetch.
    """

    def __init__(self, *, parse=feedparser.parse):
        self._parse = parse

    def discover(self, source) -> list[DiscoveredItem]:
        target = source.feed_url or source.url
        parsed = self._parse(target, agent=BROWSER_UA)
        items: list[DiscoveredItem] = []
        for entry in getattr(parsed, "entries", []) or []:
            url = entry.get("link")
            if not url:
                continue
            url = _rewrite_ip_host(url, source.url)
            items.append(DiscoveredItem(
                url=url,
                title=entry.get("title"),
                ts=_entry_ts(entry),
                source_lang=source.lang,
            ))
        return items
