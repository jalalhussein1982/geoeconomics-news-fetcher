"""Dead-man's switch: alert if today's run did not complete and send.

Runs as `geoecon watchdog` from a small 12:00 Europe/Prague CronJob — catches
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


def check(repo, day_start, day_end) -> dict:
    """Pure decision. Returns {'ok': bool, 'subject': str|None, 'body': str|None}."""
    run = repo.latest_run_for_window(day_start, day_end)
    if run is None:
        return {"ok": False, "subject": "no pipeline run today",
                "body": ("No run row exists for today — the daily job never "
                         "started (image pull error, missing secret, scheduling "
                         "problem, cluster issue) OR failed during startup before "
                         "creating a run row (config/DB error) — check the job logs.\n"
                         "Check: kubectl -n hussein-ns get cronjob,jobs,pods\n")}
    if run["status"] == "completed":
        sent = [n for n in repo.newsletters_for_run(run["run_id"])
                if n.get("send_status") == "sent"]
        if sent:
            return {"ok": True, "subject": None, "body": None}
        return {"ok": False, "subject": "pipeline ran but sent no newsletter",
                "body": (f"Run {run['run_id']} completed but no region newsletter was "
                         f"sent (likely zero records survived gating/synthesis).\n"
                         f"started:  {run['started_at']}\n"
                         f"finished: {run['finished_at']}\n")}
    fails = repo.failed_stages(run["run_id"])
    detail = "\n".join(f"  {f['stage_name']}: {f['error']}" for f in fails) \
             or "  (no failed stage recorded — run may be hung or was killed)"
    return {"ok": False, "subject": f"no completed run today (latest: {run['status']})",
            "body": (f"Latest run {run['run_id']} has status={run['status']} "
                     f"(started {run['started_at']}).\n"
                     f"Failed stages:\n{detail}\n"
                     f"Logs: kubectl -n hussein-ns logs job/$(kubectl -n hussein-ns "
                     f"get jobs -l app=geoecon "
                     f"--sort-by=.metadata.creationTimestamp -o name | tail -1)\n")}


def run(cfg, repo, *, now=None) -> int:
    now = now or datetime.now(timezone.utc)
    day_start, day_end = local_day_window(now, cfg.timezone)
    verdict = check(repo, day_start, day_end)
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
