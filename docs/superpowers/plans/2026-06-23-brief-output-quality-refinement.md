# Brief Output-Quality Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the daily briefs more readable by capping/ranking the per-story figures line, keeping figure dumps out of the executive-summary input, and ranking US/global market roundups last within each region.

**Architecture:** All changes are in the display/ranking layer. Two new env-toggleable config knobs (`FIGURES_MAX`, `MARKET_DEMOTE`); a rewritten `format_figures()` with numeric dedupe + salience ranking + cap; a trimmed summary-input builder; and a new `geoecon/ranking.py` whose demotion score is shared by the render and summary stages. No DB schema, prompt, or extraction changes.

**Tech Stack:** Python 3, pytest, Jinja2. LLM model `deepseek-v3.2` (unchanged).

## Global Constraints

- Figures remain **fully extracted** — no change to `prompts/task_a_cov.txt` or `prompts/geoecon_gate.txt`. Only display/order changes.
- No DB schema migration. No new LLM calls.
- LLM determinism kwargs stay `temperature=0, seed=12345` wherever already present.
- New behavior is reversible via env: `FIGURES_MAX=8` (set `0` to disable the cap), `MARKET_DEMOTE=true` (set `false` to disable demotion).
- Follow existing test idioms in `tests/unit/` (positional `SynthesisRecord(record_id, cluster_id, fields, quality_flag, is_headline_only)`; fake clients/repos as in `test_summary.py`).
- Work happens on branch `refine/brief-output-quality` (already created). Commit after every task.

---

### Task 1: Config knobs `FIGURES_MAX` and `MARKET_DEMOTE`

