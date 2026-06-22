import time
from datetime import datetime, timezone

import pytest

from geoecon.registry import Source
from geoecon.fetchers import base
from geoecon.fetchers.rss import RssFetcher
from geoecon.fetchers.scrape import ScrapeFetcher
from geoecon.fetchers.sitemap import SitemapFetcher
from geoecon.fetchers.api import ApiFetcher


def _src(**kw):
    base_kw = dict(id="s", institution="S", tier=1, fetch_type="rss",
                   url="https://example.org/news", lang="en")
    base_kw.update(kw)
    return Source(**base_kw)


# ---- dispatch ----

def test_get_fetcher_dispatch():
    assert isinstance(base.get_fetcher(_src(fetch_type="rss")), RssFetcher)
    assert isinstance(base.get_fetcher(_src(fetch_type="scrape")), ScrapeFetcher)
    assert isinstance(base.get_fetcher(_src(fetch_type="sitemap")), SitemapFetcher)
    assert isinstance(base.get_fetcher(_src(fetch_type="api")), ApiFetcher)
    with pytest.raises(ValueError):
        base.get_fetcher(_src(fetch_type="dead"))


# ---- rss ----

class _FakeParsed:
    def __init__(self, entries): self.entries = entries


def test_rss_parses_entries_and_ts():
    st = time.gmtime(1_700_000_000)
    entries = [
        {"link": "https://example.org/a", "title": "A", "published_parsed": st},
        {"link": "https://example.org/b", "title": "B"},   # no ts -> None
        {"title": "no link"},                                # skipped
    ]
    f = RssFetcher(parse=lambda target: _FakeParsed(entries))
    items = f.discover(_src(feed_url="https://example.org/feed", lang="ar"))
    assert [i.url for i in items] == ["https://example.org/a", "https://example.org/b"]
    assert items[0].ts == datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
    assert items[1].ts is None
    assert all(i.source_lang == "ar" for i in items)


# ---- scrape ----

_HTML = """
<html><body>
  <ul class="news">
    <li><a class="headline" href="/news/2026/a">Inflation eases</a></li>
    <li><a class="headline" href="https://example.org/news/2026/b">New budget</a></li>
    <li><a class="other" href="/ignore">nav</a></li>
  </ul>
</body></html>
"""


def test_scrape_selects_links_and_absolutizes():
    f = ScrapeFetcher(fetch=lambda url: _HTML)
    items = f.discover(_src(fetch_type="scrape", extract_hint="a.headline",
                            url="https://example.org/news"))
    assert [i.url for i in items] == [
        "https://example.org/news/2026/a",
        "https://example.org/news/2026/b",
    ]
    assert items[0].title == "Inflation eases"


def test_scrape_no_hint_returns_empty():
    f = ScrapeFetcher(fetch=lambda url: _HTML)
    assert f.discover(_src(fetch_type="scrape", extract_hint=None)) == []


# ---- sitemap ----

_URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://boi.org.il/news/rate-decision</loc><lastmod>2026-06-17T13:52:22+00:00</lastmod></url>
  <url><loc>https://boi.org.il/about/history</loc><lastmod>2024-05-02</lastmod></url>
  <url><loc>https://boi.org.il/no-lastmod-page</loc></url>
</urlset>"""

_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://x/child-old.xml</loc><lastmod>2026-01-01</lastmod></sitemap>
  <sitemap><loc>https://x/child-new.xml</loc><lastmod>2026-06-20</lastmod></sitemap>
</sitemapindex>"""


def test_sitemap_parses_loc_and_lastmod_drops_unwindowable():
    f = SitemapFetcher(fetch=lambda u: _URLSET)
    items = f.discover(_src(fetch_type="sitemap", feed_url="https://boi.org.il/sitemap.xml", lang="he"))
    # the no-lastmod entry is dropped (not windowable); the other two are kept with ts
    urls = [i.url for i in items]
    assert urls == ["https://boi.org.il/news/rate-decision", "https://boi.org.il/about/history"]
    assert items[0].ts == datetime(2026, 6, 17, 13, 52, 22, tzinfo=timezone.utc)
    assert items[1].ts == datetime(2024, 5, 2, tzinfo=timezone.utc)
    assert items[0].title is None and items[0].source_lang == "he"


def test_sitemap_url_filter():
    f = SitemapFetcher(fetch=lambda u: _URLSET)
    items = f.discover(_src(fetch_type="sitemap", feed_url="https://boi.org.il/sitemap.xml",
                            extract_hint="/news/"))
    assert [i.url for i in items] == ["https://boi.org.il/news/rate-decision"]


def test_sitemap_index_recurses_recent_first():
    fetched = []
    def fetch(u):
        fetched.append(u)
        if u.endswith("index.xml"):
            return _INDEX
        # child sitemaps return a tiny urlset each
        return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{u}/a</loc><lastmod>2026-06-21</lastmod></url></urlset>'
    f = SitemapFetcher(fetch=fetch, max_children=1)
    items = f.discover(_src(fetch_type="sitemap", feed_url="https://x/index.xml"))
    # cap=1 + recent-first => only the newer child (child-new) is fetched
    assert fetched == ["https://x/index.xml", "https://x/child-new.xml"]
    assert [i.url for i in items] == ["https://x/child-new.xml/a"]


# ---- api ----

def test_api_requires_registered_adapter():
    f = ApiFetcher(adapters={})
    with pytest.raises(ValueError, match="no api adapter"):
        f.discover(_src(fetch_type="api", id="cb_api"))


def test_api_uses_injected_adapter():
    from geoecon.fetchers.base import DiscoveredItem
    f = ApiFetcher(adapters={"cb_api": lambda s: [DiscoveredItem(url="https://x/1")]})
    items = f.discover(_src(fetch_type="api", id="cb_api"))
    assert items[0].url == "https://x/1"
