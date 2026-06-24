# geoecon/ranking.py
"""Ranking helpers shared by the render and summary stages.

De-prioritizes US/global market-index and commodity-price roundups so they sort
below country-subject stories within a region (they are never dropped — only ranked
last). Deliberately excludes country-qualified markets ("Argentine stocks",
"Colombian peso") and index *reclassification* news (MSCI), which are legitimate
country-subject geoeconomic stories.
"""
from __future__ import annotations

from typing import Callable

_MARKET_PENALTY = 1000.0

_MARKET_TERMS = (
    "s&p 500", "s&p500", "nasdaq", "dow jones", "the dow", "wall street",
    "russell 2000", "ftse", "nikkei", "hang seng", "stoxx", "treasury yields",
    "gold futures", "gold price", "bullion", "us stocks", "u.s. stocks",
)


def is_market_roundup(rec) -> bool:
    f = getattr(rec, "fields", {}) or {}
    hay = f"{f.get('headline_en', '')} {f.get('what_happened_en', '')}".lower()
    return any(term in hay for term in _MARKET_TERMS)


def make_score_of(cfg) -> Callable[[tuple], float]:
    """Return a score_of(record_tuple) for regions.top_per_bucket.

    record_tuple is (SynthesisRecord, country_fips, source_links, rank_score,
    has_curated, has_economy_section). When cfg.market_demote is on, flagged market
    roundups get a large penalty so they sort below every country-subject story
    (and are NOT rescued by the curated/economy nudge). When cfg.curated_preference
    is on, economy-section clusters get a small boost and curated clusters a small
    boost — mild reordering of ties, never dominating gate_score. Tuples shorter
    than 6 elements are treated as not-curated/not-economy (backward compatible).
    """
    demote = getattr(cfg, "market_demote", True)
    prefer = getattr(cfg, "curated_preference", False)
    curated_boost = getattr(cfg, "curated_boost", 0.0)
    economy_boost = getattr(cfg, "economy_section_boost", 0.0)

    def score_of(t) -> float:
        base = t[3] or 0.0
        if demote and is_market_roundup(t[0]):
            return base - _MARKET_PENALTY
        if prefer:
            if len(t) > 5 and t[5]:
                base += economy_boost
            if len(t) > 4 and t[4]:
                base += curated_boost
        return base

    return score_of
