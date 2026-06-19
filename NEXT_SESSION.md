# Next Session — Handoff

**State as of 2026-06-19:** Design + implementation plan are **done and approved**. The next
session's job is to **execute the Phase 1 plan** (build the walking skeleton). Do not redo the design.

Working directory: `/Users/jalalhussein/Desktop/geoeconomics_news_fetcher`

---

## Read these first, in order
1. `docs/superpowers/plans/2026-06-19-geoecon-pipeline-phase1.md` — **the plan to execute** (13 TDD tasks)
2. `docs/superpowers/specs/2026-06-19-geoecon-pipeline-design.md` — the approved design spec
3. `DEPLOYMENT_ENVIRONMENT.md` — infra + how the new app coexists with the existing one
4. `PIPELINE_BLUEPRINT.md` and `schema.sql` — locked behavior + DB schema

## Already decided (don't re-litigate)
- **Approach A:** single Python 3.12 package `geoecon/`, one Docker image, one staged **resumable
  orchestrator**, one K8s CronJob.
- **Walking skeleton first** (Phase 1 ships a daily email end-to-end). Phase 2
  (clustering / framing-divergence / throughline / bilingual render) is a **separate future plan**.
- **Embeddings (Phase 2):** BGE-M3 in-container on CPU — no GPU.
- **Reuse, don't rewrite** — vendor verbatim from `~/Desktop/metacentrum/eval/`:
  - `client.py` (e-INFRA LLM proxy client), `validator.py` (Stage-3 production guard),
  - the `task_a_cov` prompt (defined inside `tasks.py` — extract its text to
    `geoecon/prompts/task_a_cov.txt`),
  - `corpus/` + `ground_truth.json` (eval fixtures for synth tests).
- **Models** = API calls to `https://llm.ai.e-infra.cz/v1` (key env `MAETCENTRUM_KEY`, fallback
  `METACENTRUM_KEY`). Synthesis = `deepseek-v3.2`, prompt `task_a_cov`, temp 0, seed 12345,
  max_tokens 8192. `kimi-k2.6` is the coverage contingency. No GPU.
- **Deploy target:** namespace `hussein-ns` on the CERIT-SC cluster. ALL new objects prefixed
  `geoecon-`; **never touch** the existing `siem-*` / `acled-*` objects. Separate CNPG Postgres
  `geoecon-db`. CronJob `0 7 * * *`, `timeZone: Europe/Prague`.

## Access (verified working this session)
- **SSH frontend:** `ssh skirit.metacentrum.cz` — key `~/.ssh/id_ed25519_metacentrum_2026`, alias
  already in `~/.ssh/config`. (Old MetaCentrum keys were moved to
  `~/.ssh/old_meta_keys_backup_2026-06-19/`.)
- **kubectl:** from skirit, run `KUBECONFIG=~/.kube/kuba-cluster.yaml ~/bin/kubectl <args>`
  (namespace `hussein-ns` is the context default). Confirmed you can create cronjobs, statefulsets,
  PVCs, secrets, services, networkpolicies, ingresses, and CNPG `clusters.postgresql.cnpg.io`.
- **PBS** (`qstat`/`qsub`) is Kerberos-gated and **not used** — the pipeline runs on K8s, not PBS.

## Existing app you must coexist with (in `hussein-ns`)
- `siem-api` (Deployment + ingress `siem-project.com`), `siem-db` (Postgres 17, StatefulSet/CNPG-labelled),
  `acled-daily-update` (CronJob, 06:00 UTC, currently suspended). Namespace quota has large headroom
  (200m CPU / 512Mi used of 20 cores / 40Gi). Details + the 4 care-points in `DEPLOYMENT_ENVIRONMENT.md`.

## Start here
1. This folder is **not yet a git repo**. First action: `git init` + a baseline commit of the
   existing files (`PIPELINE_BLUEPRINT.md`, `schema.sql`, `DEPLOYMENT_ENVIRONMENT.md`, `NEXT_SESSION.md`,
   `docs/`).
2. Ask me whether to execute via **subagent-driven-development** (recommended in the plan) or
   **executing-plans**, then begin with **Task 1**.
3. Build task-by-task with **TDD** and frequent commits (the superpowers skill system is active).
4. DB-touching tests need a local Postgres — the plan documents the
   `docker run ... postgres:17` + `TEST_DATABASE_URL` setup.

## Open choices (only these remain)
- Execution mode: subagent-driven vs inline.
- Cron timezone: defaulted to `Europe/Prague` (flip to UTC if you'd rather match ACLED's 06:00 UTC).
```
