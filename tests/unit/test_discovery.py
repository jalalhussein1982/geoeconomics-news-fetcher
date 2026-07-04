from geoecon import gdelt


def _line(url_suffix: str = "x") -> str:
    """Return a GKG TSV line from the fixture, optionally with a different URL suffix."""
    with open("tests/fixtures/gkg_sample.tsv", encoding="utf-8") as f:
        raw = f.readline().rstrip("\n")
    if url_suffix != "x":
        # Replace the URL at col 4 so each line has a distinct URL (avoiding dedup)
        fields = raw.split("\t")
        fields[4] = f"https://www.alarabiya.net/{url_suffix}"
        raw = "\t".join(fields)
    return raw


def test_parse_gkg_line_extracts_fields():
    rec = gdelt.parse_gkg_line(_line())
    assert rec is not None
    assert rec.url.startswith("https://www.alarabiya.net")
    assert rec.domain == "alarabiya.net"
    assert "WB_1428_TRADE" in rec.themes and "ECON_SUBSIDIES" in rec.themes
    assert rec.country == "SA"
    assert rec.tone == -3.5


def test_theme_intersection_requires_two():
    assert gdelt.theme_intersection_ok(["WB_1428_TRADE", "ECON_SUBSIDIES"], ("ECON_", "WB_")) is True
    assert gdelt.theme_intersection_ok(["ECON_SUBSIDIES"], ("ECON_", "WB_")) is False
    assert gdelt.theme_intersection_ok(["SPORTS", "WEATHER"], ("ECON_", "WB_")) is False


def test_theme_intersection_requires_core_economic_hit():
    # Two WB_ hits but no ECON_/EPU_ core theme -> rejected (WB_ alone too broad).
    assert gdelt.theme_intersection_ok(["WB_1428_TRADE", "WB_470_FINANCE"]) is False
    # Same count, but with a core theme present -> accepted.
    assert gdelt.theme_intersection_ok(["WB_1428_TRADE", "ECON_INFLATION"]) is True
    assert gdelt.theme_intersection_ok(["EPU_ECONOMY", "WB_1428_TRADE"]) is True


def test_theme_salience_is_density_not_count():
    focused = ["ECON_INFLATION", "ECON_TRADE", "WB_1428_TRADE", "TAX_FNCACT"]
    omnibus = ["ECON_INFLATION", "ECON_TRADE"] + [f"MISC_{i}" for i in range(98)]
    # Omnibus has more target hits in absolute terms but far lower density,
    # so the focused article must rank higher.
    assert gdelt.theme_salience(focused) > gdelt.theme_salience(omnibus)
    assert gdelt.theme_salience([]) == 0.0


def test_region_for_country_uses_fips_codes():
    # FIPS codes for real targets that the old ISO map silently dropped.
    assert gdelt.region_for_country("LE") == "MENA"   # Lebanon (ISO LB)
    assert gdelt.region_for_country("IZ") == "MENA"   # Iraq (ISO IQ)
    assert gdelt.region_for_country("CI") == "LatAm"  # Chile (ISO CL)
    assert gdelt.region_for_country("NU") == "LatAm"  # Nicaragua (ISO NI)
    # FIPS NI is Nigeria, not Nicaragua -> must NOT land in LatAm.
    assert gdelt.region_for_country("NI") == "other"


def test_parse_gkg_line_country_is_most_mentioned():
    fields = ["20260619051500-1", "20260619051500", "", "wire.example",
              "https://wire.example/x", "", ""]
    # themes at index 7
    fields.append("ECON_INFLATION;WB_1428_TRADE")
    fields.append("")  # 8
    # locations at index 9: US mentioned once (first), Lebanon twice -> modal LE
    fields.append("4#Washington#US#US#1#1#US;4#Beirut#LE#LE#2#2#LE;4#Tripoli#LE#LE#3#3#LE")
    fields += [""] * (16 - len(fields))  # pad through tone col (index 15)
    fields[15] = "-1.0,1,2,3,0,0,5"
    rec = gdelt.parse_gkg_line("\t".join(fields))
    assert rec is not None
    assert rec.country == "LE"
    assert gdelt.region_for_country(rec.country) == "MENA"


def test_url_hash_stable():
    assert gdelt.url_hash("http://a/b") == gdelt.url_hash("http://a/b")


def test_discovery_persists_country_and_keeps_bucketed(monkeypatch):
    lines = [_line("x1"), _line("x2")]  # fixture country = SA (Saudi -> middle_east)
    captured = []

    class FakeRepo:
        def insert_article(self, run_id, **kw):
            captured.append(kw)
            return len(captured)

    from geoecon.stages import discovery
    from geoecon.config import Config
    cfg = Config.from_env({
        "DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
        "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x",
    })
    metrics = discovery.run(cfg, FakeRepo(), run_id=1, fetch=lambda ws, we: lines)
    assert metrics["discovered"] == 2
    assert all(c["country_fips"] == "SA" for c in captured)
    assert all(c["region"] == "MENA" for c in captured)  # coarse legacy tag preserved


