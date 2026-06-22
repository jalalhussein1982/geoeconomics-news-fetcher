"""Scrape fetcher — extracts article links from served HTML via a CSS selector.

Runtime is NO-JS: it fetches the page once and applies `source.extract_hint`
(a CSS selector resolved by Task 0) to the served markup. Sources whose article
list only appears after JS execution are marked `status: needs_review` by Task 0
and skipped by the stage — they have no usable `extract_hint` here.
"""
from __future__ import annotations

import logging
from urllib.parse import urljoin

import requests
from lxml import html as lxml_html

from .base import DiscoveredItem

log = logging.getLogger("geoecon.fetchers.scrape")

_UA = "Mozilla/5.0 (compatible; geoecon/1.0; +https://github.com/geoecon)"


def _default_fetch(url: str) -> str | None:
    resp = requests.get(url, timeout=30, headers={"User-Agent": _UA})
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
            items.append(DiscoveredItem(url=href, title=title, source_lang=source.lang))
        return items
