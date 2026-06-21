from __future__ import annotations
import logging, os
from jinja2 import Environment, PackageLoader, select_autoescape
log = logging.getLogger("geoecon.render")

_env = Environment(loader=PackageLoader("geoecon.render", "templates"),
                   autoescape=select_autoescape(["html", "j2"]))


def format_figures(key_figures) -> str:
    """Render key_figures as a spaced, de-duped string (no gloss_en).

    Each figure is formatted as ``value unit`` (with a space), unless the
    value already ends with the unit (e.g. "1.10%" + "%" -> "1.10%").
    Duplicate display strings are silently dropped.
    Figures are joined with " · ".
    """
    seen, out = set(), []
    for k in key_figures or []:
        val = str(k.get("value", "")).strip()
        if not val:
            continue
        unit = str(k.get("unit") or "").strip()
        disp = f"{val} {unit}" if unit and not val.lower().endswith(unit.lower()) else val
        if disp not in seen:
            seen.add(disp); out.append(disp)
    return " · ".join(out)


def render_html(records, *, mode="en_only", date_str, summary=None,
                region_display=None, throughline=None, stats=None):
    ctx = {"records": records, "date_str": date_str, "summary": summary,
           "region_display": region_display, "throughline": throughline,
           "stats": stats or {}, "mode": mode}
    archive = _env.get_template("archive.html.j2").render(**ctx)
    email = _env.get_template("email.html.j2").render(**ctx)
    return archive, email


def run(cfg, repo, run_id, date_str, *, stats=None) -> dict:
    from .. import regions
    grouped = regions.top_per_bucket(
        repo.records_for_buckets(run_id), country_of=lambda t: t[1],
        score_of=lambda t: t[3] or 0.0, n=cfg.region_cap, active=cfg.buckets)
    os.makedirs(cfg.archive_dir, exist_ok=True)
    newsletters = []
    for bucket in cfg.buckets:
        items = grouped.get(bucket, [])
        if not items:
            continue
        records = []
        for rec, _country, sources, _score in items:
            rec.source_links = sources
            rec.figures_text = format_figures(rec.fields.get("key_figures"))
            records.append(rec)
        summary = repo.get_region_summary(run_id, bucket)
        region_display = regions.BUCKET_DISPLAY[bucket]
        archive, email = render_html(records, mode="en_only", date_str=date_str,
                                     summary=summary, region_display=region_display, stats=stats)
        path = os.path.join(cfg.archive_dir, f"geoecon-{date_str}-{bucket}.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(archive)
        nid = repo.insert_newsletter(run_id, "en_only", path, email, len(records), bucket=bucket)
        newsletters.append({"newsletter_id": nid, "bucket": bucket, "record_count": len(records)})
        log.info("render[%s]: %d records -> %s", bucket, len(records), path)
    return {"newsletters": newsletters,
            "record_count": sum(n["record_count"] for n in newsletters)}
