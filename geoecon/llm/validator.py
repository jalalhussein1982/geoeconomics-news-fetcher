"""Production guard — post-synthesis quality validator (model-agnostic reference impl).

BUILD REQUIREMENT, not an eval metric. Stage-1 synthesis must never SILENTLY emit a hollow or
wrong-language record. This validator inspects a synthesized record and returns a QualityFlag with
an action (accept / retry / headline_fallback) and a `synthesis_quality_flag` string to persist on
the Postgres synthesis row.

Motivation: the eval surfaced two late scorer bugs and a silent qwen "0.0" that was a measurement
artifact — silent failure is the real risk, and the 8-fixture corpus does not clear any model on
the long tail of live Arabic. Guard at runtime.

Pipeline contract (caller):
    flag = validate_synthesis(record, expected_output_lang="en", source_has_figures=has_figs)
    if   flag.action == "accept":                       persist(record, quality_flag=flag.quality_flag)
    elif flag.action == "retry" and attempt == 0:       re-run synthesis once, re-validate
    else:                                               emit headline_only(quality_flag=flag.quality_flag)
    # always write flag.quality_flag to synthesis_quality_flag (Postgres) for monitoring.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

# Postgres synthesis_quality_flag enum
QUALITY_FLAGS = ("ok", "not_json", "empty", "lang_mismatch", "no_figures", "no_entities")


@dataclass
class QualityFlag:
    quality_flag: str          # -> Postgres synthesis_quality_flag
    action: str                # accept | retry | headline_fallback
    reasons: list = field(default_factory=list)


def _is_latin(ch: str) -> bool:
    try:
        return "LATIN" in unicodedata.name(ch)
    except ValueError:
        return False


def nonlatin_ratio(s: str) -> float:
    """Share of alphabetic chars that are NOT Latin (Arabic / CJK / Cyrillic …). A high ratio in
    what_happened means the model echoed the source language instead of producing English."""
    letters = [c for c in (s or "") if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if not _is_latin(c)) / len(letters)


def validate_synthesis(record, *, expected_output_lang: str = "en",
                       source_has_figures: bool = True,
                       min_what_happened_chars: int = 60) -> QualityFlag:
    """Validate one synthesized record. `source_has_figures` lets the caller suppress the
    no_figures check for sources that genuinely contain none (e.g. headline-only)."""
    if not isinstance(record, dict):
        return QualityFlag("not_json", "headline_fallback", ["record_not_json"])

    wh = (record.get("what_happened") or "").strip()
    figs = record.get("key_figures") or []
    ents = record.get("entities") or {}
    n_ents = (sum(len(ents.get(k, []) or []) for k in ("orgs", "people", "countries"))
              if isinstance(ents, dict) else 0)

    reasons = []
    if not wh or len(wh) < min_what_happened_chars:
        reasons.append("what_happened_empty_or_short")
    if expected_output_lang == "en" and nonlatin_ratio(wh) > 0.30:
        reasons.append("lang_mismatch")
    if source_has_figures and not figs:
        reasons.append("no_key_figures")
    if n_ents == 0:
        reasons.append("no_entities")

    if not reasons:
        return QualityFlag("ok", "accept", [])

    # hard failures (empty / wrong language) -> retry, then headline_fallback on persistence;
    # sparse (no figures / no entities) -> retry. The caller owns the retry-count -> fallback.
    primary = next((r for r in reasons
                    if r in ("what_happened_empty_or_short", "lang_mismatch")), reasons[0])
    flag = {"what_happened_empty_or_short": "empty", "lang_mismatch": "lang_mismatch",
            "no_key_figures": "no_figures", "no_entities": "no_entities"}[primary]
    return QualityFlag(flag, "retry", reasons)


# Postgres DDL (reference) for the synthesis row's quality column:
POSTGRES_DDL = """
ALTER TABLE synthesis
  ADD COLUMN IF NOT EXISTS synthesis_quality_flag text NOT NULL DEFAULT 'ok'
    CHECK (synthesis_quality_flag IN ('ok','not_json','empty','lang_mismatch','no_figures','no_entities'));
CREATE INDEX IF NOT EXISTS synthesis_quality_flag_idx ON synthesis (synthesis_quality_flag)
  WHERE synthesis_quality_flag <> 'ok';
""".strip()


if __name__ == "__main__":
    # Self-test against REAL eval records: a good record accepts; hollow/wrong-language flag.
    import json, sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import score
    cases = []
    # 1) qwen's ar_clean_01 — the "silent 0.0" that was actually a GOOD record -> accept
    r = score.load_recs(score.cell_files("qwen3.5-122b", "task_a_combined", "ar_clean_01"))
    if r:
        rec = score.extract_json(r[0].get("final_answer") or r[0]["response_text"])
        cases.append(("qwen ar_clean_01 (good record)", validate_synthesis(rec)))
    # 2) synthetic hollow record -> no_figures/no_entities
    cases.append(("hollow {}", validate_synthesis({"what_happened": "x" * 80,
                                                    "key_figures": [], "entities": {}})))
    # 3) synthetic Arabic-echo -> lang_mismatch
    cases.append(("arabic echo", validate_synthesis(
        {"what_happened": "وقعت قطر للطاقة وإكسون موبيل مذكرة تفاهم مع مصر لمعالجة الغاز القبرصي عبر البنية التحتية المصرية",
         "key_figures": [{"value": "60", "unit": "%"}], "entities": {"orgs": ["x"]}})))
    # 4) non-dict -> not_json -> headline_fallback
    cases.append(("not json", validate_synthesis("```json broken")))
    for name, f in cases:
        print(f"{name:34} -> flag={f.quality_flag:14} action={f.action:18} reasons={f.reasons}")
