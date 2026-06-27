"""Scrape fetcher — extracts article links from served HTML via a CSS selector.

Runtime is NO-JS: it fetches the page once and applies `source.extract_hint`
(a CSS selector resolved by Task 0) to the served markup. Sources whose article
list only appears after JS execution are marked `status: needs_review` by Task 0
and skipped by the stage — they have no usable `extract_hint` here.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from lxml import html as lxml_html

from .base import DiscoveredItem
from .http import BROWSER_UA

log = logging.getLogger("geoecon.fetchers.scrape")

# Many news/press URLs embed the publish date as `/YYYY/MM/DD/` (optionally
# `/HH/MM/`), e.g. CBE `/news/2026/06/07/08/19/...` and IMF `/articles/2026/06/22/...`.
# Parsing it lets the registry stage window scrape items by recency instead of
# re-ingesting the whole listing every run (the fetcher otherwise has no ts).
_URL_DATE_RX = re.compile(r"/(20\d\d)/(\d{2})/(\d{2})(?:/(\d{2})/(\d{2}))?(?:/|$)")


def _date_from_url(url: str) -> datetime | None:
    m = _URL_DATE_RX.search(url)
    if not m:
        return None
    y, mo, d, hh, mm = m.groups()
    try:
        if hh is not None:
            return datetime(int(y), int(mo), int(d), int(hh), int(mm), tzinfo=timezone.utc)
        # Date-only URL (e.g. IMF /2026/06/22/...): anchor to end-of-day, not 00:00.
        # The recency window is measured back from `now` (~05:00 UTC for the 07:00
        # run); a midnight ts would put all of "yesterday's" items before window_start
        # and false-exclude them. End-of-day keeps the whole prior calendar day.
        return datetime(int(y), int(mo), int(d), 23, 59, 59, tzinfo=timezone.utc)
    except ValueError:
        return None


def _default_fetch(url: str) -> str | None:
    resp = requests.get(url, timeout=30, headers={"User-Agent": BROWSER_UA})
    resp.raise_for_status()
    return resp.text


class ScrapeFetcher:
    """Selects `<a>` links matching `extract_hint`; link text is the provisional title.

    `fetch` is injectable so tests pass canned HTML with no network.
    """

    def __init__(self, *, fetch=_default_fetch):
        self._fetch = fetch

    def discover(self, source) -> list[DiscoveredItem]:
        if not source.extract_hint:
            return []
        markup = self._fetch(source.url)
        if not markup:
            return []
        doc = lxml_html.fromstring(markup)
        doc.make_links_absolute(source.url, resolve_base_href=True)

        items: list[DiscoveredItem] = []
        seen: set[str] = set()
        for el in doc.cssselect(source.extract_hint):
            href = el.get("href")
            if not href:  # selector matched a container; take its first descendant link
                links = el.cssselect("a[href]")
                href = links[0].get("href") if links else None
            if not href:
                continue
            href = urljoin(source.url, href)
            if href in seen:
                continue
            seen.add(href)
            title = " ".join(el.text_content().split()) or None
            items.append(DiscoveredItem(url=href, title=title, ts=_date_from_url(href),
                                        source_lang=source.lang))
        return items
