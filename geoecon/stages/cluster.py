from __future__ import annotations
import hashlib, logging, re
from .. import regions

log = logging.getLogger("geoecon.cluster")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def fingerprint(title: str | None, canonical_url: str | None) -> str:
    basis = _norm(title) if title else (canonical_url or "")
    return "p1:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()


def title_tokens(title: str | None) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(t) >= 3}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def run(cfg, repo, run_id: int) -> dict:
    arts = [a for a in repo.articles_for_run(run_id)
            if a.gate_geoeconomic and a.fetch_status != "failed"
            and regions.buckets_for_country(a.country_fips, cfg.buckets)]

    # Union-find near-dup merge: same country + title-token Jaccard >= threshold.
    parent = list(range(len(arts)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    def union(i, j):
        parent[find(i)] = find(j)

    toks = [title_tokens(a.title) for a in arts]
    for i in range(len(arts)):
        for j in range(i + 1, len(arts)):
            if (arts[i].country_fips == arts[j].country_fips and toks[i] and toks[j]
                    and jaccard(toks[i], toks[j]) >= cfg.dedup_jaccard):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(arts)):
        groups.setdefault(find(i), []).append(i)

    n = 0
    for members in groups.values():
        member_arts = [arts[m] for m in members]
        primary = max(member_arts, key=lambda a: a.gate_score or 0.0)
        langs = sorted({a.source_lang for a in member_arts if a.source_lang})
        fp = fingerprint(primary.title, primary.source_url)
        cid = repo.insert_cluster(run_id, fp, primary.region, langs, len(member_arts),
                                  country_fips=primary.country_fips,
                                  gate_score=primary.gate_score)
        for a in member_arts:
            repo.add_membership(cid, a.article_id)
        n += 1

    log.info("cluster: %d clusters from %d gated articles", n, len(arts))
    return {"clusters": n}
