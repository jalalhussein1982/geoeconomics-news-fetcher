#!/usr/bin/env python3
"""Task 0 write-back: apply a probe results JSON into config/sources.yaml
(comment-preserving via ruamel) and (re)generate config/sources.resolved.report.md.

Usage:
    python tools/registry_apply.py --results tools/registry_probe_results.json --date 2026-06-23
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from geoecon import registry  # noqa: E402

YAML_PATH = REPO / "config" / "sources.yaml"
REPORT_PATH = REPO / "config" / "sources.resolved.report.md"

CORE15 = {"AE", "AR", "BR", "CI", "CO", "EG", "GY", "IR", "IS", "IZ", "KU", "MX", "QA", "SA", "TU"}
FIPS_NAME = {"AE": "UAE", "AR": "Argentina", "BR": "Brazil", "CI": "Chile", "CO": "Colombia",
             "EG": "Egypt", "GY": "Guyana", "IR": "Iran", "IS": "Israel", "IZ": "Iraq",
             "KU": "Kuwait", "MX": "Mexico", "QA": "Qatar", "SA": "Saudi Arabia", "TU": "Turkey"}


def apply_writeback(results: dict, date: str):
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)  # match the file's "  - id:" style
    yaml.representer.add_representer(
        type(None), lambda r, d: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    doc = yaml.load(YAML_PATH.read_text())

    for entry in doc["sources"]:
        res = results.get(entry["id"])
        if not res:
            continue
        entry["fetch_type"] = res["fetch_type"]
        entry["status"] = res["status"]
        entry["verified"] = date
        if res["fetch_type"] in ("rss", "api", "sitemap") and res.get("feed_url"):
            entry["feed_url"] = res["feed_url"]
            if res.get("clear_probe_paths"):
                entry["probe_paths"] = []
        if res.get("extract_hint"):
            entry["extract_hint"] = res["extract_hint"]
        # strip any prior Task0(...) tag so re-resolution replaces it (no stacking)
        prior = re.sub(r"\s*\|\s*Task0\(.*$", "", entry.get("notes") or "", flags=re.S).strip()
        tag = f"Task0({date}): {res['note']}"
        entry["notes"] = f"{prior} | {tag}" if prior else tag

    meta = doc.get("meta")
    if meta is not None:
        ft = Counter(r["fetch_type"] for r in results.values())
        meta["mechanics_status"] = (
            f"RESOLVED via Task 0 on {date} (live prober, robots-respecting): "
            + ", ".join(f"{v} {k}" for k, v in sorted(ft.items()))
            + ". See sources.resolved.report.md.")

    with open(YAML_PATH, "w") as f:
        yaml.dump(doc, f)


def write_report(results: dict, date: str):
    srcs = {s.id: s for s in registry.load_sources(registry.default_path())}  # post-write state
    rows = sorted(
        ((s.tier, s.bucket or "—", s.country_fips or "—", sid, s.institution,
          results.get(sid, {}).get("fetch_type", "?"), results.get(sid, {}).get("status", "?"),
          results.get(sid, {}).get("note", "")) for sid, s in srcs.items()),
        key=lambda x: (x[0], x[2], x[3]))

    ft = Counter(r.get("fetch_type") for r in results.values())
    st = Counter(r.get("status") for r in results.values())
    live_primary = {s.country_fips for s in srcs.values()
                    if s.tier == 0 and s.country_fips in CORE15 and s.status == "live"}
    no_primary = sorted(CORE15 - live_primary)

    L = ["# Source registry — Task 0 resolution report\n",
         f"Resolved {date} by a live, robots-respecting prober (RSS/Atom discovery via "
         "alternate-links + probe paths + common defaults; SharePoint/OData list-API probe; "
         "no headless browser available, so JS-rendered article lists are left `needs_review`).\n",
         f"**Totals ({sum(ft.values())} sources):** "
         + ", ".join(f"`{k}`={v}" for k, v in sorted(ft.items()))
         + "  |  status: " + ", ".join(f"`{k}`={v}" for k, v in sorted(st.items())) + "\n",
         "> Conservative: only a confirmed DNS failure is `dead`. Stale-path 404s, TLS/connection "
         "timeouts, and robots blocks from the resolver environment are `needs_review` "
         "(verify from production), never `dead`.\n",
         "\n## Core-15 primary coverage\n",
         f"**{15 - len(no_primary)}/15** Core countries have a live Tier-0 primary; the rest rely "
         "on the wire + external-actor + GDELT-firehose tiers until a primary feed/selector/API "
         "resolves:\n"]
    if no_primary:
        L.append("\n| Country | Why no live primary |\n|---|---|")
        for c in no_primary:
            prim = [s for s in srcs.values() if s.tier == 0 and s.country_fips == c]
            why = "; ".join(sorted({results.get(p.id, {}).get("status", "?") for p in prim}))
            L.append(f"| {FIPS_NAME[c]} ({c}) | {len(prim)} Tier-0 source(s), all `{why}` |")
    L.append("\n\n## Every source\n")
    L.append("| Tier | Bucket | FIPS | id | institution | fetch_type | status | outcome |")
    L.append("|---|---|---|---|---|---|---|---|")
    for tier, bucket, fips, sid, inst, ftype, status, note in rows:
        L.append(f"| {tier} | {bucket} | {fips} | `{sid}` | {inst} | `{ftype}` | `{status}` "
                 f"| {(note or '').replace('|', chr(92)+'|')} |")
    REPORT_PATH.write_text("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Apply probe results into the registry + report.")
    ap.add_argument("--results", default=str(REPO / "tools" / "registry_probe_results.json"))
    ap.add_argument("--date", required=True, help="ISO date to stamp into `verified` (e.g. 2026-06-23)")
    args = ap.parse_args()

    results = {r["id"]: r for r in json.load(open(args.results))}
    apply_writeback(results, args.date)
    write_report(results, args.date)
    print(f"wrote {YAML_PATH}\nwrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
