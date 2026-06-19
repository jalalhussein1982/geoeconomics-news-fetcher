# Geoeconomic Newsletter Pipeline — Implementation Design Spec

**Date:** 2026-06-19
**Status:** Approved design, ready for implementation planning.
**Source of truth for behavior:** `PIPELINE_BLUEPRINT.md` (locked architecture) + `schema.sql` (DB).
**Deployment constraints:** `DEPLOYMENT_ENVIRONMENT.md` (MetaCentrum/CERIT-SC K8s coexistence).

This spec covers *how* to implement and deploy the pipeline whose behavior the blueprint already
locks. It does not re-litigate the blueprint's decisions; it operationalizes them.

---

## 0. Context & resolved decisions

**Already built and reusable** (at `~/Desktop/metacentrum/eval/`):

| Asset | Role | Plan |
|---|---|---|
| `client.py` | e-INFRA LLM proxy client (OpenAI-compatible, retry/timing/token accounting, thinking-split) | Vendor verbatim into `geoecon/llm/client.py` |
| `validator.py` | Stage-3 production guard (emits `synthesis_quality_flag`) | Vendor verbatim into `geoecon/llm/validator.py` |
| `models.yaml` | model roster + judge pool | Reference for model ids |
| `task_a_cov` prompt | coverage synthesis prompt | Copy into `geoecon/prompts/task_a_cov.txt` |
| `corpus/` (8 fixtures) + `ground_truth.json` | adversarial eval set + answer key | Reuse as synth integration fixtures |

**Resolved forks (decisions for this build):**
- **Sequencing:** walking skeleton first (Phase 1 ships a daily email end-to-end), then Phase 2 adds analytical depth.
- **Embeddings (Phase 2):** BGE-M3 **in-container on CPU** (no GPU, no separate service, no PBS).
- **Architecture:** single Python package, one Docker image, one K8s CronJob, staged resumable orchestrator (Approach A).
- **Models:** synthesis = `deepseek-v3.2` via `https://llm.ai.e-infra.cz/v1` (API; `kimi-k2.6` contingency). No local GPU inference.
- **Runtime:** K8s CronJob in `hussein-ns` on the CERIT-SC cluster (not PBS, not the frontend).
- **Database:** dedicated CNPG Postgres `geoecon-db` (do NOT reuse `siem-db`; generic table names would collide).
- **Schedule:** `0 7 * * *`, `timeZone: Europe/Prague` (default; can flip to UTC).
- **Language/runtime:** Python 3.12.

---

## 1. Repo layout & components

This folder becomes the git repo root.

```
geoeconomics_news_fetcher/
  PIPELINE_BLUEPRINT.md  schema.sql  DEPLOYMENT_ENVIRONMENT.md   # existing
  docs/superpowers/specs/2026-06-19-geoecon-pipeline-design.md   # this spec
  pyproject.toml  README.md  .env.example  Dockerfile
  geoecon/
    config.py             # 12-factor config: window, regions, models, recipients (from env)
    orchestrator.py       # runs stages in order; writes runs/stage_runs; RESUMABLE
    llm/
      client.py           # vendored from eval/ (unchanged)
      validator.py        # vendored from eval/ (unchanged)
    state/
      db.py               # psycopg3 connection/pool
      repo.py             # typed CRUD over the 9 schema tables
      migrations/001_init.sql   # = schema.sql
    stages/
      discovery.py        # GDELT 2.0: GKG theme-intersection >=2, ceiling 150/day
      extract.py          # trafilatura -> GDELT-metadata headline-only fallback
      cluster.py          # P1: 1 article = 1 cluster · P2: BGE-M3 multilingual
      synth.py            # per-cluster JSON (deepseek-v3.2 + task_a_cov) -> validator
      throughline.py      # P2: cross-cluster pass (null-permitted)
      render.py           # dual HTML (archive + email-safe); render_mode switch
      send.py             # SMTP direct
    prompts/              # task_a_cov.txt (from eval) + throughline.txt (P2)
    render/templates/     # Jinja2: archive.html.j2, email.html.j2
  tests/
    unit/                 # per-stage tests
    fixtures/             # eval corpus + ground_truth.json
  deploy/
    db-cluster.yaml  cronjob.yaml  secret.example.yaml  networkpolicy.yaml  pvc-archive.yaml
```

