# Curated Named Sources + Economy-First Preference — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fixed set of user-named news outlets as first-class curated registry sources (fetched in addition to GDELT search), with a mild, flag-gated ranking preference for economy-section and curated content.

**Architecture:** Two independently-landable units. **Unit A** (Tasks 1–3) adds the outlets to `config/sources.yaml` and hardens the HTTP fetchers (realistic User-Agent + a private-IP link rewrite) — this changes no pipeline criteria. **Unit B** (Tasks 4–7) derives two per-cluster booleans (`has_curated`, `has_economy_section`) from the `registry_id`/`section` already on each source, carries them into the ranking tuple, and applies small additive boosts in `ranking.make_score_of`, all behind `CURATED_PREFERENCE` (default on, reversible).

**Tech Stack:** Python 3, PostgreSQL (psycopg), pytest, PyYAML, feedparser, lxml, requests.

## Global Constraints

- Python target matches the existing repo; no new third-party dependencies — only stdlib (`ipaddress`, `urllib.parse`) plus libraries already imported (`feedparser`, `requests`, `lxml`, `yaml`, `psycopg`).
- Source registry is DATA: every new outlet is a `Source` entry in `config/sources.yaml`; the fetcher dispatches on `fetch_type ∈ {rss, sitemap, scrape, api, dead, unverified}`.
- `is_fetchable` semantics are unchanged: a source fetches only when `fetch_type ∉ {unverified, dead}` **and** `status != "needs_review"`.
- FIPS (not ISO) country codes: Mexico `MX`, Argentina `AR`, Bolivia `BL`, Ecuador `EC`. Active buckets: `middle_east`, `north_africa`, `latam`.
- Ranking boosts are small relative to `gate_score ∈ [0,1]` and must never rescue a market roundup above country-subject stories. `CURATED_PREFERENCE=false` reverts ranking to byte-identical prior behavior.
- TDD throughout: failing test → run-fail → minimal impl → run-pass → commit. Run all tests with `python -m pytest -q` before each commit; DB-gated tests need `TEST_DATABASE_URL` (skip cleanly otherwise).
- Do NOT commit on `main`. Create a feature branch first (Task 0).

---

## Task 0: Branch

**Files:** none (git only)

- [ ] **Step 1: Create the feature branch**

```bash
git checkout -b feat/curated-named-sources
```

- [ ] **Step 2: Confirm a clean baseline**

Run: `python -m pytest -q`
Expected: all tests pass (DB-gated tests may SKIP without `TEST_DATABASE_URL`).

---

## Task 1: Add `curated` + `section` fields to the Source registry

**Files:**
- Modify: `geoecon/registry.py` (the `Source` dataclass + `_coerce`)
- Test: `tests/unit/test_registry.py`

**Interfaces:**
- Produces: `Source.curated: bool` (default `False`) and `Source.section: str | None` (default `None`). `_coerce` defaults both; existing entries (which omit them) load unchanged. Valid `section` values are free-form but only `"economy"` is acted upon downstream.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_registry.py`:

```python
def test_curated_and_section_default_and_parse(tmp_path):
    text = """
sources:
  - id: plain
    institution: P
    tier: 2
    fetch_type: rss
    url: "https://p"
  - id: flagged
    institution: F
    tier: 2
    fetch_type: rss
    url: "https://f"
    curated: true
    section: economy
"""
    by_id = {s.id: s for s in registry.load_sources(_write(tmp_path, text))}
    # defaults when omitted
    assert by_id["plain"].curated is False
    assert by_id["plain"].section is None
    # parsed when present
    assert by_id["flagged"].curated is True
    assert by_id["flagged"].section == "economy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_registry.py::test_curated_and_section_default_and_parse -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'curated'` (or `AttributeError`).

- [ ] **Step 3: Add the fields to the dataclass**

In `geoecon/registry.py`, add two fields to `Source` (after `status`):

```python
    status: str | None = None         # live | needs_review | dead
    curated: bool = False             # user-named outlet: eligible for the ranking nudge
    section: str | None = None        # "economy" => economy-desk feed; else None/general
```

`_coerce` already filters to `_KNOWN_FIELDS` and passes through scalars, so `curated`/`section` are picked up automatically with their dataclass defaults. No `_coerce` change is required (the defaults handle omitted keys).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_registry.py::test_curated_and_section_default_and_parse -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add geoecon/registry.py tests/unit/test_registry.py
git commit -m "feat(registry): add curated + section fields to Source"
```

---

## Task 2: Fetcher User-Agent hardening + private-IP link rewrite

**Files:**
- Create: `geoecon/fetchers/http.py`
- Modify: `geoecon/fetchers/rss.py`, `geoecon/fetchers/scrape.py`, `geoecon/fetchers/sitemap.py`
- Test: `tests/unit/test_fetchers.py`

**Interfaces:**
- Produces: `geoecon.fetchers.http.BROWSER_UA: str` (a realistic Chrome UA). `RssFetcher.discover` calls the injected `parse(target, agent=BROWSER_UA)` and rewrites any item URL whose host is a bare IP literal to the source's public host.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_fetchers.py`:

