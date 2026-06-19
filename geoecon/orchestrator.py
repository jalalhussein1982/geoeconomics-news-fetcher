from __future__ import annotations
import argparse, logging, os, sys
from datetime import datetime, timedelta, timezone
from .config import Config

log = logging.getLogger("geoecon.orchestrator")

def _default_stages(cfg, client):
    from .stages import discovery, extract, cluster, synth, render, send
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = {}
    def _discovery(c, r, rid): state["m"] = discovery.run(c, r, rid); return state["m"]
    def _extract(c, r, rid):  return extract.run(c, r, rid)
    def _cluster(c, r, rid):  return cluster.run(c, r, rid)
    def _synth(c, r, rid):    return synth.run(c, r, rid, client)
    def _render(c, r, rid):
        out = render.run(c, r, rid, date_str, stats=state.get("m"))
        state["render"] = out; return {"record_count": out["record_count"]}
    def _send(c, r, rid):
        out = state.get("render") or r.get_newsletter(rid)
        if out is None:
            raise RuntimeError("send: render stage completed but no newsletter row found")
        if c.dry_run:
            log.info("dry-run: skipping send"); return {"send_status": "skipped"}
        if out["record_count"] == 0 and not c.send_heartbeat:
            return {"send_status": "skipped_empty"}
        subject = f"Geoeconomic Brief — {date_str}"
        result = send.run(c, r, out["newsletter_id"], subject, out["email_html"])
        if result.get("send_status") == "sent":
            r.mark_emitted(rid)
        return result
    return [("discovery", _discovery), ("extraction", _extract), ("cluster", _cluster),
            ("synthesis", _synth), ("render", _render), ("send", _send)]

def run_pipeline(cfg: Config, repo, *, now=None, stages=None, client=None) -> dict:
    now = now or datetime.now(timezone.utc)
    window_end = now
    window_start = now - timedelta(hours=cfg.window_hours)
    run_id = repo.find_resumable_run(window_start, window_end)
    if run_id is None:
        run_id = repo.create_run(window_start, window_end, now,
                                 cfg.synthesis_model, cfg.synthesis_prompt_id, cfg.embedding_model)
    done = repo.completed_stages(run_id)
    stages = stages if stages is not None else _default_stages(cfg, client)
    metrics = {}
    for name, fn in stages:
        if name in done:
            log.info("resume: skipping completed stage %s", name); continue
        repo.start_stage(run_id, name)
        try:
            result = fn(cfg, repo, run_id) or {}
        except Exception as e:
            repo.fail_stage(run_id, name, e)
            repo.set_run_status(run_id, "failed")
            log.exception("stage %s failed", name)
            raise
        repo.complete_stage(run_id, name, result)
        if isinstance(result, dict):
            metrics.update(result)
    repo.merge_run_metrics(run_id, metrics)
    repo.set_run_status(run_id, "completed")
    return {"run_id": run_id, "metrics": metrics}

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
    cfg = Config.from_env(env)
    from .state import db
    from .state.repo import Repo
    from .llm.client import LLMClient
    conn = db.connect(cfg.db_dsn); db.apply_migrations(conn)
    repo = Repo(conn)
    client = LLMClient(cfg.llm_base_url, cfg.llm_api_key)
    try:
        out = run_pipeline(cfg, repo, client=client)
        log.info("pipeline done: %s", out)
        return 0
    except Exception:
        return 1

if __name__ == "__main__":
    sys.exit(main())
