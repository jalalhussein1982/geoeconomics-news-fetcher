"""Decode news.google.com/rss/articles/<token> links to publisher URLs.

Google News RSS links don't resolve with a plain GET (they land on
consent.google.com -- verified 2026-07-04). Two strategies, cheapest first:

1. offline: base64-decode the path token. Pre-2024 tokens embed the URL in a
   tiny protobuf frame; still true for a share of links. Zero network.
2. online: the token needs Google's own decoder. CONTINGENCY TAKEN
   (2026-07-04): the vendored GET-article-page + POST-batchexecute recipe was
   live-tested here and gets stuck in an extra consent redirect loop
   (news.google.com -> consent.google.com/dl -> consent.google.com/ml -> ...)
   that this environment's egress IP triggers (resolves to a non-US region).
   Manual debugging (CONSENT cookie, gl/hl URL params, walking the redirect
   chain) did not clear it within the timeboxed attempts, while the
   `googlenewsdecoder` package's own session/redirect handling does resolve
   real links (verified live 2026-07-04). decode_online now delegates to it;
   http/timeout are accepted for signature compatibility but unused --
   the package manages its own requests session and does not take either.

Both return None on any failure; callers keep the encoded URL and count it
(telemetry), never raise.
"""
from __future__ import annotations

import base64
import re

import requests
from googlenewsdecoder import gnewsdecoder

_TOKEN_RE = re.compile(r"news\.google\.com/(?:rss/articles|articles|read)/([^?/&#]+)")
_URL_RE = re.compile(rb"https?://[\x21-\x7e]+")


def _token(gn_url: str) -> str | None:
    m = _TOKEN_RE.search(gn_url or "")
    return m.group(1) if m else None


def decode_offline(gn_url: str) -> str | None:
    tok = _token(gn_url)
    if not tok:
        return None
    try:
        raw = base64.urlsafe_b64decode(tok + "===")
    except Exception:
        return None
    m = _URL_RE.search(raw)
    if not m:
        return None
    url = m.group(0).decode(errors="replace")
    # The frame length-prefixes the URL; the regex can overrun into trailing
    # protobuf bytes for the AMP variant -- cut at the first non-URL byte the
    # length byte declared. Simplest robust trim: stop at first byte < 0x20
    # already excluded; drop a trailing partial field marker if present.
    url = url.split("\xd2")[0]
    return url if url.startswith("http") else None


def decode_online(gn_url: str, *, http=requests, timeout: int = 20) -> str | None:
    # http/timeout kept for signature compatibility with decode(); unused --
    # see module docstring (contingency: googlenewsdecoder manages its own
    # HTTP session and redirect/consent handling).
    try:
        out = gnewsdecoder(gn_url)
        if not isinstance(out, dict) or not out.get("status"):
            return None
        url = out.get("decoded_url")
        return url if isinstance(url, str) and url.startswith("http") else None
    except Exception:
        return None


def decode(gn_url: str, *, http=requests, timeout: int = 20) -> str | None:
    got = decode_offline(gn_url)
    if got:
        return got
    if http is None:
        return None
    return decode_online(gn_url, http=http, timeout=timeout)
