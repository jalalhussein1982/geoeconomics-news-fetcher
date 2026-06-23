# Brief Output-Quality Refinement — Design

**Date:** 2026-06-23
**Status:** Approved (design), pending implementation plan
**Author:** Pairing session (Claude + Jalal)

## Motivation

A double-check of the live 2026-06-23 briefs (Latin America / Middle East / North Africa)
verified that the **facts are accurate** (Colombia result, Iran oil license, Switzerland
talks, Fahmy → Arab League, Paraguay OECD, Titan/SolarizEgypt all confirmed against
independent sources). The weaknesses are **presentation and ordering**, concentrated in
finance/market stories:

1. **Number-salad.** Finance- and table-heavy sources dump 80–150 figures into a single
   story's metrics line (AbraSilver DFS ~150, S&P 500 item ~80), including locale-variant
   duplicates (`49.66% · 49,66%`, `US$72 · 72 USD`).
2. **Executive summaries ingest the same dumps.** The region-summary LLM is fed every
   extracted figure per story, so finance stories can muddy the regional summary.
3. **US/global market roundups crowd into regional briefs.** Stories like
   *"S&P 500 slips … oil eases on Iran talks"*, *"U.S. stocks hold near records …"*, and
   *"Gold futures gap lower …"* get `primary_country = Iran` at the gate (Iran is merely
   named) and land in the Middle East brief alongside genuine regional news.

## Goals

- Cap and rank the per-story figures line so only the salient numbers show.
- Stop feeding raw figure dumps into the executive-summary LLM input.
- De-prioritize (rank last, **do not drop**) US/global market-index/commodity roundups
  within each regional brief.

## Non-goals (explicitly deferred)

- **Dedup / event grouping** — the Colombia storyline appearing as ~5 separate items is a
  grouping-definition choice; not changed here.
- **Gate keep/drop and `primary_country` logic** — US/global market stories still *enter*
  the Middle East bucket; we only rank them last. No semantic re-tagging.
- **Extraction / synthesis prompts** — figures remain *fully extracted*; corroboration and
  validation are untouched. We change only what is displayed and how it is ordered.
- Out of scope entirely: North Africa coverage/recall, the "Lasperia" name garble, and the
  junk author-bio source URL. These are tracked separately.

## Guiding constraint

All three fixes live in the **display / ranking layer**. No DB schema migration, no prompt
changes, no new LLM calls. Both new behaviors are env-toggleable and reversible.

---

## Fix A — Figures line: cap + salience-rank + numeric dedupe

**File:** `geoecon/stages/render.py` → `format_figures()`
**Config:** new `FIGURES_MAX` (default `8`)

### Signature change

```python
def format_figures(key_figures, *, context_text: str = "",
                   max_figures: int | None = None) -> str:
```

Caller (`render.py` `run()`, currently line 54) passes the story prose as context and the
cap:

```python
prose = " ".join(filter(None, [
    rec.fields.get("what_happened_en"),
    rec.fields.get("geoeconomic_significance_en"),
]))
rec.figures_text = format_figures(
    rec.fields.get("key_figures"), context_text=prose, max_figures=cfg.figures_max)
```

### Algorithm

1. **Build display + canonical key** for each figure with a non-empty value. Display is the
   existing `value [unit]` form. Canonical key = `(_norm_num(value), _norm_unit(unit))`.
   - `_norm_num` reduces a numeric string to a canonical token: strip spaces and currency
     symbols/words; when both `.` and `,` are present, treat the **last** separator as the
     decimal and drop the other as a thousands separator; when only `,` is present and it
     reads as a decimal comma, convert to `.`. Examples that must collapse to one key:
     `49.66%`/`49,66%`; `US$72`/`72 USD`; `3.388,48`/`3,388.48`.
   - `_norm_unit` lowercases and maps common synonyms (`usd`/`us$`/`$` → `usd`,
     `%`/`percent` → `%`). Pragmatic; finalized in the implementation plan.
2. **Salience rank.** A figure is *referenced* if its `_norm_num(value)` token appears
   (token-boundary match) in `_norm_num(context_text)`. Referenced figures sort first, by
   the position of first occurrence in `context_text`; unreferenced figures follow in
   original extraction order.
3. **Dedupe** by canonical key, preserving the salience order (keep the first/most-salient
   display for each key). This subsumes the current exact-string dedupe.
4. **Cap** to `max_figures` when set (`None`/`0` → no cap). Default via `cfg.figures_max`.

Result: AbraSilver's ~150-figure line becomes the ~8 numbers its prose actually uses.

### Rejected alternative

Editing the extraction prompt (`task_a_cov.txt`, the "enumerate EVERY … omit none" line) to
emit fewer figures — riskier (degrades `what_happened` completeness and corroboration
inputs) and harder to tune than a render-time cap.

---

## Fix B — Keep figure dumps out of the executive-summary input

**File:** `geoecon/stages/summary.py` → `build_summary_messages()`

Remove the `KEY FIGURES: …` block (current lines 18–21 build `figs`; line 26 appends it).
Each story's summary input becomes headline + what_happened + significance only — which
already carry the salient numbers in prose. The region-summary LLM stops ingesting
150-number blobs from finance stories.

### Rejected alternative

