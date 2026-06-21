from __future__ import annotations
import hashlib, json, logging, math, re
from .. import regions

log = logging.getLogger("geoecon.cluster")

EMBED_PREFIX = "Instruct: Identify news articles reporting the same event\nQuery: "
EMBED_CHARS = 500


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


def embed_text(art) -> str:
    """e5-prefixed 'title + body lead' for one article; '' if it has neither."""
    title = (art.title or "").strip()
    body = (art.body or "").strip()
    if title and body:
        core = f"{title}\n{body[:EMBED_CHARS]}"
    elif title:
        core = title
    elif body:
        core = body[:EMBED_CHARS]
    else:
        return ""
    return EMBED_PREFIX + core


def _l2norm(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


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


def _group_embedding(arts, vecs, threshold):
    """Union-find on cosine >= threshold (any country). Returns (groups, normed_vecs).
    Articles with empty embed_text stay singletons."""
    norms = [_l2norm(v) for v in vecs]
    embeddable = [bool(embed_text(a)) for a in arts]
    uf = _UnionFind(len(arts))
    for i in range(len(arts)):
        if not embeddable[i]:
            continue
        for j in range(i + 1, len(arts)):
            if not embeddable[j]:
                continue
            if sum(x * y for x, y in zip(norms[i], norms[j])) >= threshold:
                uf.union(i, j)
    return uf.groups(), norms


def _group_lexical(arts, threshold):
    """Fallback: same-country + title-token Jaccard >= threshold."""
    toks = [title_tokens(a.title) for a in arts]
    uf = _UnionFind(len(arts))
    for i in range(len(arts)):
        for j in range(i + 1, len(arts)):
            if (arts[i].country_fips == arts[j].country_fips and toks[i] and toks[j]
                    and jaccard(toks[i], toks[j]) >= threshold):
                uf.union(i, j)
    return uf.groups()


def run(cfg, repo, run_id: int, client) -> dict:
    arts = [a for a in repo.articles_for_run(run_id)
            if a.gate_geoeconomic and a.fetch_status != "failed"
            and regions.buckets_for_country(a.country_fips, cfg.buckets)]
    if not arts:
        log.info("cluster: 0 clusters from 0 gated articles")
        return {"clusters": 0}

    texts = [embed_text(a) or " " for a in arts]   # " " placeholder: endpoints reject ""
    vecs = client.embed(cfg.embedding_model, texts)
    norms = None
    if vecs is not None and len(vecs) == len(arts):
        groups, norms = _group_embedding(arts, vecs, cfg.cluster_cosine_threshold)
        mode = "embedding"
    else:
        log.warning("cluster: embeddings unavailable, falling back to lexical merge")
        groups = _group_lexical(arts, cfg.dedup_jaccard)
        mode = "lexical"

    n = 0
    for members in groups.values():
        member_arts = [arts[m] for m in members]
        primary = max(member_arts, key=lambda a: a.gate_score or 0.0)
        langs = sorted({a.source_lang for a in member_arts if a.source_lang})
        fp = fingerprint(primary.title, primary.source_url)
        centroid = None
        if norms is not None:
            dim = len(norms[members[0]])
            centroid = [sum(norms[m][k] for m in members) / len(members) for k in range(dim)]
        cid = repo.insert_cluster(run_id, fp, primary.region, langs, len(member_arts),
                                  country_fips=primary.country_fips,
                                  gate_score=primary.gate_score, centroid=centroid)
        for a in member_arts:
            repo.add_membership(cid, a.article_id)
        n += 1

    max_cluster_size = max((len(m) for m in groups.values()), default=0)
    if arts and max_cluster_size > 0.25 * len(arts):
        log.warning("cluster: largest cluster has %d/%d gated articles (%.0f%%) — "
                    "possible over-merge/chaining at threshold %.3f",
                    max_cluster_size, len(arts),
                    100 * max_cluster_size / len(arts), cfg.cluster_cosine_threshold)
    log.info("cluster: %d clusters from %d gated articles (%s)", n, len(arts), mode)
    return {"clusters": n, "max_cluster_size": max_cluster_size}


# ---------------------------------------------------------------------------
# LLM-based per-country event grouping
# ---------------------------------------------------------------------------

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
