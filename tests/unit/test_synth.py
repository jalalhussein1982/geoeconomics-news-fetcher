"""Unit tests for geoecon.stages.synth — TDD (RED first, then GREEN)."""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from geoecon.stages import synth
from geoecon.state.models import Article, Cluster


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _client(answer):
    """Return a fake LLM client that always responds with *answer*."""
    class Resp:
        def __init__(self, fa):
            self.final_answer = fa

        def ok(self):
            return True

    class C:
        def complete(self, model, messages, **kw):
            return Resp(answer)

    return C()


# A valid EVAL-schema JSON string (brief's original GOOD constant, retained for
# parse_record / held-record tests that don't touch the real validator).
GOOD_BRIEF = (
    '{"headline_en":"X","what_happened_en":"Y",'
    '"entities":[{"canonical_en":"E"}],"key_figures":[{"value":1}]}'
)

# A valid EVAL-schema record that the REAL validator will accept.
GOOD_EVAL_JSON = (
    '{"headline":"H",'
    '"what_happened":"Qatar Energy and ExxonMobil signed a long-term LNG deal worth '
    'thirty billion dollars to supply European markets through 2045.",'
    '"geoeconomic_significance":"This secures European energy supply for two decades.",'
    '"entities":{"orgs":["QatarEnergy","ExxonMobil"],"people":[],"countries":["Qatar","USA"]},'
    '"key_figures":[{"value":"30","unit":"billion USD","source_span":"thirty billion dollars"}],'
    '"framing_divergence":"none",'
    '"sources":["Reuters"]}'
)

# Hollow EVAL-schema record (what_happened empty, no figures, no entities).
HOLLOW_EVAL_JSON = '{"what_happened":"","key_figures":[],"entities":{}}'


def _make_arts(body="body text"):
    return [Article(1, "u", "h", "d", "MENA", [], None, None, "ar", "T", body, "extracted")]


def _make_repo(arts, saved_list):
    class FakeRepo:
        def clusters_for_run(self, rid):
            return [Cluster(10, "fp", "MENA", ["ar"], 1, False)]

        def articles_in_cluster(self, cid):
            return arts

        def get_seen(self, fp):
            return None

        def upsert_synthesis_record(self, rid, cid, fields, flag,
                                    quality_detail=None, is_headline_only=False):
            saved_list.append((cid, flag, fields, quality_detail))
            return 1

    return FakeRepo()


_CFG_OK = type("C", (), {"synthesis_model": "deepseek-v3.2", "synthesis_prompt_id": "task_a_cov"})()
_CFG_PLAIN = type("C", (), {"synthesis_model": "m", "synthesis_prompt_id": "p"})()


# ---------------------------------------------------------------------------
# Brief test 1: parse_record
# ---------------------------------------------------------------------------

def test_parse_record_extracts_json():
    assert synth.parse_record("noise " + GOOD_BRIEF + " trailer")["headline_en"] == "X"


# ---------------------------------------------------------------------------
# Brief test 2: run persists ok record (injected validate)
# ---------------------------------------------------------------------------

def test_run_persists_ok_record():
    arts = _make_arts()
    saved = []
    repo = _make_repo(arts, saved)
    cfg = _CFG_OK
    metrics = synth.run(
        cfg, repo, 1, _client(GOOD_BRIEF),
        prompt="PROMPT",
        validate=lambda rec, **kw: ("ok", None),
    )
    assert metrics["records_ok"] == 1
    assert saved[0][1] == "ok"


# ---------------------------------------------------------------------------
# Brief test 3: run holds non-ok record (injected validate)
# ---------------------------------------------------------------------------

def test_run_holds_non_ok_record():
    arts = _make_arts()
    saved = []
    repo = _make_repo(arts, saved)
    cfg = _CFG_PLAIN
    synth.run(
        cfg, repo, 1, _client("not json"),
        prompt="P",
        validate=lambda rec, **kw: ("invalid_json", {"why": "x"}),
    )
    assert saved[0][1] == "invalid_json"


# ---------------------------------------------------------------------------
# New test 4: adapt_flag_vocab — all 6 mappings
# ---------------------------------------------------------------------------

def test_adapt_flag_vocab_mapping():
    from geoecon.llm.validator import QualityFlag

    mapping = [
        ("ok",           "accept",           "ok",           None),
        ("not_json",     "headline_fallback", "invalid_json", True),
        ("empty",        "retry",            "retry_fallback", True),
        ("lang_mismatch","retry",            "lang_mismatch", True),
        ("no_figures",   "retry",            "empty_figures", True),
        ("no_entities",  "retry",            "zero_entities", True),
    ]

    for vflag, action, expected_schema_flag, has_detail in mapping:
        qf = QualityFlag(quality_flag=vflag, action=action, reasons=["r1"])
        flag, detail = synth.adapt_flag_vocab(qf)
        assert flag == expected_schema_flag, (
            f"validator flag {vflag!r} -> expected schema {expected_schema_flag!r}, got {flag!r}"
        )
        if has_detail is None:
            assert detail is None, f"expected detail=None for ok, got {detail!r}"
        else:
            assert detail is not None, f"expected detail dict for {vflag!r}"
            assert "reasons" in detail
            assert "action" in detail


