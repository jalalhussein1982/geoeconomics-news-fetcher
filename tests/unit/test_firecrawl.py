"""Firecrawl-backed render path for JS-rendered scrape sources."""
import pytest

from geoecon.registry import Source
from geoecon.fetchers import base
from geoecon.fetchers.scrape import ScrapeFetcher
from geoecon.fetchers import firecrawl as fc


def _src(**kw):
    base_kw = dict(id="s", institution="S", tier=0, fetch_type="scrape",
                   url="https://example.org/news", lang="en")
    base_kw.update(kw)
    return Source(**base_kw)


# ---- dispatch: render: firecrawl wires the ScrapeFetcher to firecrawl_fetch ----

def test_scrape_without_render_uses_default_fetch():
    f = base.get_fetcher(_src(fetch_type="scrape"))
    assert isinstance(f, ScrapeFetcher)
    assert f._fetch is not fc.firecrawl_fetch


def test_scrape_with_render_firecrawl_uses_firecrawl_fetch():
    f = base.get_fetcher(_src(fetch_type="scrape", render="firecrawl"))
    assert isinstance(f, ScrapeFetcher)
    assert f._fetch is fc.firecrawl_fetch


# ---- firecrawl_fetch ----

def test_firecrawl_fetch_missing_key_raises(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with pytest.raises(fc.FirecrawlError, match="FIRECRAWL_API_KEY"):
        fc.firecrawl_fetch("https://example.org")


def test_firecrawl_fetch_returns_rendered_html(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    captured = {}

    def fake_post(url, key):
        captured["url"] = url
        captured["key"] = key
        return {"success": True, "data": {"rawHtml": "<html><body>rendered</body></html>"}}

    html = fc.firecrawl_fetch("https://example.org/news", _post=fake_post)
    assert html == "<html><body>rendered</body></html>"
    assert captured["url"] == "https://example.org/news"
    assert captured["key"] == "fc-test"


def test_firecrawl_fetch_api_failure_raises(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    with pytest.raises(fc.FirecrawlError, match="failed"):
        fc.firecrawl_fetch("https://example.org",
                           _post=lambda url, key: {"success": False, "error": "blocked"})


def test_firecrawl_render_then_existing_selector_logic(monkeypatch):
    """End-to-end: rendered HTML flows through the unchanged extract_hint logic."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    rendered = (
        '<html><body><ul class="news">'
        '<li><a class="headline" href="/news/2026/06/a">Reserves rise</a></li>'
        '<li><a class="other" href="/about">nav</a></li>'
        '</ul></body></html>'
    )
    f = ScrapeFetcher(fetch=lambda url: rendered)
    items = f.discover(_src(extract_hint="a.headline", render="firecrawl",
                            url="https://example.org/news"))
    assert [i.url for i in items] == ["https://example.org/news/2026/06/a"]
    assert items[0].title == "Reserves rise"
