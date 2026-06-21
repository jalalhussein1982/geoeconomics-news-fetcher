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
