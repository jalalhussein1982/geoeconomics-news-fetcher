from __future__ import annotations
import logging, os
from jinja2 import Environment, PackageLoader, select_autoescape
log = logging.getLogger("geoecon.render")

_env = Environment(loader=PackageLoader("geoecon.render", "templates"),
                   autoescape=select_autoescape(["html", "j2"]))

def render_html(records, *, mode="en_only", date_str, throughline=None, stats=None):
    ctx = {"records": records, "date_str": date_str, "throughline": throughline,
           "stats": stats or {}, "mode": mode}
    archive = _env.get_template("archive.html.j2").render(**ctx)
    email = _env.get_template("email.html.j2").render(**ctx)
    return archive, email

def run(cfg, repo, run_id, date_str, *, stats=None) -> dict:
    records = repo.ok_records(run_id)
    archive, email = render_html(records, mode="en_only", date_str=date_str, stats=stats)
    os.makedirs(cfg.archive_dir, exist_ok=True)
    archive_path = os.path.join(cfg.archive_dir, f"geoecon-{date_str}.html")
    with open(archive_path, "w", encoding="utf-8") as fh:
        fh.write(archive)
    nid = repo.insert_newsletter(run_id, "en_only", archive_path, email, len(records))
    log.info("render: %d records -> %s", len(records), archive_path)
    return {"newsletter_id": nid, "record_count": len(records),
            "email_html": email, "archive_path": archive_path}
