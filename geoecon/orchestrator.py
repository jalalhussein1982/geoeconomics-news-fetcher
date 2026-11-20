from __future__ import annotations
import argparse, logging, os, signal, sys, time, traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .config import Config
from . import alerts

log = logging.getLogger("geoecon.orchestrator")

# The schedule, the delivery target and the operator all live in Prague. UTC-only
# timestamps in alerts put a 23:00Z run on the previous calendar day, which is
# what made 2026-07-30's cleanup mail look like it concerned an unrelated night.
PRAGUE = ZoneInfo("Europe/Prague")

# How far back a starting process looks for a run whose briefs were built but not
# all delivered. Wide enough to cover every retry of one night's Job (and a manual
# re-run the same morning), far too narrow to reach the previous night's run.
RESUME_MAX_AGE_HOURS = 12

# What the pipeline is doing right now — read by the SIGTERM handler so a
# deadline-kill alert can name the hung stage.
_active = {"run_id": None, "stage": None}


def _install_sigterm_alert(cfg, *, clock=time.monotonic):
    """Alert on SIGTERM (deadline kill, node drain, or the Job giving up).

    SystemExit(143) is a BaseException: it passes through both the per-stage
    `except Exception` (no fail_stage row) and main()'s crash handler (no
    duplicate alert), terminating the process with the conventional code.

    The handler CANNOT know why SIGTERM arrived, so it must not guess. It
    previously asserted "most likely the activeDeadlineSeconds hang-guard",
    which on 2026-07-29 was sent for a job that had lived 5m52s against an 8h
    deadline — the reader went looking for a hang that never happened. Report
    elapsed wall-clock instead: that single number distinguishes a deadline kill
    from a backoff-limit collapse, and it is the one fact the handler owns.
    """
    t0 = clock()

    def handler(signum, frame):
        stage = _active.get("stage") or "startup"
        h, rem = divmod(int(clock() - t0), 3600)
        m, s = divmod(rem, 60)
        elapsed = f"{h}h{m:02d}m{s:02d}s"
        alerts.send_alert(
            cfg, f"run killed (SIGTERM) during {stage}",
            f"The pipeline pod received SIGTERM after {elapsed} of wall-clock.\n\n"
            f"run_id:  {_active.get('run_id')}\n"
            f"stage:   {stage}\n"
            f"elapsed: {elapsed}\n\n"
            f"Compare `elapsed` against activeDeadlineSeconds before assuming a hang:\n"
            f"  - close to the deadline -> the hang-guard killed a genuinely stuck run\n"
            f"  - far short of it       -> the Job itself gave up (backoffLimit exhausted\n"
            f"    by EARLIER attempts that died), or a node drain/eviction, or a manual\n"
            f"    delete. In that case this run is a symptom, not the failure.\n\n"
            f"Attempts killed by SIGKILL (OOM / node-pressure eviction) send NO alert at\n"
            f"all and leave their run row in 'running'. Always check for siblings:\n"
            f"  kubectl -n hussein-ns get pods -l app=geoecon\n"
            f"  kubectl -n hussein-ns get jobs -l app=geoecon\n\n"
            f"The run row stays 'running'/'failed'; the morning watchdog will "
            f"also report if nothing completes today.\n")
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, handler)
    return handler