def test_discovery_run_inserts_and_caps(monkeypatch):
    # 3 DISTINCT URLs so url_hash dedup does not collapse them
    lines = [_line("x1"), _line("x2"), _line("x3")]
    captured = []

    class FakeRepo:
        def insert_article(self, run_id, **kw):
            captured.append(kw)
            return len(captured)

    from geoecon.stages import discovery
    from geoecon.config import Config

    cfg = Config.from_env({
        "DB_DSN": "x",
        "MAETCENTRUM_KEY": "k",
        "SMTP_HOST": "h",
        "SMTP_USER": "u",
        "SMTP_PASSWORD": "p",
        "FROM_ADDR": "f",
        "RECIPIENTS": "r@x",
    })
    metrics = discovery.run(cfg, FakeRepo(), run_id=1, fetch=lambda ws, we: lines)
    assert metrics["discovered"] == 3
    assert all(c["region"] == "MENA" for c in captured)


def test_prefilter_ok_requires_one_economic_theme():
    # one economic theme is enough now (recall up; gate restores precision)
    assert gdelt.prefilter_ok(["ECON_INFLATION"]) is True
    assert gdelt.prefilter_ok(["WB_1428_TRADE"]) is True
    assert gdelt.prefilter_ok(["EPU_POLICY"]) is True
    # geopolitics-only, no economic theme -> still excluded
    assert gdelt.prefilter_ok(["MILITARY", "ARMEDCONFLICT"]) is False
    # pure non-economic -> excluded
    assert gdelt.prefilter_ok(["SPORTS", "WEATHER"]) is False
    assert gdelt.prefilter_ok([]) is False


def test_geopol_theme_prefixes_present():
    assert "SANCTIONS" in gdelt.GEOPOL_THEME_PREFIXES
    assert "EPU_CATS_GEOPOLITICAL" in gdelt.GEOPOL_THEME_PREFIXES


def test_discovery_caps_per_bucket(monkeypatch):
    # 3 distinct SA (middle_east) lines; ceiling per bucket = 2 -> keep 2
    lines = [_line("s1"), _line("s2"), _line("s3")]
    captured = []
    class FakeRepo:
        def insert_article(self, run_id, **kw):
            captured.append(kw); return len(captured)
    from geoecon.stages import discovery
    from geoecon.config import Config
    cfg = Config.from_env({
        "DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
        "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x",
        "DISCOVERY_CEILING": "2",
    })
    metrics = discovery.run(cfg, FakeRepo(), run_id=1, fetch=lambda ws, we: lines)
    assert metrics["discovered"] == 2  # capped at 2 for the middle_east bucket
    assert all(c["country_fips"] == "SA" for c in captured)


from datetime import datetime, timedelta, timezone as _tz

from geoecon import gdelt_doc
from geoecon.config import Config
from geoecon.search.matrix import CountrySpec
from geoecon.stages import discovery


def _cfg(**over):
    env = {"DB_DSN": "x", "MAETCENTRUM_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
           "SMTP_PASSWORD": "p", "FROM_ADDR": "f", "RECIPIENTS": "r@x"}
    env.update(over)
    return Config.from_env(env)


class FakeRepo:
    def __init__(self): self.captured = []
    def insert_article(self, run_id, **kw):
        self.captured.append(kw); return len(self.captured)

_NOW = datetime(2026, 7, 4, 7, 0, tzinfo=_tz.utc)
_EG = CountrySpec(fips="EG", name="Egypt", gl="EG", langs=("ar",), gdelt_sc="egypt")
_MX = CountrySpec(fips="MX", name="Mexico", gl="MX", langs=("es",), gdelt_sc="mexico")


def test_docapi_mode_inserts_with_queried_country():
    calls = []
    def doc_fetch(query, timespan_h, **kw):
        calls.append(query)
        if "egypt" in query:
            return [gdelt_doc.DocArticle(url="https://ahram.org.eg/a", title="T",
                                         domain="ahram.org.eg", language="Arabic",
                                         seendate=_NOW - timedelta(hours=3))]
        return []
    repo = FakeRepo()
    m = discovery.run(_cfg(GDELT_MODE="docapi"), repo, 1,
                      doc_fetch=doc_fetch, countries=[_EG, _MX], now=_NOW)
    assert len(calls) == 2 and calls[0].startswith("sourcecountry:egypt ")
    kw = repo.captured[0]
    assert kw["discovery_source"] == "gdelt"
    assert kw["country_fips"] == "EG"           # attribution from the query, not text
    assert kw["region"] == "MENA"
    assert kw["published_at"] == _NOW - timedelta(hours=3)
    assert m["gdelt_doc_queries"] == 2
    assert m["discovered"] == 1                  # same headline metric as gkg mode


def test_docapi_mode_out_of_window_and_dedup():
    old = _NOW - timedelta(hours=30)
    def doc_fetch(query, timespan_h, **kw):
        return [gdelt_doc.DocArticle(url="https://x.eg/1", title="a", domain="x.eg",
                                     language="Arabic", seendate=old),
                gdelt_doc.DocArticle(url="https://x.eg/2", title="b", domain="x.eg",
                                     language="Arabic", seendate=None),
                gdelt_doc.DocArticle(url="https://x.eg/2?ref=t", title="dup",
                                     domain="x.eg", language="Arabic", seendate=None)]
    repo = FakeRepo()
    m = discovery.run(_cfg(GDELT_MODE="docapi"), repo, 1,
                      doc_fetch=doc_fetch, countries=[_EG], now=_NOW)
    assert [c["source_url"] for c in repo.captured] == ["https://x.eg/2"]
    assert m["discovered"] == 1
