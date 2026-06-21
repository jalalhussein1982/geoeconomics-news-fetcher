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

@dataclass
class Cluster:
    cluster_id: int
    event_fingerprint: str
    region: str | None
    languages: list[str]
    article_count: int
    framing_divergent: bool
    country_fips: str | None = None

@dataclass
class SynthesisRecord:
    record_id: int
    cluster_id: int
    fields: dict[str, Any]
    quality_flag: str
    is_headline_only: bool