Feeding the Fix-A capped top-N figures instead — still noise, and needlessly couples B to A.

---

## Fix C — De-prioritize market/index roundups (rank last, never drop)

**New module:** `geoecon/ranking.py`
**Files touched:** `geoecon/stages/render.py`, `geoecon/stages/summary.py`
**Config:** new `MARKET_DEMOTE` (default `true`)

Both `render.py` and `summary.py` rank a bucket via
`regions.top_per_bucket(..., score_of=lambda t: t[3] or 0.0, n=cfg.region_cap, ...)`, where
`t = (SynthesisRecord, country_fips, source_links, rank_score)`. We replace the inline
`score_of` with a shared factory so demotion is applied identically in both stages.

```python
# geoecon/ranking.py
from __future__ import annotations

_MARKET_PENALTY = 1000.0

# US/global index & commodity-price roundups. Deliberately EXCLUDES country-qualified
# markets ("Argentine stocks", "Colombian peso") and index *reclassification* (MSCI),
# which are legitimate country-subject geoeconomic stories.
_MARKET_TERMS = (
    "s&p 500", "s&p500", "nasdaq", "dow jones", "the dow", "wall street",
    "russell 2000", "ftse", "nikkei", "hang seng", "stoxx", "treasury yields",
    "gold futures", "gold price", "bullion", "us stocks", "u.s. stocks",
)

def is_market_roundup(rec) -> bool:
    f = getattr(rec, "fields", {}) or {}
    hay = f"{f.get('headline_en', '')} {f.get('what_happened_en', '')}".lower()
    return any(term in hay for term in _MARKET_TERMS)

def make_score_of(cfg):
    demote = getattr(cfg, "market_demote", True)
    def score_of(t):
        base = t[3] or 0.0
        return base - _MARKET_PENALTY if (demote and is_market_roundup(t[0])) else base
    return score_of
```

`render.py` and `summary.py` import `ranking` and pass `score_of=ranking.make_score_of(cfg)`.

### Behavior

- Base gate scores are in `[0, 1]`; the `-1000` penalty guarantees any flagged story sorts
  **below** every country-subject story. Flagged stories keep their relative order by base.
- On a >`region_cap` day, flagged stories are trimmed first by `top_per_bucket`'s `[:n]`.
  On a light day they render last. **Never dropped at the gate.**

### Precision check against the 2026-06-23 ME brief

Correctly demoted: *"S&P 500 slips…"* (`s&p 500`), *"U.S. stocks hold near records…Wall
Street"* (`u.s. stocks`/`wall street`), *"Gold futures gap lower…"* (`gold futures`).
Correctly **not** demoted: *"Oil prices fall as U.S.–Iran talks yield waivers"*,
*"Argentina stocks hit record…before MSCI decision"* (no term matches; `msci` excluded),
*"Credicorp sees more upside in Colombian stocks"* (`colombian stocks` ≠ `us stocks`).

### Rejected alternative

Adding an LLM `is_market` flag to the gate — needs a schema + prompt change for what is
purely an ordering tweak. The heuristic's worst case is benign (mis-order, not loss). If
precision proves insufficient, promoting to an LLM flag is a clean follow-up.

---

## Config additions

`geoecon/config.py` (`Config` dataclass + `from_env`):

```python
figures_max: int = 8       # FIGURES_MAX
market_demote: bool = True # MARKET_DEMOTE
```

```python
figures_max=int(env.get("FIGURES_MAX", "8")),
market_demote=env.get("MARKET_DEMOTE", "true").lower() == "true",
```

## Testing

- **`tests/unit/test_render.py`**
  - Cap: 12 figures, `max_figures=8` → 8 rendered.
  - Numeric dedupe: `["49.66%", "49,66%"]` → one; `{value:"US$72"}` + `{value:"72",unit:"USD"}` → one.
  - Salience: a figure referenced in `context_text` precedes an unreferenced one that came
    earlier in extraction order.
  - `max_figures=None` → no cap (back-compat).
- **`tests/unit/test_summary.py`**
  - `build_summary_messages(...)` user content contains no `KEY FIGURES`.
- **`tests/unit/test_ranking.py`** (new)
  - `is_market_roundup` True for an `S&P 500` headline; False for the Argentina/MSCI headline.
  - `make_score_of(cfg)` returns `base - 1000` for a flagged record, `base` otherwise; with
    `MARKET_DEMOTE=false` returns `base` for both.
  - `top_per_bucket` places a flagged record last.

## Rollout

1. Land code + tests; run the unit suite.
2. `DRY_RUN` against a real run; eyeball a before/after brief for all three regions
   (same validation pattern as prior deploys).
3. Ship with defaults on (`FIGURES_MAX=8`, `MARKET_DEMOTE=true`); both reversible via env.

## Risks & reversibility

- **Fix C heuristic** false positives/negatives — mitigated by demote-only semantics
  (benign), a precise US/global term list, env toggle, and a clear upgrade path to an LLM
  flag.
- **Fix A salience matching** is approximate — but all figures remain extracted; only
  display order and an 8-item cap are affected. `FIGURES_MAX` tunes the aggressiveness.
- Every change is behind a default-valued env var; setting `FIGURES_MAX=0` /
  `MARKET_DEMOTE=false` restores prior behavior without a code change.
