"""GDELT DOC 2.0 API client (spec 2026-07-04) -- replaces raw GKG file pulls.

Full-text search runs over GDELT's machine-translated English, so English
keyword expressions cover Arabic/Spanish/etc. sources; `sourcecountry:` gives
source-based country attribution (cleaner than most-mentioned-FIPS).

Operational lessons baked in (2026-07-04 probe): the public limiter wants
MORE than 5s between requests -- we pace 6s BEFORE every call and back off
30s once on 429; errors degrade to [] (the caller counts, never crashes)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_UA = "geoecon-pipeline/0.1 (research newsletter; low volume)"


@dataclass
class DocArticle:
    url: str
    title: str | None
    domain: str | None
    language: str | None
    seendate: datetime | None


def build_query(gdelt_sc: str, en_terms: list[str]) -> str:
    return f"sourcecountry:{gdelt_sc} (" + " OR ".join(en_terms) + ")"


def _parse_seendate(s) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_articles(query: str, timespan_h: int, *, max_records: int = 250,
                   http=requests, sleep=time.sleep, pace_s: float = 6.0) -> list[DocArticle]:
    params = {"query": query, "mode": "artlist", "format": "json",
              "timespan": f"{timespan_h}h", "maxrecords": max_records,
              "sort": "datedesc"}
    for attempt in (1, 2):
        sleep(pace_s)
        try:
            r = http.get(BASE_URL, params=params, timeout=60,
                         headers={"User-Agent": _UA})
        except Exception:
            # Request-level exception: return [] immediately, no retry
            return []
        if r.status_code == 429:
            if attempt == 1:
                sleep(30.0)
                continue
            return []
        try:
            data = json.loads(r.text)
        except (ValueError, TypeError):
            return []
        # Guard against null or non-dict JSON
        if not isinstance(data, dict):
            return []
        return [DocArticle(url=a.get("url"), title=a.get("title"),
                           domain=a.get("domain"), language=a.get("language"),
                           seendate=_parse_seendate(a.get("seendate")))
                for a in data.get("articles", []) if a.get("url")]
