# tests/unit/test_regions.py
from geoecon import regions


def test_egypt_in_both_middle_east_and_north_africa():
    bs = regions.buckets_for_country("EG")
    assert "middle_east" in bs and "north_africa" in bs


def test_new_middle_east_countries_present():
    for cc in ("IS", "TU", "CY"):  # Israel, Turkey, Cyprus (FIPS)
        assert "middle_east" in regions.buckets_for_country(cc)


def test_palestine_kept_sudan_dropped():
    assert "middle_east" in regions.buckets_for_country("WE")  # West Bank
    assert "middle_east" in regions.buckets_for_country("GZ")  # Gaza
    assert regions.buckets_for_country("SU") == []             # Sudan dropped


def test_latam_unchanged():
    assert regions.buckets_for_country("BR") == ["latam"]


def test_uncovered_country_is_empty():
    assert regions.buckets_for_country("US") == []
    assert regions.buckets_for_country(None) == []


def test_order_follows_bucket_order():
    assert regions.buckets_for_country("EG") == ["middle_east", "north_africa"]


def test_active_filter_restricts_buckets():
    assert regions.buckets_for_country("EG", active=("north_africa",)) == ["north_africa"]


def test_group_by_bucket_duplicates_overlap():
    items = [("a", "EG"), ("b", "BR"), ("c", "SU")]
    grouped = regions.group_by_bucket(items, country_of=lambda t: t[1])
    assert ("a", "EG") in grouped["middle_east"]
    assert ("a", "EG") in grouped["north_africa"]
    assert grouped["latam"] == [("b", "BR")]
    assert all(("c", "SU") not in v for v in grouped.values())  # Sudan dropped everywhere


def test_country_to_fips_names_and_aliases():
    assert regions.country_to_fips("Egypt") == "EG"
    assert regions.country_to_fips("saudi arabia") == "SA"
    assert regions.country_to_fips("UAE") == "AE"
    assert regions.country_to_fips("United Arab Emirates") == "AE"
    assert regions.country_to_fips("Türkiye") == "TU"
    assert regions.country_to_fips("Brazil") == "BR"
    assert regions.country_to_fips("other") is None
    assert regions.country_to_fips("United States") is None
    assert regions.country_to_fips(None) is None


def test_top_per_bucket_ranks_caps_and_handles_egypt_dual():
    # tuples: (id, country_fips, score)
    items = [("eg", "EG", 0.9), ("sa", "SA", 0.8), ("ir", "IR", 0.7),
             ("br", "BR", 0.5), ("ly", "LY", 0.4)]
    out = regions.top_per_bucket(items, country_of=lambda t: t[1],
                                 score_of=lambda t: t[2], n=2)
    me = [t[0] for t in out["middle_east"]]
    na = [t[0] for t in out["north_africa"]]
    la = [t[0] for t in out["latam"]]
    assert me == ["eg", "sa"]          # top 2 of EG/SA/IR by score
    assert na == ["eg", "ly"]          # Egypt appears in NA too, independently capped
    assert la == ["br"]
