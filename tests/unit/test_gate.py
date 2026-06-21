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
    assert counts == {"gate_total": 3, "gate_kept": 2, "gate_dropped": 1, "gate_unparsed": 0}
    assert repo.gates[1]["country_fips"] == "SA" and repo.gates[1]["geoeconomic"] is True
    assert repo.gates[2]["geoeconomic"] is False and repo.gates[2]["country_fips"] is None
    assert repo.gates[3]["country_fips"] == "EG"  # Egypt resolves; dual-bucket handled downstream
    # determinism kwargs forwarded to the gate model
    first_kw = client.calls[0][2]
    assert first_kw["temperature"] == 0 and first_kw["seed"] == 12345


def test_gate_fail_open_on_unparseable():
    arts = [_art(1, "SA", "x")]
    repo = _Repo(arts)
    client = _client("not json")
    counts = gate.run(_CFG, repo, 1, client, prompt="SYS")
    assert counts["gate_unparsed"] == 1 and counts["gate_kept"] == 1
    assert repo.gates[1]["geoeconomic"] is True and repo.gates[1]["reason"] == "gate_unparsed"
    assert repo.gates[1]["score"] == 0.0
    assert len(client.calls) == 2  # primary call + one retry before failing open


def test_build_messages_numbers_items():
    msgs = gate.build_gate_messages("SYS", [_art(1, "SA", "T1"), _art(2, "EG", "T2")])
    assert msgs[0]["content"] == "SYS"
    assert "[0]" in msgs[1]["content"] and "[1]" in msgs[1]["content"]
    assert "T1" in msgs[1]["content"]
