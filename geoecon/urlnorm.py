"""URL normalization for cross-source dedup.

GDELT and the registry surface the same story under URL variants (AMP, tracking
params, redirect wrappers, www/non-www). `sha1(url.strip())` treats those as
distinct, which would double-count witnesses and later corrupt corroboration
counts (Change 4). `normalize_url` collapses the known-equivalent variants into
one canonical string; `normalized_url_hash` is the dedup key both discovery
stages use for `url_hash`.

Design bias: CONSERVATIVE. Over-normalizing (stripping a meaningful `?id=`)
merges *distinct* articles and silently drops coverage — strictly worse than
under-normalizing. Only the known-tracking param set is stripped; everything
else is kept.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

# Known tracking params — exact names and prefixes. Nothing else is dropped.
_TRACKING_EXACT = {"gclid", "fbclid", "ref", "cmpid"}
_TRACKING_PREFIXES = ("utm_", "mc_", "at_")

# Redirect wrappers we unwrap ONLY when the destination is in the URL itself
# (no network call). Modern news.google.com / feedproxy / t.co links that hide
# the target behind an HTTP redirect are left as-is.
_WRAPPER_HOSTS = {"news.google.com", "feedproxy.google.com", "t.co"}
_TARGET_PARAMS = ("url", "u", "q")

_AMP_TAIL = re.compile(r"/amp/?$", re.IGNORECASE)
_MULTISLASH = re.compile(r"/{2,}")


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k in _TRACKING_EXACT or any(k.startswith(p) for p in _TRACKING_PREFIXES) or k == "amp"


def _unwrap(url: str, depth: int = 0) -> str:
    """If `url` is a known wrapper carrying its destination in a query param,
    return that destination (recursively). Otherwise return `url` unchanged."""
    if depth > 3:
        return url
    parts = urlsplit(url)
    host = _strip_www((parts.hostname or "").lower())
    if host in _WRAPPER_HOSTS:
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        for p in _TARGET_PARAMS:
            target = params.get(p)
            if target and target.lower().startswith(("http://", "https://")):
                return _unwrap(unquote(target), depth + 1)
    return url


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(_unwrap(url.strip()))

    scheme = (parts.scheme or "https").lower()
    host = _strip_www((parts.hostname or "").lower())
    netloc = f"{host}:{parts.port}" if parts.port else host

    path = _MULTISLASH.sub("/", parts.path or "")
    path = _AMP_TAIL.sub("", path)
    if len(path) > 1:
        path = path.rstrip("/")
    path = path or "/"

    # Keep every non-tracking param; sort for order-independence (same params in
    # any order -> same hash). Conservative: meaningful params (?id=, ?p=) survive.
    kept = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                  if not _is_tracking(k))
    query = urlencode(kept)

    return urlunsplit((scheme, netloc, path, query, ""))  # fragment dropped


def normalized_url_hash(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()