**Component contract** — each module has one purpose and communicates only through (a) typed
dataclasses and (b) the Postgres state; no stage reaches into another stage's internals.

| Module | Responsibility | Depends on |
|---|---|---|
| `orchestrator` | sequence stages, own `runs`/`stage_runs`, resume | `state.repo`, all stages |
| `stages.*` | one pipeline stage each | `state.repo`, `llm.*`, external APIs |
| `state.repo` | typed CRUD over schema | `state.db` |
| `llm.client` / `llm.validator` | model calls / quality gate | (reused as-is) |
| `render` / `send` | data-model -> HTML -> SMTP | `state.repo` |

**Reuse, not rewrite:** `client.py`, `validator.py`, and the `task_a_cov` prompt come from the eval
verbatim. Net-new code = the stages, state layer, orchestrator, render/send, deploy manifests.

---

## 2. Data & state flow / resumability

One CronJob run = one `runs` row. The orchestrator is the sole writer of `runs`/`stage_runs`; each
stage writes its own domain rows + a checkpoint.

```
orchestrator start
  ├─ find-or-resume run for today's window [T-24h, T]   (T = cron instant, stored UTC)
  │     • no open run for window → INSERT runs(status=running, models, prompt_id)
  │     • open/failed run exists  → RESUME (read stage_runs, skip completed stages)
  └─ for stage in [discovery, extraction, cluster, synthesis, (throughline P2), render, send]:
        UPSERT stage_runs(run_id, stage_name, status=running, started_at)
        … work, writing domain rows …
        UPSERT stage_runs(status=completed, finished_at, checkpoint=JSONB)
     on exception → stage_runs(status=failed, error); runs(status=failed|partial); exit≠0
```

The schema's `stage_runs` allows an `rss` stage; in v1 it is **not run** (RSS depth layer deferred by
design). The orchestrator either skips it entirely or records it as `status=skipped` for audit clarity.

| Stage | Writes | Idempotency key |
|---|---|---|
| discovery | `articles` (pending, GDELT provenance, region, themes, lang, url_hash) | `url_hash` |
| extraction | updates `articles.body` + `fetch_status` (`extracted`/`headline_only`/`failed`) | per `article_id` |
| cluster | `clusters` + `cluster_articles` (P1: 1 article→1 cluster, fp=hash(norm title\|canonical_url)) | `event_fingerprint` |
| synthesis | `synthesis_records` (+ `synthesis_quality_flag`) | `UNIQUE(run_id, cluster_id)` upsert |
| throughline (P2) | `throughlines` (NULL body allowed) | `run_id` PK |
| render | `newsletters` (render_mode, archive path, record_count) | `UNIQUE(run_id)` |
| send | updates `newsletters.send_status`/`sent_at`/`send_error` | — |

**Resumability:** a re-run for the same window resumes from the last `completed` stage. Re-running
synthesis upserts on `(run_id, cluster_id)`; re-discovery is deduped by `url_hash`.

**Non-overlap (seen-store):** before emitting, each `event_fingerprint` is checked against
`seen_events`; already-emitted events are suppressed unless materially updated. `seen_events` is
upserted **only after a successful send** (`times_seen`, `last_emitted_at`, `expires_at = now + TTL`),
so a failed send does not wrongly suppress next run.

**Only `ok` records ship:** non-`ok` `synthesis_records` are persisted and flagged but excluded from
render.

---

## 3. Phase 1 — walking skeleton (ships a daily email)

- **`discovery.py`** — pull GKG 15-min update files across `[T-24h, T]`; keep rows with **≥2** target
  themes (`ECON_*`, `WB_*`, region themes) **and** a MENA/LatAm location; rank by salience
  (mentions/sources); cap **150/day**. Insert `articles` (`pending`).
- **`extract.py`** — `trafilatura.fetch_url` + extract → `body`/`extracted`; on paywall/403/JS/empty →
  GDELT-metadata **`headline_only`** (body NULL); hard error → `failed`. Bounded concurrency, per-URL
  timeout, custom UA.
