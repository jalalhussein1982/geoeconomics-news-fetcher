import pytest
from types import SimpleNamespace

from geoecon.stages import gate
from geoecon.state.models import Article


def _art(aid, country, title, body="econ body"):
    return Article(aid, f"http://x/{aid}", f"h{aid}", "d", "MENA", ["ECON_X"], None,
                   None, None, title, body, "extracted", country)


def _client(answer):
    class Resp:
        def __init__(self, fa): self.final_answer = fa
        def ok(self): return True
    class C:
        def __init__(self): self.calls = []
        def complete(self, model, messages, **kw):
            self.calls.append((model, messages, kw)); return Resp(answer)
    return C()


class ScriptedClient:
    """Returns canned responses in order; records the batch sizes it was asked to
    judge. Mirrors the real client contract: ok() is a method, final_answer is a
    string, and unparsable garbage still reports ok()==True (the parse failure is
    detected downstream by parse_verdicts returning None)."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.batch_sizes = []
        self.calls = []

    def complete(self, model, messages, **kw):
        user = messages[1]["content"]
        self.batch_sizes.append(user.count("COUNTRY_HINT="))
        self.calls.append((model, messages, kw))
        text = self.responses.pop(0) if self.responses else "garbage"
        return SimpleNamespace(final_answer=text, ok=lambda: True)


class _Repo:
    def __init__(self, arts): self._arts = arts; self.gates = {}
    def articles_for_run(self, rid, fetch_status=None): return self._arts
    def upsert_gate(self, aid, *, geoeconomic, score, country, reason, country_fips):
        self.gates[aid] = dict(geoeconomic=geoeconomic, score=score, country=country,
                               reason=reason, country_fips=country_fips)


_CFG = type("C", (), {"gate_model": "m", "gate_batch_size": 10,
                      "buckets": ("middle_east", "north_africa", "latam")})()


def test_gate_keeps_resolves_region_and_drops_other():
    arts = [_art(1, "SA", "Saudi sovereign fund shifts to Asia"),
            _art(2, "IR", "US Fed holds rates"),     # gate says other
            _art(3, "EG", "Egypt pound rallies on US-Iran deal")]
    answer = ('[{"i":0,"geoeconomic":true,"primary_country":"Saudi Arabia","score":0.9,"reason":"swf geopolitics"},'
              '{"i":1,"geoeconomic":false,"primary_country":"other","score":0.1,"reason":"us fed"},'
              '{"i":2,"geoeconomic":true,"primary_country":"Egypt","score":0.8,"reason":"oil geopolitics"}]')
    repo = _Repo(arts)
    client = _client(answer)
    counts = gate.run(_CFG, repo, 1, client, prompt="SYS")
    assert counts == {"gate_total": 3, "gate_kept": 2, "gate_dropped": 1,
                       "gate_unparsed": 0, "gate_unparsed_dropped": 0}
    assert repo.gates[1]["country_fips"] == "SA" and repo.gates[1]["geoeconomic"] is True
    assert repo.gates[2]["geoeconomic"] is False and repo.gates[2]["country_fips"] is None
    assert repo.gates[3]["country_fips"] == "EG"  # Egypt resolves; dual-bucket handled downstream
    # determinism kwargs forwarded to the gate model
    first_kw = client.calls[0][2]
    assert first_kw["temperature"] == 0 and first_kw["seed"] == 12345


def test_gate_drops_not_keeps_on_unparseable():
    # Updated for the re-gate cascade (was: fail-open keep with reason
    # "gate_unparsed"). A single article that never parses now cascades all the
    # way to a drop with reason "gate_unparsed_dropped" instead of being kept
    # unjudged.
    arts = [_art(1, "SA", "x")]
    repo = _Repo(arts)
    client = _client("not json")
    counts = gate.run(_CFG, repo, 1, client, prompt="SYS")
    assert counts["gate_unparsed"] == 1
    assert counts["gate_kept"] == 0
    assert counts["gate_dropped"] == 1
    assert counts["gate_unparsed_dropped"] == 1
    assert repo.gates[1]["geoeconomic"] is False
    assert repo.gates[1]["reason"] == "gate_unparsed_dropped"
    assert repo.gates[1]["score"] == 0.0
    assert repo.gates[1]["country_fips"] is None


def test_unparsed_batch_cascades_to_subbatches():
    arts = [_art(i, "SA", f"T{i}") for i in range(6)]
    repo = _Repo(arts)
    valid5 = "[" + ",".join(
        f'{{"i":{i},"geoeconomic":true,"primary_country":"Saudi Arabia","score":0.5,"reason":"r"}}'
        for i in range(5)
    ) + "]"
    valid1 = '[{"i":0,"geoeconomic":true,"primary_country":"Saudi Arabia","score":0.5,"reason":"r"}]'
    client = ScriptedClient(["garbage", "garbage", valid5, valid1])
    cfg = type("C", (), {"gate_model": "m", "gate_batch_size": 6,
                          "buckets": ("middle_east", "north_africa", "latam")})()
    counts = gate.run(cfg, repo, 1, client, prompt="SYS")
    assert client.batch_sizes == [6, 6, 5, 1]
    assert counts["gate_total"] == 6
    assert counts["gate_unparsed"] == 6
    assert counts["gate_unparsed_dropped"] == 0
    assert counts["gate_kept"] == 6
    for i in range(6):
        assert repo.gates[i]["geoeconomic"] is True
        assert repo.gates[i]["country_fips"] == "SA"
        assert repo.gates[i]["reason"] != "gate_unparsed" and repo.gates[i]["reason"] != "gate_unparsed_dropped"


def test_totally_unparsable_drops_not_keeps():
    arts = [_art(i, "SA", f"T{i}") for i in range(6)]
    repo = _Repo(arts)
    client = ScriptedClient([])  # every call falls back to "garbage"
    cfg = type("C", (), {"gate_model": "m", "gate_batch_size": 6,
                          "buckets": ("middle_east", "north_africa", "latam")})()
    counts = gate.run(cfg, repo, 1, client, prompt="SYS")
    assert counts["gate_kept"] == 0
    assert counts["gate_unparsed_dropped"] == 6
    assert counts["gate_dropped"] == 6
    for i in range(6):
        assert repo.gates[i]["reason"] == "gate_unparsed_dropped"
        assert repo.gates[i]["geoeconomic"] is False
        assert repo.gates[i]["country_fips"] is None
        assert repo.gates[i]["score"] == 0.0


def _big_cfg(bs=10):
    return type("C", (), {"gate_model": "m", "gate_batch_size": bs,
                          "buckets": ("middle_east", "north_africa", "latam")})()


def test_gate_raises_when_a_real_run_is_entirely_unjudged():
    """2026-07-27 run 67: the deepseek backend was down, all 1500 articles came
    back `gate_unparsed_dropped`, and gate still returned status=completed with
    gate_kept=0 — handing an empty article set to cluster/synthesis. A run that
    judged nothing must fail loudly, not emit an empty newsletter."""
    arts = [_art(i, "SA", f"T{i}") for i in range(gate.MIN_TOTAL_FOR_HEALTH_CHECK)]
    repo = _Repo(arts)
    client = ScriptedClient([])          # every call falls back to "garbage"

    with pytest.raises(gate.NullGateError) as ei:
        gate.run(_big_cfg(), repo, 1, client, prompt="SYS")

    assert "unjudged" in str(ei.value).lower()
    # verdicts are still persisted before the raise — the evidence survives
    assert len(repo.gates) == gate.MIN_TOTAL_FOR_HEALTH_CHECK
    assert all(v["reason"] == "gate_unparsed_dropped" for v in repo.gates.values())


def test_gate_does_not_raise_on_a_small_limited_run():
    """`--limit` / smoke runs judge a handful of articles; a low count is not
    evidence of a systemic outage, so the guard must not fire below the floor."""
    n = gate.MIN_TOTAL_FOR_HEALTH_CHECK - 1
    arts = [_art(i, "SA", f"T{i}") for i in range(n)]
    repo = _Repo(arts)
    counts = gate.run(_big_cfg(), repo, 1, client=ScriptedClient([]), prompt="SYS")
    assert counts["gate_kept"] == 0 and counts["gate_unparsed_dropped"] == n


def test_gate_does_not_raise_when_model_legitimately_drops_everything():
    """All-dropped with clean, parseable verdicts is a real answer (quiet day /
    off-topic batch), not an outage. Only UNPARSED output implies a broken model."""
    n = gate.MIN_TOTAL_FOR_HEALTH_CHECK
    arts = [_art(i, "SA", f"T{i}") for i in range(n)]
    repo = _Repo(arts)
    verdicts = "[" + ",".join(
        f'{{"i":{i},"geoeconomic":false,"primary_country":null,"score":0.0,"reason":"off-topic"}}'
        for i in range(n)) + "]"
    counts = gate.run(_big_cfg(bs=n), repo, 1, ScriptedClient([verdicts]), prompt="SYS")
    assert counts["gate_kept"] == 0
    assert counts["gate_unparsed_dropped"] == 0


def test_missing_index_goes_to_per_article_not_keep():
    arts = [_art(0, "SA", "T0"), _art(1, "EG", "T1"), _art(2, "IR", "T2")]
    repo = _Repo(arts)
    partial = ('[{"i":0,"geoeconomic":true,"primary_country":"Saudi Arabia","score":0.5,"reason":"r"},'
               '{"i":1,"geoeconomic":true,"primary_country":"Egypt","score":0.5,"reason":"r"}]')
    per_article_2 = '[{"i":0,"geoeconomic":true,"primary_country":"Iran","score":0.4,"reason":"r"}]'
    client = ScriptedClient([partial, per_article_2])
    counts = gate.run(_CFG, repo, 1, client, prompt="SYS")
    assert counts["gate_total"] == 3
    assert counts["gate_unparsed"] == 1
    assert counts["gate_kept"] == 3
    assert counts["gate_unparsed_dropped"] == 0
    assert repo.gates[2]["geoeconomic"] is True
    assert repo.gates[2]["country_fips"] == "IR"
    # the old fail-open keep contract must no longer exist anywhere
    for aid, g in repo.gates.items():
        assert not (g["reason"] == "gate_unparsed" and g["score"] == 0.0 and g["geoeconomic"] is True)


def test_build_messages_numbers_items():
    msgs = gate.build_gate_messages("SYS", [_art(1, "SA", "T1"), _art(2, "EG", "T2")])
    assert msgs[0]["content"] == "SYS"
    assert "[0]" in msgs[1]["content"] and "[1]" in msgs[1]["content"]
    assert "T1" in msgs[1]["content"]


def test_gate_parallel_workers_same_results_and_counts():
    """gate_workers>1 must produce byte-identical verdict application and counts
    vs sequential — batches fan out to threads, repo writes stay on main thread."""
    arts = [_art(i, "SA", f"T{i}") for i in range(9)]  # 5 batches of 2 (last=1)
    answer = ('[{"i":0,"geoeconomic":true,"primary_country":"Saudi Arabia","score":0.9,"reason":"r"},'
              '{"i":1,"geoeconomic":true,"primary_country":"Saudi Arabia","score":0.9,"reason":"r"}]')
    cfg = type("C", (), {"gate_model": "m", "gate_batch_size": 2, "gate_workers": 4,
                         "buckets": ("middle_east", "north_africa", "latam")})()
    repo = _Repo(arts)
    counts = gate.run(cfg, repo, 1, _client(answer), prompt="SYS")
    assert counts == {"gate_total": 9, "gate_kept": 9, "gate_dropped": 0,
                      "gate_unparsed": 0, "gate_unparsed_dropped": 0}
    assert len(repo.gates) == 9
    for i in range(9):
        assert repo.gates[i]["geoeconomic"] is True
        assert repo.gates[i]["country_fips"] == "SA"


def test_systemic_llm_failure_escapes_the_worker_pool():
    """gate fans batches out to GATE_WORKERS threads and collects them through
    fut.result(). A breaker trip inside a worker has to propagate out of gate.run
    so run_pipeline can record the failed stage. If it were swallowed per-future
    the breaker would silently degrade into 'every batch dropped unjudged' — the
    exact run-67 outcome it exists to prevent."""
    from geoecon.llm.client import SystemicLLMFailure

    class _Boom:
        def __init__(self): self.calls = 0
        def complete(self, model, messages, **kw):
            self.calls += 1
            raise SystemicLLMFailure("gemma4: 5 consecutive complete() calls exhausted")

    arts = [_art(i, "SA", f"T{i}") for i in range(20)]
    cfg = type("C", (), {"gate_model": "m", "gate_batch_size": 2, "gate_workers": 4,
                         "buckets": ("middle_east", "north_africa", "latam")})()
    client = _Boom()

    with pytest.raises(SystemicLLMFailure) as ei:
        gate.run(cfg, _Repo(arts), 1, client, prompt="SYS")

    assert "gemma4" in str(ei.value)


def test_gate_parallel_unparsable_still_cascades_to_drop():
    """The cascade (unparsable -> subbatch -> per-article -> drop) must survive
    the parallel path unchanged."""
    arts = [_art(i, "SA", f"T{i}") for i in range(4)]
    cfg = type("C", (), {"gate_model": "m", "gate_batch_size": 2, "gate_workers": 3,
                         "buckets": ("middle_east", "north_africa", "latam")})()
    repo = _Repo(arts)
    counts = gate.run(cfg, repo, 1, _client("not json"), prompt="SYS")
    assert counts["gate_kept"] == 0
    assert counts["gate_dropped"] == 4
    assert counts["gate_unparsed"] == 4
    assert counts["gate_unparsed_dropped"] == 4
    for i in range(4):
        assert repo.gates[i]["reason"] == "gate_unparsed_dropped"
        assert repo.gates[i]["geoeconomic"] is False


def test_gate_workers_actually_run_concurrently():
    """Two batch calls must be in flight at once when gate_workers>1: each call
    waits on a 2-party barrier that only a concurrent second call can release."""
    import threading
    barrier = threading.Barrier(2, timeout=10)
    broken = []
    answer = ('[{"i":0,"geoeconomic":true,"primary_country":"Saudi Arabia","score":0.9,"reason":"r"},'
              '{"i":1,"geoeconomic":true,"primary_country":"Saudi Arabia","score":0.9,"reason":"r"}]')

    class BarrierClient:
        def complete(self, model, messages, **kw):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                broken.append(1)
            return SimpleNamespace(final_answer=answer, ok=lambda: True)

    arts = [_art(i, "SA", f"T{i}") for i in range(4)]  # 2 batches of 2
    cfg = type("C", (), {"gate_model": "m", "gate_batch_size": 2, "gate_workers": 2,
                         "buckets": ("middle_east", "north_africa", "latam")})()
    repo = _Repo(arts)
    counts = gate.run(cfg, repo, 1, BarrierClient(), prompt="SYS")
    assert not broken, "gate batches ran sequentially — no two LLM calls overlapped"
    assert counts["gate_kept"] == 4
