"""Phase-0 probe for the discovery redesign (spec 2026-07-04).

Runs Family-1 (economy core) and Family-2 (geopolitics x economy) queries for
10 representative countries against:
  A) TheNewsAPI /v1/news/all  (free tier: 100 req/day, 3 articles/req, but
     meta.found gives the TOTAL match count -- that's the volume signal)
  B) Google News RSS search feeds (free; up to ~100 items/feed -> diversity
     + freshness signal)

Go/no-go evidence for TheNewsAPI Basic ($19/mo) as the Layer-1 backbone.
Throwaway tool; prints a report and writes probe JSON next to it.

Usage: .venv/bin/python tools/search_probe.py [outdir]
Reads THENEWSAPI_KEY from .env at repo root (or environment).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parents[1]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------------------------------------------------------------- keywords
# Short probe versions of the two families (full vocabulary lives in the
# future config/keywords.yaml). TheNewsAPI: '|' = OR. Google News: 'OR'.
FAM1 = {  # economy core
    "ar": ['اقتصاد', 'تضخم', '"البنك المركزي"', 'استثمار'],
    "tr": ['ekonomi', 'enflasyon', '"merkez bankası"', 'faiz'],
    "es": ['economía', 'inflación', '"banco central"', 'deuda'],
    "pt": ['economia', 'inflação', '"banco central"', 'juros'],
    "en": ['economy', 'inflation', '"central bank"', 'investment'],
}
FAM2 = {  # geopolitics x economy intersection
    "ar": ['عقوبات', '"رسوم جمركية"', 'أوبك', '"صندوق النقد"'],
    "tr": ['yaptırım', 'gümrük', 'OPEC', 'IMF'],
    "es": ['sanciones', 'aranceles', '"guerra comercial"', 'FMI'],
    "pt": ['sanções', 'tarifas', '"guerra comercial"', 'FMI'],
    "en": ['sanctions', 'tariffs', 'OPEC', 'IMF'],
}

# country: (thenewsapi locale, language, google hl, google gl, google ceid)
COUNTRIES = {
    "EG": ("eg", "ar", "ar", "EG", "EG:ar"),
    "SA": ("sa", "ar", "ar", "SA", "SA:ar"),
    "AE": ("ae", "ar", "ar", "AE", "AE:ar"),
    "TR": ("tr", "tr", "tr", "TR", "TR:tr"),
    "IQ": ("iq", "ar", "ar", "IQ", "IQ:ar"),
    "BR": ("br", "pt", "pt-BR", "BR", "BR:pt-419"),
    "MX": ("mx", "es", "es-419", "MX", "MX:es-419"),
    "AR": ("ar", "es", "es-419", "AR", "AR:es-419"),
    "CL": ("cl", "es", "es-419", "CL", "CL:es-419"),
    "CO": ("co", "es", "es-419", "CO", "CO:es-419"),
}

WINDOW_H = 31  # "all of yesterday + today so far" worst case


def load_key() -> str:
    if os.environ.get("THENEWSAPI_KEY"):
        return os.environ["THENEWSAPI_KEY"]
    envf = ROOT / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if line.startswith("THENEWSAPI_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("THENEWSAPI_KEY not found in env or .env")


def age_hours(ts: datetime, now: datetime) -> float:
    return (now - ts) / timedelta(hours=1)


def probe_thenewsapi(key: str, now: datetime) -> list[dict]:
    out = []
    after = (now - timedelta(hours=WINDOW_H)).strftime("%Y-%m-%dT%H:%M:%S")
    for cc, (locale, lang, *_rest) in COUNTRIES.items():
        for fam, vocab in (("F1", FAM1), ("F2", FAM2)):
            q = " | ".join(vocab[lang])
            params = {
                "api_token": key,
                "search": q,
                "language": lang,
                "locale": locale,
                "published_after": after,
                "sort": "published_at",
            }
            row = {"layer": "thenewsapi", "country": cc, "family": fam,
                   "lang": lang, "query": q}
            try:
                r = requests.get("https://api.thenewsapi.com/v1/news/all",
                                 params=params, timeout=30,
                                 headers={"User-Agent": UA})
                row["http"] = r.status_code
                data = r.json() if r.ok else {}
                meta = data.get("meta", {})
                arts = data.get("data", [])
                row["found"] = meta.get("found")
                row["returned"] = len(arts)
                row["domains"] = sorted({urllib.parse.urlparse(a.get("url", "")).netloc
                                         for a in arts if a.get("url")})
                ages = []
                for a in arts:
                    try:
                        ts = datetime.fromisoformat(
                            a["published_at"].replace("Z", "+00:00"))
                        ages.append(round(age_hours(ts, now), 1))
                    except (KeyError, ValueError):
                        pass
                row["ages_h"] = ages
                row["sample_titles"] = [a.get("title", "")[:90] for a in arts]
                if not r.ok:
                    row["error"] = r.text[:200]
            except Exception as e:  # probe: record and continue
                row["error"] = repr(e)
            out.append(row)
            print(f"  thenewsapi {cc} {fam}: found={row.get('found')} "
                  f"err={row.get('error', '')[:80]}", flush=True)
            time.sleep(1.2)
    return out


def probe_googlenews(now: datetime) -> list[dict]:
    out = []
    for cc, (_loc, lang, hl, gl, ceid) in COUNTRIES.items():
        for fam, vocab in (("F1", FAM1), ("F2", FAM2)):
            q = " OR ".join(vocab[lang]) + " when:1d"
            url = ("https://news.google.com/rss/search?q="
                   + urllib.parse.quote(q)
                   + f"&hl={hl}&gl={gl}&ceid={urllib.parse.quote(ceid)}")
            row = {"layer": "googlenews", "country": cc, "family": fam,
                   "lang": lang, "query": q}
            try:
                r = requests.get(url, timeout=30, headers={"User-Agent": UA})
                row["http"] = r.status_code
                feed = feedparser.parse(r.content)
                items = feed.entries
                row["returned"] = len(items)
                srcs = Counter()
                ages = []
                for e in items:
                    src = getattr(e, "source", None)
                    srcs[(src.get("title") if src else None) or "?"] += 1
                    if getattr(e, "published_parsed", None):
                        ts = datetime(*e.published_parsed[:6],
                                      tzinfo=timezone.utc)
                        ages.append(round(age_hours(ts, now), 1))
                row["unique_sources"] = len(srcs)
                row["top_sources"] = srcs.most_common(8)
                row["ages_h"] = sorted(ages)
                row["fresh_lt6h"] = sum(1 for a in ages if a < 6)
                row["sample_titles"] = [e.title[:90] for e in items[:5]]
            except Exception as e:
                row["error"] = repr(e)
            out.append(row)
            print(f"  googlenews {cc} {fam}: n={row.get('returned')} "
                  f"srcs={row.get('unique_sources')} "
                  f"fresh<6h={row.get('fresh_lt6h')}", flush=True)
            time.sleep(2.0)
    return out


def main() -> None:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tools"
    now = datetime.now(timezone.utc)
    key = load_key()
    print(f"probe start {now.isoformat()} window={WINDOW_H}h")

    print("== TheNewsAPI ==")
    tna = probe_thenewsapi(key, now)
    print("== Google News RSS ==")
    gn = probe_googlenews(now)

    results = {"ts": now.isoformat(), "window_h": WINDOW_H,
               "thenewsapi": tna, "googlenews": gn}
    outfile = outdir / "search_probe_results.json"
    outfile.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\nwrote {outfile}")

    # -------- summary table
    print(f"\n{'cc':<4}{'TNA F1':>8}{'TNA F2':>8}"
          f"{'GN F1 n/src':>13}{'GN F2 n/src':>13}{'GN fresh<6h':>12}")
    for cc in COUNTRIES:
        t1 = next(r for r in tna if r["country"] == cc and r["family"] == "F1")
        t2 = next(r for r in tna if r["country"] == cc and r["family"] == "F2")
        g1 = next(r for r in gn if r["country"] == cc and r["family"] == "F1")
        g2 = next(r for r in gn if r["country"] == cc and r["family"] == "F2")
        fresh = (g1.get("fresh_lt6h") or 0) + (g2.get("fresh_lt6h") or 0)
        print(f"{cc:<4}{str(t1.get('found', 'ERR')):>8}"
              f"{str(t2.get('found', 'ERR')):>8}"
              f"{str(g1.get('returned', 'E')) + '/' + str(g1.get('unique_sources', '')):>13}"
              f"{str(g2.get('returned', 'E')) + '/' + str(g2.get('unique_sources', '')):>13}"
              f"{fresh:>12}")


if __name__ == "__main__":
    main()