- **`cluster.py`** — trivial: 1 article = 1 cluster; `event_fingerprint = sha1(norm title|canonical_url)`.
- **`synth.py`** — per cluster: assemble member title+body (or headline-only note); call
  `client.complete('deepseek-v3.2', temp=0, seed=12345, max_tokens=8192)` with `task_a_cov`; parse
  `final_answer` JSON; run `validator.py`; not `ok` → retry once → headline-only fallback / hold-flag;
  insert `synthesis_records`.
- **`render.py`** — `en_only`: take `ok` records, build one data model, render `archive.html`
  (rich standalone → archive PVC) and `email.html` (table-based, inline CSS, ~600px) from it; insert
  `newsletters`.
- **`send.py`** — multipart email (`Geoeconomic Brief — {date}`) to recipient(s); update
  `newsletters.send_status`. **Zero-qualifying-items:** send a short "quiet day — N discovered, 0
  passed" heartbeat (configurable to skip).

---

## 4. Phase 2 — analytical differentiators

Schema already has every column; Phase 2 is mostly internal to `cluster.py` + new `throughline.py`
+ richer `synth` fields + extra templates. Orchestrator/DB flow/schema unchanged.

1. **Multilingual clustering** — embed each article with BGE-M3 (in-container, CPU); cluster by cosine
   similarity (threshold + min/max size guards) so the same event across languages collapses into one
   cluster. `event_fingerprint` becomes language-agnostic (dominant entities/centroid) for stable
   cross-day seen-store matching. Centroid → `clusters.centroid` JSONB (pgvector deferred).
2. **Framing-divergence retention** — detect divergent framing within multi-source clusters (embedding
   spread + lightweight LLM stance check) → `clusters.framing_divergent=true` + `frame_label`; `synth`
   fills `framing_divergence` JSONB.
3. **Throughline** (`throughline.py`) — volume-gated, stronger model; input = day's cluster summaries;
   output = short throughline **or explicit null**; writes `throughlines` (NULL = ran & declined).
   Model = blueprint's queued TASK_B (refusal-passer pool); human-read its out-of-sample output.
4. **Seen-store sharpened** — suppress fingerprints emitted within the suppression window unless
   materially updated.
5. **Render modes** — add `en_plus_original` and `bilingual_rtl` (RTL/LTR, Arabic typography); pure
   template logic over stored pairs, no re-synthesis.

**Deferred (forward-compatible, not built):** RSS depth layer (1c), pgvector migration, keyword/actor
discovery fallback, claim-level provenance. **Eval dependencies:** embedding/reranker choice and TASK_B
throughline model are queued evals; proceed with BGE-M3 (provisional) + a refusal-passer, swap if evals land.

---

## 5. Kubernetes deployment objects

All `geoecon-*`, in `hussein-ns`, mirroring the existing ACLED/siem pattern; honoring
`DEPLOYMENT_ENVIRONMENT.md`.

| Object | Kind | Key config |
|---|---|---|
| `geoecon-db` | CNPG `Cluster` | 1 instance, vanilla PG17 CNPG image (pgvector in P2 if wanted); own PVC 5Gi `nfs-csi`; bootstrap creates DB `geoeconomics` + app role and applies `schema.sql` via `postInitApplicationSQL`. Auto-generated `geoecon-db-app` creds secret. |
| `geoecon-secrets` | Secret | `MAETCENTRUM_KEY`, SMTP host/port/user/pass, recipient(s), timezone. Committed only as `secret.example.yaml`. |
| `geoecon-daily` | CronJob | `0 7 * * *`, `timeZone: Europe/Prague`, `concurrencyPolicy: Forbid`, `backoffLimit: 2`, history 7/3, `envFrom` both secrets. Image `jalalhussein/geoecon-pipeline:<date>-<n>` (Docker Hub). |
| — resources | (CronJob) | Explicit (override 512Mi LimitRange): P1 req `1/1Gi` lim `2/2Gi`; P2 req `2/6Gi` lim `4/8Gi`. |
| — securityContext | (CronJob) | runAsNonRoot, uid/gid 1000, drop ALL, seccomp RuntimeDefault, no priv-esc (copied from ACLED). |
| `geoecon-archive` | PVC | 2Gi `nfs-csi`, mounted for the standalone HTML archive. (S3 to `object-store.cloud.muni.cz` later.) |
| `geoecon-db-netpol` | NetworkPolicy | Ingress to `geoecon-db` only from pods labeled `app=geoecon`; mirrors `siem-db-network-policy`. |

