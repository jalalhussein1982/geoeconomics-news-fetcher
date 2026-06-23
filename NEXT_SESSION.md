# Next Session — Handoff

**State as of 2026-06-23:** The pipeline is **live in production** (CERIT-SC k8s CronJob
`geoecon-daily`, 07:00 Europe/Prague, image `jalalhussein/geoecon-pipeline:20260623-2`).
The **registry-driven multi-source redesign (Changes 1–4 + sitemap) is deployed and
validated** — run 16 shipped 36 records across 3 region briefs, all emails sent.

Working directory: `/Users/jalalhussein/Desktop/geoeconomics_news_fetcher`

---

## Read these first, in order
1. **`docs/how-it-works/`** — the authoritative living docs (README overview → 01 fetch →
   02 process → **03 registry-redesign**). Read 03 for anything touching `discovery_registry`,
   the registry, provenance, or corroboration.
2. `PIPELINE_BLUEPRINT.md` — high-level reference (now points at the how-it-works docs).
3. `schema.sql` + `geoecon/state/migrations/` — DB schema (migrations through `006`).
4. `DEPLOYMENT_ENVIRONMENT.md` + `deploy/` — infra, CronJob, smoke job.

## What was built recently (all committed + pushed; image deployed)
- **Change 1** registry-driven discovery: `geoecon/registry.py`, `geoecon/fetchers/`
  (rss/scrape/sitemap/api), `geoecon/stages/discovery_registry.py`; runs alongside GDELT.
- **Change 2** provenance: migration 004 — `articles` provenance cols + `clusters` aggregates.
- **Change 3** URL-normalized dedup: `geoecon/urlnorm.py` (both discovery stages).
- **Change 4** corroboration scoring: `geoecon/corroboration.py` + migration 005; ranking
  switch `RANK_BY_CORROBORATION` ships **OFF** (dormant, calibration-deferred).
- **Sitemap** fetch strategy + **Task 0** registry resolution (`config/sources.yaml`,
  `config/sources.resolved.report.md`, `tools/registry_*.py`).
- **Guardrails:** recall-floor WARNING, coverage metrics (`core15_primaries_live` etc.),
  strategy precedence `rss/api > sitemap > scrape`.
- **Deploy fixes:** Dockerfile `COPY config`; CronJob `SOURCES_PATH` + flag; migration 006
  (added `discovery_registry` to the `stage_runs` CHECK — its absence crashed the first run).
- 112 unit tests pass; requires_db / requires_net tests are skipped locally.

## Coverage is the objective — and it's gated, not done
Live primaries are **14/48** (11 rss + 3 sitemap); **Core-15 primary coverage 6/15**. The
other 34 are JS-rendered government portals. **Spike B ruled out the SharePoint API**;
headless rendering is the real lever and is unavailable in this environment. Don't read
"Changes landed + tests pass" as "fetching fixed" — it's "fetching can be fixed once the
primaries resolve."

## The single follow-up task (live-env: Playwright + DB + LLM proxy + unblocked GDELT)
In order (mirrored in agent memory `registry-coverage-state` and in 03-registry-redesign.md):
1. Consolidated re-resolution (`tools/registry_probe.py`, applying the precedence rule) to
   **headless-resolve the 34 needs_review primaries** (run API + sitemap probes in the same pass).
2. Re-run Task 0's report; confirm `core15_primaries_live` jumps from 6.
3. One full live run; **review the brief as a brief**.
4. Only then flip `RANK_BY_CORROBORATION` ON; review the before/after diff — confirm the
   clusters that sink below `REGION_CAP` are genuinely the weakly-sourced ones. **That diff
   review IS the calibration** (a judgment on one real day, not a computed number).
5. Tune the named weights in `geoecon/corroboration.py`; commit. **Do not flip before step 3.**

## Open editorial observations (from the run-16 review; not bugs)
- **Egypt appears in both the Middle East and North Africa briefs** (`EG` maps to both
  buckets). Decide if the dual-send is desirable.
- A dominant day's story can take many of a region's slots via distinct angles; `region_cap`
  + dedup prevent pure repetition, but topical-diversity tuning is a ranking question.

## Deploy / access notes
- Cluster reached via `ssh skirit` then kubectl (`$HOME/bin/kubectl`,
  `KUBECONFIG=$HOME/.kube/kuba-cluster.yaml`, namespace `hussein-ns`). Key auth is flaky
  (Perun wipe) — fallback is the SSH master-socket method (memory `skirit-access-caveat`).
- Push git under gh account **`jalalhussein1982`** then restore `menaregionsolutions`
  (memory `gh-account-for-repo`).
- Images **must** build `--platform linux/amd64` (cluster is amd64; arm64 fails to pull).
- Build/deploy: `docker build --platform linux/amd64 -t jalalhussein/geoecon-pipeline:<tag> .`
  → push → bump tag in `deploy/cronjob.yaml` + `deploy/smoke-job.yaml` → `kubectl apply`.
- Smoke test: `deploy/smoke-job.yaml` (DRY_RUN + LIMIT — renders, sends no email).
- **Never touch** the co-tenant `siem-*` / `acled-*` objects in `hussein-ns`.

## Models / coexistence (unchanged, still true)
- LLM = API calls to `https://llm.ai.e-infra.cz/v1` (key `MAETCENTRUM_KEY`, fallback
  `METACENTRUM_KEY`). Synthesis/gate/dedup = `deepseek-v3.2`, temp 0, seed 12345. No GPU.
- Separate CNPG Postgres `geoecon-db`; all objects prefixed `geoecon-`. See
  `DEPLOYMENT_ENVIRONMENT.md`.
