from __future__ import annotations
from dataclasses import dataclass
from typing import Any

@dataclass
class Article:
    article_id: int
    source_url: str
    url_hash: str
    domain: str | None
    region: str | None
    gkg_themes: list[str]
    gkg_tone: float | None
    num_mentions: int | None
    source_lang: str | None
    title: str | None
    body: str | None
    fetch_status: str
    country_fips: str | None = None
    gate_geoeconomic: bool | None = None
    gate_score: float | None = None
    gate_country: str | None = None
    gate_reason: str | None = None
    # source provenance (Change 2). Appended so positional construction from the
    # shorter articles_in_cluster SELECT still works (these default to None there).
    discovery_source: str | None = None
    registry_id: str | None = None
    source_tier: int | None = None
    origin_class: list[str] | None = None
    ownership: str | None = None
    state_alignment: str | None = None

@dataclass
class Cluster:
    cluster_id: int
    event_fingerprint: str
    region: str | None
    languages: list[str]
    article_count: int
    framing_divergent: bool
    country_fips: str | None = None
    gate_score: float | None = None
    # provenance aggregates (Change 2); populated, not yet used for ranking.
    tier_min: int | None = None
    has_primary: bool | None = None
    distinct_state_alignments: int | None = None
    source_count: int | None = None

@dataclass
class SynthesisRecord:
    record_id: int
    cluster_id: int
    fields: dict[str, Any]
    quality_flag: str
    is_headline_only: bool