**No ingress.** Every object is net-new and `geoecon-*`-prefixed — nothing touches `siem-*`/`acled-*`.
**Image build:** `Dockerfile` on `python:3.12-slim`; P2 bakes BGE-M3 weights; pushed to Docker Hub
(same flow as `siem-*`).

---

## 6. Error handling & observability

Principle: **fail loud, never silently ship hollow content.**

- **Failure isolation:** discovery skips malformed rows (zero rows = valid quiet day); extraction
  per-URL failure → `headline_only`/`failed`; synthesis per-cluster failure → retry once →
  headline-only fallback / hold-flag. No item silently vanishes.
- **Stage failures:** exception → `stage_runs.failed`+error, `runs.failed|partial`, pod exit≠0 → K8s
  retries (`backoffLimit=2`) → resume from last completed stage.
- **Validator gate (non-negotiable):** `validator.py` runs on every record; non-`ok` flags
  (`empty_figures`/`zero_entities`/`lang_mismatch`/`pair_incomplete`/`invalid_json`/`retry_fallback`)
  → record held, flagged, persisted; render reads only `ok`. Truncated responses (`finish_reason=length`)
  → retry once with larger `max_tokens`, else hold.
- **Send failures:** archive HTML still written (durable); `send_status=failed`; run `partial`;
  `seen_events` upserted only after successful send.
- **Observability:** `runs.metrics` JSONB (discovered/extracted/headline_only/failed_fetch/clusters/
  records_ok/records_held-by-flag/tokens/per-stage latency/send status); structured stdout logs; the
  daily email (incl. heartbeat) is the health signal; on terminal failure send an operator alert email
  (reuses `send.py`, configurable).

---

## 7. Testing strategy

1. **Unit tests, per stage, no network:** discovery (GKG parsing + ≥2-theme/region/150-cap), extract
   (mocked fetch success/fallback), cluster (P1 fingerprint; P2 deterministic embedding fixtures), synth
   (prompt assembly + JSON parse + validator, LLM mocked), render (golden-file `archive.html`/`email.html`;
   `en_only` ignores originals; email inline-CSS/~600px/no external assets), send (local `aiosmtpd`),
   state/repo (ephemeral Postgres: schema applies, CRUD, UNIQUE/idempotency, resumability replay).
2. **Eval corpus as synth integration fixtures:** wire the 8 fixtures + `ground_truth.json` gates
   (valid JSON, headline-only refusal, contradiction, must-not-claim, span fidelity) into synth tests.
3. **Dry-run/staging mode:** `--dry-run` (render, no send, throwaway run), `--limit N`, offline
   canned-LLM mode.
4. **Pre-deploy smoke:** full orchestrator locally vs live proxy + ephemeral PG, `--limit 10 --dry-run`;
   then one-shot in-cluster Job (`kubectl create job --from=cronjob/geoecon-daily`) before enabling schedule.
5. **Human-read gate (blueprint authority):** week-1 live outputs human-read; "review first N dailies"
   checkpoint + `kimi-k2.6` contingency trigger.
6. **Implementation discipline:** build follows TDD (failing test per stage before code).

---

## 8. Phasing summary

**Phase 1 (walking skeleton) — exit criteria:** a daily K8s CronJob discovers from live GDELT,
extracts, synthesizes (1 article = 1 cluster) through the validator gate, renders `en_only` dual HTML,
emails the recipient, and persists a fully resumable run in `geoecon-db` — coexisting cleanly with the
SIEM app.

**Phase 2 (differentiators) — exit criteria:** multilingual BGE-M3 clustering, framing-divergence
retention, null-permitted throughline, sharpened seen-store, and `en_plus_original` / `bilingual_rtl`
render modes — all additive over the same schema and orchestrator.

**Deferred:** RSS depth layer, pgvector, keyword/actor discovery, claim-level provenance, S3 archive,
aggressive paywall recovery.
