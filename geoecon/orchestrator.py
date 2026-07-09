from __future__ import annotations
import argparse, logging, os, signal, sys, traceback
from datetime import datetime, timedelta, timezone
from .config import Config
from . import alerts

log = logging.getLogger("geoecon.orchestrator")

# What the pipeline is doing right now — read by the SIGTERM handler so a
# deadline-kill alert can name the hung stage.
_active = {"run_id": None, "stage": None}


def _install_sigterm_alert(cfg):
    """Alert on SIGTERM (k8s activeDeadlineSeconds kill or node drain).

    SystemExit(143) is a BaseException: it passes through both the per-stage
    `except Exception` (no fail_stage row) and main()'s crash handler (no
    duplicate alert), terminating the process with the conventional code.
    """
    def handler(signum, frame):
        stage = _active.get("stage") or "startup"
        alerts.send_alert(
            cfg, f"run killed (SIGTERM) during {stage}",
            f"The pipeline pod received SIGTERM — most likely the "
            f"activeDeadlineSeconds hang-guard killing a stuck run "
            f"(or node maintenance).\n\n"
            f"run_id: {_active.get('run_id')}\n"
            f"stage:  {stage}\n\n"
            f"The run row stays 'running'/'failed'; the morning watchdog will "
            f"also report if nothing completes today.\n")
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, handler)
    return handler

def _default_stages(cfg, client):
    from .stages import (discovery, discovery_registry, discovery_search, extract, gate, cluster,
                         synth, summary, render, send)
    from . import regions
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
    run_id = repo.find_resumable_run(window_start, window_end)
    if run_id is None:
        run_id = repo.create_run(window_start, window_end, now,
                                 cfg.synthesis_model, cfg.synthesis_prompt_id, cfg.embedding_model)
    _active["run_id"] = run_id
    done = repo.completed_stages(run_id)
    stages = stages if stages is not None else _default_stages(cfg, client)
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
    return cfg, Repo(conn), LLMClient(cfg.llm_base_url, cfg.llm_api_key)

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
