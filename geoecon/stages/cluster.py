from __future__ import annotations
import hashlib, logging, re

log = logging.getLogger("geoecon.cluster")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def fingerprint(title: str | None, canonical_url: str | None) -> str:
    basis = _norm(title) if title else (canonical_url or "")
    return "p1:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()


def run(cfg, repo, run_id: int) -> dict:
    n = 0
    for art in repo.articles_for_run(run_id):
        if art.fetch_status == "failed":
            continue
        fp = fingerprint(art.title, art.source_url)
        langs = [art.source_lang] if art.source_lang else []
        cid = repo.insert_cluster(run_id, fp, art.region, langs, 1,
                                  country_fips=art.country_fips)
        repo.add_membership(cid, art.article_id)
        n += 1
    log.info("cluster(P1): %d clusters", n)
    return {"clusters": n}
