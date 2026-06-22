from __future__ import annotations
import hashlib, json, logging, re
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


class _UnionFind:
    def __init__(self, n): self.parent = list(range(n))
    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]; x = self.parent[x]
        return x
    def union(self, i, j): self.parent[self.find(i)] = self.find(j)
    def groups(self):
        g: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            g.setdefault(self.find(i), []).append(i)
        return g


def _lead(art, n: int = 200) -> str:
    return (art.body or "")[:n]


_OBJ_RE = re.compile(r"\{.*\}", re.S)
_ARR_RE = re.compile(r"\[.*\]", re.S)


def _parse_groups(text, n):
    """Parse the LLM reply into a validated partition of range(n), or None."""
    if not text:
        return None
    raw = None
    for candidate in (text,
                      (_OBJ_RE.search(text).group(0) if _OBJ_RE.search(text) else None),
                      (_ARR_RE.search(text).group(0) if _ARR_RE.search(text) else None)):
        if candidate is None:
            continue
        try:
            raw = json.loads(candidate)
            break
        except json.JSONDecodeError:
            raw = None
    if isinstance(raw, dict):
        raw = raw.get("groups")
    if not isinstance(raw, list):
        return None
    groups, seen = [], set()
    for g in raw:
        if not isinstance(g, list):
            return None
        members = []
        for x in g:
            if not isinstance(x, int) or x < 0 or x >= n or x in seen:
                return None
            seen.add(x); members.append(x)
        if members:
            groups.append(members)
    if len(seen) != n:
        return None
    return groups


def group_events(client, model, country, articles, *, prompt=None):
    """Partition one country's articles into event groups via the LLM.
    Returns a list of Article groups, or None on failure (caller falls back)."""
    if prompt is None:
        from importlib.resources import files
        prompt = files("geoecon.prompts").joinpath("event_grouping.txt").read_text("utf-8")
    system = prompt.replace("{COUNTRY}", country or "this region")
    parts = [f"[{i}] {a.title or '(no title)'}\n{_lead(a)}" for i, a in enumerate(articles)]
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "\n\n".join(parts)}]
    for _ in range(2):
        resp = client.complete(model, messages, temperature=0, seed=12345, max_tokens=2048)
        groups = _parse_groups(getattr(resp, "final_answer", "") or "", len(articles)) if resp.ok() else None
        if groups is not None:
            return [[articles[i] for i in g] for g in groups]
    return None


def _lexical_groups(arts, threshold):
    """Per-country fallback: title-token Jaccard union-find -> list of Article groups."""
    toks = [title_tokens(a.title) for a in arts]
    uf = _UnionFind(len(arts))
    for i in range(len(arts)):
        for j in range(i + 1, len(arts)):
            if toks[i] and toks[j] and jaccard(toks[i], toks[j]) >= threshold:
                uf.union(i, j)
    return [[arts[m] for m in members] for members in uf.groups().values()]


def run(cfg, repo, run_id: int, client) -> dict:
    arts = [a for a in repo.articles_for_run(run_id)
            if a.gate_geoeconomic and a.fetch_status != "failed"
            and regions.buckets_for_country(a.country_fips, cfg.buckets)]
    if not arts:
        log.info("cluster: 0 clusters from 0 gated articles")
        return {"clusters": 0, "max_cluster_size": 0}

    by_country: dict = {}
    for a in arts:
        by_country.setdefault(a.country_fips, []).append(a)

    groups: list[list] = []
    for country, c_arts in by_country.items():
        if len(c_arts) == 1:
            groups.append(c_arts); continue
        g = group_events(client, cfg.dedup_model, country, c_arts)
        if g is None:
            log.warning("cluster[%s]: LLM grouping failed, lexical fallback", country)
            g = _lexical_groups(c_arts, cfg.dedup_jaccard)
        groups.extend(g)

    n = 0
    for member_arts in groups:
        primary = max(member_arts, key=lambda a: a.gate_score or 0.0)
        langs = sorted({a.source_lang for a in member_arts if a.source_lang})
        fp = fingerprint(primary.title, primary.source_url)
        # Provenance aggregates over members (Change 2): populated, not yet used
        # for ranking. None of this changes the primary, which stays max gate_score.
        tiers = [a.source_tier for a in member_arts if a.source_tier is not None]
        cid = repo.insert_cluster(
            run_id, fp, primary.region, langs, len(member_arts),
            country_fips=primary.country_fips, gate_score=primary.gate_score,
            tier_min=min(tiers) if tiers else None,
            has_primary=any(a.source_tier == 0 for a in member_arts),
            distinct_state_alignments=len({a.state_alignment for a in member_arts if a.state_alignment}),
            source_count=len(member_arts),
        )
        for a in member_arts:
            repo.add_membership(cid, a.article_id)
        n += 1

    max_cluster_size = max((len(g) for g in groups), default=0)
    if arts and max_cluster_size > 0.25 * len(arts):
        log.warning("cluster: largest cluster has %d/%d gated articles (%.0f%%) — verify it is one event",
                    max_cluster_size, len(arts), 100 * max_cluster_size / len(arts))
    log.info("cluster: %d clusters from %d gated articles", n, len(arts))
    return {"clusters": n, "max_cluster_size": max_cluster_size}
