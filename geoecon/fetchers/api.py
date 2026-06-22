"""API fetcher — per-source adapters keyed by source id.

Deliberately minimal: there are NO speculative adapters. Task 0 registers a
concrete adapter only for a source it actually confirmed exposes a JSON/OData
article list, by adding an entry to `ADAPTERS` here. Until then, dispatching an
`api` source raises (a loud signal the adapter is missing), and the stage's
per-source try/except logs+skips it rather than aborting the run.
"""
from __future__ import annotations

from typing import Callable

from .base import DiscoveredItem

# id -> callable(source, fetch) -> list[DiscoveredItem]. Populated by Task 0.
ADAPTERS: dict[str, Callable] = {}


class ApiFetcher:
    def __init__(self, *, adapters: dict | None = None):
        self._adapters = ADAPTERS if adapters is None else adapters

    def discover(self, source) -> list[DiscoveredItem]:
        adapter = self._adapters.get(source.id)
        if adapter is None:
            raise ValueError(f"no api adapter registered for source id={source.id!r}")
        return adapter(source)
