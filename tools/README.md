# tools/ — registry resolution (Task 0)

One-off tooling that resolves the **mechanics layer** of `config/sources.yaml`
(`fetch_type` / `feed_url` / `extract_hint` / `status`) by probing each source
live. Not part of the runtime pipeline — the pipeline reads the registry via
`geoecon.registry` (PyYAML); these scripts only produce/maintain that file.

## Reproduce a resolution

```bash
# 1. Probe (read-only; hits the network, respects robots.txt). --runs>1 merges
#    RSS findings across flaky passes. Writes a results JSON.
python tools/registry_probe.py --runs 2 --out tools/registry_probe_results.json

# 2. Review tools/registry_probe_results.json, then write it back into the
#    registry (comment-preserving) + regenerate the report.
python tools/registry_apply.py --results tools/registry_probe_results.json --date YYYY-MM-DD
```

Probe a subset (e.g. to re-resolve a few primaries from a better-networked host):

```bash
python tools/registry_probe.py --only sa_cb_sama,ae_cb_cbuae --out tools/registry_probe_results.json
```

## Strategy precedence (deterministic re-resolution order)

When a source could resolve under more than one strategy — e.g. it exposes both an
RSS feed and a sitemap, or (after a headless pass) both a sitemap and a scrape
selector — resolve it to the **first** that applies, in this order:

> **rss / api  >  sitemap  >  scrape**

Rationale: most stable and cheapest first. A feed/API is a purpose-built, dated
list; a sitemap is windowable but broad; a scrape selector rots fastest (it breaks
on any redesign). Picking the higher-precedence strategy keeps discovery cheap and
durable.

This is enforced, not just documented:
- `registry_probe.py::classify` tries the strategies in this order and returns on
  the first hit (so a source with a feed never resolves to its sitemap).
- `registry_probe.py::merge` (the `--runs`/re-run combiner) keeps the
  higher-precedence live result across passes via `PRECEDENCE`, so two
  re-resolution runs over the same sources converge to the **same** registry
  rather than last-writer-wins.

**Consolidated live-env re-resolution** (headless + API re-probe + sitemap in one
pass) MUST apply this rule: a freshly-captured scrape selector does NOT override an
existing rss/sitemap resolution.

## Classification policy (conservative)

- `rss` — a feed was found and validated through feedparser (≥1 entry).
- `api` — a JSON/OData list endpoint (e.g. SharePoint `_api/web/lists`) returned a
  usable article list; `feed_url` points at the items endpoint.
- `scrape` + `needs_review` — origin reachable but no feed/API; the article list is
  JS-rendered (no headless browser here) or a selector needs manual capture.
- `dead` — **only** a confirmed DNS failure. Stale-path 404s, TLS/connection
  timeouts, and robots/ToS blocks are `needs_review` ("verify from production"),
  never retired.

`registry_probe_results.json` is a generated artifact (gitignored).

Requires the `dev` extra (`pip install -e '.[dev]'`) for `ruamel.yaml`.
