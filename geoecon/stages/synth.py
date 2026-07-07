"""Synthesis stage — builds a structured record per cluster via deepseek-v3.2.

Schema bridge
-------------
The LLM prompt (task_a_cov.txt) and the vendored validator (geoecon/llm/validator.py) both
operate on the *EVAL schema*:
    headline, what_happened, geoeconomic_significance, entities (dict), key_figures, sources, …

The pipeline DB (synthesis_records) uses a different *PIPELINE schema*:
    headline_en, what_happened_en, geoeconomic_significance_en, entities (JSONB), …

Two thin adapters bridge them:
    adapt_flag_vocab(qf) — maps validator's QualityFlag vocab → DB CHECK vocab
    map_record(rec)      — maps eval-schema keys → pipeline-schema keys

Public API
----------
    parse_record(text)            -> dict | None
    build_messages(prompt, arts)  -> list[dict]
    adapt_flag_vocab(qf)          -> (flag: str, detail: dict | None)
    map_record(rec)               -> dict
    run(cfg, repo, run_id, client, *, prompt=None, validate=_default_validate) -> dict
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("geoecon.synth")

_JSON_RE = re.compile(r"\{.*\}", re.S)

# ---------------------------------------------------------------------------
# Validator flag vocab → DB CHECK vocab
# ---------------------------------------------------------------------------

# validator.py QUALITY_FLAGS: ok not_json empty lang_mismatch no_figures no_entities
# schema.sql  CHECK:          ok invalid_json retry_fallback lang_mismatch empty_figures zero_entities
_FLAG_MAP: dict[str, str] = {
    "ok":           "ok",
    "not_json":     "invalid_json",
    "empty":        "retry_fallback",
    "lang_mismatch":"lang_mismatch",
    "no_figures":   "empty_figures",
    "no_entities":  "zero_entities",
}


def adapt_flag_vocab(qf) -> tuple[str, dict | None]:
    """Map a validator QualityFlag to (schema_flag, detail_dict | None).

    The detail dict is None for 'ok'; otherwise carries the validator's
    reasons list and action for diagnostics / quality_detail JSONB.
    """
    schema_flag = _FLAG_MAP.get(qf.quality_flag, "retry_fallback")
    if qf.quality_flag == "ok":
        return schema_flag, None
    return schema_flag, {"reasons": qf.reasons, "action": qf.action}


# ---------------------------------------------------------------------------
# Eval → pipeline field mapper
# ---------------------------------------------------------------------------

def map_record(rec: dict) -> dict:
    """Map an eval-schema LLM response dict to pipeline-schema fields dict.

    Phase 1 (en_only):
    - headline_original / headline_original_lang / framing_divergence -> None
      (framing_divergence is a string in eval vs. a paired-array in pipeline; don't coerce)
    - geoeconomic_vector omitted (embedding step is out-of-scope for Phase 1)
    """
    return {
        "headline_en":               rec.get("headline"),
        "headline_original":         None,
        "headline_original_lang":    None,
        "what_happened_en":          rec.get("what_happened"),
        "geoeconomic_significance_en": rec.get("geoeconomic_significance"),
        "entities":                  rec.get("entities"),
        "key_figures":               rec.get("key_figures"),
        "framing_divergence":        None,
        "sources":                   rec.get("sources"),
    }


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def parse_record(text: str) -> dict | None:
    """Extract the first JSON object from *text*, tolerating markdown fences."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def build_messages(prompt: str, articles, *, body_chars: int = 0, total_chars: int = 0) -> list[dict]:
    """Build the [system, user] messages list from *prompt* and *articles*.

    Parameters
    ----------
    body_chars : int, optional
        Per-article body character cap (0 = unlimited). Bodies exceeding this are
        truncated with "[… truncated]" appended.
    total_chars : int, optional
        Cumulative character cap for entire input (0 = unlimited). Once exceeded,
        remaining articles render as headline-only format.
    """
    parts = []
    cumulative_len = 0

    for a in articles:
        # Check if we've exceeded total_chars cumulative limit
        if total_chars > 0 and cumulative_len > total_chars:
            # All remaining articles become headline-only
            parts.append(f"[HEADLINE-ONLY · {a.source_lang or '?'}] {a.title or a.source_url}")
        elif a.body:
            body = a.body
            # Apply per-article body_chars cap
            if body_chars > 0 and len(body) > body_chars:
                body = body[:body_chars] + "\n[… truncated]"
            part = f"[{a.source_lang or '?'}] {a.title or ''}\n{body}"
            parts.append(part)
            cumulative_len += len(part)
        else:
            # Headline-only articles (no body)
            part = f"[HEADLINE-ONLY · {a.source_lang or '?'}] {a.title or a.source_url}"
            parts.append(part)
            cumulative_len += len(part)

    return [
        {"role": "system", "content": prompt},
        {"role": "user",   "content": "\n\n---\n\n".join(parts)},
    ]


# ---------------------------------------------------------------------------
# Default validator — wraps the vendored production guard
# ---------------------------------------------------------------------------

def _default_validate(record: dict, *, source_has_figures: bool = True):
    """Thin wrapper around the vendored validate_synthesis; returns (flag, detail)."""
    from geoecon.llm import validator as v
    qf = v.validate_synthesis(record, source_has_figures=source_has_figures)
    return adapt_flag_vocab(qf)


# ---------------------------------------------------------------------------
# Stage entry-point
# ---------------------------------------------------------------------------

def run(cfg, repo, run_id, client, *, prompt: str | None = None,
        validate=_default_validate) -> dict:
    """Run synthesis for every cluster in *run_id*.

    Parameters
    ----------
    cfg      : config object with .synthesis_model and .synthesis_prompt_id
    repo     : Repo-like with clusters_for_run / articles_in_cluster / get_seen /
               upsert_synthesis_record
    run_id   : int pipeline run identifier
    client   : LLM client with .complete(model, messages, **kw) -> resp
    prompt   : optional override; loaded from prompts package if omitted
    validate : callable(record, **kw) -> (flag, detail); defaults to _default_validate
               Test doubles should accept **kw for source_has_figures tolerance.
    """
    if prompt is None:
        from importlib.resources import files
        prompt = (
            files("geoecon.prompts")
            .joinpath(f"{cfg.synthesis_prompt_id}.txt")
            .read_text("utf-8")
        )

    counts = {"records_total": 0, "records_ok": 0, "records_held": 0}

    for cl in repo.clusters_for_run(run_id):
        # Seen-store suppression (non-overlap: skip already-emitted clusters)
        seen = repo.get_seen(cl.event_fingerprint)
        if seen and seen.get("last_emitted_at"):
            continue

        articles = repo.articles_in_cluster(cl.cluster_id)
        headline_only = all(a.body is None for a in articles)
        source_has_figures = not headline_only

        messages = build_messages(
            prompt, articles,
            body_chars=getattr(cfg, "synth_body_chars", 0),
            total_chars=getattr(cfg, "synth_total_chars", 0),
        )
        resp = client.complete(cfg.synthesis_model, messages,
                               temperature=0, seed=12345,
                               max_tokens=getattr(cfg, "synth_max_tokens", 8192))

        record = parse_record(getattr(resp, "final_answer", "") or "") if resp.ok() else None

        if record is None:
            flag, detail = "invalid_json", {"error": getattr(resp, "error", "parse failed")}
        else:
            flag, detail = validate(record, source_has_figures=source_has_figures)
            if flag != "ok":
                # One retry on non-ok
                resp2 = client.complete(cfg.synthesis_model, messages,
                                        temperature=0, seed=12345,
                                        max_tokens=getattr(cfg, "synth_max_tokens", 8192))
                record2 = (
                    parse_record(getattr(resp2, "final_answer", "") or "")
                    if resp2.ok() else None
                )
                if record2 is not None:
                    f2, d2 = validate(record2, source_has_figures=source_has_figures)
                    if f2 == "ok":
                        record, flag, detail = record2, f2, d2

        fields = map_record(record) if record else {}
        repo.upsert_synthesis_record(
            run_id, cl.cluster_id, fields, flag,
            quality_detail=detail, is_headline_only=headline_only,
        )
        counts["records_total"] += 1
        counts["records_ok" if flag == "ok" else "records_held"] += 1

    log.info("synthesis: %s", counts)
    return counts