```python
from geoecon.fetchers import http as fetch_http


def test_browser_ua_looks_like_a_browser():
    assert "Mozilla/5.0" in fetch_http.BROWSER_UA
    assert "Chrome" in fetch_http.BROWSER_UA


def test_rss_passes_browser_ua_to_parser():
    captured = {}
    def fake_parse(target, **kw):
        captured["target"] = target
        captured["agent"] = kw.get("agent")
        return _FakeParsed([{"link": "https://example.org/a", "title": "A"}])
    f = RssFetcher(parse=fake_parse)
    f.discover(_src(feed_url="https://example.org/feed"))
    assert captured["agent"] == fetch_http.BROWSER_UA


def test_rss_rewrites_private_ip_link_to_source_host():
    entries = [{"link": "https://10.0.137.12/economia/nota-1", "title": "A"},
               {"link": "https://larazon.bo/economia/nota-2", "title": "B"}]
    f = RssFetcher(parse=lambda target, **kw: _FakeParsed(entries))
    items = f.discover(_src(feed_url="https://larazon.bo/feed/",
                            url="https://larazon.bo/"))
    # private-IP host rewritten to the source's public host; path preserved
    assert items[0].url == "https://larazon.bo/economia/nota-1"
    # already-public URL untouched
    assert items[1].url == "https://larazon.bo/economia/nota-2"


def test_scrape_default_fetch_sends_browser_ua(monkeypatch):
    seen = {}
    class _Resp:
        text = "<html></html>"
        def raise_for_status(self): pass
    def fake_get(url, timeout=None, headers=None):
        seen["ua"] = (headers or {}).get("User-Agent"); return _Resp()
    monkeypatch.setattr("geoecon.fetchers.scrape.requests.get", fake_get)
    from geoecon.fetchers.scrape import _default_fetch
    _default_fetch("https://example.org")
    assert seen["ua"] == fetch_http.BROWSER_UA
```

Also update the two existing RSS test fakes so their lambdas accept the new keyword argument. Change:

