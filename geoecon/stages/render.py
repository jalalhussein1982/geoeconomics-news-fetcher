from __future__ import annotations
import logging, os, re
from jinja2 import Environment, PackageLoader, select_autoescape
log = logging.getLogger("geoecon.render")

_env = Environment(loader=PackageLoader("geoecon.render", "templates"),
                   autoescape=select_autoescape(["html", "j2"]))


_NUM_RE = re.compile(r"\d[\d.,]*")
_UNIT_ALIASES = {"us$": "usd", "$": "usd", "usd": "usd", "percent": "%", "%": "%"}


def _norm_num(s: str) -> str:
    """Canonical numeric token for dedupe/matching: thousands stripped, decimal as '.'."""
    m = _NUM_RE.search(str(s or ""))
    if not m:
        return ""
    tok = m.group(0).strip(".,")
    if "." in tok and "," in tok:
        if tok.rfind(",") > tok.rfind("."):       # european: comma is the decimal
            tok = tok.replace(".", "").replace(",", ".")
        else:                                     # anglo: comma is thousands
            tok = tok.replace(",", "")
    elif "," in tok:
        if tok.count(",") == 1 and len(tok.split(",")[1]) <= 2:  # decimal comma
            tok = tok.replace(",", ".")
        else:                                                     # thousands
            tok = tok.replace(",", "")
    return tok


def _norm_unit(u: str) -> str:
    u = str(u or "").strip().lower()
    return _UNIT_ALIASES.get(u, u)


def _canon_key(val: str, unit: str) -> tuple:
    """Dedupe key: (normalized number, normalized unit), inferring the unit from
    currency/percent symbols in the value when the unit field is empty — so
    "US$72" (unit "") and "72" (unit "USD") collapse to the same key."""
    num = _norm_num(val)
    u = _norm_unit(unit)
    if not u:
        low = val.lower()
        if "%" in low or "percent" in low:
            u = "%"
        elif "$" in low or "usd" in low:
            u = "usd"
        elif "€" in low or "eur" in low:
            u = "eur"
    return (num, u)


def _ctx_positions(text: str) -> dict:
    """First-occurrence index of each normalized numeric token in *text*."""
    pos: dict[str, int] = {}
    for i, m in enumerate(_NUM_RE.finditer(text or "")):
        pos.setdefault(_norm_num(m.group(0)), i)
    return pos


def format_figures(key_figures, *, context_text: str = "", max_figures: int | None = None) -> str:
    """Render key_figures as a spaced string, de-duped by numeric value, salience-ranked, capped.

    Each figure is formatted as ``value unit`` (with a space) unless the value already ends
    with the unit (e.g. "1.10%" + "%" -> "1.10%"). Figures whose numeric value appears in
    *context_text* (the story prose) sort first, in prose order; the rest follow in extraction
    order. Duplicates sharing a normalized (value, unit) key are dropped. When *max_figures*
    is truthy, at most that many figures are returned. Figures are joined with " · ".
    """
    ctx_pos = _ctx_positions(context_text)
    seen, items = set(), []
    for k in key_figures or []:
        val = str(k.get("value", "")).strip()
        if not val:
            continue
        unit = str(k.get("unit") or "").strip()
        disp = f"{val} {unit}" if unit and not val.lower().endswith(unit.lower()) else val
        key = _canon_key(val, unit)
        if key in seen:
            continue
        seen.add(key)
        items.append((disp, ctx_pos.get(key[0])))  # second element: prose position or None
    referenced = sorted((it for it in items if it[1] is not None), key=lambda it: it[1])
    unreferenced = [it for it in items if it[1] is None]
    ordered = [disp for disp, _ in (referenced + unreferenced)]
    if max_figures:
        ordered = ordered[:max_figures]
    return " · ".join(ordered)


def render_html(records, *, mode="en_only", date_str, summary=None,
                region_display=None, throughline=None, stats=None):
    ctx = {"records": records, "date_str": date_str, "summary": summary,
           "region_display": region_display, "throughline": throughline,
           "stats": stats or {}, "mode": mode}
    archive = _env.get_template("archive.html.j2").render(**ctx)
    email = _env.get_template("email.html.j2").render(**ctx)
    return archive, email


def run(cfg, repo, run_id, date_str, *, stats=None) -> dict:
    from .. import regions, ranking
    grouped = regions.top_per_bucket(
        repo.records_for_buckets(run_id, cfg.rank_by_corroboration), country_of=lambda t: t[1],
        score_of=ranking.make_score_of(cfg), n=cfg.region_cap, active=cfg.buckets)
    os.makedirs(cfg.archive_dir, exist_ok=True)
    newsletters = []
    for bucket in cfg.buckets:
        items = grouped.get(bucket, [])
        if not items:
            continue
        records = []
        for rec, _country, sources, _score in items:
            rec.source_links = sources
            prose = " ".join(x for x in (rec.fields.get("what_happened_en"),
                                         rec.fields.get("geoeconomic_significance_en")) if x)
            rec.figures_text = format_figures(rec.fields.get("key_figures"),
                                              context_text=prose, max_figures=cfg.figures_max)
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
