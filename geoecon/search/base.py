"""Search-provider contract (spec 2026-07-04). Mirrors fetchers/base.py:
a provider takes one query and returns links it can see right now; bodies are
the extraction stage's job. Adding a vendor = one adapter file, no stage change."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class SearchItem:
    url: str
    title: str | None = None
    ts: datetime | None = None          # publish time if known; None => keep (gate judges)
    source_name: str | None = None
    lang: str | None = None
    decoded: bool = True                # False => url is still an aggregator redirect


class SearchProvider(Protocol):
    name: str

    def search(self, expr: str, spec, lang: str, window_hours: int,
               limit: int) -> list[SearchItem]:
        ...
