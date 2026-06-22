from datetime import datetime, timedelta, timezone

from geoecon.registry import Source
from geoecon.fetchers.base import DiscoveredItem
from geoecon.stages import discovery_registry
from geoecon.config import Config

NOW = datetime(2026, 6, 23, 7, 0, tzinfo=timezone.utc)


def _cfg(**over):
    env = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
           "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"}
    env.update(over)
    return Config.from_env(env)


def _src(**kw):
    base = dict(id="s", institution="S", tier=0, fetch_type="rss",
                url="https://example.org/news", lang="en")
    base.update(kw)
    return Source(**base)


class FakeRepo:
    def __init__(self): self.captured = []
    def insert_article(self, run_id, **kw):
        self.captured.append(kw); return len(self.captured)


def _fetcher_for(items_by_id):
    class _F:
        def __init__(self, src): self.src = src
        def discover(self, src): return items_by_id.get(src.id, [])
    return lambda src: _F(src)


def test_skips_unfetchable_and_counts():
    sources = [
        _src(id="a", fetch_type="unverified", status="needs_review"),
        _src(id="b", fetch_type="dead", status="dead"),
        _src(id="c", fetch_type="scrape", status="needs_review", extract_hint=None),
        _src(id="d", fetch_type="rss", status="live", country_fips="SA", bucket="middle_east"),
    ]
    items = {"d": [DiscoveredItem(url="https://example.org/d1", title="t", ts=None)]}
    repo = FakeRepo()
    m = discovery_registry.run(_cfg(), repo, 1, sources=sources,
                               get_fetcher=_fetcher_for(items), now=NOW)
    assert m["registry_discovered"] == 1
    assert m["registry_skipped_unverified"] == 1
    assert m["registry_skipped_needs_review"] == 1   # the scrape one
    assert m["registry_per_tier"] == {0: 1}
    assert repo.captured[0]["country_fips"] == "SA"
    assert repo.captured[0]["region"] == "MENA"
    assert repo.captured[0]["title"] == "t"


def test_writes_registry_provenance():
    src = _src(id="sa_cb", fetch_type="rss", status="live", country_fips="SA",
               bucket="middle_east", tier=0, origin_class=["monetary", "macro"],
               ownership="state", state_alignment="SA", lang="ar")
    items = {"sa_cb": [DiscoveredItem(url="https://x/1", title="t")]}
    repo = FakeRepo()
    discovery_registry.run(_cfg(), repo, 1, sources=[src],
                           get_fetcher=_fetcher_for(items), now=NOW)
    kw = repo.captured[0]
    assert kw["discovery_source"] == "registry"
    assert kw["registry_id"] == "sa_cb"
    assert kw["source_tier"] == 0
    assert kw["origin_class"] == ["monetary", "macro"]
    assert kw["ownership"] == "state"
    assert kw["state_alignment"] == "SA"


def test_bucket_filter_and_cross_cutting():
    sources = [
        # latam source but only middle_east+north_africa active -> dropped
        _src(id="br", fetch_type="rss", status="live", country_fips="BR", bucket="latam"),
        # cross-cutting wire (bucket None) -> always runs, country resolved later by gate
        _src(id="wire", fetch_type="rss", status="live", country_fips=None, bucket=None),
    ]
    items = {
        "br": [DiscoveredItem(url="https://x/br")],
        "wire": [DiscoveredItem(url="https://x/wire")],
    }
    repo = FakeRepo()
    m = discovery_registry.run(_cfg(BUCKETS="middle_east,north_africa"), repo, 1,
                               sources=sources, get_fetcher=_fetcher_for(items), now=NOW)
    assert m["registry_discovered"] == 1
    assert repo.captured[0]["source_url"] == "https://x/wire"
    assert repo.captured[0]["country_fips"] is None


def test_window_filters_old_but_keeps_null_ts():
    old = NOW - timedelta(hours=48)
    fresh = NOW - timedelta(hours=2)
    items = {"d": [
        DiscoveredItem(url="https://x/old", ts=old),
        DiscoveredItem(url="https://x/fresh", ts=fresh),
        DiscoveredItem(url="https://x/null", ts=None),
    ]}
    repo = FakeRepo()
    m = discovery_registry.run(
        _cfg(WINDOW_HOURS="24"), repo, 1,
        sources=[_src(id="d", status="live", country_fips="SA", bucket="middle_east")],
        get_fetcher=_fetcher_for(items), now=NOW)
    urls = {c["source_url"] for c in repo.captured}
    assert urls == {"https://x/fresh", "https://x/null"}
    assert m["registry_discovered"] == 2


def test_within_stage_url_dedup():
    items = {"d": [
        DiscoveredItem(url="https://x/same"),
        DiscoveredItem(url="https://x/same"),
    ]}
    repo = FakeRepo()
    m = discovery_registry.run(
        _cfg(), repo, 1,
        sources=[_src(id="d", status="live", country_fips="SA", bucket="middle_east")],
        get_fetcher=_fetcher_for(items), now=NOW)
    assert m["registry_discovered"] == 1


def test_one_bad_source_does_not_abort():
    def boom(src):
        class _F:
            def discover(self, s):
                if s.id == "bad":
                    raise RuntimeError("network")
                return [DiscoveredItem(url="https://x/ok")]
        return _F()
    sources = [
        _src(id="bad", status="live", country_fips="SA", bucket="middle_east"),
        _src(id="ok", status="live", country_fips="SA", bucket="middle_east"),
    ]
    repo = FakeRepo()
    m = discovery_registry.run(_cfg(), repo, 1, sources=sources, get_fetcher=boom, now=NOW)
    assert m["registry_discovered"] == 1
    assert repo.captured[0]["source_url"] == "https://x/ok"
