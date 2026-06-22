import time
from datetime import datetime, timezone

import pytest

from geoecon.registry import Source
from geoecon.fetchers import base
from geoecon.fetchers.rss import RssFetcher
from geoecon.fetchers.scrape import ScrapeFetcher
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
