from geoecon import ranking, regions
from geoecon.state.models import SynthesisRecord


def _rec(headline, what="x"):
    return SynthesisRecord(1, 10, {"headline_en": headline, "what_happened_en": what}, "ok", False)


class _Cfg:
    market_demote = True


class _CfgB:
    market_demote = True
    curated_preference = True
    curated_boost = 0.15
    economy_section_boost = 0.10


def _t(headline, score, *, curated=False, economy=False):
    return (_rec(headline), "IR", [], score, curated, economy)


def test_is_market_roundup_flags_us_global_indices():
    assert ranking.is_market_roundup(_rec("S&P 500 slips as AI stocks slide")) is True
    assert ranking.is_market_roundup(_rec("U.S. stocks hold near records on Wall Street")) is True
    assert ranking.is_market_roundup(_rec("Gold futures gap lower on open")) is True


def test_is_market_roundup_spares_country_market_stories():
    assert ranking.is_market_roundup(_rec("Argentina stocks hit record before MSCI decision")) is False
    assert ranking.is_market_roundup(_rec("De la Espriella victory drives Colombian peso higher")) is False
    assert ranking.is_market_roundup(_rec("Oil prices fall as US-Iran talks yield waivers")) is False


def test_make_score_of_demotes_market_when_enabled():
    score_of = ranking.make_score_of(_Cfg())
    market = (_rec("S&P 500 slips"), "IR", [], 0.9)
    country = (_rec("Iran oil license issued"), "IR", [], 0.1)
    assert score_of(market) < score_of(country)


def test_make_score_of_respects_toggle_off():
    class Off:
        market_demote = False
    score_of = ranking.make_score_of(Off())
    assert score_of((_rec("S&P 500 slips"), "IR", [], 0.9)) == 0.9


def test_top_per_bucket_places_market_last():
    score_of = ranking.make_score_of(_Cfg())
    items = [(_rec("S&P 500 slips"), "IR", [], 0.9),
             (_rec("Iran oil license"), "IR", [], 0.2)]
    grouped = regions.top_per_bucket(items, country_of=lambda t: t[1],
                                     score_of=score_of, n=10, active=("middle_east",))
    me = grouped["middle_east"]
    assert me[0][0].fields["headline_en"] == "Iran oil license"
    assert me[-1][0].fields["headline_en"] == "S&P 500 slips"


def test_economy_section_outranks_equal_general():
    score_of = ranking.make_score_of(_CfgB())
    econ = _t("Iran budget passed", 0.5, economy=True, curated=True)
    gen = _t("Iran politics shift", 0.5, curated=True)
    assert score_of(econ) > score_of(gen)


def test_curated_outranks_equal_noncurated():
    score_of = ranking.make_score_of(_CfgB())
    cur = _t("Iran trade deal", 0.5, curated=True)
    non = _t("Iran trade deal", 0.5)
    assert score_of(cur) > score_of(non)


def test_curated_market_roundup_still_demoted():
    score_of = ranking.make_score_of(_CfgB())
    market = _t("S&P 500 slips", 0.9, curated=True, economy=True)
    country = _t("Iran oil license", 0.1)
    assert score_of(market) < score_of(country)


def test_curated_preference_off_is_byte_identical():
    class Off:
        market_demote = True
        curated_preference = False
        curated_boost = 0.15
        economy_section_boost = 0.10
    score_of = ranking.make_score_of(Off())
    assert score_of(_t("Iran trade deal", 0.5, curated=True, economy=True)) == 0.5


def test_short_tuple_is_backward_compatible():
    # the existing 4-tuple shape must not raise
    score_of = ranking.make_score_of(_CfgB())
    assert score_of((_rec("Iran trade deal"), "IR", [], 0.5)) == 0.5
