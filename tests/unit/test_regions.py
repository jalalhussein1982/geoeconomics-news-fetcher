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


def test_cap_per_country_limits_dominant_when_supply_exists():
    # 9 IR (highest scores) + 8 distinct others; cap 4, n 12
    items = ([("IR", 100 - i) for i in range(9)]
             + [("QA", 20), ("AE", 19), ("LE", 18), ("KU", 17),
                ("BA", 16), ("JO", 15), ("SY", 14), ("YM", 13)])
    out = regions.cap_per_country(items, lambda t: t[0], cap=4, n=12)
    assert len(out) == 12
    assert sum(1 for t in out if t[0] == "IR") == 4          # IR capped at 4
    assert [t[0] for t in out[:4]] == ["IR", "IR", "IR", "IR"]  # IR top-4 by score kept, order preserved


def test_cap_per_country_backfills_when_supply_thin():
    # 7 IR + 2 others = 9 total; cap 4, n 12 -> never invents, backfills IR surplus to stay full
    items = [("IR", 100 - i) for i in range(7)] + [("QA", 5), ("AE", 4)]
    out = regions.cap_per_country(items, lambda t: t[0], cap=4, n=12)
    assert len(out) == 9                                      # only 9 items exist
    assert sum(1 for t in out if t[0] == "IR") == 7          # 4 + 3 backfilled surplus


def test_cap_per_country_noop_when_under_cap():
    items = [("QA", 5), ("AE", 4), ("IR", 3), ("IR", 2)]
    assert regions.cap_per_country(items, lambda t: t[0], cap=4, n=12) == items


def test_top_per_bucket_applies_country_cap():
    # 9 IR + 8 distinct middle_east others (17 items) so the capped set reaches n=12
    # WITHOUT backfill -> IR capped at exactly 4.
    score_of = lambda t: t[3]
    items = ([("r", "IR", None, 100 - i) for i in range(9)]
             + [("r", "QA", None, 20), ("r", "AE", None, 19), ("r", "LE", None, 18),
                ("r", "KU", None, 17), ("r", "BA", None, 16), ("r", "SA", None, 15),
                ("r", "JO", None, 14), ("r", "SY", None, 13)])
    grouped = regions.top_per_bucket(items, country_of=lambda t: t[1], score_of=score_of,
                                     n=12, active=("middle_east",), country_cap=4)
    me = grouped["middle_east"]
    assert len(me) == 12 and sum(1 for t in me if t[1] == "IR") == 4


def test_top_per_bucket_cap_none_is_unchanged():
    score_of = lambda t: t[3]
    items = [("r", "IR", None, 100 - i) for i in range(9)] + [("r", "QA", None, 1)]
    off = regions.top_per_bucket(items, country_of=lambda t: t[1], score_of=score_of,
                                 n=12, active=("middle_east",))
    explicit_none = regions.top_per_bucket(items, country_of=lambda t: t[1], score_of=score_of,
                                           n=12, active=("middle_east",), country_cap=None)
    assert off == explicit_none
    assert len(off["middle_east"]) == 10   # all 10 items, no cap applied
