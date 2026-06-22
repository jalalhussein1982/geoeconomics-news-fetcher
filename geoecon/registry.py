"""Source registry — load + validate config/sources.yaml into typed Source records.

The YAML is DATA, not code (see its header). This module turns it into validated
`Source` objects the discovery_registry stage dispatches on. Two layers of trust:
the *librarian* fields (identity/tier/provenance/lang) are human-verified; the
*mechanics* fields (fetch_type/feed_url/extract_hint) are resolved by Task 0 and
ship as `fetch_type: unverified` until then — those are skipped at runtime.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("geoecon.registry")

VALID_FETCH_TYPES = {"rss", "api", "scrape", "dead", "unverified"}
VALID_TIERS = {0, 1, 2}

# fetch_types that cannot be fetched yet — skipped at runtime by the stage.
SKIP_FETCH_TYPES = {"unverified", "dead"}


@dataclass(frozen=True)
class Source:
    id: str
    institution: str
    tier: int
    fetch_type: str
    url: str
    country: str | None = None        # ISO 3166-1 (registry-side, human-readable)
    country_fips: str | None = None   # FIPS 10-4 (what the pipeline buckets on)
    bucket: str | None = None         # middle_east | north_africa | latam | None
    origin_class: list[str] = field(default_factory=list)
    feed_url: str | None = None
    probe_paths: list[str] = field(default_factory=list)
    extract_hint: str | None = None
    lang: str | None = None
    ownership: str | None = None
    state_alignment: str | None = None
    reliability: str | None = None
    notes: str | None = None
    verified: str | None = None
    status: str | None = None         # live | needs_review | dead

    @property
    def is_fetchable(self) -> bool:
        """True if Task 0 has resolved real mechanics and the source is ready."""
        return self.fetch_type not in SKIP_FETCH_TYPES and self.status != "needs_review"


def default_path() -> Path:
    """Repo-root/config/sources.yaml, resolved relative to this file (CWD-independent)."""
    return Path(__file__).resolve().parent.parent / "config" / "sources.yaml"


_KNOWN_FIELDS = set(Source.__dataclass_fields__.keys())


def _coerce(entry: dict) -> Source:
    kw = {k: v for k, v in entry.items() if k in _KNOWN_FIELDS}
    # Normalize list fields that YAML may emit as null.
    kw["origin_class"] = list(kw.get("origin_class") or [])
    kw["probe_paths"] = list(kw.get("probe_paths") or [])
    return Source(**kw)


def load_sources(path: str | Path | None = None) -> list[Source]:
    """Parse + validate the registry. Raises ValueError on a malformed registry.

    Validation: unique `id`; `tier ∈ {0,1,2}`; `fetch_type ∈ {rss,api,scrape,dead,
    unverified}`. `country_fips` may be a FIPS code or null (both valid).
    """
    p = Path(path) if path else default_path()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    entries = raw.get("sources") or []

    sources: list[Source] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or "id" not in entry:
            raise ValueError(f"registry entry #{i} is missing an id")
        sid = entry["id"]
        if sid in seen_ids:
            raise ValueError(f"duplicate source id: {sid!r}")
        seen_ids.add(sid)

        src = _coerce(entry)
        if src.tier not in VALID_TIERS:
            raise ValueError(f"{sid}: tier {src.tier!r} not in {sorted(VALID_TIERS)}")
        if src.fetch_type not in VALID_FETCH_TYPES:
            raise ValueError(f"{sid}: fetch_type {src.fetch_type!r} not in {sorted(VALID_FETCH_TYPES)}")
        sources.append(src)

    return sources
