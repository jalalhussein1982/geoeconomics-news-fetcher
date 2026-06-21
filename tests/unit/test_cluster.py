import re
from geoecon.stages import cluster as cluster_stage
from geoecon.state.models import Article


def _art(aid, title, country="SA", geo=True, score=0.5):
    return Article(aid, f"http://x/{aid}", f"h{aid}", "d", "MENA", [], None, None,
                   None, title, "body", "extracted", country,
                   gate_geoeconomic=geo, gate_score=score)


class FakeRepo:
    def __init__(self, arts):
        self._arts = arts; self.clusters = []; self.memberships = []
    def articles_for_run(self, rid, fetch_status=None): return self._arts
    def insert_cluster(self, run_id, fp, region, langs, count,
                       framing_divergent=False, country_fips=None, gate_score=None):
        cid = len(self.clusters) + 1
        self.clusters.append(dict(cid=cid, count=count, country_fips=country_fips,
                                  gate_score=gate_score)); return cid
    def add_membership(self, cid, aid, frame_label=None):
        self.memberships.append((cid, aid))


_CFG = type("C", (), {"buckets": ("middle_east", "north_africa", "latam"),
                      "dedup_jaccard": 0.6})()


def test_jaccard_and_tokens():
    a = cluster_stage.title_tokens("Saudi non-oil exports rise 10.7 percent")
    b = cluster_stage.title_tokens("Saudi non-oil exports rise 10.7%, driven by chemicals")
    assert cluster_stage.jaccard(a, b) >= 0.6


def test_cluster_merges_near_dups_and_filters_gate():
    arts = [
        _art(1, "Saudi non-oil exports rise 10.7 percent", score=0.9),
        _art(2, "Saudi non-oil exports rise 10.7%, driven by chemicals", score=0.7),
        _art(3, "Egypt pound rallies below 50 per dollar", country="EG", score=0.8),
        _art(4, "Bitcoin ETFs shed billions", geo=False),  # dropped by gate
    ]
    repo = FakeRepo(arts)
    out = cluster_stage.run(_CFG, repo, 1)
    # art 4 excluded (geo False); arts 1&2 merge -> one cluster; art 3 own cluster
    assert out["clusters"] == 2
    sizes = sorted(c["count"] for c in repo.clusters)
    assert sizes == [1, 2]
    merged = [c for c in repo.clusters if c["count"] == 2][0]
    assert merged["country_fips"] == "SA" and abs(merged["gate_score"] - 0.9) < 1e-6
