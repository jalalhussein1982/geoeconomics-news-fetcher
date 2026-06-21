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
    def records_for_buckets(self, rid):
        return self._tuples
    def upsert_region_summary(self, rid, bucket, lede, takeaways, model, count):
        self.saved.append((bucket, lede, takeaways, count))


_CFG = type("C", (), {"synthesis_model": "deepseek-v3.2", "synthesis_prompt_id": "x",
                      "buckets": ("middle_east", "north_africa", "latam")})()

GOOD = '{"lede":"Gulf capital rotates east.","takeaways":["PIF shifts $15B.","UAE-Turkey swap widens."]}'


def test_groups_by_bucket_and_skips_empty():
    # one Egypt record (-> middle_east AND north_africa) + one Brazil record (-> latam)
    tuples = [(_rec(1, "Egypt FX move"), "EG", []), (_rec(2, "Brazil tariff"), "BR", [])]
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
    tuples = [(_rec(1, "X"), "BR", [])]
    repo = _Repo(tuples)
    counts = summary.run(_CFG, repo, 1, _client("not json at all"), prompt='SYS {REGION}')
    assert counts["summaries_empty"] == 1
    bucket, lede, takeaways, count = repo.saved[0]
    assert bucket == "latam" and lede is None and takeaways == []


def test_no_records_writes_nothing():
    repo = _Repo([])
    counts = summary.run(_CFG, repo, 1, _client(GOOD), prompt='SYS {REGION}')
    assert repo.saved == [] and counts["summaries_total"] == 0
