"""Integration (no network, no DB): GDELT + registry discovery converge into one
idempotent `articles` store. Proves the discovery_source split and that an
overlapping URL collapses to a single row via the normalized hash."""
from datetime import datetime, timezone

from geoecon.registry import Source
from geoecon.fetchers.base import DiscoveredItem
from geoecon.stages import discovery, discovery_registry
from geoecon.config import Config

NOW = datetime(2026, 6, 23, 7, 0, tzinfo=timezone.utc)


def _cfg():
    return Config.from_env({"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h",
        "SMTP_USER": "u", "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"})


class IdempotentRepo:
    """Mimics insert_article idempotency on (run_id, url_hash) without a DB."""
    def __init__(self):
        self.rows = []
        self._by_hash = {}
    def insert_article(self, run_id, *, url_hash, **kw):
        key = (run_id, url_hash)
        if key in self._by_hash:
            return self._by_hash[key]
        aid = len(self.rows) + 1
        self.rows.append({"article_id": aid, "url_hash": url_hash, **kw})
        self._by_hash[key] = aid
        return aid


def _gkg_line(url):
    f = [""] * 16
    f[0] = "g1"; f[1] = "20260623060000"; f[3] = "example.com"; f[4] = url
    f[7] = "ECON_INFLATION;WB_1428_TRADE"
    f[9] = "4#Riyadh#SA#SA#1#1#SA"   # SA -> middle_east, passes the bucket filter
    f[15] = "-1.0"
    return "\t".join(f)


def test_gdelt_and_registry_converge_with_dedup_and_split():
    cfg = _cfg()
    repo = IdempotentRepo()

    # GDELT surfaces the shared story with a tracking param.
    shared_gdelt = "https://www.example.com/news/article-123?utm_source=gdelt"
    discovery.run(cfg, repo, 1, fetch=lambda ws, we: [_gkg_line(shared_gdelt)])

    # The registry surfaces the SAME story as an AMP variant, plus a registry-only one.
    src = Source(id="sa_cb", institution="SAMA", tier=0, fetch_type="rss",
                 url="https://example.com/news", country_fips="SA", bucket="middle_east",
                 status="live", lang="ar", ownership="state", state_alignment="SA")
    items = {"sa_cb": [
        DiscoveredItem(url="https://example.com/news/article-123/amp", title="dup"),
        DiscoveredItem(url="https://example.com/news/article-999", title="registry only"),
    ]}
    class _F:
        def __init__(self, s): pass
        def discover(self, s): return items[s.id]
    discovery_registry.run(cfg, repo, 1, sources=[src],
                           get_fetcher=lambda s: _F(s), now=NOW)

    # The shared URL collapsed to ONE row (kept by GDELT, which inserted first);
    # the registry-only URL is a second row. Two rows total, not three.
    assert len(repo.rows) == 2
    split = {}
    for r in repo.rows:
        split[r["discovery_source"]] = split.get(r["discovery_source"], 0) + 1
    assert split == {"gdelt": 1, "registry": 1}

    # The shared row is the GDELT one (idempotent insert did not overwrite provenance).
    shared = [r for r in repo.rows if r["discovery_source"] == "gdelt"][0]
    assert shared["country_fips"] == "SA"
    reg = [r for r in repo.rows if r["discovery_source"] == "registry"][0]
    assert reg["registry_id"] == "sa_cb" and reg["source_tier"] == 0
