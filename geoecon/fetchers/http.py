"""Shared HTTP constants for the fetchers.

Several outlets (Clarín, La Razón, El País, Seúl) serve valid RSS but reject a
bot/identifying User-Agent. Fetchers send this realistic browser UA so those
feeds resolve instead of returning 403/empty.
"""
from __future__ import annotations

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
