import re
import math
from geoecon.stages import cluster as cluster_stage
from geoecon.state.models import Article


def _art(aid, title, country="SA", geo=True, score=0.5, body="body"):
    return Article(aid, f"http://x/{aid}", f"h{aid}", "d", "MENA", [], None, None,
                   "en", title, body, "extracted", country,
                   gate_geoeconomic=geo, gate_score=score)


class FakeRepo:
    def __init__(self, arts):
        self._arts = arts; self.clusters = []; self.memberships = []
    def articles_for_run(self, rid, fetch_status=None): return self._arts
    def insert_cluster(self, run_id, fp, region, langs, count,
                       framing_divergent=False, country_fips=None, gate_score=None,
                       centroid=None):
        cid = len(self.clusters) + 1
        self.clusters.append(dict(cid=cid, count=count, country_fips=country_fips,
                                  gate_score=gate_score, centroid=centroid)); return cid
    def add_membership(self, cid, aid, frame_label=None):
        self.memberships.append((cid, aid))


class FakeClient:
    """embed() returns a fixed vector list (aligned to article order), or None."""
    def __init__(self, vecs): self._vecs = vecs
    def embed(self, model, texts): return self._vecs


_CFG = type("C", (), {"buckets": ("middle_east", "north_africa", "latam"),
                      "dedup_jaccard": 0.6, "embedding_model": "e5",
                      "cluster_cosine_threshold": 0.85})()


def test_jaccard_and_tokens():
    a = cluster_stage.title_tokens("Saudi non-oil exports rise 10.7 percent")
    b = cluster_stage.title_tokens("Saudi non-oil exports rise 10.7%, driven by chemicals")
    assert cluster_stage.jaccard(a, b) >= 0.6


def test_embedding_merges_same_event_across_countries():
    # A(Egypt) & B(Turkey) describe one event -> near-identical vectors -> MERGE
    # despite different country_fips; C(Brazil) is orthogonal -> separate.
    arts = [_art(1, "Cairo summit on energy", country="EG", score=0.6),
            _art(2, "Foreign ministers meet in Cairo", country="TU", score=0.9),
            _art(3, "Brazil tariff shift", country="BR", score=0.7)]
    vecs = [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]]
    repo = FakeRepo(arts)
    out = cluster_stage.run(_CFG, repo, 1, FakeClient(vecs))
    assert out["clusters"] == 2
    assert out["max_cluster_size"] == 2            # EG+TU merge -> largest cluster = 2
    merged = [c for c in repo.clusters if c["count"] == 2][0]
    assert merged["country_fips"] == "TU"          # primary = max gate_score (0.9)
    assert merged["centroid"] is not None and len(merged["centroid"]) == 2


def test_embedding_failure_falls_back_to_lexical():
    # client.embed returns None -> lexical same-country Jaccard merge path.
    arts = [_art(1, "Saudi non-oil exports rise 10.7 percent", score=0.9),
            _art(2, "Saudi non-oil exports rise 10.7%, driven by chemicals", score=0.7),
            _art(3, "Egypt pound rallies below 50 per dollar", country="EG", score=0.8),
            _art(4, "Bitcoin ETFs shed billions", geo=False)]  # dropped by gate
    repo = FakeRepo(arts)
    out = cluster_stage.run(_CFG, repo, 1, FakeClient(None))
    assert out["clusters"] == 2                     # 1&2 merge; 3 separate; 4 gated out
    sizes = sorted(c["count"] for c in repo.clusters)
    assert sizes == [1, 2]
    merged = [c for c in repo.clusters if c["count"] == 2][0]
    assert merged["country_fips"] == "SA" and abs(merged["gate_score"] - 0.9) < 1e-6
    assert merged["centroid"] is None               # lexical path stores no centroid