```python
    f = RssFetcher(parse=lambda target: _FakeParsed(entries))
```
to
```python
    f = RssFetcher(parse=lambda target, **kw: _FakeParsed(entries))
```
(in `test_rss_parses_entries_and_ts`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_fetchers.py -v`
Expected: FAIL — `ModuleNotFoundError: geoecon.fetchers.http` / the new assertions error.

- [ ] **Step 3: Create the shared UA module**

Create `geoecon/fetchers/http.py`:

```python
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
```

- [ ] **Step 4: Update the rss fetcher (UA + private-IP rewrite)**

Replace `geoecon/fetchers/rss.py` with:

```python
"""RSS/Atom fetcher — parses a feed into DiscoveredItems via feedparser."""
from __future__ import annotations

import ipaddress
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import feedparser

from .base import DiscoveredItem
from .http import BROWSER_UA


def _entry_ts(entry) -> datetime | None:
    """Best-effort publish time from an entry's parsed time struct, else None."""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    try:
        return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _is_ip_host(host: str | None) -> bool:
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _rewrite_ip_host(url: str, source_url: str) -> str:
    """If `url`'s host is a bare IP literal (e.g. La Razón's internal 10.0.137.12),
    swap scheme+host for the source's public host, preserving path/query."""
    u = urlparse(url)
    if not _is_ip_host(u.hostname):
        return url
    s = urlparse(source_url)
    return urlunparse((s.scheme or u.scheme, s.netloc, u.path, u.params, u.query, u.fragment))


class RssFetcher:
    """Discovers items from `source.feed_url` (falling back to `source.url`).

    `parse` is injectable so tests can feed a canned feedparser result with no
    network — mirroring how GDELT discovery injects its fetch.
    """

    def __init__(self, *, parse=feedparser.parse):
        self._parse = parse

    def discover(self, source) -> list[DiscoveredItem]:
        target = source.feed_url or source.url
        parsed = self._parse(target, agent=BROWSER_UA)
        items: list[DiscoveredItem] = []
        for entry in getattr(parsed, "entries", []) or []:
            url = entry.get("link")
            if not url:
                continue
            url = _rewrite_ip_host(url, source.url)
            items.append(DiscoveredItem(
                url=url,
                title=entry.get("title"),
                ts=_entry_ts(entry),
                source_lang=source.lang,
            ))
        return items
```

- [ ] **Step 5: Update scrape + sitemap to use the shared UA**

In `geoecon/fetchers/scrape.py`, remove the local `_UA = ...` line and import the shared one. Change the top imports + `_default_fetch`:

```python
import requests
from lxml import html as lxml_html

from .base import DiscoveredItem
from .http import BROWSER_UA

log = logging.getLogger("geoecon.fetchers.scrape")


def _default_fetch(url: str) -> str | None:
    resp = requests.get(url, timeout=30, headers={"User-Agent": BROWSER_UA})
    resp.raise_for_status()
    return resp.text
```

In `geoecon/fetchers/sitemap.py`, do the same — remove `_UA = ...`, import `BROWSER_UA`, and update `_default_fetch`:

```python
from .base import DiscoveredItem
from .http import BROWSER_UA

log = logging.getLogger("geoecon.fetchers.sitemap")
_MAX_DEPTH = 2
_MAX_CHILDREN = 25  # cap child sitemaps fetched from an index (recent-first)


def _default_fetch(url: str) -> bytes | None:
    resp = requests.get(url, timeout=30, headers={"User-Agent": BROWSER_UA})
    resp.raise_for_status()
    return resp.content
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_fetchers.py -v`
Expected: PASS (all, including the pre-existing fetcher tests).

- [ ] **Step 7: Commit**

```bash
git add geoecon/fetchers/http.py geoecon/fetchers/rss.py geoecon/fetchers/scrape.py geoecon/fetchers/sitemap.py tests/unit/test_fetchers.py
git commit -m "feat(fetchers): realistic browser UA + private-IP link rewrite"
```

---

## Task 3: Register the named outlets in `config/sources.yaml`

**Files:**
- Modify: `config/sources.yaml` (append new Tier-2 entries; update existing `reg_aljazeera_ar` and `wire_reuters_world`)
- Test: `tests/unit/test_registry.py` (`test_real_registry_loads_and_validates`)

**Interfaces:**
- Consumes: `Source.curated`/`Source.section` from Task 1.
- Produces: ~17 new `curated: true` sources the discovery_registry stage will fetch (live) or skip (needs_review). Economy-section feeds carry `section: economy`.

- [ ] **Step 1: Verify the three unconfirmed feeds (one-shot)**

Run each and confirm it returns XML (HTTP 200, `<rss`/`<feed`/`<urlset`/`<sitemapindex>`):

```bash
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
for u in \
  "https://larazon.bo/economia/feed/" \
  "https://www.eluniverso.com/arc/outboundfeeds/rss/category/economia/?outputType=xml" \
  "https://www.milenio.com/sitemap-google-news-index.xml" ; do
  echo "== $u =="; curl -sS -A "$UA" -o /dev/null -w "%{http_code}\n" "$u"
  curl -sS -A "$UA" "$u" | head -c 200; echo
done
```

Record which return valid XML. The Milenio sitemap URL that returns a `<sitemapindex>` is the `feed_url` for `reg_milenio_mx` (if `sitemap-google-news-index.xml` 404s, try `https://www.milenio.com/sitemap-articles-index.xml`). For La Razón / El Universo economy feeds: if valid, add the two `*_econ` entries below; if not, omit them (the general feed already covers the outlet).

- [ ] **Step 2: Update the two existing entries (flag as curated)**

In `config/sources.yaml`, find `id: reg_aljazeera_ar` and add `curated: true` (it already covers Al Jazeera; keep it `live`). Find `id: wire_reuters_world` and set it parked + curated — change its `status` to `needs_review` (if not already) and add `curated: true`, appending to its `notes`: `Bot-gated (DataDome); needs headless. Parked until headless lands.`

- [ ] **Step 3: Append the new entries**

Append the following under the Tier-2 section of `config/sources.yaml` (after the existing `reg_*` regional entries). Omit `reg_larazon_bo_econ` and `reg_eluniverso_ec_econ` unless Step 1 verified them.

```yaml
  # --- Curated named outlets (added 2026-06-24) ---------------------------------
  - id: reg_aljazeera_en
    country: null
    country_fips: null
    bucket: middle_east
    institution: "Al Jazeera (English)"
    tier: 2
    origin_class: [macro, trade, energy, fiscal, monetary, sanctions, investment]
    fetch_type: rss
    url: "https://www.aljazeera.com/economy/"
    feed_url: https://www.aljazeera.com/xml/rss/all.xml
    probe_paths: []
    extract_hint: null
    lang: en
    ownership: state_linked
    state_alignment: QA
    reliability: high
    notes: "Qatari-funded — state_alignment=QA. Site-wide RSS (no economy-only feed); gate does precision. English desk."
    verified: "2026-06-24"
    status: live
    curated: true
    section: null

  - id: reg_arab_news_econ
    country: null
    country_fips: null
    bucket: middle_east
    institution: "Arab News (Business & Economy)"
    tier: 2
    origin_class: [macro, trade, energy, investment, monetary, fiscal]
    fetch_type: rss
    url: "https://www.arabnews.com/economy"
    feed_url: https://www.arabnews.com/cat/4/rss.xml
    probe_paths: []
    extract_hint: null
    lang: en
    ownership: state_linked
    state_alignment: SA
    reliability: high
    notes: "Saudi-owned — state_alignment=SA. Business & Economy category feed."
    verified: "2026-06-24"
    status: live
    curated: true
    section: economy

  - id: reg_arab_news
    country: null
    country_fips: null
    bucket: middle_east
    institution: "Arab News (general)"
    tier: 2
    origin_class: [macro, trade, energy, investment, monetary, fiscal]
    fetch_type: rss
    url: "https://www.arabnews.com/"
    feed_url: https://www.arabnews.com/rss.xml
    probe_paths: []
    extract_hint: null
    lang: en
    ownership: state_linked
    state_alignment: SA
    reliability: high
    notes: "General feed; politically-framed economy stories survive the gate."
    verified: "2026-06-24"
    status: live
    curated: true
    section: null

  - id: reg_milenio_mx
    country: MX
    country_fips: MX
    bucket: latam
    institution: "Milenio (Mexico)"
    tier: 2
    origin_class: [macro, monetary, fiscal, trade, investment, energy]
    fetch_type: sitemap
    url: "https://www.milenio.com/"
    feed_url: https://www.milenio.com/sitemap-google-news-index.xml
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: high
    notes: "No RSS; Google-News XML sitemap index (verify exact URL on add)."
    verified: "2026-06-24"
    status: live
    curated: true
    section: null

  - id: reg_lanacion_ar_econ
    country: AR
    country_fips: AR
    bucket: latam
    institution: "La Nación (Argentina — Economía)"
    tier: 2
    origin_class: [macro, monetary, fiscal, trade, investment]
    fetch_type: rss
    url: "https://www.lanacion.com.ar/economia/"
    feed_url: "https://www.lanacion.com.ar/arc/outboundfeeds/rss/category/economia/?outputType=xml"
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: high
    notes: "Arc CMS economy-section feed."
    verified: "2026-06-24"
    status: live
    curated: true
    section: economy

  - id: reg_lanacion_ar
    country: AR
    country_fips: AR
    bucket: latam
    institution: "La Nación (Argentina — general)"
    tier: 2
    origin_class: [macro, monetary, fiscal, trade, investment]
    fetch_type: rss
    url: "https://www.lanacion.com.ar/"
    feed_url: "https://www.lanacion.com.ar/arc/outboundfeeds/rss/?outputType=xml"
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: high
    notes: "General feed; gate filters to economy×geopolitics."
    verified: "2026-06-24"
    status: live
    curated: true
    section: null

  - id: reg_clarin_ar_econ
    country: AR
    country_fips: AR
    bucket: latam
    institution: "Clarín (Argentina — Economía)"
    tier: 2
    origin_class: [macro, monetary, fiscal, trade, investment]
    fetch_type: rss
    url: "https://www.clarin.com/economia/"
    feed_url: "https://www.clarin.com/rss/economia/"
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: high
    notes: "Economy-section feed. Needs browser UA (handled by fetcher)."
    verified: "2026-06-24"
    status: live
    curated: true
    section: economy

  - id: reg_clarin_ar
    country: AR
    country_fips: AR
    bucket: latam
    institution: "Clarín (Argentina — general)"
    tier: 2
    origin_class: [macro, monetary, fiscal, trade, investment]
    fetch_type: rss
    url: "https://www.clarin.com/"
    feed_url: "https://www.clarin.com/rss/lo-ultimo/"
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: high
    notes: "Latest-news feed. Needs browser UA."
    verified: "2026-06-24"
    status: live
    curated: true
    section: null

  - id: reg_laizquierda_ar
    country: AR
    country_fips: AR
    bucket: latam
    institution: "La Izquierda Diario (Argentina)"
    tier: 2
    origin_class: [macro, fiscal, trade]
    fetch_type: unverified
    url: "https://www.laizquierdadiario.com/"
    feed_url: null
    probe_paths: ["/?page=backend", "/sitemap_news.xml"]
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: degraded
    notes: "Cloudflare interactive JS challenge on every path; needs headless. Parked."
    verified: "2026-06-24"
    status: needs_review
    curated: true
    section: null

  - id: reg_larazon_bo
    country: BL
    country_fips: BL
    bucket: latam
    institution: "La Razón (Bolivia)"
    tier: 2
    origin_class: [macro, monetary, fiscal, energy, trade]
    fetch_type: rss
    url: "https://larazon.bo/"
    feed_url: https://larazon.bo/feed/
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: high
    notes: "WordPress feed. Needs browser UA; feed <link> uses an internal IP — fetcher rewrites to public host."
    verified: "2026-06-24"
    status: live
    curated: true
    section: null

  - id: reg_anfibia_ar
    country: AR
    country_fips: AR
    bucket: latam
    institution: "Revista Anfibia (Argentina)"
    tier: 2
    origin_class: [macro, fiscal, trade]
    fetch_type: rss
    url: "https://www.revistaanfibia.com/"
    feed_url: https://www.revistaanfibia.com/feed/
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: degraded
    notes: "Long-form magazine; low geoeconomic yield (accepted). WordPress feed."
    verified: "2026-06-24"
    status: live
    curated: true
    section: null

  - id: reg_vientosur
    country: null
    country_fips: null
    bucket: latam
    institution: "Viento Sur"
    tier: 2
    origin_class: [macro, fiscal, trade]
    fetch_type: rss
    url: "https://vientosur.info/"
    feed_url: https://vientosur.info/feed/
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: degraded
    notes: "Analysis outlet; low geoeconomic yield (accepted). WordPress feed."
    verified: "2026-06-24"
    status: live
    curated: true
    section: null

  - id: reg_elpais_america_econ
    country: null
    country_fips: null
    bucket: latam
    institution: "El País América (Economía)"
    tier: 2
    origin_class: [macro, monetary, fiscal, trade, investment, energy]
    fetch_type: rss
    url: "https://elpais.com/economia/"
    feed_url: "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/economia/portada"
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: high
    notes: "MRSS economy feed. Needs browser UA."
    verified: "2026-06-24"
    status: live
    curated: true
    section: economy

  - id: reg_elpais_america
    country: null
    country_fips: null
    bucket: latam
    institution: "El País América (portada)"
    tier: 2
    origin_class: [macro, monetary, fiscal, trade, investment]
    fetch_type: rss
    url: "https://elpais.com/america/"
    feed_url: "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/america/portada"
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: high
    notes: "America-edition front feed. Needs browser UA."
    verified: "2026-06-24"
    status: live
    curated: true
    section: null

  - id: reg_eluniverso_ec
    country: EC
    country_fips: EC
    bucket: latam
    institution: "El Universo (Ecuador)"
    tier: 2
    origin_class: [macro, monetary, fiscal, trade, energy, investment]
    fetch_type: rss
    url: "https://www.eluniverso.com/"
    feed_url: "https://www.eluniverso.com/arc/outboundfeeds/rss/?outputType=xml"
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: high
    notes: "Arc CMS general feed."
    verified: "2026-06-24"
    status: live
    curated: true
    section: null

  - id: reg_seul_ar
    country: AR
    country_fips: AR
    bucket: latam
    institution: "Seúl (Argentina)"
    tier: 2
    origin_class: [macro, trade, investment]
    fetch_type: rss
    url: "https://seul.ar/"
    feed_url: https://seul.ar/feed/
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: degraded
    notes: "Niche outlet; low geoeconomic yield (accepted). WordPress feed; needs browser UA."
    verified: "2026-06-24"
    status: live
    curated: true
    section: null

  - id: reg_lapoliticaonline_ar
    country: AR
    country_fips: AR
    bucket: latam
    institution: "La Política Online (Argentina — Economía)"
    tier: 2
    origin_class: [macro, monetary, fiscal, trade, investment]
    fetch_type: rss
    url: "https://www.lapoliticaonline.com/economia/"
    feed_url: "https://www.lapoliticaonline.com/files/rss/economia.xml"
    probe_paths: []
    extract_hint: null
    lang: es
    ownership: private
    state_alignment: null
    reliability: high
    notes: "Economy-section feed (the only feed registered for this outlet)."
    verified: "2026-06-24"
    status: live
    curated: true
    section: economy
```

If Step 1 verified them, also append `reg_larazon_bo_econ` (feed `https://larazon.bo/economia/feed/`, `section: economy`) and `reg_eluniverso_ec_econ` (feed `https://www.eluniverso.com/arc/outboundfeeds/rss/category/economia/?outputType=xml`, `section: economy`), copying the shape of `reg_lanacion_ar_econ` with the matching country/lang.

- [ ] **Step 4: Update the registry-count assertion**

Get the exact total:

```bash
grep -c '^  - id:' config/sources.yaml
```

In `tests/unit/test_registry.py`, set both numbers in `test_real_registry_loads_and_validates` to that integer:

```python
def test_real_registry_loads_and_validates():
    """The shipped config/sources.yaml must parse and validate as-is."""
    srcs = registry.load_sources(registry.default_path())
    assert len(srcs) == <COUNT_FROM_GREP>
    assert len({s.id for s in srcs}) == <COUNT_FROM_GREP>  # ids unique
    assert all(s.tier in registry.VALID_TIERS for s in srcs)
    assert all(s.fetch_type in registry.VALID_FETCH_TYPES for s in srcs)
```

Replace `<COUNT_FROM_GREP>` with the integer the grep printed (e.g. `65`).

- [ ] **Step 5: Run registry tests**

Run: `python -m pytest tests/unit/test_registry.py -v`
Expected: PASS. The real registry parses, ids are unique, and the count matches.

- [ ] **Step 6: Sanity-check fetchability of the new live sources**

Run:

```bash
python -c "from geoecon import registry; s=[x for x in registry.load_sources() if x.curated]; print('curated:',len(s)); print('fetchable:',sorted(x.id for x in s if x.is_fetchable)); print('parked:',sorted(x.id for x in s if not x.is_fetchable))"
```

Expected: `parked` contains `reg_laizquierda_ar` and `wire_reuters_world`; everything else curated is fetchable.

- [ ] **Step 7: Commit**

```bash
git add config/sources.yaml tests/unit/test_registry.py
git commit -m "feat(sources): register curated named MENA + LATAM outlets"
```

---

## Task 4: Cluster columns + migration + repo plumbing (Unit B)

**Files:**
- Create: `geoecon/state/migrations/007_curated_flags.sql`
- Modify: `schema.sql` (clusters table), `geoecon/state/repo.py` (`insert_cluster`, `records_for_buckets`)
- Test: `tests/unit/test_repo.py`

**Interfaces:**
- Produces: `clusters.has_curated BOOLEAN DEFAULT FALSE`, `clusters.has_economy_section BOOLEAN DEFAULT FALSE`. `repo.insert_cluster(..., has_curated=False, has_economy_section=False)`. `repo.records_for_buckets` returns 6-tuples: `(SynthesisRecord, country_fips, source_links, rank_score, has_curated, has_economy_section)`.
- Consumes: nothing from Unit A.

- [ ] **Step 1: Write the failing test (DB-gated)**

Add to `tests/unit/test_repo.py`:

```python
@requires_db
def test_records_for_buckets_carries_curated_flags(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    aid = repo.insert_article(rid, source_url="u", url_hash="h", domain="d",
                              region="MENA", gkg_themes=[], title="t")
    cid = repo.insert_cluster(rid, "fp1", "MENA", ["en"], 1,
                              has_curated=True, has_economy_section=True)
    repo.add_membership(cid, aid)
    repo.upsert_synthesis_record(rid, cid, {"headline_en": "A"}, "ok")
    recs = repo.records_for_buckets(rid)
    assert len(recs) == 1
    rec = recs[0]
    assert len(rec) == 6
    assert rec[4] is True   # has_curated
    assert rec[5] is True   # has_economy_section


@requires_db
def test_records_for_buckets_flags_default_false(repo):
    ws, we, sf = _win()
    rid = repo.create_run(ws, we, sf, "deepseek-v3.2", "task_a_cov")
    aid = repo.insert_article(rid, source_url="u", url_hash="h", domain="d",
                              region="MENA", gkg_themes=[], title="t")
    cid = repo.insert_cluster(rid, "fp1", "MENA", ["en"], 1)  # no flags passed
    repo.add_membership(cid, aid)
    repo.upsert_synthesis_record(rid, cid, {"headline_en": "A"}, "ok")
    rec = repo.records_for_buckets(rid)[0]
    assert rec[4] is False and rec[5] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TEST_DATABASE_URL=$TEST_DATABASE_URL python -m pytest tests/unit/test_repo.py::test_records_for_buckets_carries_curated_flags -v`
Expected: FAIL — `insert_cluster() got an unexpected keyword argument 'has_curated'` (or the tuple has length 4). If `TEST_DATABASE_URL` is unset, the test SKIPs — set it to a reachable Postgres to run Unit B's DB tests.

- [ ] **Step 3: Add the migration**

Create `geoecon/state/migrations/007_curated_flags.sql`:

```sql
-- Curated-source / economy-section flags on clusters (Unit B).
-- Additive + defaulted FALSE. Populated by the cluster stage from member
-- registry_ids resolved against the registry (curated / section=economy).
-- Consumed for ranking ONLY when Config.curated_preference is on.

ALTER TABLE clusters
    ADD COLUMN IF NOT EXISTS has_curated          BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS has_economy_section  BOOLEAN NOT NULL DEFAULT FALSE;
```

- [ ] **Step 4: Update `schema.sql`**

In `schema.sql`, inside `CREATE TABLE clusters (...)`, after the `corroboration_score` line, add:

```sql
    corroboration_score       REAL,                    -- Change 4: scored from the aggregates above
    has_curated               BOOLEAN NOT NULL DEFAULT FALSE,  -- any member from a curated named source
    has_economy_section       BOOLEAN NOT NULL DEFAULT FALSE,  -- any member from a section=economy feed
```

- [ ] **Step 5: Update `repo.insert_cluster`**

In `geoecon/state/repo.py`, change the signature and INSERT:

```python
    def insert_cluster(self, run_id, event_fingerprint, region, languages, article_count,
                       framing_divergent=False, country_fips=None, gate_score=None,
                       tier_min=None, has_primary=None, distinct_state_alignments=None,
                       source_count=None, corroboration_score=None,
                       has_curated=False, has_economy_section=False) -> int:
        return self.conn.execute(
            """INSERT INTO clusters(run_id, event_fingerprint, region, country_fips,
                 languages, article_count, framing_divergent, gate_score,
                 tier_min, has_primary, distinct_state_alignments, source_count,
                 corroboration_score, has_curated, has_economy_section)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING cluster_id""",
            (run_id, event_fingerprint, region, country_fips, languages, article_count,
             framing_divergent, gate_score, tier_min, has_primary, distinct_state_alignments,
             source_count, corroboration_score, has_curated, has_economy_section)).fetchone()[0]
```

- [ ] **Step 6: Update `repo.records_for_buckets`**

In the SQL SELECT, add the two columns at the end of the cluster columns. Change the SELECT list line that ends `..., c.corroboration_score` to:

```python
                 a.domain, c.corroboration_score, c.has_curated, c.has_economy_section
```

(They become `r[18]` and `r[19]`.) Update the per-record tuple construction and the type hint:

```python
    def records_for_buckets(self, run_id, rank_by_corroboration=False
                            ) -> list[tuple[SynthesisRecord, str | None, list[dict], float | None, bool, bool]]:
```

and the build line:

```python
                rank_score = r[17] if rank_by_corroboration else r[14]
                by_id[rec_id] = [rec, r[13], [], rank_score, bool(r[18]), bool(r[19])]
```

The `source_links` accumulation (`by_id[rec_id][2]`) is unchanged.

- [ ] **Step 7: Run tests to verify they pass**

Run: `TEST_DATABASE_URL=$TEST_DATABASE_URL python -m pytest tests/unit/test_repo.py -v`
Expected: PASS (the two new tests + all existing repo tests; the migration applies cleanly to the fresh per-test DB).

- [ ] **Step 8: Commit**

```bash
git add geoecon/state/migrations/007_curated_flags.sql schema.sql geoecon/state/repo.py tests/unit/test_repo.py
git commit -m "feat(repo): cluster curated/economy flags + carry into rank tuple"
```

---

## Task 5: Populate the cluster flags in the cluster stage

**Files:**
- Modify: `geoecon/stages/cluster.py` (`run`)
- Test: `tests/unit/test_cluster.py` (extend `FakeRepo`, add a test)

**Interfaces:**
- Consumes: `Source.curated`/`Source.section` (Task 1); `repo.insert_cluster(..., has_curated, has_economy_section)` (Task 4).
- Produces: `cluster.run(cfg, repo, run_id, client, *, sources=None)` — when `sources` is None it loads `registry.load_sources(getattr(cfg, "sources_path", None))`. Computes per-cluster `has_curated` / `has_economy_section` from member `registry_id`s.

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_cluster.py`, first extend `FakeRepo.insert_cluster` to accept the new kwargs and record them:

```python
    def insert_cluster(self, run_id, fp, region, langs, count,
                       framing_divergent=False, country_fips=None, gate_score=None,
                       tier_min=None, has_primary=None, distinct_state_alignments=None,
                       source_count=None, corroboration_score=None,
                       has_curated=False, has_economy_section=False):
        cid = len(self.clusters) + 1
        self.clusters.append(dict(cid=cid, count=count, country_fips=country_fips,
                                  gate_score=gate_score, tier_min=tier_min,
                                  has_primary=has_primary,
                                  distinct_state_alignments=distinct_state_alignments,
                                  source_count=source_count,
                                  corroboration_score=corroboration_score,
                                  has_curated=has_curated,
                                  has_economy_section=has_economy_section)); return cid
```

Extend the `_art` helper to accept a `registry_id`:

```python
def _art(aid, title, country="SA", geo=True, score=0.5, body="body",
         source_tier=None, state_alignment=None, registry_id=None):
    return Article(aid, f"http://x/{aid}", f"h{aid}", "d", "MENA", [], None, None,
                   "en", title, body, "extracted", country,
                   gate_geoeconomic=geo, gate_score=score,
                   source_tier=source_tier, state_alignment=state_alignment,
                   registry_id=registry_id)
```

Then add the test:

```python
def test_run_sets_curated_and_economy_flags():
    from geoecon.registry import Source
    sources = [
        Source(id="reg_named_econ", institution="N", tier=2, fetch_type="rss",
               url="https://n", curated=True, section="economy"),
        Source(id="reg_named_gen", institution="G", tier=2, fetch_type="rss",
               url="https://g", curated=True, section=None),
        Source(id="reg_plain", institution="P", tier=2, fetch_type="rss",
               url="https://p", curated=False, section=None),
    ]
    # SA cluster: one curated-economy member -> both flags true (one event)
    sa = [_art(1, "Saudi budget", country="SA", score=0.9, registry_id="reg_named_econ"),
          _art(2, "Saudi budget detail", country="SA", score=0.5, registry_id="reg_plain")]
    # EG cluster: a curated general member -> has_curated true, economy false
    eg = [_art(3, "Egypt pound", country="EG", score=0.6, registry_id="reg_named_gen")]
    repo = FakeRepo(sa + eg)
    llm = _LLM(['{"groups": [[0, 1]]}'])   # SA grouped; EG singleton
    cluster_stage.run(_CFG, repo, 1, llm, sources=sources)
    sac = [c for c in repo.clusters if c["count"] == 2][0]
    egc = [c for c in repo.clusters if c["count"] == 1][0]
    assert sac["has_curated"] is True and sac["has_economy_section"] is True
    assert egc["has_curated"] is True and egc["has_economy_section"] is False


def test_run_flags_false_for_gdelt_only_cluster():
    arts = [_art(1, "Egypt pound", country="EG", score=0.5)]  # registry_id=None
    repo = FakeRepo(arts)
    cluster_stage.run(_CFG, repo, 1, _LLM([]), sources=[])
    c = repo.clusters[0]
    assert c["has_curated"] is False and c["has_economy_section"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_cluster.py::test_run_sets_curated_and_economy_flags -v`
Expected: FAIL — `run()` does not accept `sources`, or the flags are not in the recorded cluster dict.

- [ ] **Step 3: Implement in `cluster.py`**

Add `from .. import registry` to the imports at the top:

```python
from .. import corroboration, regions, registry
```

Change `run`'s signature and build the lookup sets at the top of the function (right after the `arts = [...]` filter and the empty-guard):

```python
def run(cfg, repo, run_id: int, client, *, sources=None) -> dict:
    arts = [a for a in repo.articles_for_run(run_id)
            if a.gate_geoeconomic and a.fetch_status != "failed"
            and regions.buckets_for_country(a.country_fips, cfg.buckets)]
    if not arts:
        log.info("cluster: 0 clusters from 0 gated articles")
        return {"clusters": 0, "max_cluster_size": 0}

    if sources is None:
        sources = registry.load_sources(getattr(cfg, "sources_path", None))
    curated_ids = {s.id for s in sources if s.curated}
    economy_ids = {s.id for s in sources if s.section == "economy"}
```

Then, inside the `for member_arts in groups:` loop, alongside the existing aggregates, compute the two booleans and pass them to `insert_cluster`:

```python
        has_curated = any(a.registry_id in curated_ids for a in member_arts)
        has_economy_section = any(a.registry_id in economy_ids for a in member_arts)
        cid = repo.insert_cluster(
            run_id, fp, primary.region, langs, len(member_arts),
            country_fips=primary.country_fips, gate_score=primary.gate_score,
            tier_min=tier_min, has_primary=has_primary,
            distinct_state_alignments=distinct_alignments, source_count=len(member_arts),
            corroboration_score=corroboration.score(
                tier_min=tier_min, has_primary=has_primary,
                distinct_state_alignments=distinct_alignments, source_count=len(member_arts)),
            has_curated=has_curated, has_economy_section=has_economy_section,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_cluster.py -v`
Expected: PASS (new tests + all existing cluster tests; existing tests call `run(...)` without `sources`, which loads the real registry harmlessly — their articles carry `registry_id=None`, so both flags stay False and no existing assertion changes).

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/cluster.py tests/unit/test_cluster.py
git commit -m "feat(cluster): set has_curated/has_economy_section from member sources"
```

---

## Task 6: Config knobs for the ranking nudge

**Files:**
- Modify: `geoecon/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `Config.curated_preference: bool = True` (`CURATED_PREFERENCE`), `Config.curated_boost: float = 0.15` (`CURATED_BOOST`), `Config.economy_section_boost: float = 0.10` (`ECONOMY_SECTION_BOOST`).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_config.py`:

```python
def test_curated_preference_defaults():
    cfg = Config.from_env(dict(_ENV))
    assert cfg.curated_preference is True
    assert cfg.curated_boost == 0.15
    assert cfg.economy_section_boost == 0.10


def test_curated_preference_env_overrides():
    cfg = Config.from_env({**_ENV, "CURATED_PREFERENCE": "false",
                           "CURATED_BOOST": "0.3", "ECONOMY_SECTION_BOOST": "0.2"})
    assert cfg.curated_preference is False
    assert cfg.curated_boost == 0.3
    assert cfg.economy_section_boost == 0.2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_config.py::test_curated_preference_defaults -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'curated_preference'`.

- [ ] **Step 3: Add the fields + env loading**

In `geoecon/config.py`, add three fields to the dataclass (after `market_demote`):

```python
    market_demote: bool = True    # MARKET_DEMOTE; rank US/global market roundups last
    # Curated-source ranking nudge (Unit B). Reversible via env; master flag default ON.
    curated_preference: bool = True       # CURATED_PREFERENCE
    curated_boost: float = 0.15           # CURATED_BOOST
    economy_section_boost: float = 0.10   # ECONOMY_SECTION_BOOST
    limit: int | None = None
```

In `from_env`, add (after the `market_demote=...` line):

```python
            market_demote=env.get("MARKET_DEMOTE", "true").lower() == "true",
            curated_preference=env.get("CURATED_PREFERENCE", "true").lower() == "true",
            curated_boost=float(env.get("CURATED_BOOST", "0.15")),
            economy_section_boost=float(env.get("ECONOMY_SECTION_BOOST", "0.10")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add geoecon/config.py tests/unit/test_config.py
git commit -m "feat(config): curated_preference + boost knobs (default on, reversible)"
```

---

## Task 7: Apply the boosts in ranking

**Files:**
- Modify: `geoecon/ranking.py` (`make_score_of`)
- Test: `tests/unit/test_ranking.py`

**Interfaces:**
- Consumes: the 6-tuple shape from `records_for_buckets` (Task 4); `Config.curated_preference`/`curated_boost`/`economy_section_boost` (Task 6).
- Produces: `make_score_of` adds `economy_section_boost` when `t[5]` is truthy and `curated_boost` when `t[4]` is truthy, only if `cfg.curated_preference`. Market demote still applies first (returns before boosts). Tuples shorter than 6 elements are treated as not-curated/not-economy (backward compatible).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_ranking.py`. Extend the local `_Cfg` and add tests:

```python
class _CfgB:
    market_demote = True
    curated_preference = True
    curated_boost = 0.15
    economy_section_boost = 0.10


def _t(headline, score, *, curated=False, economy=False):
    return (_rec(headline), "IR", [], score, curated, economy)


def test_economy_section_outranks_equal_general():
    score_of = ranking.make_score_of(_CfgB())
    econ = _t("Iran budget passed", 0.5, economy=True, curated=True)
    gen = _t("Iran politics shift", 0.5, curated=True)
    assert score_of(econ) > score_of(gen)


def test_curated_outranks_equal_noncurated():
    score_of = ranking.make_score_of(_CfgB())
    cur = _t("Iran trade deal", 0.5, curated=True)
    non = _t("Iran trade deal", 0.5)
    assert score_of(cur) > score_of(non)


def test_curated_market_roundup_still_demoted():
    score_of = ranking.make_score_of(_CfgB())
    market = _t("S&P 500 slips", 0.9, curated=True, economy=True)
    country = _t("Iran oil license", 0.1)
    assert score_of(market) < score_of(country)


def test_curated_preference_off_is_byte_identical():
    class Off:
        market_demote = True
        curated_preference = False
        curated_boost = 0.15
        economy_section_boost = 0.10
    score_of = ranking.make_score_of(Off())
    assert score_of(_t("Iran trade deal", 0.5, curated=True, economy=True)) == 0.5


def test_short_tuple_is_backward_compatible():
    # the existing 4-tuple shape must not raise
    score_of = ranking.make_score_of(_CfgB())
    assert score_of((_rec("Iran trade deal"), "IR", [], 0.5)) == 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_ranking.py -v`
Expected: FAIL — boosts not applied (e.g. `score_of(econ) == score_of(gen)`).

- [ ] **Step 3: Implement the boosts**

Replace `make_score_of` in `geoecon/ranking.py`:

```python
def make_score_of(cfg) -> Callable[[tuple], float]:
    """Return a score_of(record_tuple) for regions.top_per_bucket.

    record_tuple is (SynthesisRecord, country_fips, source_links, rank_score,
    has_curated, has_economy_section). When cfg.market_demote is on, flagged market
    roundups get a large penalty so they sort below every country-subject story
    (and are NOT rescued by the curated/economy nudge). When cfg.curated_preference
    is on, economy-section clusters get a small boost and curated clusters a small
    boost — mild reordering of ties, never dominating gate_score. Tuples shorter
    than 6 elements are treated as not-curated/not-economy (backward compatible).
    """
    demote = getattr(cfg, "market_demote", True)
    prefer = getattr(cfg, "curated_preference", False)
    curated_boost = getattr(cfg, "curated_boost", 0.0)
    economy_boost = getattr(cfg, "economy_section_boost", 0.0)

    def score_of(t) -> float:
        base = t[3] or 0.0
        if demote and is_market_roundup(t[0]):
            return base - _MARKET_PENALTY
        if prefer:
            if len(t) > 5 and t[5]:
                base += economy_boost
            if len(t) > 4 and t[4]:
                base += curated_boost
        return base

    return score_of
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_ranking.py -v`
Expected: PASS (new tests + all pre-existing ranking tests, which use a `_Cfg` stub without `curated_preference`, so `prefer` defaults False and their scores are unchanged).

- [ ] **Step 5: Commit**

```bash
git add geoecon/ranking.py tests/unit/test_ranking.py
git commit -m "feat(ranking): economy-first + curated nudge (flag-gated)"
```

---

## Task 8: Full suite + dry-run validation

**Files:** none (validation only)

- [ ] **Step 1: Run the full unit suite**

Run: `python -m pytest -q`
Expected: all pass (DB-gated tests pass with `TEST_DATABASE_URL`, otherwise SKIP).

- [ ] **Step 2: Registry sanity + load check**

Run:

```bash
python -c "from geoecon import registry; rs=registry.load_sources(); cur=[s for s in rs if s.curated]; print('total',len(rs),'curated',len(cur),'economy',sum(s.section=='economy' for s in cur))"
```

Expected: counts match what Task 3 added (no exception).

- [ ] **Step 3: Live dry-run (if a DB + network are available)**

Run: `python -m geoecon run --dry-run --limit 60`
Expected: the registry discovery stage logs the new `reg_*` sources; UA-gated outlets (Clarín, La Razón, El País, Seúl) return items; `reg_laizquierda_ar` and `wire_reuters_world` are skipped as `needs_review`. No send occurs (dry-run).

- [ ] **Step 4: Final review note**

Confirm `CURATED_PREFERENCE` behavior is the only ranking change: with `CURATED_PREFERENCE=false` the brief ordering matches pre-change behavior. Decide (with the user) whether to deploy with the flag on or off initially, and set `deploy/cronjob.yaml` env accordingly when bumping the image.

---

## Self-Review (completed by plan author)

- **Spec coverage:** §5 Unit A → Tasks 1–3; Unit B → Tasks 4–7; §A2/A3 fetcher hardening → Task 2; §B4 config → Task 6; §9 rollout/dry-run → Task 8. All spec sections map to a task.
- **Placeholder scan:** The only intentional fill-in is `<COUNT_FROM_GREP>` in Task 3 Step 4, which is a deterministic value produced by the exact command given in the same step.
- **Type consistency:** `insert_cluster(..., has_curated, has_economy_section)` (Task 4) matches the `FakeRepo` and call site in Task 5; the 6-tuple `(rec, fips, links, score, has_curated, has_economy_section)` produced in Task 4 is consumed by indices `t[4]`/`t[5]` in Task 7; `Source.curated`/`Source.section` defined in Task 1 are read in Tasks 3 and 5; `curated_preference`/`curated_boost`/`economy_section_boost` defined in Task 6 are read in Task 7.
