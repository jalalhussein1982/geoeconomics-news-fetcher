# Geoeconomic News Pipeline

A daily, fully automated pipeline that discovers geoeconomic news across **MENA** and **Latin America** in multiple source languages (Arabic, Spanish, Portuguese, French, plus Chinese/English), clusters it into events, uses an LLM to synthesize grounded structured records behind a validation gate, and delivers the result as both a standalone HTML archive and an email newsletter.

Deployed as a daily **Kubernetes CronJob** on the CERIT-SC / MetaCentrum academic cloud, with fully **resumable** run-state in PostgreSQL.

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-state%20store-4169E1?logo=postgresql&logoColor=white">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-image-2496ED?logo=docker&logoColor=white">
  <img alt="Kubernetes" src="https://img.shields.io/badge/Kubernetes-CronJob-326CE5?logo=kubernetes&logoColor=white">
  <img alt="LLM" src="https://img.shields.io/badge/LLM-DeepSeek--V3.2-7B61FF">
</p>

---

## Why this project is interesting

- **End-to-end data + LLM engineering** — ingestion, extraction, clustering, LLM synthesis, validation, rendering and delivery, wired together as one orchestrated batch job.
- **Grounded, gated LLM output** — every synthesized record is produced from clustered source evidence and passed through a separate **validator gate** before it can ship, reducing hallucination and keeping output traceable to sources.
- **Multilingual by design** — discovers and processes news in Arabic, Spanish, Portuguese, French, Chinese and English rather than English-only feeds.
- **Production-grade operations** — containerized, scheduled on Kubernetes, with resumable stage checkpoints in Postgres so a failed run picks up where it left off instead of restarting.
- **Clean secrets hygiene** — all credentials are injected via environment variables / Kubernetes secrets; the repository ships only `.env.example` and `deploy/secret.example.yaml` templates.

## Pipeline at a glance

Nine staged, resumable steps run by `geoecon/orchestrator.py`:

```
07:00 daily cron
  → DISCOVERY            GDELT 2.0 firehose, [T-24h, T] window, economic-theme prefilter
  → DISCOVERY_REGISTRY   curated sources (config/sources.yaml): rss / sitemap / scrape,
                         URL-normalized dedup against GDELT, with source provenance
  → EXTRACTION           trafilatura article extraction → metadata/headline fallback
  → GATE                 batched LLM geoeconomic + country relevance gate
  → CLUSTER              per-country LLM event grouping (lexical Jaccard fallback)
                         + corroboration scoring
  → SYNTHESIS            per-cluster structured JSON record (DeepSeek-V3.2) + validator guard
  → SUMMARY              per-region executive lede + key takeaways
  → RENDER               per-region dual HTML (standalone archive + email-safe)
  → SEND                 one email per region via SMTP; commit the seen-store on success

  STATE: PostgreSQL — runs, stage checkpoints, article provenance, cluster aggregates,
         seen-store (non-overlap across runs), synthesis quality flags — fully resumable
```

## Tech stack

| Layer | Tools |
|---|---|
| Language | Python 3.12 |
| Ingestion / extraction | GDELT 2.0, `feedparser`, `trafilatura`, sitemap & HTTP scrapers |
| LLM | OpenAI-compatible endpoint (DeepSeek-V3.2) for gating, clustering and synthesis |
| State | PostgreSQL (`psycopg` 3), SQL migrations |
| Rendering | Jinja2 (HTML archive + email templates) |
| Packaging / ops | `pyproject.toml`, Docker, Kubernetes CronJob, CloudNativePG |
| Testing | `pytest` (unit + DB/network-marked integration), `aiosmtpd` for SMTP tests |

## Repository layout

```
geoecon/
  orchestrator.py        # stage runner / CLI entrypoint (`geoecon` console script)
  config.py              # env-driven configuration
  gdelt.py               # GDELT 2.0 discovery
  registry.py            # curated source registry
  fetchers/              # rss / sitemap / http / scrape / firecrawl / api fetchers
  stages/                # discovery, extract, gate, cluster, synth, summary, render, send
  llm/                   # OpenAI-compatible client + output validator
  prompts/               # gate / grouping / summary / synthesis prompts
  render/templates/      # Jinja2 archive + email templates
  state/                 # Postgres models, repo, and SQL migrations
config/                  # source registry & runtime config
deploy/                  # Kubernetes manifests (CronJob, secret templates)
docs/how-it-works/       # authoritative living documentation
tests/                   # unit + integration tests
```

## Running locally

```bash
# 1. Install (Python 3.12+)
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env        # fill in DB_DSN, LLM key, SMTP, regions, window

# 3. Initialize the database
psql "$DB_DSN" -f schema.sql

# 4. Run the pipeline
geoecon                     # runs the staged, resumable orchestrator

# Tests
pytest                      # unit tests; DB/network tests are opt-in via markers
```

Configuration is environment-driven — see [`.env.example`](.env.example) for every supported variable.

## Documentation

The authoritative, living documentation lives in [`docs/how-it-works/`](docs/how-it-works/); [`PIPELINE_BLUEPRINT.md`](PIPELINE_BLUEPRINT.md) is the high-level architecture reference.

---

*Built as a research/production data pipeline. The LLM endpoint and compute run on the Czech national academic cloud (MetaCentrum / CERIT-SC).*
