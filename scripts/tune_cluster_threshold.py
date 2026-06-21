"""Sweep cluster_cosine_threshold over one completed run's gated articles.

Usage (inside the cluster, where the DB + MAETCENTRUM_KEY are reachable):
  python -m scripts.tune_cluster_threshold <run_id>

Prints, for each candidate threshold, the resulting cluster count and the
largest merged groups (by title) so a human can pick the value that collapses
known duplicates without chaining distinct events. Reuses the already-tested
cluster helpers (embed_text, _group_embedding) and LLMClient.embed.
"""
import os
import sys

from geoecon import regions
from geoecon.config import Config
from geoecon.llm.client import LLMClient
from geoecon.stages import cluster
from geoecon.state import db
from geoecon.state.repo import Repo


def main(run_id: int):
    cfg = Config.from_env(dict(os.environ))
    conn = db.connect(cfg.db_dsn)
    repo = Repo(conn)
    client = LLMClient(cfg.llm_base_url, cfg.llm_api_key)
    arts = [a for a in repo.articles_for_run(run_id)
            if a.gate_geoeconomic and a.fetch_status != "failed"
            and regions.buckets_for_country(a.country_fips, cfg.buckets)]
    texts = [cluster.embed_text(a) or " " for a in arts]
    vecs = client.embed(cfg.embedding_model, texts)
    if vecs is None:
        print("embed failed"); return
    print(f"gated articles: {len(arts)}  (model {cfg.embedding_model})")
    # e5 cosine baseline is high (~0.93 all-pairwise); low thresholds chain everything
    # into one cluster. Clean event separation appears near ~0.98.
    for thr in (0.90, 0.94, 0.96, 0.97, 0.975, 0.98, 0.985, 0.99):
        groups, _ = cluster._group_embedding(arts, vecs, thr)
        merged = sorted((g for g in groups.values() if len(g) > 1),
                        key=len, reverse=True)
        print(f"\n=== threshold {thr}: {len(groups)} clusters, {len(merged)} merged groups ===")
        for g in merged[:8]:
            print(f"  [{len(g)}] " + " || ".join((arts[m].title or "?")[:55] for m in g[:6]))


if __name__ == "__main__":
    main(int(sys.argv[1]))
