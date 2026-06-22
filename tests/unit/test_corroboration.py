"""Change 4 — corroboration scorer. Tests assert RELATIVE orderings (the weights are
provisional / calibration-deferred), proving the independence logic on the four mixes
the brief actually produces."""
from geoecon import corroboration


def s(tier_min, has_primary, distinct, count):
    return corroboration.score(tier_min=tier_min, has_primary=has_primary,
                               distinct_state_alignments=distinct, source_count=count)


# The four scenarios from the spec.
ALL_WIRE       = s(tier_min=1, has_primary=False, distinct=0, count=3)   # only wires
WIRE_PRIMARY   = s(tier_min=0, has_primary=True,  distinct=1, count=2)   # wire + a Tier-0 primary
CO_ALIGNED     = s(tier_min=2, has_primary=False, distinct=1, count=2)   # AJ@QA + 2nd QA source
CROSS_ALIGNED  = s(tier_min=2, has_primary=False, distinct=2, count=2)   # AJ@QA + AlArabiya@SA
SINGLE_REGIONAL = s(tier_min=2, has_primary=False, distinct=1, count=1)  # one regional source


def test_primary_anchor_beats_all_wire():
    assert WIRE_PRIMARY > ALL_WIRE


def test_cross_aligned_corroboration_beats_co_aligned():
    # Two sources sharing a state_alignment are NOT two independent witnesses;
    # two cross-aligned ones ARE. Independence must raise the score.
    assert CROSS_ALIGNED > CO_ALIGNED


def test_co_alignment_adds_no_independence_only_volume():
    # Going single -> co-aligned adds only the faint volume nudge (no independence);
    # co-aligned -> cross-aligned adds a real independent witness. The second jump
    # must dominate the first, i.e. independence outweighs raw volume.
    volume_only = CO_ALIGNED - SINGLE_REGIONAL
    independence = CROSS_ALIGNED - CO_ALIGNED
    assert volume_only >= 0
    assert independence > volume_only


def test_more_independent_witnesses_score_higher():
    one = s(tier_min=2, has_primary=False, distinct=1, count=2)
    two = s(tier_min=2, has_primary=False, distinct=2, count=2)
    three = s(tier_min=2, has_primary=False, distinct=3, count=3)
    assert three > two > one


def test_none_aggregates_degrade_not_raise():
    # a GDELT-only cluster carries no provenance
    val = corroboration.score(tier_min=None, has_primary=False,
                              distinct_state_alignments=0, source_count=1)
    assert isinstance(val, float)