# ---------------------------------------------------------------------------
# New test 5: map_record (eval -> pipeline)
# ---------------------------------------------------------------------------

def test_map_record_eval_to_pipeline():
    eval_rec = {
        "headline": "H",
        "what_happened": "Qatar Energy and ExxonMobil signed a long-term LNG deal worth "
                         "thirty billion dollars to supply European markets through 2045.",
        "geoeconomic_significance": "This secures supply for decades.",
        "entities": {"orgs": ["X"]},
        "key_figures": [{"value": "1", "unit": "%", "source_span": "1%"}],
        "framing_divergence": "none",
        "sources": ["Reuters"],
    }
    mapped = synth.map_record(eval_rec)

    assert mapped["headline_en"] == "H"
    assert mapped["what_happened_en"] == eval_rec["what_happened"]
    assert mapped["geoeconomic_significance_en"] == "This secures supply for decades."
    assert mapped["entities"] == {"orgs": ["X"]}
    assert mapped["key_figures"] == [{"value": "1", "unit": "%", "source_span": "1%"}]
    assert mapped["sources"] == ["Reuters"]
    # Phase 1 nulls
    assert mapped.get("headline_original") is None
    assert mapped.get("headline_original_lang") is None
    assert mapped.get("framing_divergence") is None
    # geoeconomic_vector not set (omitted, not en_only coercion)
    assert "geoeconomic_vector" not in mapped


# ---------------------------------------------------------------------------
# New test 6: default validator with hollow record -> schema-valid held flag
# ---------------------------------------------------------------------------

def test_run_default_validator_holds_hollow_record():
    arts = _make_arts("some body text")
    saved = []
    repo = _make_repo(arts, saved)
    # Both initial call AND retry return the same hollow JSON.
    metrics = synth.run(
        _CFG_OK, repo, 1, _client(HOLLOW_EVAL_JSON),
        prompt="PROMPT",
        # no validate injection -> uses _default_validate (real validator)
    )
    assert len(saved) == 1
    persisted_flag = saved[0][1]
    schema_valid = {"ok", "empty_figures", "zero_entities", "lang_mismatch",
                    "pair_incomplete", "retry_fallback", "invalid_json"}
    assert persisted_flag in schema_valid, f"flag {persisted_flag!r} not in schema CHECK set"
    assert persisted_flag != "ok"
    # Specifically hollow record -> empty what_happened -> empty -> retry_fallback
    assert persisted_flag == "retry_fallback"


# ---------------------------------------------------------------------------
# New test 7: default validator with good record -> ok + fields populated
# ---------------------------------------------------------------------------

def test_run_default_validator_accepts_good_record():
    arts = _make_arts("some body text")
    saved = []
    repo = _make_repo(arts, saved)
    metrics = synth.run(
        _CFG_OK, repo, 1, _client(GOOD_EVAL_JSON),
        prompt="PROMPT",
        # no validate injection -> uses _default_validate (real validator)
    )
    assert len(saved) == 1
    cid, flag, fields, detail = saved[0]
    assert flag == "ok", f"expected ok, got {flag!r}"
    assert fields["headline_en"] == "H"
    assert fields["what_happened_en"] is not None and len(fields["what_happened_en"]) >= 60
    assert metrics["records_ok"] == 1


# ---------------------------------------------------------------------------
# Task 4: Synthesis input caps
# ---------------------------------------------------------------------------

def test_build_messages_caps_body_chars():
    """Per-article body truncation when body exceeds body_chars."""
    arts = [SimpleNamespace(source_lang="es", title="T", body="x" * 10000, source_url="u")]
    msgs = synth.build_messages("P", arts, body_chars=6000)
    user = msgs[1]["content"]
    assert "[… truncated]" in user and len(user) < 7000


def test_build_messages_total_cap_demotes_tail_to_headline_only():
    """Cumulative total_chars cap demotes remaining articles to headline-only format."""
    arts = [SimpleNamespace(source_lang="es", title=f"T{i}", body="x" * 900, source_url=f"u{i}")
            for i in range(5)]
    msgs = synth.build_messages("P", arts, body_chars=0, total_chars=2000)
    user = msgs[1]["content"]
    assert user.count("HEADLINE-ONLY") >= 2          # tail demoted
    assert "T0" in user and "x" * 100 in user        # head keeps its body


def test_build_messages_zero_caps_is_uncapped():
    """Zero (default) caps mean unlimited (backward compatible)."""
    arts = [SimpleNamespace(source_lang="es", title="T", body="x" * 10000, source_url="u")]
    user = synth.build_messages("P", arts)[1]["content"]
    assert len(user) > 10000 and "truncated" not in user
