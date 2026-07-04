from datetime import datetime, timedelta, timezone

from geoecon.config import Config
from geoecon.search.base import SearchItem
from geoecon.search.matrix import CountrySpec
from geoecon.stages import discovery_search

NOW = datetime(2026, 7, 4, 7, 0, tzinfo=timezone.utc)
EG = CountrySpec(fips="EG", name="Egypt", gl="EG", langs=("ar",), gdelt_sc="egypt")
MX = CountrySpec(fips="MX", name="Mexico", gl="MX", langs=("es",), gdelt_sc="mexico")
KW = {"family1": {"ar": ["اقتصاد"], "es": ["economía"]},
      "family2": {"ar": ["عقوبات"], "es": ["sanciones"]}}


def _cfg(**over):
    env = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
           "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x",
           "SEARCH_DISCOVERY": "true"}
    env.update(over)
    return Config.from_env(env)


class FakeRepo:
    def __init__(self): self.captured = []
    def insert_article(self, run_id, **kw):
        self.captured.append(kw); return len(self.captured)


class FakeProvider:
    name = "fake"
    def __init__(self, items_by_key): self.items = items_by_key; self.calls = []
    def search(self, expr, spec, lang, window_hours, limit):
        self.calls.append((spec.fips, lang, expr, limit))
        return self.items.get((spec.fips, expr), [])


def test_inserts_with_provenance_window_and_dedup():
    old = NOW - timedelta(hours=30)
    items = {
        ("EG", "(اقتصاد)"): [
            SearchItem(url="https://ahram.org/a1", title="T1", ts=NOW - timedelta(hours=2),
                       source_name="Ahram", lang="ar"),
            SearchItem(url="https://ahram.org/a1?utm_source=x", title="dup", ts=None),  # same url_hash
            SearchItem(url="https://old.example/x", title="old", ts=old),               # out of window
            SearchItem(url="https://undated.example/y", title="undated", ts=None),      # kept
        ],
        ("EG", "(عقوبات)"): [],
        ("MX", "(economía)"): [SearchItem(url="https://elpais.com/m1", title="M1",
                                          ts=NOW - timedelta(hours=1), decoded=False)],
        ("MX", "(sanciones)"): [],
    }
    repo, prov = FakeRepo(), FakeProvider(items)
    m = discovery_search.run(_cfg(), repo, 1, providers=[prov],
                             countries=[EG, MX], keywords=KW, now=NOW)
    urls = [c["source_url"] for c in repo.captured]
    assert urls == ["https://ahram.org/a1", "https://undated.example/y", "https://elpais.com/m1"]
    eg = repo.captured[0]
    assert eg["discovery_source"] == "search:fake"
    assert eg["country_fips"] == "EG" and eg["region"] == "MENA"
    assert eg["source_lang"] == "ar" and eg["title"] == "T1"
    assert eg["published_at"] == NOW - timedelta(hours=2)
    assert eg["domain"] == "ahram.org"
    assert m["search_discovered"] == 3
    assert m["search_per_provider"] == {"fake": 3}
    assert m["search_decode_failures"] == 1              # the decoded=False one
    # limit passed through = search_country_cap default
    assert prov.calls[0][3] == 25


def test_provider_failure_is_isolated_and_counted():
    class Boom:
        name = "boom"
        def search(self, *a, **k): raise RuntimeError("nope")
    repo = FakeRepo()
    m = discovery_search.run(_cfg(), repo, 1, providers=[Boom()],
                             countries=[EG], keywords=KW, now=NOW)
    assert repo.captured == []
    assert m["search_feeds_failed"] == 2                 # both family queries failed
    assert m["search_discovered"] == 0


def test_limit_caps_feed_budget():
    repo = FakeRepo()
    prov = FakeProvider({})
    discovery_search.run(_cfg(LIMIT="5"), repo, 1, providers=[prov],
                         countries=[MX], keywords=KW, now=NOW)
    assert prov.calls[0][3] == 5                          # min(cap, limit)
