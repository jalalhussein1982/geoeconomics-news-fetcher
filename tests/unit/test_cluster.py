import re
import math
from geoecon.stages import cluster as cluster_stage
from geoecon.state.models import Article


def _art(aid, title, country="SA", geo=True, score=0.5, body="body",
         source_tier=None, state_alignment=None, registry_id=None):
    return Article(aid, f"http://x/{aid}", f"h{aid}", "d", "MENA", [], None, None,
                   "en", title, body, "extracted", country,
                   gate_geoeconomic=geo, gate_score=score,
                   source_tier=source_tier, state_alignment=state_alignment,
                   registry_id=registry_id)


class FakeRepo:
    def __init__(self, arts):
        self._arts = arts; self.clusters = []; self.memberships = []
    def articles_for_run(self, rid, fetch_status=None): return self._arts
    def insert_cluster(self, run_id, fp, region, langs, count,
                       framing_divergent=False, country_fips=None, gate_score=None,
                       tier_min=None, has_primary=None, distinct_state_alignments=None,
                       source_count=None, corroboration_score=None,
                       has_curated=False, has_economy_section=False):
        cid = len(self.clusters) + 1
        self.clusters.append(dict(cid=cid, count=count, country_fips=country_fips,
                                  gate_score=gate_score, tier_min=tier_min,
                                  has_primary=has_primary,
                                  distinct_state_alignments=distinct_state_alignments,
                                  source_count=source_count,
                                  corroboration_score=corroboration_score,
                                  has_curated=has_curated,
                                  has_economy_section=has_economy_section)); return cid
    def add_membership(self, cid, aid, frame_label=None):
        self.memberships.append((cid, aid))


_CFG = type("C", (), {"buckets": ("middle_east", "north_africa", "latam"),
                      "dedup_jaccard": 0.6, "dedup_model": "m"})()


def test_jaccard_and_tokens():
    a = cluster_stage.title_tokens("Saudi non-oil exports rise 10.7 percent")
    b = cluster_stage.title_tokens("Saudi non-oil exports rise 10.7%, driven by chemicals")
    assert cluster_stage.jaccard(a, b) >= 0.6


def test_run_groups_per_country_via_llm():
    # SA has 2 articles the LLM groups as one event; EG has 1 (singleton, no LLM call).
    arts = [_art(1, "Saudi oil cut", country="SA", score=0.9),
            _art(2, "Saudi reduces output", country="SA", score=0.7),
            _art(3, "Egypt pound rallies", country="EG", score=0.8)]
    llm = _LLM(['{"groups": [[0, 1]]}'])     # one call, for SA (2 arts); EG is a singleton
    repo = FakeRepo(arts)
    out = cluster_stage.run(_CFG, repo, 1, llm)
    assert out["clusters"] == 2 and out["max_cluster_size"] == 2
    assert len(llm.calls) == 1               # only the multi-article country hit the LLM
    merged = [c for c in repo.clusters if c["count"] == 2][0]
    assert merged["country_fips"] == "SA" and abs(merged["gate_score"] - 0.9) < 1e-6


def test_run_populates_cluster_aggregates():
    # A registry primary (Tier-0, SA-aligned) + a wire (Tier-1, unaligned), one event.
    arts = [_art(1, "Saudi raises rates", country="SA", score=0.9, source_tier=0, state_alignment="SA"),
            _art(2, "Saudi central bank hikes", country="SA", score=0.6, source_tier=1, state_alignment=None)]
    llm = _LLM(['{"groups": [[0, 1]]}'])
    repo = FakeRepo(arts)
    cluster_stage.run(_CFG, repo, 1, llm)
    c = repo.clusters[0]
    assert c["source_count"] == 2
    assert c["tier_min"] == 0           # best tier among members
    assert c["has_primary"] is True     # a Tier-0 primary is present
    assert c["distinct_state_alignments"] == 1  # only SA (wire is unaligned/None)
    # primary selection is unchanged — still the max gate_score article
    assert abs(c["gate_score"] - 0.9) < 1e-6


def test_run_aggregates_default_for_gdelt_only_cluster():
    # GDELT-origin articles carry no provenance: aggregates degrade cleanly.
    arts = [_art(1, "Egypt pound", country="EG", score=0.5)]
    repo = FakeRepo(arts)
    cluster_stage.run(_CFG, repo, 1, _LLM([]))
    c = repo.clusters[0]
    assert c["tier_min"] is None and c["has_primary"] is False
    assert c["distinct_state_alignments"] == 0 and c["source_count"] == 1
    assert c["corroboration_score"] is not None  # scored from the (degraded) aggregates


