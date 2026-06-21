# geoecon/regions.py
from __future__ import annotations

from typing import Callable, Iterable

# FIPS 10-4 country codes (GDELT GKG emits FIPS, not ISO). A country may belong
# to more than one bucket (Egypt -> middle_east AND north_africa).
BUCKETS: dict[str, set[str]] = {
    "middle_east": {
        "BA",  # Bahrain
        "IR",  # Iran
        "IZ",  # Iraq
        "IS",  # Israel
        "JO",  # Jordan
        "KU",  # Kuwait
        "LE",  # Lebanon
        "MU",  # Oman
        "QA",  # Qatar
        "SA",  # Saudi Arabia
        "SY",  # Syria
        "AE",  # United Arab Emirates
        "YM",  # Yemen
        "EG",  # Egypt (also north_africa)
        "TU",  # Turkey
        "CY",  # Cyprus
        "WE",  # West Bank
        "GZ",  # Gaza Strip
    },
    "north_africa": {
        "EG",  # Egypt (also middle_east)
        "LY",  # Libya
        "TS",  # Tunisia
        "AG",  # Algeria
        "MO",  # Morocco
    },
    "latam": {
        "BR", "AR", "MX", "CI", "CO", "PE", "VE", "EC", "BL",
        "PA", "UY", "GT", "CU", "DR", "HO", "NU", "CS", "PM", "ES",
    },
}

BUCKET_DISPLAY: dict[str, str] = {
    "middle_east": "Middle East",
    "north_africa": "North Africa",
    "latam": "Latin America",
}

BUCKET_ORDER: tuple[str, ...] = ("middle_east", "north_africa", "latam")


def buckets_for_country(cc: str | None, active: Iterable[str] = BUCKET_ORDER) -> list[str]:
    """Return every ACTIVE bucket whose FIPS set contains *cc*, in BUCKET_ORDER.

    Empty list means the country is not covered (article is dropped at discovery).
    """
    if not cc:
        return []
    active_set = set(active)
    return [b for b in BUCKET_ORDER if b in active_set and cc in BUCKETS[b]]


def group_by_bucket(items, country_of: Callable, active: Iterable[str] = BUCKET_ORDER) -> dict[str, list]:
    """Group *items* into {bucket: [items]} by each item's country (via *country_of*).

    Every active bucket is present (possibly empty). An item whose country maps to
    multiple buckets is placed in each.
    """
    active_list = [b for b in BUCKET_ORDER if b in set(active)]
    out: dict[str, list] = {b: [] for b in active_list}
    for it in items:
        for b in buckets_for_country(country_of(it), active_list):
            out[b].append(it)
    return out


# Country NAME -> FIPS for the in-scope set, with common aliases. Used by the
# geoeconomic gate, which returns a primary_country NAME chosen from this list.
_NAME_TO_FIPS: dict[str, str] = {
    "bahrain": "BA", "iran": "IR", "iraq": "IZ", "israel": "IS", "jordan": "JO",
    "kuwait": "KU", "lebanon": "LE", "oman": "MU", "qatar": "QA",
    "saudi arabia": "SA", "syria": "SY", "united arab emirates": "AE", "uae": "AE",
    "yemen": "YM", "egypt": "EG", "turkey": "TU", "türkiye": "TU", "cyprus": "CY",
    "west bank": "WE", "palestine": "WE", "gaza": "GZ", "gaza strip": "GZ",
    "libya": "LY", "tunisia": "TS", "algeria": "AG", "morocco": "MO",
    "brazil": "BR", "argentina": "AR", "mexico": "MX", "chile": "CI",
    "colombia": "CO", "peru": "PE", "venezuela": "VE", "ecuador": "EC",
    "bolivia": "BL", "paraguay": "PA", "uruguay": "UY", "guatemala": "GT",
    "cuba": "CU", "dominican republic": "DR", "honduras": "HO", "nicaragua": "NU",
    "costa rica": "CS", "panama": "PM", "el salvador": "ES",
}


def country_to_fips(name: str | None) -> str | None:
    """Resolve an in-scope country NAME (or alias) to its FIPS code; None otherwise."""
    if not name:
        return None
    return _NAME_TO_FIPS.get(name.strip().lower())


def top_per_bucket(items, country_of: Callable, score_of: Callable, n: int,
                   active: Iterable[str] = BUCKET_ORDER) -> dict[str, list]:
    """group_by_bucket, then sort each bucket by score_of desc and keep the top n."""
    grouped = group_by_bucket(items, country_of, active)
    return {b: sorted(v, key=score_of, reverse=True)[:n] for b, v in grouped.items()}
