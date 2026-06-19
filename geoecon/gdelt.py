from __future__ import annotations

import gzip
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone

# GKG 2.1 tab-delimited column indices (per published codebook)
_DATE, _DOMAIN, _URL, _THEMES, _LOCATIONS, _TONE = 1, 3, 4, 7, 9, 15

# Country code -> region mapping
MENA = {
    "SA", "AE", "EG", "IQ", "IR", "SY", "JO", "LB", "QA", "KW",
    "OM", "BH", "YE", "DZ", "MA", "TN", "LY", "SD", "PS",
}
LATAM = {
    "BR", "AR", "MX", "CL", "CO", "PE", "VE", "EC", "BO", "PY",
    "UY", "GT", "CU", "DO", "HN", "NI", "CR", "PA", "SV",
}
TARGET_THEME_PREFIXES = ("ECON_", "WB_", "EPU_")


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
) -> bool:
    hits = sum(1 for t in themes if any(t.startswith(p) for p in target_prefixes))
    return hits >= min_count


def parse_gkg_line(line: str) -> GkgRecord | None:
    f = line.rstrip("\n").split("\t")
    if len(f) <= _TONE:
        return None
    url = f[_URL].strip()
    if not url:
        return None
    themes = [t.split(",")[0] for t in f[_THEMES].split(";") if t]
    country = None
    if f[_LOCATIONS]:
        first = f[_LOCATIONS].split(";")[0].split("#")
        if len(first) > 2:
            country = first[2] or None
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
