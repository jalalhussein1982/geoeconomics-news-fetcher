from __future__ import annotations

import gzip
import hashlib
import io
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

# GKG 2.1 tab-delimited column indices (per published codebook)
_DATE, _DOMAIN, _URL, _THEMES, _LOCATIONS, _TONE = 1, 3, 4, 7, 9, 15

# Country code -> region mapping.
# IMPORTANT: GDELT GKG LOCATIONS emit FIPS 10-4 country codes, NOT ISO 3166-1.
# Using ISO codes here silently dropped real targets (Lebanon LE, Iraq IZ, Chile
# CI, ...) and mislabeled collisions (FIPS NI=Nigeria was read as ISO Nicaragua).
# Codes below are FIPS 10-4; the trailing comment is the country name.
MENA = {
    "SA",  # Saudi Arabia
    "AE",  # United Arab Emirates
    "EG",  # Egypt
    "IZ",  # Iraq
    "IR",  # Iran
    "SY",  # Syria
    "JO",  # Jordan
    "LE",  # Lebanon
    "QA",  # Qatar
    "KU",  # Kuwait
    "MU",  # Oman
    "BA",  # Bahrain
    "YM",  # Yemen
    "AG",  # Algeria
    "MO",  # Morocco
    "TS",  # Tunisia
    "LY",  # Libya
    "SU",  # Sudan
    "WE",  # West Bank
    "GZ",  # Gaza Strip
}
LATAM = {
    "BR",  # Brazil
    "AR",  # Argentina
    "MX",  # Mexico
    "CI",  # Chile
    "CO",  # Colombia
    "PE",  # Peru
    "VE",  # Venezuela
    "EC",  # Ecuador
    "BL",  # Bolivia
    "PA",  # Paraguay
    "UY",  # Uruguay
    "GT",  # Guatemala
    "CU",  # Cuba
    "DR",  # Dominican Republic
    "HO",  # Honduras
    "NU",  # Nicaragua
    "CS",  # Costa Rica
    "PM",  # Panama
    "ES",  # El Salvador
}
TARGET_THEME_PREFIXES = ("ECON_", "WB_", "EPU_")
# Core economic-policy prefixes. WB_ alone (the ~2,000-entry World Bank taxonomy)
# is too broad; requiring at least one core hit keeps genuinely geoeconomic items.
CORE_THEME_PREFIXES = ("ECON_", "EPU_")


@dataclass
class GkgRecord:
    gkg_id: str
    ts: datetime | None
    domain: str
    url: str
    themes: list[str]
    country: str | None
    tone: float | None
    lang: str | None


def url_hash(url: str) -> str:
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()


def region_for_country(cc: str | None) -> str:
    if cc in MENA:
        return "MENA"
    if cc in LATAM:
        return "LatAm"
    return "other"


def theme_intersection_ok(
    themes: list[str],
    target_prefixes: tuple[str, ...] = TARGET_THEME_PREFIXES,
    min_count: int = 2,
    core_prefixes: tuple[str, ...] = CORE_THEME_PREFIXES,
) -> bool:
    """True if the article clears the geoeconomic gate.

    Requires >= min_count target-prefix hits AND at least one *core* economic
    theme (ECON_/EPU_). The core requirement rejects items that only match the
    very broad WB_ taxonomy, which on its own admits almost anything economic.
    """
    hits = sum(1 for t in themes if any(t.startswith(p) for p in target_prefixes))
    if hits < min_count:
        return False
    return any(any(t.startswith(p) for p in core_prefixes) for t in themes)


def theme_salience(
    themes: list[str],
    target_prefixes: tuple[str, ...] = TARGET_THEME_PREFIXES,
) -> float:
    """Salience proxy in [0, 1]: density of target themes among all themes.

    Replaces raw theme *count*, which promoted sprawling omnibus articles
    (news briefs, opinion roundups touching hundreds of shallow themes) to the
    top. Density rewards focused economic coverage instead.
    """
    if not themes:
        return 0.0
    hits = sum(1 for t in themes if any(t.startswith(p) for p in target_prefixes))
    return hits / len(themes)


def parse_gkg_line(line: str) -> GkgRecord | None:
    f = line.rstrip("\n").split("\t")
    if len(f) <= _TONE:
        return None
    url = f[_URL].strip()
    if not url:
        return None
    themes = [t.split(",")[0] for t in f[_THEMES].split(";") if t]
    # Country = the most-mentioned country across ALL locations, not just the
    # first. GDELT lists locations in order of appearance, so "first" tracked the
    # dateline/wire origin (often US) rather than the article's geographic focus.
    country = None
    if f[_LOCATIONS]:
        codes: list[str] = []
        for loc in f[_LOCATIONS].split(";"):
            parts = loc.split("#")
            if len(parts) > 2 and parts[2]:
                codes.append(parts[2])
        if codes:
            # Counter.most_common breaks ties by first insertion (first mention).
            country = Counter(codes).most_common(1)[0][0]
    tone = None
    if f[_TONE]:
        try:
            tone = float(f[_TONE].split(",")[0])
        except ValueError:
            tone = None
    try:
        ts = datetime.strptime(f[_DATE], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        ts = None
    return GkgRecord(
        gkg_id=f[0],
        ts=ts,
        domain=f[_DOMAIN].strip(),
        url=url,
        themes=themes,
        country=country,
        tone=tone,
        lang=None,
    )


def unzip_gkg_bytes(blob: bytes) -> list[str]:
    """Decompress a .gkg.csv.zip (or .gz) payload into text lines."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = z.namelist()[0]
            return z.read(name).decode("utf-8", "replace").splitlines()
    except zipfile.BadZipFile:
        return gzip.decompress(blob).decode("utf-8", "replace").splitlines()
