# Failure Alert Layer — Design

**Date:** 2026-07-08
**Status:** Approved

## Problem

The daily pipeline (CronJob `geoecon-daily`, 07:00 Europe/Prague) fails silently.
When a stage crashes, a run hangs past the 4h `activeDeadlineSeconds`, or the job
never gets scheduled at all, the only symptom is a missing newsletter — discovered
hours later by the recipient. The 2026-07-07 kimi-k2.6 incident (runs stalled
9–18h, daily brief missed) is the motivating example.

## Goal

Any day the newsletter does not arrive, an email explaining why lands in the same
inboxes within hours (usually minutes) of the failure.

## Decisions (with user)

- **Coverage:** in-process crash alerts + a watchdog CronJob (dead-man's switch).
  External services (healthchecks.io) rejected — no new dependencies.
- **Recipients:** same `RECIPIENTS` list as the newsletter. No new recipient config.
- **Channel:** email via the existing SMTP config. Accepted limitation: if SMTP
  itself is down, both newsletter and alerts fail. The watchdog narrows the window
  (SMTP must be down at both ~07:00–11:00 and 12:00 for a failure to stay silent).

## Component 1 — In-process crash alert

New module `geoecon/alerts.py`:

- `send_alert(cfg, subject, body)` — plain-text email using the same SMTP
  settings and transport pattern as `stages/send.py` (starttls + login,
  short timeout). Subject prefix: `[GEOECON ALERT]`.
- **Never raises.** Any exception during alerting is logged and swallowed so it
  cannot mask or alter the original failure path.

Hook points in `geoecon/orchestrator.py`:

1. **Crash hook** — in `main()`'s existing top-level `except` (currently
   log-and-return-1): build an alert with the failed stage name, run_id (when
   known), the full traceback, and a `kubectl logs` hint; send it; still exit 1.
   `run_pipeline` already records the failed stage via `repo.fail_stage` /
   `set_run_status("failed")` — that behavior is unchanged. To surface stage/run_id
   to `main()`, `run_pipeline`'s per-stage handler attaches them to the raised
   exception (e.g. `e.geoecon_ctx = {"run_id": ..., "stage": ...}`) before re-raising.
2. **SIGTERM hook** — a handler installed at the start of `main()` (pipeline
   entrypoint only, not the watchdog). When k8s kills a hung run at the 4h
   deadline it sends SIGTERM first; the handler sends
   `[GEOECON ALERT] run killed (SIGTERM) — likely exceeded 4h deadline at stage <X>`
   within the grace period, then re-raises/exits so termination proceeds. The
   currently-running stage is tracked in a module-level variable updated by the
   stage loop. False positives (node drain also SIGTERMs) are acceptable — a
   killed run is always worth knowing about.

## Component 2 — Watchdog CronJob (dead-man's switch)

New subcommand `geoecon watchdog` (the CLI already takes a positional `command`;
`main()` dispatches on it). Same image, same secrets.

Logic:

1. Connect to the DB (no migrations applied — read-only intent).
2. Determine "today" as the current date in Europe/Prague.
3. Query for a run scheduled today with `status='completed'`.
   - **Completed run exists** → check its newsletters: if at least one region has
     `send_status='sent'` → healthy, exit 0 silently. If the run completed but
     sent nothing (`skipped_empty`) → alert: "pipeline ran but produced no
     newsletter" (with record counts if available).
   - **No completed run** → alert. Include the latest run's status
     (`running`/`failed`/absent), its failed stage and `stage_runs.error` text
     when present, and "no run row at all — job may never have started" when the
     runs table has nothing for today.
4. If the DB itself is unreachable, that raises → the watchdog job fails; but it
   first attempts an alert ("watchdog could not reach DB") since SMTP needs no DB.

Repo additions (small, read-only): `latest_run_for_day(day_start, day_end)`
returning run row + failed-stage info, or equivalent narrow queries.

Manifest `deploy/watchdog-cronjob.yaml`:

- Schedule `0 12 * * *` Europe/Prague (main run starts 07:00, healthy ≈1h; worst
  case the 4h deadline ends the job by ~11:00).
- Same `envFrom` secrets + DB env as the main CronJob; `args: ["watchdog"]`;
  tiny resources (100m CPU / 128Mi); no archive volume; `activeDeadlineSeconds`
  ~300; `backoffLimit: 1`.

Accepted residual risk: the watchdog is itself a CronJob and could fail silently,
but it is one DB query + one SMTP send — far less failure surface than the
pipeline. No watcher-for-the-watcher.

## Error handling summary

| Failure | Detected by | Latency |
|---|---|---|
| Stage exception (incl. send failure) | crash hook | immediate |
| Hang killed at 4h deadline | SIGTERM hook, watchdog | immediate / by 12:00 |
| Job never scheduled / image pull / secret missing | watchdog | by 12:00 |
| Run completed but nothing sent | watchdog | by 12:00 |
| SMTP down | undetectable by design (accepted) | — |

## Testing

Unit tests follow the existing injectable-`transport` pattern of `stages/send.py`:

- `alerts.send_alert` formats subject/body and never raises when transport blows up.
- Crash hook: a stage that raises produces exactly one alert containing stage
  name + traceback, and exit code stays 1.
- Watchdog decision matrix: completed+sent → no alert; completed+empty → alert;
  failed run → alert with stage error; no run → alert; each against a stubbed repo.

Live validation after deploy:

- One-off manual watchdog Job on a day with a good run → exits 0, no email.
- Forced-failure smoke run (e.g. bogus LLM base URL) → real alert email arrives.

## Out of scope

- Non-email channels (Slack, push).
- Alerting on degraded-but-successful runs (thin coverage, slow stages).
- Retry/self-healing behavior changes — job semantics stay exactly as they are.