**Files:**
- Modify: `geoecon/config.py` (`Config` dataclass + `from_env`)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `cfg.figures_max: int` (default `8`), `cfg.market_demote: bool` (default `True`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_config.py` (the module already defines `_ENV` near line 33):

```python
def test_output_quality_knob_defaults():
    cfg = Config.from_env(dict(_ENV))
    assert cfg.figures_max == 8
    assert cfg.market_demote is True


def test_output_quality_knob_overrides():
    cfg = Config.from_env({**_ENV, "FIGURES_MAX": "5", "MARKET_DEMOTE": "false"})
    assert cfg.figures_max == 5
    assert cfg.market_demote is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_config.py -k output_quality -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'figures_max'`.

- [ ] **Step 3: Add the fields**

In `geoecon/config.py`, in the `Config` dataclass, immediately after the line `rank_by_corroboration: bool = False` (and its comment block), before `limit: int | None = None`, add:

```python
    # Output-quality knobs (2026-06-23 refinement). Both reversible via env.
    figures_max: int = 8          # FIGURES_MAX; cap per-story figures line (0 => no cap)
    market_demote: bool = True    # MARKET_DEMOTE; rank US/global market roundups last
```

In `from_env`, immediately before the line `limit=int(limit) if limit else None,`, add:

```python
            figures_max=int(env.get("FIGURES_MAX", "8")),
            market_demote=env.get("MARKET_DEMOTE", "true").lower() == "true",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_config.py -v`
Expected: PASS (all config tests, including the two new ones).

- [ ] **Step 5: Commit**

```bash
git add geoecon/config.py tests/unit/test_config.py
git commit -m "$(printf 'feat(config): add FIGURES_MAX and MARKET_DEMOTE knobs\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 2: Figures line — numeric dedupe + salience rank + cap

**Files:**
- Modify: `geoecon/stages/render.py` (`format_figures` + new module-level helpers + the caller in `run()`)
- Test: `tests/unit/test_render.py`

**Interfaces:**
- Consumes: `cfg.figures_max` (Task 1).
- Produces: `format_figures(key_figures, *, context_text: str = "", max_figures: int | None = None) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_render.py`:

```python
def test_format_figures_caps_to_max():
    figs = [{"value": str(i), "unit": ""} for i in range(1, 13)]  # 12 figures
    out = render.format_figures(figs, max_figures=8)
    assert len(out.split(" · ")) == 8


def test_format_figures_numeric_dedupe_locale_and_currency():
    figs = [{"value": "49.66%", "unit": "%"}, {"value": "49,66%", "unit": "%"},
            {"value": "US$72", "unit": ""}, {"value": "72", "unit": "USD"}]
    out = render.format_figures(figs)
    assert len(out.split(" · ")) == 2  # one percent value, one currency value


def test_format_figures_salience_orders_referenced_first():
    figs = [{"value": "999", "unit": ""},   # not in prose -> ranked last
            {"value": "42", "unit": ""}]     # appears in prose -> ranked first
    out = render.format_figures(figs, context_text="growth hit 42 this year")
    assert out == "42 · 999"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_render.py -k format_figures -v`
Expected: FAIL — `test_format_figures_caps_to_max` errors on the unexpected `max_figures` keyword (and the salience/dedupe assertions fail).

- [ ] **Step 3: Rewrite `format_figures` and helpers**

In `geoecon/stages/render.py`, change the imports line `import logging, os` to:

```python
import logging, os, re
```

Replace the entire current `format_figures` function (the `def format_figures(key_figures) -> str:` block) with:

```python
_NUM_RE = re.compile(r"\d[\d.,]*")
_UNIT_ALIASES = {"us$": "usd", "$": "usd", "usd": "usd", "percent": "%", "%": "%"}


def _norm_num(s: str) -> str:
    """Canonical numeric token for dedupe/matching: thousands stripped, decimal as '.'."""
    m = _NUM_RE.search(str(s or ""))
    if not m:
        return ""
    tok = m.group(0).strip(".,")
    if "." in tok and "," in tok:
        if tok.rfind(",") > tok.rfind("."):       # european: comma is the decimal
            tok = tok.replace(".", "").replace(",", ".")
        else:                                     # anglo: comma is thousands
            tok = tok.replace(",", "")
    elif "," in tok:
        if tok.count(",") == 1 and len(tok.split(",")[1]) <= 2:  # decimal comma
            tok = tok.replace(",", ".")
        else:                                                     # thousands
            tok = tok.replace(",", "")
    return tok


def _norm_unit(u: str) -> str:
    u = str(u or "").strip().lower()
    return _UNIT_ALIASES.get(u, u)


def _canon_key(val: str, unit: str) -> tuple:
    """Dedupe key: (normalized number, normalized unit), inferring the unit from
    currency/percent symbols in the value when the unit field is empty — so
    "US$72" (unit "") and "72" (unit "USD") collapse to the same key."""
    num = _norm_num(val)
    u = _norm_unit(unit)
    if not u:
        low = val.lower()
        if "%" in low or "percent" in low:
            u = "%"
        elif "$" in low or "usd" in low:
            u = "usd"
        elif "€" in low or "eur" in low:
            u = "eur"
    return (num, u)


def _ctx_positions(text: str) -> dict:
    """First-occurrence index of each normalized numeric token in *text*."""
    pos: dict[str, int] = {}
    for i, m in enumerate(_NUM_RE.finditer(text or "")):
        pos.setdefault(_norm_num(m.group(0)), i)
    return pos


def format_figures(key_figures, *, context_text: str = "", max_figures: int | None = None) -> str:
    """Render key_figures as a spaced string, de-duped by numeric value, salience-ranked, capped.

    Each figure is formatted as ``value unit`` (with a space) unless the value already ends
    with the unit (e.g. "1.10%" + "%" -> "1.10%"). Figures whose numeric value appears in
    *context_text* (the story prose) sort first, in prose order; the rest follow in extraction
    order. Duplicates sharing a normalized (value, unit) key are dropped. When *max_figures*
    is truthy, at most that many figures are returned. Figures are joined with " · ".
    """
    ctx_pos = _ctx_positions(context_text)
    seen, items = set(), []
    for k in key_figures or []:
        val = str(k.get("value", "")).strip()
        if not val:
            continue
        unit = str(k.get("unit") or "").strip()
        disp = f"{val} {unit}" if unit and not val.lower().endswith(unit.lower()) else val
        key = _canon_key(val, unit)
        if key in seen:
            continue
        seen.add(key)
        items.append((disp, ctx_pos.get(key[0])))  # second element: prose position or None
    referenced = sorted((it for it in items if it[1] is not None), key=lambda it: it[1])
    unreferenced = [it for it in items if it[1] is None]
    ordered = [disp for disp, _ in (referenced + unreferenced)]
    if max_figures:
        ordered = ordered[:max_figures]
    return " · ".join(ordered)
```

- [ ] **Step 4: Update the caller in `run()`**

In `geoecon/stages/render.py`, in `run()`, replace the line:

```python
            rec.figures_text = format_figures(rec.fields.get("key_figures"))
```

with:

```python
            prose = " ".join(x for x in (rec.fields.get("what_happened_en"),
                                         rec.fields.get("geoeconomic_significance_en")) if x)
            rec.figures_text = format_figures(rec.fields.get("key_figures"),
                                              context_text=prose, max_figures=cfg.figures_max)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_render.py -v`
Expected: PASS — the three new tests AND the pre-existing `test_format_figures_spacing_and_dedup` (back-compat: no `context_text`/`max_figures` => no cap, dedupe preserved).

- [ ] **Step 6: Commit**

```bash
git add geoecon/stages/render.py tests/unit/test_render.py
git commit -m "$(printf 'feat(render): cap, salience-rank, and numeric-dedupe the figures line\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 3: Keep figure dumps out of the executive-summary input

**Files:**
- Modify: `geoecon/stages/summary.py` (`build_summary_messages`)
- Test: `tests/unit/test_summary.py`

**Interfaces:**
- Produces: `build_summary_messages(prompt, region_display, records)` user message no longer contains a `KEY FIGURES` line.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_summary.py` (reuses the module's `_rec`, `_Repo`, `_client`, `_CFG`, `GOOD`):

```python
def test_summary_input_excludes_key_figures():
    tuples = [(_rec(1, "Egypt FX move"), "EG", [], 0.5)]
    repo = _Repo(tuples)
    client = _client(GOOD)
    summary.run(_CFG, repo, 1, client, prompt='SYS {REGION}')
    user_msg = client.calls[0][1][1]["content"]
    assert "KEY FIGURES" not in user_msg
    assert "HEADLINE: Egypt FX move" in user_msg
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_summary.py::test_summary_input_excludes_key_figures -v`
Expected: FAIL on `assert "KEY FIGURES" not in user_msg` (the `_rec` fixture has `key_figures=[{"value":5,"unit":"%"}]`, currently rendered into the input).

- [ ] **Step 3: Trim `build_summary_messages`**

In `geoecon/stages/summary.py`, replace the body of `build_summary_messages` (the `for rec in records:` loop that builds `figs` and the `block`) so the figures are dropped:

```python
def build_summary_messages(prompt: str, region_display: str, records) -> list[dict]:
    """Build [system, user] messages for the region executive summary."""
    parts = []
    for rec in records:
        f = rec.fields
        block = "\n".join(x for x in [
            f"HEADLINE: {f.get('headline_en', '')}",
            f"WHAT HAPPENED: {f.get('what_happened_en', '')}",
            f"SIGNIFICANCE: {f.get('geoeconomic_significance_en', '')}",
        ] if x)
        parts.append(block)
    system = prompt.replace("{REGION}", region_display)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n---\n\n".join(parts)},
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_summary.py -v`
Expected: PASS — the new test plus all existing summary tests (none assert `KEY FIGURES` presence).

- [ ] **Step 5: Commit**

```bash
git add geoecon/stages/summary.py tests/unit/test_summary.py
git commit -m "$(printf 'feat(summary): drop key-figures dump from exec-summary input\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 4: Demote US/global market roundups in ranking

**Files:**
- Create: `geoecon/ranking.py`
- Modify: `geoecon/stages/render.py` (`run()` — import + `score_of`)
- Modify: `geoecon/stages/summary.py` (`run()` — import + `score_of`)
- Test: `tests/unit/test_ranking.py`

**Interfaces:**
- Consumes: `cfg.market_demote` (Task 1); record tuples `(SynthesisRecord, country_fips, source_links, rank_score)` from `repo.records_for_buckets`.
- Produces: `ranking.is_market_roundup(rec) -> bool`; `ranking.make_score_of(cfg) -> Callable[[tuple], float]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_ranking.py`:

```python
from geoecon import ranking, regions
from geoecon.state.models import SynthesisRecord


def _rec(headline, what="x"):
    return SynthesisRecord(1, 10, {"headline_en": headline, "what_happened_en": what}, "ok", False)


class _Cfg:
    market_demote = True


def test_is_market_roundup_flags_us_global_indices():
    assert ranking.is_market_roundup(_rec("S&P 500 slips as AI stocks slide")) is True
    assert ranking.is_market_roundup(_rec("U.S. stocks hold near records on Wall Street")) is True
    assert ranking.is_market_roundup(_rec("Gold futures gap lower on open")) is True


def test_is_market_roundup_spares_country_market_stories():
    assert ranking.is_market_roundup(_rec("Argentina stocks hit record before MSCI decision")) is False
    assert ranking.is_market_roundup(_rec("De la Espriella victory drives Colombian peso higher")) is False
    assert ranking.is_market_roundup(_rec("Oil prices fall as US-Iran talks yield waivers")) is False


def test_make_score_of_demotes_market_when_enabled():
    score_of = ranking.make_score_of(_Cfg())
    market = (_rec("S&P 500 slips"), "IR", [], 0.9)
    country = (_rec("Iran oil license issued"), "IR", [], 0.1)
    assert score_of(market) < score_of(country)


def test_make_score_of_respects_toggle_off():
    class Off:
        market_demote = False
    score_of = ranking.make_score_of(Off())
    assert score_of((_rec("S&P 500 slips"), "IR", [], 0.9)) == 0.9


def test_top_per_bucket_places_market_last():
    score_of = ranking.make_score_of(_Cfg())
    items = [(_rec("S&P 500 slips"), "IR", [], 0.9),
             (_rec("Iran oil license"), "IR", [], 0.2)]
    grouped = regions.top_per_bucket(items, country_of=lambda t: t[1],
                                     score_of=score_of, n=10, active=("middle_east",))
    me = grouped["middle_east"]
    assert me[0][0].fields["headline_en"] == "Iran oil license"
    assert me[-1][0].fields["headline_en"] == "S&P 500 slips"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_ranking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'geoecon.ranking'`.

- [ ] **Step 3: Create `geoecon/ranking.py`**

```python
# geoecon/ranking.py
"""Ranking helpers shared by the render and summary stages.

De-prioritizes US/global market-index and commodity-price roundups so they sort
below country-subject stories within a region (they are never dropped — only ranked
last). Deliberately excludes country-qualified markets ("Argentine stocks",
"Colombian peso") and index *reclassification* news (MSCI), which are legitimate
country-subject geoeconomic stories.
"""
from __future__ import annotations

from typing import Callable

_MARKET_PENALTY = 1000.0

_MARKET_TERMS = (
    "s&p 500", "s&p500", "nasdaq", "dow jones", "the dow", "wall street",
    "russell 2000", "ftse", "nikkei", "hang seng", "stoxx", "treasury yields",
    "gold futures", "gold price", "bullion", "us stocks", "u.s. stocks",
)


def is_market_roundup(rec) -> bool:
    f = getattr(rec, "fields", {}) or {}
    hay = f"{f.get('headline_en', '')} {f.get('what_happened_en', '')}".lower()
    return any(term in hay for term in _MARKET_TERMS)


def make_score_of(cfg) -> Callable[[tuple], float]:
    """Return a score_of(record_tuple) for regions.top_per_bucket.

    record_tuple is (SynthesisRecord, country_fips, source_links, rank_score). When
    cfg.market_demote is on, flagged market roundups get a large penalty so they sort
    below every country-subject story; relative order among them is preserved.
    """
    demote = getattr(cfg, "market_demote", True)

    def score_of(t) -> float:
        base = t[3] or 0.0
        if demote and is_market_roundup(t[0]):
            return base - _MARKET_PENALTY
        return base

    return score_of
```

- [ ] **Step 4: Wire it into render and summary**

In `geoecon/stages/render.py`, in `run()`, change the line `from .. import regions` to:

```python
    from .. import regions, ranking
```

and change `score_of=lambda t: t[3] or 0.0,` to:

```python
        score_of=ranking.make_score_of(cfg),
```

In `geoecon/stages/summary.py`, change the top-level import `from .. import regions` to:

```python
from .. import regions, ranking
```

and in `run()` change `score_of=lambda t: t[3] or 0.0,` to:

```python
        score_of=ranking.make_score_of(cfg),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_ranking.py tests/unit/test_render.py tests/unit/test_summary.py -v`
Expected: PASS — the new ranking tests plus no regressions in render/summary (existing summary cap test still passes; demotion does not affect its non-market fixtures).

- [ ] **Step 6: Commit**

```bash
git add geoecon/ranking.py geoecon/stages/render.py geoecon/stages/summary.py tests/unit/test_ranking.py
git commit -m "$(printf 'feat(ranking): demote US/global market roundups in region ranking\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 5: Full-suite verification and rollout notes

**Files:** none (verification only)

- [ ] **Step 1: Run the entire unit suite**

Run: `python -m pytest tests/unit -q`
Expected: PASS — all tests green (no regressions across config, render, summary, ranking, and the rest).

- [ ] **Step 2: Sanity-check the new knobs are wired end-to-end**

Run: `python -c "from geoecon.config import Config; import os; c=Config.from_env({**os.environ, 'DB_DSN':'x','MAETCENTRUM_KEY':'k','SMTP_HOST':'h','SMTP_USER':'u','SMTP_PASSWORD':'p','FROM_ADDR':'f','RECIPIENTS':'r@x'}); print('figures_max=',c.figures_max,'market_demote=',c.market_demote)"`
Expected: `figures_max= 8 market_demote= True`

- [ ] **Step 3: Pre-ship DRY_RUN (deploy environment)**

In the deploy environment (the k8s job has DB + SMTP), run the pipeline with `DRY_RUN=true` and eyeball the rendered briefs for all three regions:
- AbraSilver-style stories show ≤ 8 salient figures (no number-salad).
- Executive summaries read cleanly (no embedded number blobs).
- Any `S&P 500` / `U.S. stocks` / `gold futures` items appear **last** in their region (or are trimmed first when the region overflows `region_cap`).
Confirm a genuine `Argentina stocks … MSCI` / `Colombian peso` story is **not** demoted.

- [ ] **Step 4: (No code change) Record outcome**

If the DRY_RUN looks right, the branch is ready to merge/build/deploy via the usual flow. If a market story is mis-ordered, tune `_MARKET_TERMS` in `geoecon/ranking.py` (or set `MARKET_DEMOTE=false`); if the figures cap feels tight/loose, adjust `FIGURES_MAX`.

---

## Notes on scope (deferred, by prior decision)

- Event-grouping/dedup (the Colombia storyline appearing ~5×) is **not** addressed here.
- The gate's keep/drop and `primary_country` logic is unchanged — US/global market stories still enter the Middle East bucket; they are only ranked last.
- The "Lasperia" name garble, the junk author-bio source URL, and North Africa coverage/recall are separate items, not in this plan.
