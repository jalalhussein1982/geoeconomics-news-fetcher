from geoecon.search import matrix
from geoecon import regions


def test_load_countries_covers_all_bucket_fips_once():
    specs = matrix.load_countries()
    fips = [s.fips for s in specs]
    assert len(fips) == len(set(fips))                      # no dup entries
    bucket_fips = set().union(*regions.BUCKETS.values())
    # GZ is deliberately folded into WE (Palestine) -- the only allowed gap.
    assert bucket_fips - set(fips) == {"GZ"}
    assert set(fips) <= bucket_fips                          # no out-of-scope countries


def test_country_fields():
    by_fips = {s.fips: s for s in matrix.load_countries()}
    assert by_fips["CI"].gl == "CL"                          # FIPS Chile -> ISO CL
    assert by_fips["IR"].global_en and by_fips["IR"].langs == ()
    assert by_fips["EG"].langs == ("ar", "en")
    assert by_fips["BR"].ceid == {"pt": "BR:pt-419"}
    assert by_fips["SA"].gdelt_sc == "saudiarabia"


def test_load_keywords_families_and_langs():
    kw = matrix.load_keywords()
    assert set(kw) == {"family1", "family2"}
    for fam in kw.values():
        assert set(fam) == {"en", "ar", "es", "pt", "tr", "he", "fr"}
        for terms in fam.values():
            assert len(terms) >= 8


def test_or_expr_and_edition_helpers():
    assert matrix.or_expr(["a", '"b c"']) == '(a OR "b c")'
    specs = {s.fips: s for s in matrix.load_countries()}
    assert matrix.hl_for("es") == "es-419"
    assert matrix.ceid_for(specs["MX"], "es") == "MX:es-419"
    assert matrix.ceid_for(specs["BR"], "pt") == "BR:pt-419"   # explicit override
    assert matrix.ceid_for(specs["EG"], "ar") == "EG:ar"
