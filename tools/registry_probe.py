#!/usr/bin/env python3
"""Task 0 prober — resolve config/sources.yaml fetch mechanics live (read-only).

For each source: respect robots.txt, fetch the origin, hunt for an RSS/Atom feed
(<link alternate> + probe_paths + common defaults, validated through feedparser),
and otherwise inspect the served HTML. Writes NOTHING to the registry — emits a
results JSON that tools/registry_apply.py writes back.

Conservative by design: only a confirmed DNS failure is `dead`. Stale-path 404s,
TLS/connection timeouts, and robots blocks from THIS environment are
`needs_review` (verify from production), never `dead`.

Usage:
    python tools/registry_probe.py --out tools/registry_probe_results.json --runs 2
RSS feeds don't vanish, so --runs>1 merges positive feed findings across flaky
passes (monotonic: a pass that flaked never demotes a feed found earlier).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import re

import feedparser
import requests
from lxml import etree
from lxml import html as lxml_html

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from geoecon import registry  # noqa: E402

UA = "geoecon-registry-resolver/1.0 (+research; respects robots.txt)"
HEADERS = {"User-Agent": UA, "Accept": "*/*"}
COMMON = ["/rss", "/feed", "/feed/", "/rss.xml", "/index.xml", "/atom.xml", "/sitemap.xml"]
TIMEOUT = 10

_robots: dict[str, RobotFileParser] = {}
_DNS_MARKERS = ("name or service not known", "nodename nor servname",
                "failed to resolve", "name resolution", "no address associated")


def origin_of(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def robots_ok(url: str) -> bool:
    o = origin_of(url)
    rp = _robots.get(o)
    if rp is None:
        rp = RobotFileParser()
        try:
            r = requests.get(urljoin(o, "/robots.txt"), timeout=TIMEOUT, headers=HEADERS)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])  # unreachable robots => allow (standard)
        _robots[o] = rp
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


def get(url: str):
    if not robots_ok(url):
        return None, "robots_disallow"
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=HEADERS, allow_redirects=True)
        return r, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:300]


def is_dns_failure(err: str) -> bool:
    e = (err or "").lower()
    return any(m in e for m in _DNS_MARKERS)


def is_valid_feed(resp) -> bool:
    ct = (resp.headers.get("content-type") or "").lower()
    body = resp.text or ""
    if "html" in ct and "<rss" not in body[:2000].lower() and "<feed" not in body[:2000].lower():
        return False
    d = feedparser.parse(body)
    return bool(getattr(d, "version", "")) and len(d.entries) > 0


def find_feed(src) -> str | None:
    o = origin_of(src.url)
    candidates: list[str] = []
    if src.feed_url:
        candidates.append(src.feed_url)
    candidates += [urljoin(o + "/", p.lstrip("/")) for p in (src.probe_paths or [])]
    candidates += [urljoin(o, p) for p in COMMON]

    origin_resp, _ = get(src.url)
    if origin_resp is not None and origin_resp.status_code == 200 and "html" in \
            (origin_resp.headers.get("content-type") or "").lower():
        try:
            doc = lxml_html.fromstring(origin_resp.text)
            for ln in doc.cssselect('link[rel="alternate"]'):
                t = (ln.get("type") or "").lower()
                if "rss" in t or "atom" in t or "xml" in t:
                    href = ln.get("href")
                    if href:
                        candidates.insert(0, urljoin(src.url, href))
        except Exception:
            pass

    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        resp, _ = get(c)
        if resp is None or resp.status_code != 200:
            continue
        try:
            if is_valid_feed(resp):
                return resp.url
        except Exception:
            continue
    return None


_NEWS = ("news", "press", "article", "noticia", "comunicado", "media", "release",
         "duyuru", "haber", "akhbar", "basin", "insight", "publication")


def _xml_locs_lastmods(content: bytes):
    """Return (locs, n_with_lastmod) for a <urlset>, or (None, 0) / ('INDEX', children)."""
    try:
        root = etree.fromstring(content)
    except Exception:
        return None, 0
    tag = etree.QName(root).localname.lower()
    if tag == "sitemapindex":
        kids = [e.text for e in root.iter() if etree.QName(e).localname == "loc" and e.text]
        return "INDEX", kids
    if tag != "urlset":
        return None, 0
    locs, n_lastmod = [], 0
    for ue in root:
        if etree.QName(ue).localname.lower() != "url":
            continue
        loc = lastmod = None
        for ch in ue:
            ln = etree.QName(ch).localname.lower()
            if ln == "loc":
                loc = ch.text
            elif ln == "lastmod":
                lastmod = ch.text
        if loc:
            locs.append(loc)
            if lastmod:
                n_lastmod += 1
    return locs, n_lastmod


# Reject non-article sitemaps outright (their URLs pass no news filter anyway, but
# their filename is a clear signal and they shouldn't even be followed).
_BAD_SITEMAP = ("video", "image", "img", "product", "category", "tag-")


def _sitemap_candidates(origin: str) -> list[str]:
    cands = [urljoin(origin, p) for p in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")]
    rob, _ = get(urljoin(origin, "/robots.txt"))
    if rob is not None and rob.status_code == 200:
        for m in re.findall(r"(?i)^\s*sitemap:\s*(\S+)", rob.text, re.M):
            cands.insert(0, m.strip())
    seen, out = set(), []
    for c in cands:
        if c in seen or any(b in c.lower() for b in _BAD_SITEMAP):
            continue
        seen.add(c); out.append(c)
    return out


def find_sitemap(src) -> str | None:
    """A sitemap is usable only if windowable (>=50% of <url> have <lastmod>) AND it
    is genuinely article-bearing (news-like locs are >=25% AND >=5). The fraction
    gate rejects navigation/product sitemaps (mostly non-article URLs) that a bare
    count would wave through; otherwise discovery floods with site structure."""
    for cand in _sitemap_candidates(origin_of(src.url)):
        resp, _ = get(cand)
        if resp is None or resp.status_code != 200:
            continue
        body = resp.content or b""
        if b"<urlset" not in body[:600] and b"<sitemapindex" not in body[:600]:
            continue
        locs, n_lastmod = _xml_locs_lastmods(body)
        if locs == "INDEX":
            child = next((u for u in n_lastmod
                          if any(k in u.lower() for k in _NEWS)
                          and not any(b in u.lower() for b in _BAD_SITEMAP)), None)
            if not child:
                continue
            rc, _ = get(child)
            if rc is None or rc.status_code != 200:
                continue
            locs, n_lastmod = _xml_locs_lastmods(rc.content or b"")
            if not isinstance(locs, list):
                continue
        if not locs:
            continue
        news = sum(1 for u in locs if any(k in u.lower() for k in _NEWS))
        if n_lastmod / len(locs) >= 0.5 and news >= 5 and news / len(locs) >= 0.25:
            return cand
    return None


def looks_js_shell(html: str) -> bool:
    low = html.lower()
    markers = ("window.__", "ng-app", "react", "__next", "__nuxt", "vue", "spa",
               "requirejs", "system.import", "sharepoint", "_sppagecontextinfo")
    return html.count("<a ") < 8 or any(m in low for m in markers)


def article_links(html: str, base: str) -> int:
    try:
        doc = lxml_html.fromstring(html)
        doc.make_links_absolute(base, resolve_base_href=True)
    except Exception:
        return 0
    base_host = urlsplit(base).netloc
    n = 0
    for a in doc.cssselect("a[href]"):
        href = a.get("href") or ""
        if urlsplit(href).netloc != base_host:
            continue
        if any(seg.isdigit() for seg in href.split("/")) or \
                any(k in href.lower() for k in ("news", "press", "article", "post", "media", "release")):
            if len((a.text_content() or "").split()) >= 3:
                n += 1
    return n


def classify(src) -> dict:
    out = {"id": src.id, "tier": src.tier, "country_fips": src.country_fips,
           "fetch_type": None, "feed_url": None, "extract_hint": None,
           "status": None, "clear_probe_paths": False, "note": ""}

    feed = find_feed(src)
    if feed:
        root = urlsplit(feed).path.strip("/").count("/") == 0
        note = "RSS/Atom feed found and validated (>=1 entry)."
        if root:
            note += " Portal-root feed (broad) — refine to a section feed later; gate does precision."
        out.update(fetch_type="rss", feed_url=feed, status="live", clear_probe_paths=True, note=note)
        return out

    sm = find_sitemap(src)
    if sm:
        out.update(fetch_type="sitemap", feed_url=sm, status="live", clear_probe_paths=True,
                   note="XML sitemap with <lastmod> found and windowable; article URLs discovered "
                        "(titles via extraction).")
        return out

    resp, err = get(src.url)
    if resp is None:
        if err == "robots_disallow":
            out.update(fetch_type="scrape", status="needs_review",
                       note="robots.txt disallows our UA — scraping not permitted; feed-of-links/manual only (ToS).")
        elif is_dns_failure(err):
            out.update(fetch_type="dead", status="dead", note=f"DNS resolution failed (domain defunct): {err}.")
        else:
            out.update(fetch_type="scrape", status="needs_review",
                       note=f"unreachable from resolver env ({err}); verify from production network.")
        return out
    code = resp.status_code
    if code in (404, 410):
        out.update(fetch_type="scrape", status="needs_review",
                   note=f"origin {code} — registry URL likely stale; needs updated news-section path.")
        return out
    if code in (401, 403):
        out.update(fetch_type="scrape", status="needs_review",
                   note=f"origin {code} to bots; no feed. Needs authenticated/headless review.")
        return out
    if code >= 500:
        out.update(fetch_type="scrape", status="needs_review",
                   note=f"origin {code} (server error/transient); no feed found.")
        return out

    html = resp.text or ""
    nlinks = article_links(html, resp.url)
    if looks_js_shell(html) or nlinks < 5:
        out.update(fetch_type="scrape", status="needs_review",
                   note=f"no feed; served HTML JS-rendered/thin (article-links~{nlinks}); selector needs headless capture.")
    else:
        out.update(fetch_type="scrape", status="needs_review",
                   note=f"no feed; served HTML has ~{nlinks} article links; CSS selector needs manual capture.")
    return out


def probe_all(sources, workers: int) -> list[dict]:
    results = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(classify, s): s for s in sources}
        for fut in cf.as_completed(futs):
            s = futs[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"id": s.id, "tier": s.tier, "country_fips": s.country_fips,
                                "fetch_type": "scrape", "status": "needs_review", "feed_url": None,
                                "extract_hint": None, "clear_probe_paths": False,
                                "note": f"probe crashed: {e!r}"})
    return results


def merge(prev: list[dict], cur: list[dict]) -> list[dict]:
    """Feeds/sitemaps don't vanish: keep an earlier confirmed live result (rss or
    sitemap) if this pass flaked to needs_review."""
    live = {"rss", "sitemap"}
    by_id = {r["id"]: r for r in prev}
    for r in cur:
        p = by_id.get(r["id"])
        if r["fetch_type"] not in live and p and p.get("fetch_type") in live:
            note = p["note"]
            if "prior probe" not in note:
                note += " (confirmed in a prior probe; flaked this run)"
            r.update(fetch_type=p["fetch_type"], feed_url=p["feed_url"], status="live",
                     clear_probe_paths=True, note=note)
    return cur


def main():
    ap = argparse.ArgumentParser(description="Probe registry fetch mechanics (read-only).")
    ap.add_argument("--out", default=str(REPO / "tools" / "registry_probe_results.json"))
    ap.add_argument("--runs", type=int, default=1, help="passes to merge (RSS findings accumulate)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only", default="", help="comma-separated source ids to probe")
    args = ap.parse_args()

    sources = registry.load_sources(registry.default_path())
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        sources = [s for s in sources if s.id in wanted]

    results: list[dict] = []
    if os.path.exists(args.out):
        results = json.load(open(args.out))
    for i in range(args.runs):
        cur = probe_all(sources, args.workers)
        results = merge(results, cur) if results else cur
        ft = Counter(r["fetch_type"] for r in results)
        print(f"pass {i+1}/{args.runs}: {dict(ft)}")

    results.sort(key=lambda r: (r["tier"], r["id"]))
    json.dump(results, open(args.out, "w"), indent=2)
    print(f"wrote {args.out}  ({len(results)} sources)")


if __name__ == "__main__":
    main()
