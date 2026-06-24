"""Sitemap fetcher — a third discovery strategy alongside rss and scrape.

XML sitemaps are more common on government CMS than discoverable RSS, and many
JS-rendered portals still emit a static sitemap their SEO generator produces even
when the news page is client-rendered. So this is a general strategy, not a
two-source special case.

Windowing: each `<url>` is emitted with `ts = <lastmod>`, so the discovery stage's
existing window filter drops anything outside `[now-WINDOW_HOURS, now]`. Entries
WITHOUT a parseable `<lastmod>` are dropped here — a sitemap with no lastmod isn't
windowable, and emitting it with `ts=None` (which the stage keeps) would flood the
gate with the whole archive. `<sitemapindex>` is followed into child sitemaps
(recent-first, capped). Titles are left None; extraction fills them (existing
behaviour).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests
from lxml import etree

from .base import DiscoveredItem
from .http import BROWSER_UA

log = logging.getLogger("geoecon.fetchers.sitemap")
_MAX_DEPTH = 2
_MAX_CHILDREN = 25  # cap child sitemaps fetched from an index (recent-first)


def _default_fetch(url: str) -> bytes | None:
    resp = requests.get(url, timeout=30, headers={"User-Agent": BROWSER_UA})
    resp.raise_for_status()
    return resp.content


def _localname(el) -> str:
    return etree.QName(el).localname.lower()


def _parse_lastmod(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _child(el, name: str):
    for c in el:
        if _localname(c) == name:
            return c.text
    return None


class SitemapFetcher:
    """Fetches `source.feed_url` (the sitemap URL Task 0 resolved) → DiscoveredItems.

    `fetch` is injectable (returns bytes/str) so tests parse canned XML with no
    network. `url_filter`, when given, keeps only `<loc>`s containing it (an
    operator can narrow a broad sitemap to e.g. '/news/').
    """

    def __init__(self, *, fetch=_default_fetch, max_children: int = _MAX_CHILDREN):
        self._fetch = fetch
        self._max_children = max_children

    def discover(self, source) -> list[DiscoveredItem]:
        target = source.feed_url or source.url
        url_filter = source.extract_hint or None
        return self._collect(target, source.lang, url_filter, depth=0)

    def _collect(self, url, lang, url_filter, depth) -> list[DiscoveredItem]:
        if depth > _MAX_DEPTH:
            return []
        try:
            raw = self._fetch(url)
        except Exception as e:
            log.warning("sitemap fetch failed %s: %s", url, e)
            return []
        if not raw:
            return []
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        try:
            root = etree.fromstring(raw)
        except etree.XMLSyntaxError:
            return []

        if _localname(root) == "sitemapindex":
            children = []
            for sm in root:
                if _localname(sm) != "sitemap":
                    continue
                loc = _child(sm, "loc")
                if loc:
                    children.append((loc, _parse_lastmod(_child(sm, "lastmod"))))
            # recent-first so the cap keeps the freshest child sitemaps
            children.sort(key=lambda c: c[1] or datetime.min.replace(tzinfo=timezone.utc),
                          reverse=True)
            items: list[DiscoveredItem] = []
            for loc, _lm in children[:self._max_children]:
                items.extend(self._collect(loc, lang, url_filter, depth + 1))
            return items

        items = []
        for ue in root:
            if _localname(ue) != "url":
                continue
            loc = _child(ue, "loc")
            ts = _parse_lastmod(_child(ue, "lastmod"))
            if not loc or ts is None:          # no lastmod => not windowable => drop
                continue
            if url_filter and url_filter not in loc:
                continue
            items.append(DiscoveredItem(url=loc, title=None, ts=ts, source_lang=lang))
        return items