def reconcile_abandoned_runs(cfg, repo, *, now=None, max_age_hours=6) -> int:
    """Close out runs whose process died without unwinding, and alert once.

    An uncatchable SIGKILL — cgroup OOM, kubelet node-pressure eviction, node
    loss — runs no handler at all: no `fail_stage` row, no crash alert, no
    SIGTERM alert. The run row is simply orphaned in 'running' forever. On
    2026-07-29 that hid three consecutive kills (runs 69/70/71); run 62 had been
    sitting orphaned since 2026-07-23 and nobody noticed. Since the dying
    process cannot report, the NEXT process to start has to.

    `max_age_hours` must exceed activeDeadlineSeconds (5.5h) so a pipeline that
    is legitimately still running is never mistaken for an abandoned one.

    Best-effort by contract: never raises, so a reconciliation problem can never
    stop today's pipeline. Returns the number of runs closed out.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    try:
        stale = repo.stale_running_runs(cutoff)
        if not stale:
            return 0
        note = (f"abandoned: still 'running' with no progress since before {cutoff:%Y-%m-%d %H:%M}Z; "
                f"process ended without unwinding (cause not recorded). "
                f"Closed out by reconciliation at {now:%Y-%m-%d %H:%M}Z.")
        for r in stale:
            repo.mark_run_failed(r["run_id"], note)
        listing = "\n".join(
            f"  run {r['run_id']}  started "
            f"{r['started_at'].astimezone(PRAGUE):%Y-%m-%d %H:%M:%S} Prague  "
            f"({r['started_at'].astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}Z)"
            for r in stale)
        log.error("reconciled %d abandoned run(s):\n%s", len(stale), listing)
        days = sorted({r["started_at"].astimezone(PRAGUE).date().isoformat() for r in stale})
        span = days[0] if len(days) == 1 else f"{days[0]}..{days[-1]}"
        alerts.send_alert(
            cfg, f"{len(stale)} abandoned run(s) from {span} closed out "
                 f"— retrospective, no action needed",
            f"This is housekeeping, NOT a new failure. Every run below died before "
            f"the current process even started; they appear here only because "
            f"nothing recorded their death at the time, and they have now been "
            f"closed out. Dates are Prague, matching the 01:00 schedule.\n\n"
            f"{listing}\n\n"
            f"They have been marked 'failed'.\n\n"
            f"WHY YOU ARE HEARING ABOUT THIS ONLY NOW\n"
            f"Reconciliation only touches runs older than {max_age_hours}h, so it can "
            f"never mistake a pipeline that is still legitimately running for a dead "
            f"one. A run killed shortly after 01:00 therefore is not reported until "
            f"the NEXT night's run — roughly 24h later. A same-night kill is instead "
            f"surfaced by the retained pod and by the 07:30 watchdog.\n\n"
            f"WHAT THIS DOES AND DOES NOT TELL YOU\n"
            f"An orphaned 'running' row does not identify a cause. Both of these "
            f"leave one, and only the first sends an alert of its own:\n"
            f"  - SIGTERM (deadline kill, node drain, job giving up) — the handler "
            f"raises SystemExit(143), a BaseException that bypasses the per-stage "
            f"`except Exception`, so fail_stage never runs either.\n"
            f"  - SIGKILL (OOM, node-pressure eviction, node loss) — uncatchable, "
            f"runs no handler at all and is completely silent.\n\n"
            f"It also does NOT mean a brief was missed: a later attempt or the next "
            f"day's run may have delivered normally. Check before worrying:\n"
            f"  SELECT r.run_id, r.status, n.bucket, n.send_status FROM runs r\n"
            f"    LEFT JOIN newsletters n ON n.run_id = r.run_id\n"
            f"   WHERE (r.started_at AT TIME ZONE 'Europe/Prague')::date = 'YYYY-MM-DD';\n\n"
            f"For anything recent, the pod holds the real answer — the job template "
            f"uses restartPolicy: Never, so failed pods are retained (older runs' "
            f"pods will already have been garbage-collected):\n"
            f"  kubectl -n hussein-ns get pods -l app=geoecon\n"
            f"  kubectl -n hussein-ns get pods -l app=geoecon -o jsonpath="
            f"'{{range .items[*]}}{{.metadata.name}}{{\" \"}}"
            f"{{.status.containerStatuses[*].state.terminated.reason}}{{\"\\n\"}}{{end}}'\n",
            prefix=alerts.CLEANUP_PREFIX)
        return len(stale)
    except Exception:
        log.exception("reconciliation failed (continuing — this must never block a run)")
        return 0


def _default_stages(cfg, client, date_str=None):
    from .stages import (discovery, discovery_registry, discovery_search, extract, gate, cluster,
                         synth, summary, render, send)
    from . import regions
    # A resumed run passes the date its briefs were rendered under, so the email
    # subject matches the content even if the retry starts after midnight UTC.
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = {}
    def _discovery(c, r, rid): state["m"] = discovery.run(c, r, rid); return state["m"]
    def _discovery_registry(c, r, rid): return discovery_registry.run(c, r, rid)
    def _discovery_search(c, r, rid): return discovery_search.run(c, r, rid)
    def _extract(c, r, rid):   return extract.run(c, r, rid)
    def _gate(c, r, rid):      return gate.run(c, r, rid, client)
    def _cluster(c, r, rid):   return cluster.run(c, r, rid, client)
    def _synth(c, r, rid):     return synth.run(c, r, rid, client)
    def _summary(c, r, rid):   return summary.run(c, r, rid, client)
    def _render(c, r, rid):
        out = render.run(c, r, rid, date_str, stats=state.get("m"))
        return {"record_count": out["record_count"], "newsletters": out["newsletters"]}
    def _send(c, r, rid):
        if c.dry_run:
            log.info("dry-run: skipping send"); return {"send_status": "skipped"}
        newsletters = r.newsletters_for_run(rid)
        if not newsletters:
            return {"send_status": "skipped_empty"}
        def subject_for(bucket):
            display = regions.BUCKET_DISPLAY.get(bucket, bucket or "")
            return f"Geoeconomic Brief — {display} — {date_str}"
        out = send.send_pending(c, r, newsletters, subject_for)
        if out["all_sent"]:
            r.mark_emitted(rid)
            return {"send_status": "sent", "sent": out["results"]}
        raise RuntimeError(f"send: some regions failed to send: {out['results']}")
    stages = [("discovery", _discovery), ("discovery_registry", _discovery_registry)]
    if cfg.search_discovery:
        stages.append(("discovery_search", _discovery_search))
    stages += [("extraction", _extract), ("gate", _gate), ("cluster", _cluster),
               ("synthesis", _synth), ("summary", _summary), ("render", _render),
               ("send", _send)]
    return stages

def run_pipeline(cfg: Config, repo, *, now=None, stages=None, client=None) -> dict:
    now = now or datetime.now(timezone.utc)
    window_end = now
    window_start = now - timedelta(hours=cfg.window_hours)
    # If an earlier attempt built the briefs but could not deliver all of them,
    # adopt that run: its completed stages are skipped below and `send` re-sends
    # only the unsent regions. On 2026-11-06 the retry instead started from scratch,
    # crashed in extraction, and a finished brief sat undelivered in the database.
    # A dry run never adopts a real run — it would mark it sent without sending.
    resumed = None if cfg.dry_run else repo.find_unsent_run(
        now - timedelta(hours=RESUME_MAX_AGE_HOURS))
    date_str = None
    if resumed:
        run_id = resumed["run_id"]
        date_str = resumed["scheduled_for"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        log.warning("resuming run %s: briefs already built, re-sending unsent regions only",
                    run_id)
    else:
        run_id = repo.create_run(window_start, window_end, now,
                                 cfg.synthesis_model, cfg.synthesis_prompt_id, cfg.embedding_model)
    _active["run_id"] = run_id
    done = repo.completed_stages(run_id)
    stages = stages if stages is not None else _default_stages(cfg, client, date_str)
    metrics = {}
    for name, fn in stages:
        _active["stage"] = name
        if name in done:
            log.info("resume: skipping completed stage %s", name); continue
        repo.start_stage(run_id, name)
        try:
            result = fn(cfg, repo, run_id) or {}
        except Exception as e:
            repo.fail_stage(run_id, name, e)
            repo.set_run_status(run_id, "failed")
            log.exception("stage %s failed", name)
            e.geoecon_ctx = {"run_id": run_id, "stage": name}
            raise
        repo.complete_stage(run_id, name, result)
        if isinstance(result, dict):
            metrics.update(result)
    repo.merge_run_metrics(run_id, metrics)
    repo.set_run_status(run_id, "completed")
    return {"run_id": run_id, "metrics": metrics}

def _bootstrap(env):
    """Config + DB (with migrations) + LLM client for a pipeline run."""
    cfg = Config.from_env(env)
    from .state import db
    from .state.repo import Repo
    from .llm.client import LLMClient
    conn = db.connect(cfg.db_dsn); db.apply_migrations(conn)
    return cfg, Repo(conn), LLMClient(cfg.llm_base_url, cfg.llm_api_key,
                                      systemic_threshold=cfg.llm_systemic_threshold)

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="geoecon")
    p.add_argument("command", nargs="?", default="run")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    env = dict(os.environ)
    if args.dry_run: env["DRY_RUN"] = "true"
    if args.limit: env["LIMIT"] = str(args.limit)
    if args.command == "watchdog":
        from . import watchdog
        from .state import db
        from .state.repo import Repo
        cfg = Config.from_env(env)
        try:
            conn = db.connect(cfg.db_dsn)
        except Exception as e:
            log.exception("watchdog: DB connect failed")
            alerts.send_alert(cfg, "watchdog could not reach DB",
                              f"DB connect failed: {e!r}\n"
                              f"Pipeline state unknown — check the cluster and DB pod.\n")
            return 1
        return watchdog.run(cfg, Repo(conn))
    cfg, repo, client = _bootstrap(env)
    _install_sigterm_alert(cfg)
    # Report any run orphaned by a previous process that was SIGKILLed. Runs
    # before the pipeline so the alert goes out even if today's run then fails.
    reconcile_abandoned_runs(cfg, repo)
    try:
        out = run_pipeline(cfg, repo, client=client)
        log.info("pipeline done: %s", out)
        return 0
    except Exception as e:
        # Log before exiting — a bare `return 1` made a CheckViolation in
        # start_stage (outside the per-stage handler) a silent exit-1 with no trace.
        log.exception("pipeline failed")
        ctx = getattr(e, "geoecon_ctx", {})
        stage = ctx.get("stage", "unknown")
        alerts.send_alert(
            cfg, f"pipeline failed at stage {stage}",
            f"Daily pipeline failed.\n\n"
            f"stage:  {stage}\n"
            f"run_id: {ctx.get('run_id', 'unknown')}\n"
            f"error:  {e!r}\n\n"
            f"{traceback.format_exc()}\n"
            f"Logs: kubectl -n hussein-ns logs job/$(kubectl -n hussein-ns get jobs "
            f"-l app=geoecon --sort-by=.metadata.creationTimestamp -o name | tail -1)\n")
        return 1

if __name__ == "__main__":
    sys.exit(main())
