"""Google News RSS search provider -- the Layer-1 spine (spec 2026-07-04).

One feed per (country, lang, family): /rss/search?q=<expr> when:Nd with the
country's edition params. Feeds cap at ~100 items server-side. Links are
aggregator-encoded; we keep the newest `limit` items and decode only those
(each online decode costs up to 2 HTTP calls -- see gn_decode)."""
from __future__ import annotations

import calendar
import math
import time
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
import requests

from ..fetchers.http import BROWSER_UA
from . import gn_decode, matrix
from .base import SearchItem

_SEARCH_URL = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def _entry_ts(entry) -> datetime | None:
    st = entry.get("published_parsed")
    if not st:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


class GoogleNewsProvider:
    name = "googlenews"

    def __init__(self, *, http=requests, decode=gn_decode.decode,
                 pace_s: float = 1.0, sleep=time.sleep):
        self._http = http
        self._decode = decode
        self._pace_s = pace_s
        self._sleep = sleep

    def search(self, expr: str, spec, lang: str, window_hours: int,
               limit: int) -> list[SearchItem]:
        days = max(1, math.ceil(window_hours / 24))
        q = f"{expr} when:{days}d"
        if spec.global_en:
            # No viable local edition/vocabulary (e.g. Iran/Farsi): global
            # English edition, country name ANDed into the query.
            q = f'"{spec.name}" {q}'
            hl, gl, ceid = "en", "US", "US:en"
        else:
            hl, gl, ceid = matrix.hl_for(lang), spec.gl, matrix.ceid_for(spec, lang)
        url = _SEARCH_URL.format(q=quote(q), hl=hl, gl=gl, ceid=quote(ceid))
        r = self._http.get(url, timeout=30, headers={"User-Agent": BROWSER_UA})
        entries = feedparser.parse(r.content).entries or []
        # newest first; undated entries sort last but are kept (recall bias)
        entries = sorted(entries, key=lambda e: _entry_ts(e) or datetime.min.replace(
            tzinfo=timezone.utc), reverse=True)[:limit]
        items: list[SearchItem] = []
        for e in entries:
            link = e.get("link")
            if not link:
                continue
            src = getattr(e, "source", None)
            decoded = self._decode(link, http=self._http)
            items.append(SearchItem(
                url=decoded or link,
                title=e.get("title"),
                ts=_entry_ts(e),
                source_name=(src.get("title") if src else None),
                lang=lang if not spec.global_en else "en",
                decoded=decoded is not None,
            ))
            if self._pace_s:
                self._sleep(self._pace_s)
        return items
