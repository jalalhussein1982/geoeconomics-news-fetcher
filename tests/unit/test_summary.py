# tests/unit/test_summary.py
from geoecon.stages import summary
from geoecon.state.models import SynthesisRecord


def _rec(rid, headline):
    return SynthesisRecord(rid, rid * 10, {
        "headline_en": headline, "what_happened_en": "Something happened.",
        "geoeconomic_significance_en": "It matters.", "key_figures": [{"value": 5, "unit": "%"}],
    }, "ok", False)


def _client(answer):
    class Resp:
        def __init__(self, fa): self.final_answer = fa
        def ok(self): return True

    class C:
        def __init__(self): self.calls = []
        def complete(self, model, messages, **kw):
            self.calls.append((model, messages, kw)); return Resp(answer)

    return C()


class _Repo:
    def __init__(self, tuples):
        self._tuples = tuples; self.saved = []
    def records_for_buckets(self, rid, rank_by_corroboration=False):
        return self._tuples
    def upsert_region_summary(self, rid, bucket, lede, takeaways, model, count):
        self.saved.append((bucket, lede, takeaways, count))


_CFG = type("C", (), {"synthesis_model": "deepseek-v3.2", "synthesis_prompt_id": "x",
                      "buckets": ("middle_east", "north_africa", "latam"),
                      "region_cap": 10, "rank_by_corroboration": False})()


GOOD = '{"lede":"Gulf capital rotates east.","takeaways":["PIF shifts $15B.","UAE-Turkey swap widens."]}'


def test_ranking_flag_threaded_to_repo():
    # Change 4: the corroboration ranking switch reaches the repo. Dormant by default,
    # active when cfg.rank_by_corroboration is on.
    class SpyRepo(_Repo):
        def __init__(self, tuples): super().__init__(tuples); self.flag = None
        def records_for_buckets(self, rid, rank_by_corroboration=False):
            self.flag = rank_by_corroboration; return self._tuples
    tuples = [(_rec(1, "Egypt FX move"), "EG", [], 0.5)]
    off = SpyRepo(tuples); summary.run(_CFG, off, 1, _client(GOOD), prompt='SYS {REGION}')
    assert off.flag is False
    cfg_on = type("C", (), {**{k: getattr(_CFG, k) for k in
                  ("synthesis_model", "synthesis_prompt_id", "buckets", "region_cap")},
                  "rank_by_corroboration": True})()
    on = SpyRepo(tuples); summary.run(cfg_on, on, 1, _client(GOOD), prompt='SYS {REGION}')
    assert on.flag is True


def test_groups_by_bucket_and_skips_empty():
    # one Egypt record (-> middle_east AND north_africa) + one Brazil record (-> latam)
    tuples = [(_rec(1, "Egypt FX move"), "EG", [], 0.5), (_rec(2, "Brazil tariff"), "BR", [], 0.5)]
    repo = _Repo(tuples)
    client = _client(GOOD)
    counts = summary.run(_CFG, repo, 1, client, prompt='SYS {REGION}')
    buckets_saved = {s[0] for s in repo.saved}
    assert buckets_saved == {"middle_east", "north_africa", "latam"}  # 3 non-empty
    assert counts["summaries_total"] == 3 and counts["summaries_ok"] == 3
    # region display interpolated into the system prompt
    me_system = client.calls[0][1][0]["content"]
    assert "Middle East" in me_system
    # determinism kwargs forwarded to the client (Global Constraint)
    first_kw = client.calls[0][2]
    assert first_kw["temperature"] == 0 and first_kw["seed"] == 12345


def test_bad_json_degrades_to_empty_summary():
    tuples = [(_rec(1, "X"), "BR", [], 0.5)]
    repo = _Repo(tuples)
    counts = summary.run(_CFG, repo, 1, _client("not json at all"), prompt='SYS {REGION}')
    assert counts["summaries_empty"] == 1
    bucket, lede, takeaways, count = repo.saved[0]
    assert bucket == "latam" and lede is None and takeaways == []


def test_no_records_writes_nothing():
    repo = _Repo([])
    counts = summary.run(_CFG, repo, 1, _client(GOOD), prompt='SYS {REGION}')
    assert repo.saved == [] and counts["summaries_total"] == 0


def test_summary_caps_per_region():
    # 3 latam records, region_cap 2 -> summary built from top 2 by score
    recs = [(_rec(i, f"H{i}"), "BR", [], s) for i, s in [(1, 0.9), (2, 0.8), (3, 0.1)]]
    class Repo:
        def __init__(self): self.saved = []
        def records_for_buckets(self, rid, rank_by_corroboration=False): return recs
        def upsert_region_summary(self, rid, bucket, lede, takeaways, model, count):
            self.saved.append((bucket, count))
    cfg = type("C", (), {"synthesis_model": "m", "buckets": ("middle_east","north_africa","latam"),
                         "region_cap": 2, "rank_by_corroboration": False})()
    repo = Repo()
    client = _client(GOOD)
    summary.run(cfg, repo, 1, client, prompt="SYS {REGION}")
    latam = [s for s in repo.saved if s[0] == "latam"][0]
    assert latam[1] == 2  # capped to 2
    # Verify the *lowest-scoring* record (H3, score 0.1) was dropped, not an arbitrary one
    latam_call = client.calls[-1]  # only bucket is latam, so last (and only) call
    user_msg = latam_call[1][1]["content"]
    assert "HEADLINE: H1" in user_msg and "HEADLINE: H2" in user_msg
    assert "HEADLINE: H3" not in user_msg   # lowest-scoring record (0.1) dropped by the cap


def test_summary_input_excludes_key_figures():
    tuples = [(_rec(1, "Egypt FX move"), "EG", [], 0.5)]
    repo = _Repo(tuples)
    client = _client(GOOD)
    summary.run(_CFG, repo, 1, client, prompt='SYS {REGION}')
    user_msg = client.calls[0][1][1]["content"]
    assert "KEY FIGURES" not in user_msg
    assert "HEADLINE: Egypt FX move" in user_msg
