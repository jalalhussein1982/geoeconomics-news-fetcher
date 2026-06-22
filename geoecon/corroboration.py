"""Corroboration scoring (Change 4).

Scores a cluster by how well-corroborated its event is, from the provenance
aggregates Change 2 populated on the cluster (tier_min, has_primary,
distinct_state_alignments, source_count). The intent: an event carried by a
Tier-0 primary, or by multiple *independent* witnesses, is more trustworthy than
one carried by a single source or by several sources that all share a state's
framing.

Independence is keyed on `distinct_state_alignments`, NOT raw `source_count`:
two sources sharing a state_alignment (e.g. two Qatar-aligned outlets) are ONE
independent witness, not two. Raw volume gets only a faint nudge so it can never
outweigh genuine cross-aligned corroboration.

CALIBRATION STATUS: the *structure* here is the deliverable; the weight *values*
are provisional placeholders. They cannot be calibrated in a unit-test
environment — they must be tuned against a live run's real source distribution.
The ranking switch that consumes this (Config.rank_by_corroboration) ships OFF;
flip it only after reviewing a real before/after brief diff.
"""
from __future__ import annotations

# --- weights -----------------------------------------------------------------
# CALIBRATION TODO: tune against live run distribution. Provisional values chosen
# only to encode the required orderings (primary-anchor > all-wire; cross-aligned
# corroboration > co-aligned; independence dominates raw volume).
W_PRIMARY_ANCHOR = 1.0   # a Tier-0 primary is present in the cluster
W_TIER = 0.5             # per tier-level better than 2 (tier0 -> +1.0, tier1 -> +0.5)
W_INDEPENDENT = 0.6      # per INDEPENDENT (distinct state-alignment) witness beyond the first
W_VOLUME = 0.1           # raw source count, de-emphasized so it can't outweigh independence


def score(*, tier_min, has_primary, distinct_state_alignments, source_count) -> float:
    """Corroboration score for one cluster from its provenance aggregates.

    All args may be None (e.g. a GDELT-only cluster carries no provenance); they
    degrade to a low-but-defined score rather than raising.
    """
    s = 0.0
    if has_primary:
        s += W_PRIMARY_ANCHOR
    if tier_min is not None:
        s += W_TIER * (2 - tier_min)
    # independent witnesses = distinct state-alignments beyond the first. Co-aligned
    # sources do not add independence; cross-aligned ones do.
    independent = max((distinct_state_alignments or 0) - 1, 0)
    s += W_INDEPENDENT * independent
    # faint volume nudge (cannot dominate a single independence step)
    s += W_VOLUME * max((source_count or 1) - 1, 0)
    return s
