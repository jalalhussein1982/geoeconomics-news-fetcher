import pytest

from geoecon import registry

_VALID = """
meta: {schema_version: 1}
sources:
  - id: sa_cb_sama
    institution: "SAMA"
    tier: 0
    fetch_type: unverified
    url: "https://www.sama.gov.sa/news"
    country: SA
    country_fips: SA
    bucket: middle_east
    origin_class: [monetary, macro]
    lang: ar
    ownership: state
    state_alignment: SA
    status: needs_review
  - id: wire_ap
    institution: "Associated Press"
    tier: 1
    fetch_type: rss
    url: "https://apnews.com/hub/economy"
    feed_url: "https://apnews.com/hub/economy/feed"
    country_fips: null
    bucket: null
    lang: en
    ownership: private
    status: live
"""


def _write(tmp_path, text):
    p = tmp_path / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_and_types(tmp_path):
    srcs = registry.load_sources(_write(tmp_path, _VALID))
    assert [s.id for s in srcs] == ["sa_cb_sama", "wire_ap"]
    sama = srcs[0]
    assert sama.tier == 0 and sama.fetch_type == "unverified"
    assert sama.country_fips == "SA" and sama.bucket == "middle_east"
    assert sama.origin_class == ["monetary", "macro"]
    # cross-cutting wire source: null country/bucket are valid
    assert srcs[1].country_fips is None and srcs[1].bucket is None


def test_is_fetchable_property(tmp_path):
    srcs = registry.load_sources(_write(tmp_path, _VALID))
    by_id = {s.id: s for s in srcs}
    assert by_id["sa_cb_sama"].is_fetchable is False   # unverified + needs_review
    assert by_id["wire_ap"].is_fetchable is True        # rss + live


def test_duplicate_id_raises(tmp_path):
    dup = _VALID + """
  - id: sa_cb_sama
    institution: "dup"
    tier: 0
    fetch_type: rss
    url: "https://x"
"""
    with pytest.raises(ValueError, match="duplicate source id"):
        registry.load_sources(_write(tmp_path, dup))


def test_bad_tier_raises(tmp_path):
    bad = """
sources:
  - {id: x, institution: X, tier: 3, fetch_type: rss, url: "https://x"}
"""
    with pytest.raises(ValueError, match="tier"):
        registry.load_sources(_write(tmp_path, bad))


def test_bad_fetch_type_raises(tmp_path):
    bad = """
sources:
  - {id: x, institution: X, tier: 0, fetch_type: telepathy, url: "https://x"}
"""
    with pytest.raises(ValueError, match="fetch_type"):
        registry.load_sources(_write(tmp_path, bad))


def test_real_registry_loads_and_validates():
    """The shipped config/sources.yaml must parse and validate as-is."""
    srcs = registry.load_sources(registry.default_path())
    assert len(srcs) == 66
    assert len({s.id for s in srcs}) == 66  # ids unique
    assert all(s.tier in registry.VALID_TIERS for s in srcs)
    assert all(s.fetch_type in registry.VALID_FETCH_TYPES for s in srcs)


def test_curated_and_section_default_and_parse(tmp_path):
    text = """
sources:
  - id: plain
    institution: P
    tier: 2
    fetch_type: rss
    url: "https://p"
  - id: flagged
    institution: F
    tier: 2
    fetch_type: rss
    url: "https://f"
    curated: true
    section: economy
"""
    by_id = {s.id: s for s in registry.load_sources(_write(tmp_path, text))}
    # defaults when omitted
    assert by_id["plain"].curated is False
    assert by_id["plain"].section is None
    # parsed when present
    assert by_id["flagged"].curated is True
    assert by_id["flagged"].section == "economy"
