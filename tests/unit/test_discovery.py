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


def test_url_hash_stable():
    assert gdelt.url_hash("http://a/b") == gdelt.url_hash("http://a/b")


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
