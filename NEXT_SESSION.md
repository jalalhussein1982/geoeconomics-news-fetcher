# Next Session — Handoff

**State as of 2026-06-27:** The pipeline is **live in production** (CERIT-SC k8s CronJob
`geoecon-daily`, 07:00 Europe/Prague, image `jalalhussein/geoecon-pipeline:20260627-3`).
Deployed and running: the registry-driven multi-source redesign (Changes 1–4 + sitemap), the
brief output-quality refinement (figures cap, exec-summary trim, market-roundup demotion), the
curated named-source nudge (`CURATED_PREFERENCE`), the per-country slot cap (`COUNTRY_CAP=4`),
and — newest — the **Firecrawl render path** that recovers JS-rendered/403 dark primaries
(6 sources now live; see below). Next real email-sending run is the daily 07:00 Europe/Prague
run, which will be the first brief carrying all 6 Firecrawl-recovered sources.

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
- **Firecrawl render path (2026-06-27, images `20260627-2`/`-3`):** JS-rendered/403 dark
  primaries are recovered by fetching the rendered DOM via Firecrawl, then running the same
  `extract_hint` selector. `render: firecrawl` per source; key in `geoecon-secrets` as
  `FIRECRAWL_API_KEY`. New: `fetchers/firecrawl.py`, `Source.render`/`require_date`,
  `_date_from_url` (parses `/YYYY/MM/DD/` + `/YYYY-MM-DD/`, end-of-day for date-only). **6 live:**
  `eg_cb_cbe`, `actor_imf`, `actor_worldbank`, `wire_efe`, `reg_alarabiya_ar`, `eg_mof`
  (Core-15 6→7, tier1_live 2→3). Full detail in `docs/how-it-works/03-registry-redesign.md`
  → "Firecrawl render path", and agent memory `firecrawl-render-path`.
- **Per-country slot cap (image `20260624-2`):** `COUNTRY_CAP=4` — no country takes >4 of a
  region's 12 slots (backfills if thin). `COUNTRY_CAP=0` disables.
- **Curated named-source nudge (image `20260624-1`):** user-named MENA/LatAm outlets +
  economy-first ranking; `CURATED_PREFERENCE` (ON), `CURATED_BOOST`, `ECONOMY_SECTION_BOOST`.
- **Brief output-quality refinement (2026-06-23, image `20260623-3`, branch merged to main):**
  `render.format_figures` now caps the figures line at `FIGURES_MAX` (8) with salience ranking +
  locale/currency-aware numeric dedup; `summary.build_summary_messages` no longer feeds the raw
  `key_figures` dump to the exec-summary LLM; new `geoecon/ranking.py` demotes US/global market
  roundups (`MARKET_DEMOTE`) so they rank last (country markets + MSCI spared). 124 unit tests;
  spec/plan in `docs/superpowers/{specs,plans}/2026-06-23-brief-output-quality-refinement*.md`;
  blueprint §14. Sparked by a recipient review (the 2026-06-23 briefs read accurate but the
  finance stories dumped 80–150 figures).
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

## Coverage — progressing via Firecrawl, not done
The headless lever (deferred for months as "unavailable") is now in place via Firecrawl, a
hosted render API — no in-cluster browser. **Core-15 primary coverage 6→7** (adds Egypt);
6 `render: firecrawl` sources live. The remaining dark sources fall into clear buckets:

- **Stale-URL (404), ~6** — `ar_cb_bcra` (highest value), `mx_cb_banxico`, `mx_shcp`,
  `kw_cb_cbk`, `qa_qia`, `gy_bog`. **Cheapest win, no render** — just need the correct
  current listing URL.
- **`iq_cb_cbi`** — renders, but ~67 undated, non-chronological links → no safe window.
  Needs per-row date extraction (or chronological selector + a `max_items` cap).
- **Parked SPAs** — SharePoint (SAMA/QCB/CBUAE/Tadawul/ADX), WebSphere (Turkey CB), Liferay
  (Chile CB), Iran CB: render but the article list loads via further AJAX. Need Firecrawl
  interaction (click/scroll) or a per-site API.
- **`actor_eu_sanctions`, `actor_opec`** — sites restructured; need the new listing URL.
- **`wire_reuters_world`** — excluded permanently: ToS forbids scraping.

**Per-source workflow** (probe → pattern → selector → validate → redeploy) is documented in
`docs/how-it-works/03-registry-redesign.md` → Follow-ups, and agent memory
`firecrawl-render-path`. Probe scaffolding lives in the session scratchpad (`fc_batch.mjs`).