def test_run_falls_back_to_lexical_on_llm_failure():
    arts = [_art(1, "Saudi non-oil exports rise 10.7 percent", country="SA", score=0.9),
            _art(2, "Saudi non-oil exports rise 10.7%, driven by chemicals", country="SA", score=0.7),
            _art(3, "Saudi tourism numbers climb", country="SA", score=0.5)]
    llm = _LLM(["bad", "bad"])               # grouping fails -> lexical fallback for SA
    repo = FakeRepo(arts)
    out = cluster_stage.run(_CFG, repo, 1, llm)
    # arts 1&2 merge by title Jaccard; art 3 separate
    assert out["clusters"] == 2
    assert sorted(c["count"] for c in repo.clusters) == [1, 2]


# ---------------------------------------------------------------------------
# LLM event-grouping tests
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, fa, ok=True): self.final_answer = fa; self._ok = ok
    def ok(self): return self._ok

class _LLM:
    """complete() returns the next queued answer string."""
    def __init__(self, answers): self._answers = list(answers); self.calls = []
    def complete(self, model, messages, **kw):
        self.calls.append((model, messages, kw)); return _Resp(self._answers.pop(0))


def test_group_events_partitions_articles():
    arts = [_art(1, "Bolivia declares state of emergency", country="BL"),
            _art(2, "Bolivia emergency clears blockades", country="BL"),
            _art(3, "Bolivia central bank raises rates", country="BL")]
    llm = _LLM(['{"groups": [[0, 1], [2]]}'])
    out = cluster_stage.group_events(llm, "m", "BL", arts, prompt="SYS {COUNTRY}")
    assert [[a.article_id for a in g] for g in out] == [[1, 2], [3]]
    # determinism + country interpolation
    assert llm.calls[0][2]["temperature"] == 0 and llm.calls[0][2]["seed"] == 12345
    assert "BL" in llm.calls[0][1][0]["content"]


def test_group_events_returns_none_on_bad_json_after_retry():
    arts = [_art(1, "x", country="BL"), _art(2, "y", country="BL")]
    llm = _LLM(["not json", "still not json"])
    assert cluster_stage.group_events(llm, "m", "BL", arts, prompt="SYS") is None
    assert len(llm.calls) == 2          # retried once


def test_parse_groups_rejects_non_partition():
    assert cluster_stage._parse_groups('{"groups": [[0]]}', 2) is None     # missing index 1
    assert cluster_stage._parse_groups('{"groups": [[0,1],[1]]}', 2) is None  # dup index
    assert cluster_stage._parse_groups('{"groups": [[0],[1]]}', 2) == [[0], [1]]


def test_run_sets_curated_and_economy_flags():
    from geoecon.registry import Source
    sources = [
        Source(id="reg_named_econ", institution="N", tier=2, fetch_type="rss",
               url="https://n", curated=True, section="economy"),
        Source(id="reg_named_gen", institution="G", tier=2, fetch_type="rss",
               url="https://g", curated=True, section=None),
        Source(id="reg_plain", institution="P", tier=2, fetch_type="rss",
               url="https://p", curated=False, section=None),
    ]
    # SA cluster: one curated-economy member -> both flags true (one event)
    sa = [_art(1, "Saudi budget", country="SA", score=0.9, registry_id="reg_named_econ"),
          _art(2, "Saudi budget detail", country="SA", score=0.5, registry_id="reg_plain")]
    # EG cluster: a curated general member -> has_curated true, economy false
    eg = [_art(3, "Egypt pound", country="EG", score=0.6, registry_id="reg_named_gen")]
    repo = FakeRepo(sa + eg)
    llm = _LLM(['{"groups": [[0, 1]]}'])   # SA grouped; EG singleton
    cluster_stage.run(_CFG, repo, 1, llm, sources=sources)
    sac = [c for c in repo.clusters if c["count"] == 2][0]
    egc = [c for c in repo.clusters if c["count"] == 1][0]
    assert sac["has_curated"] is True and sac["has_economy_section"] is True
    assert egc["has_curated"] is True and egc["has_economy_section"] is False


def test_run_flags_false_for_gdelt_only_cluster():
    arts = [_art(1, "Egypt pound", country="EG", score=0.5)]  # registry_id=None
    repo = FakeRepo(arts)
    cluster_stage.run(_CFG, repo, 1, _LLM([]), sources=[])
    c = repo.clusters[0]
    assert c["has_curated"] is False and c["has_economy_section"] is False
