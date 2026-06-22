# How the pipeline works — overview

This folder explains, end to end, what happens when the daily job runs — from the
moment the Kubernetes CronJob fires to the moment a newsletter lands in an inbox.

Read in order:

1. **[01-fetching-news.md](01-fetching-news.md)** — the *search & fetch* mechanism:
   how the app discovers candidate news (GDELT) and pulls the article text
   (extraction). "How it finds and grabs the news."
2. **[02-processing-to-report.md](02-processing-to-report.md)** — how that raw
   news is *turned into reports*: relevance gate → clustering → LLM synthesis (with a
   quality gate) → per-region summary → HTML render → email send. "How it thinks about
   the news and ships the briefs."

---

## The 30-second version

```
07:00 Europe/Prague  ──>  Kubernetes CronJob `geoecon-daily`
                              runs the container: `python -m geoecon.orchestrator run`
                                 │
                                 ▼
        ┌──────────────────────────────────────────────────────────────┐
        │  orchestrator.run_pipeline()  — one run = one 24h window       │
        │  drives 8 stages in order, recording progress in Postgres so   │
        │  a crashed run RESUMES from the last completed stage           │
        └──────────────────────────────────────────────────────────────┘
                                 │
   FETCH    discovery  ──►  extraction                                     (file 01)
            GDELT GKG       trafilatura
            find URLs       get body text
                                 │
 PROCESS    gate ─► cluster ─► synthesis ─► summary ─► render ─► send      (file 02)
            is it    group      LLM→JSON     per-       per-      one SMTP
            geo-     into       + quality    region     region    email
            econ?    events     gate         lede +     HTML      per
            +which              (only `ok`   takeaways  brief     region
            country?             ships)
                                 │
                                 ▼
   📧 one "Geoeconomic Brief — <Region> — <date>" per region  +  🗄 per-region HTML archives
```

## The trigger and the orchestrator

- **Trigger.** A Kubernetes `CronJob` (`deploy/cronjob.yaml`, schedule `0 7 * * *`,
  `timeZone: Europe/Prague`) launches one pod per day. The container's entrypoint is
  `python -m geoecon.orchestrator` with the argument `run`.
- **Entrypoint.** `geoecon/orchestrator.py:main()` loads configuration from environment
  variables (`Config.from_env`), opens the Postgres connection and applies migrations
  (`geoecon.state.db`), builds the LLM client, then calls `run_pipeline(...)`.
- **The run.** `run_pipeline()` defines the run's time window as `[now − WINDOW_HOURS, now]`
  (default 24h). It then either **resumes an existing run** for that window or **creates a
  new one**, and executes the eight stages in order:

  `discovery → extraction → gate → cluster → synthesis → summary → render → send`

  Each stage is wrapped so that it:
  1. marks itself `running` in the `stage_runs` table,
  2. does its work (writing its own domain rows),
  3. marks itself `completed` with a small JSON checkpoint of metrics.

  If a stage raises, the run is marked `failed` and the pod exits non-zero; Kubernetes
  retries (`backoffLimit: 2`), and the next attempt **skips already-completed stages**.
  Stages that are already `completed` for the window are skipped on resume.

## State lives in Postgres (so runs are auditable and resumable)

A dedicated CloudNativePG database (`geoecon-db`) holds all run state. The schema is in
`schema.sql` / `geoecon/state/migrations/001_init.sql`; the typed access layer is
`geoecon/state/repo.py`. The tables you'll meet in the two detailed docs:

| Table | What it holds |
|---|---|
| `runs` | one row per daily window (status, models used, metrics JSON) |
| `stage_runs` | per-stage checkpoint — the resume anchor |
| `articles` | every discovered URL + its extraction outcome + the gate verdict (`gate_*`, `country_fips`) |
| `clusters` / `cluster_articles` | event groupings and membership |
| `seen_events` | cross-day memory — suppress events already emitted |
| `synthesis_records` | the per-cluster structured output + a quality flag |
| `region_summaries` | the per-region executive lede + takeaways (one row per `(run_id, bucket)`) |
| `newsletters` | the rendered artifact + the SMTP send outcome (one per region) |
| `throughlines` | reserved for a future cross-cluster editorial thread |

## Configuration knobs (env vars → `Config`)

Defined in `geoecon/config.py`, supplied at runtime from the `geoecon-secrets` Secret and
the CronJob env. The most relevant:

| Var | Default | Meaning |
|---|---|---|
| `WINDOW_HOURS` | `24` | discovery window `[T-WINDOW_HOURS, T]` |
| `BUCKETS` | `middle_east,north_africa,latam` | which region buckets are active (the operative keep/group set) |
| `DISCOVERY_CEILING` | `120` | per-bucket cap on articles carried out of discovery |
| `GATE_MODEL` / `GATE_BATCH_SIZE` | `deepseek-v3.2` / `15` | the relevance-gate model and batch size |
| `DEDUP_MODEL` / `DEDUP_JACCARD` | `deepseek-v3.2` / `0.6` | event-grouping model and the lexical-fallback threshold |
| `REGION_CAP` | `12` | top records per region carried into summary/render |
| `SYNTHESIS_MODEL` | `deepseek-v3.2` | the per-cluster synthesis (and summary) model |
| `SYNTHESIS_PROMPT_ID` | `task_a_cov` | the synthesis prompt used |
| `LLM_BASE_URL` | `https://llm.ai.e-infra.cz/v1` | the e-INFRA LLM proxy |
| `MAETCENTRUM_KEY` | — | LLM proxy API key (fallback `METACENTRUM_KEY`) |
| `RECIPIENTS` / `FROM_ADDR` / `SMTP_*` | — | email delivery |
| `LIMIT` | none | cap articles (overrides `DISCOVERY_CEILING`; used for smoke runs) |
| `DRY_RUN` | `false` | render but don't send |

> `REGIONS` (`MENA,LatAm`) still exists on `Config` and is stored on each article for
> provenance, but the active **`BUCKETS`** set is what actually drives the keep/group/render
> decisions. `SEND_HEARTBEAT` is also still defined on `Config` but the orchestrator's send
> step no longer consults it; the "quiet day" line is a template-level fallback in render.

> **Scope note.** The pipeline is fully wired end to end: an LLM **relevance gate**,
> **per-country LLM event grouping** (with a lexical Jaccard fallback), a quality-gated
> synthesis, a **per-region executive summary**, and one English-only (`en_only`) brief
> **per region**. An earlier embedding-based clustering approach (BGE-M3) was built and
> reverted in favour of the LLM grouping. Still future: original-language / bilingual render
> modes and a cross-cluster throughline — the schema (`render_mode`, `throughlines`,
> `clusters.centroid`/`framing_divergent`) already has room for them.