## Corroboration ranking (independent of coverage)
`RANK_BY_CORROBORATION` still ships **OFF**. Flip it only after a real run: review the
before/after brief diff — confirm the clusters that sink below `REGION_CAP` are genuinely the
weakly-sourced ones (**that diff review IS the calibration**, a judgment on one real day, not
a computed number) — then tune the weights in `geoecon/corroboration.py`; commit.

## Open editorial observations (from the run-16 review; not bugs)
- **Egypt appears in both the Middle East and North Africa briefs** (`EG` maps to both
  buckets). Decide if the dual-send is desirable.
- A dominant day's story can take many of a region's slots via distinct angles; `region_cap`
  + dedup prevent pure repetition, but topical-diversity tuning is a ranking question.
- **Deferred from the 2026-06-23 output-quality refinement** (scoped out by decision, not done):
  the event-grouping **over-split** (one storyline rendered as ~5 separate items — same-event
  angles, distinct from the topical-diversity point above), the occasional **garbled entity name**
  in synthesis (a person's name corrupted from a non-English source), and **oil-price-term
  demotion** (deliberately NOT demoted — oil is Middle-East-relevant signal).

## Deploy / access notes
- **Cluster access via SSH ControlMaster socket** (worked 2026-06-27; key auth is flaky after
  Perun wipe). The user opens a master in a 2nd terminal with their own auth:
  `ssh -M -S ~/.ssh/skirit-cm.sock -fNT skirit`. Then every kubectl runs over it without
  re-auth: `ssh -S ~/.ssh/skirit-cm.sock skirit 'KUBECONFIG=$HOME/.kube/kuba-cluster.yaml
  $HOME/bin/kubectl -n hussein-ns …'`. Tear down with `ssh -S ~/.ssh/skirit-cm.sock -O exit skirit`.
- **There is NO repo checkout on skirit** — apply manifests by piping the LOCAL file over the
  socket: `ssh -S ~/.ssh/skirit-cm.sock skirit '… kubectl -n hussein-ns apply -f -' <
  deploy/cronjob.yaml`. (`kubectl set image cronjob/geoecon-daily pipeline=…:<tag>` also works
  and needs no file.) DB checks: `kubectl exec -i geoecon-db-1 -- psql -U postgres -d geoeconomics`.
- **Secrets** live in `geoecon-secrets` (`envFrom`). To add/rotate a key:
  `kubectl -n hussein-ns patch secret geoecon-secrets --type merge -p
  '{"stringData":{"KEY":"value"}}'`. `FIRECRAWL_API_KEY` is required for `render: firecrawl`
  sources. **(The Firecrawl key has been in plaintext during dev — rotate it.)**
- Push git under gh account **`jalalhussein1982`** then restore `menaregionsolutions`
  (memory `gh-account-for-repo`).
- Images **must** build `--platform linux/amd64` (cluster is amd64; arm64 fails to pull).
  Build/deploy: start Docker Desktop (`open -a Docker`), then `docker buildx build
  --platform linux/amd64 --builder amd64builder -t jalalhussein/geoecon-pipeline:<tag> --push .`
  (Docker-Hub creds are in the Desktop keychain — pushed without a prompt this session) → bump
  tag in `deploy/cronjob.yaml` + `deploy/smoke-job.yaml` → apply (above). Tag scheme `YYYYMMDD-N`.
- Smoke test: `deploy/smoke-job.yaml` (DRY_RUN — renders, sends no email; doesn't touch
  `seen_events`). **Gotcha (2026-06-23):** `LIMIT` no longer caps the run — the registry
  discovery path ignores it, so the smoke is a full ~48-min run. **For a quick coverage check,
  prefer a one-off discovery Job** (a Job that runs `python -c` calling
  `get_fetcher(src).discover(src)` for specific source ids) — minutes, not ~48.
- **Never touch** the co-tenant `siem-*` / `acled-*` objects in `hussein-ns`.

## Models / coexistence (unchanged, still true)
- LLM = API calls to `https://llm.ai.e-infra.cz/v1` (key `MAETCENTRUM_KEY`, fallback
  `METACENTRUM_KEY`). Synthesis/gate/dedup = `deepseek-v3.2`, temp 0, seed 12345. No GPU.
- Separate CNPG Postgres `geoecon-db`; all objects prefixed `geoecon-`. See
  `DEPLOYMENT_ENVIRONMENT.md`.
