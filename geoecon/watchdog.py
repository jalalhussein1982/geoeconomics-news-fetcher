"""Dead-man's switch: alert if today's briefs were not all delivered.

Runs as `geoecon watchdog` from a small 07:30 Europe/Prague CronJob — catches
the failures in-process hooks never see (job never scheduled, image pull
error, missing secret) and double-covers crashes and deadline-kills.
"""
from __future__ import annotations
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from . import alerts

log = logging.getLogger("geoecon.watchdog")


def local_day_window(now_utc, tz_name):
    """UTC instants for [00:00, 24:00) of `now_utc`'s date in tz_name."""
    tz = ZoneInfo(tz_name)
    start = datetime.combine(now_utc.astimezone(tz).date(), time(0), tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _attempts(repo, runs, tz_name) -> str:
    """One line per run of the day, so the reader sees every attempt at once."""
    tz = ZoneInfo(tz_name)
    lines = []
    for r in runs:
        fails = repo.failed_stages(r["run_id"])
        detail = "; ".join(f"{f['stage_name']}: {f['error']}" for f in fails) \
                 or "(no failed stage recorded — hung, killed or crashed)"
        lines.append(f"  run {r['run_id']}  {r['status']:<9}  started "
                     f"{r['started_at'].astimezone(tz):%H:%M}  {detail}")
    return (f"Today's attempts ({tz_name} time):\n" + "\n".join(lines) + "\n"
            "A 'running' row is not proof that anything is still running: an attempt "
            "that crashed or was killed leaves one behind.\n"
            "Check: kubectl -n hussein-ns get pods -l app=geoecon\n")


def check(repo, day_start, day_end, tz_name="Europe/Prague") -> dict:
    """Pure decision. Returns {'ok': bool, 'subject': str|None, 'body': str|None}.

    Judges the whole day, not the newest run row. On 2026-11-06 an earlier
    attempt had delivered 2 of 3 briefs, but the newest row was a retry that had
    segfaulted and stayed 'running' — so the alert read "no completed run today
    (latest: running)" when nothing was running and two briefs had arrived.
    """
    runs = repo.runs_for_window(day_start, day_end)
    if not runs:
        return {"ok": False, "subject": "no pipeline run today",
                "body": ("No run row exists for today — the daily job never "
                         "started (image pull error, missing secret, scheduling "
                         "problem, cluster issue) OR failed during startup before "
                         "creating a run row (config/DB error) — check the job logs.\n"
                         "Check: kubectl -n hussein-ns get cronjob,jobs,pods\n")}
    briefs = {r["run_id"]: repo.newsletters_for_run(r["run_id"]) for r in runs}
    sent = {rid: [n for n in nls if n.get("send_status") == "sent"]
            for rid, nls in briefs.items()}
    # Delivered in full by any attempt -> the reader has today's briefs.
    if any(briefs[rid] and len(sent[rid]) == len(briefs[rid]) for rid in briefs):
        return {"ok": True, "subject": None, "body": None}

    best = max(runs, key=lambda r: (len(sent[r["run_id"]]), r["run_id"]))
    rid = best["run_id"]
    if sent[rid]:
        from .orchestrator import RESUME_MAX_AGE_HOURS
        unsent = [n for n in briefs[rid] if n.get("send_status") != "sent"]
        missing = ", ".join(str(n.get("bucket")) for n in unsent)
        table = "\n".join(
            f"  {str(n.get('bucket')):<14} {n.get('send_status'):<8} {n.get('send_error') or ''}".rstrip()
            for n in briefs[rid])
        resume_by = (best["started_at"] + timedelta(hours=RESUME_MAX_AGE_HOURS)) \
            .astimezone(ZoneInfo(tz_name))
        return {"ok": False,
                "subject": f"only {len(sent[rid])} of {len(briefs[rid])} briefs sent today "
                           f"(missing: {missing})",
                "body": (f"Run {rid} built {len(briefs[rid])} briefs but delivered only "
                         f"{len(sent[rid])}:\n{table}\n\n"
                         f"The missing brief is built and stored. Starting one more attempt "
                         f"before {resume_by:%H:%M} {tz_name} time re-sends only what is "
                         f"missing and rebuilds nothing:\n"
                         f"  kubectl -n hussein-ns create job --from=cronjob/geoecon-daily "
                         f"geoecon-resend-{rid}\n\n"
                         + _attempts(repo, runs, tz_name))}

    completed = [r for r in runs if r["status"] == "completed"]
    if completed:
        run = completed[-1]
        return {"ok": False, "subject": "pipeline ran but sent no newsletter",
                "body": (f"Run {run['run_id']} completed but no region newsletter was "
                         f"sent (likely zero records survived gating/synthesis).\n"
                         f"started:  {run['started_at']}\n"
                         f"finished: {run['finished_at']}\n")}
    latest = runs[-1]
    return {"ok": False, "subject": f"no completed run today (latest: {latest['status']})",
            "body": (f"No brief was sent today. {len(runs)} attempt(s) were made; the "
                     f"latest, run {latest['run_id']}, has status={latest['status']}.\n\n"
                     + _attempts(repo, runs, tz_name) +
                     f"Logs: kubectl -n hussein-ns logs job/$(kubectl -n hussein-ns "
                     f"get jobs -l app=geoecon "
                     f"--sort-by=.metadata.creationTimestamp -o name | tail -1)\n")}


def run(cfg, repo, *, now=None) -> int:
    now = now or datetime.now(timezone.utc)
    day_start, day_end = local_day_window(now, cfg.timezone)
    verdict = check(repo, day_start, day_end, cfg.timezone)
    if verdict["ok"]:
        log.info("watchdog: today's run completed and sent — OK")
        return 0
    log.warning("watchdog: %s", verdict["subject"])
    # Alert delivered == the watchdog did its job -> exit 0. If the alert email
    # itself fails to send (e.g. SMTP down at the same time the pipeline is
    # unhealthy), exit 1 so the Job shows up as failed in `kubectl get jobs`
    # history — the cheap second signal when the email channel is also dead.
    if alerts.send_alert(cfg, "watchdog: " + verdict["subject"], verdict["body"]):
        return 0
    log.error("watchdog: alert send FAILED for %r — exiting 1", verdict["subject"])
    return 1
