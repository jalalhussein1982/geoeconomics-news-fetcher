# Per-Country Slot Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cap how many records one country can take in a regional brief (default 4), so a single dominant country can't swamp a region; backfill keeps the brief full when supply is thin.

**Architecture:** A pure helper `cap_per_country` in `geoecon/regions.py`, applied by `top_per_bucket` after its existing score-sort via a new optional `country_cap` param. A `COUNTRY_CAP` config knob (default 4, 0 disables) is read at the render/summary call sites. No schema, no migration, no new LLM calls.

**Tech Stack:** Python 3, pytest.

## Global Constraints

- Selection-only change: no change to discovery, gate, clustering, synthesis, market-demote, or the curated/economy nudge.
- `cap_per_country` preserves score order, is a strict refinement (identical to today when no country exceeds the cap), and never shrinks the brief below what an uncapped selection would yield (backfills the dominant country's surplus when distinct-country supply is thin).
- Cap is per country **per region** (`top_per_bucket` already processes buckets separately; Egypt is capped independently in `middle_east` and `north_africa`).
- Config knob `country_cap: int = 4` (env `COUNTRY_CAP`); `0` (or None at the param) disables → ranking byte-identical to today.
- Call sites read the knob defensively as `getattr(cfg, "country_cap", None)` — matching `ranking.py`'s existing `getattr(cfg, ...)` pattern for knobs, so test `cfg` stubs that lack the attr keep today's behavior with no edits.
- TDD: failing test → run-fail → minimal impl → run-pass → commit. Run with `.venv/bin/python -m pytest`.
- Do NOT commit on `main`. Branch first (Task 0).

---

## Task 0: Branch

**Files:** none (git only)

- [ ] **Step 1: Create the feature branch**

```bash
git checkout -b feat/per-country-cap
```

- [ ] **Step 2: Confirm a clean baseline**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (DB-gated tests SKIP without `TEST_DATABASE_URL`).

---

## Task 1: `cap_per_country` helper + `top_per_bucket` param

**Files:**
- Modify: `geoecon/regions.py` (add `cap_per_country`; add `country_cap` param to `top_per_bucket`)
- Test: `tests/unit/test_regions.py`

**Interfaces:**
- Produces: `cap_per_country(sorted_items, country_of, cap, n) -> list` and `top_per_bucket(items, country_of, score_of, n, active=BUCKET_ORDER, country_cap=None) -> dict`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_regions.py`:

```python
def test_cap_per_country_limits_dominant_when_supply_exists():
    # 9 IR (highest scores) + 8 distinct others; cap 4, n 12
    items = ([("IR", 100 - i) for i in range(9)]
             + [("QA", 20), ("AE", 19), ("LE", 18), ("KU", 17),
                ("BA", 16), ("JO", 15), ("SY", 14), ("YM", 13)])
    out = regions.cap_per_country(items, lambda t: t[0], cap=4, n=12)
    assert len(out) == 12
    assert sum(1 for t in out if t[0] == "IR") == 4          # IR capped at 4
    assert [t[0] for t in out[:4]] == ["IR", "IR", "IR", "IR"]  # IR top-4 by score kept, order preserved


def test_cap_per_country_backfills_when_supply_thin():
    # 7 IR + 2 others = 9 total; cap 4, n 12 -> never invents, backfills IR surplus to stay full
    items = [("IR", 100 - i) for i in range(7)] + [("QA", 5), ("AE", 4)]
    out = regions.cap_per_country(items, lambda t: t[0], cap=4, n=12)
    assert len(out) == 9                                      # only 9 items exist
    assert sum(1 for t in out if t[0] == "IR") == 7          # 4 + 3 backfilled surplus


def test_cap_per_country_noop_when_under_cap():
    items = [("QA", 5), ("AE", 4), ("IR", 3), ("IR", 2)]
    assert regions.cap_per_country(items, lambda t: t[0], cap=4, n=12) == items


def test_top_per_bucket_applies_country_cap():
    score_of = lambda t: t[3]
    items = ([("r", "IR", None, 100 - i) for i in range(9)]
             + [("r", "QA", None, 20), ("r", "AE", None, 19), ("r", "LE", None, 18),
                ("r", "KU", None, 17), ("r", "BA", None, 16)])
    grouped = regions.top_per_bucket(items, country_of=lambda t: t[1], score_of=score_of,
                                     n=12, active=("middle_east",), country_cap=4)
    me = grouped["middle_east"]
    assert len(me) == 12 and sum(1 for t in me if t[1] == "IR") == 4


def test_top_per_bucket_cap_none_is_unchanged():
    score_of = lambda t: t[3]
    items = [("r", "IR", None, 100 - i) for i in range(9)] + [("r", "QA", None, 1)]
    off = regions.top_per_bucket(items, country_of=lambda t: t[1], score_of=score_of,
                                 n=12, active=("middle_east",))
    explicit_none = regions.top_per_bucket(items, country_of=lambda t: t[1], score_of=score_of,
                                           n=12, active=("middle_east",), country_cap=None)
    assert off == explicit_none
    assert len(off["middle_east"]) == 10   # all 10 items, no cap applied
```

Confirm `import` at the top of the file includes `from geoecon import regions` (it already does — the existing region tests use `regions.top_per_bucket`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_regions.py -k "cap_per_country or country_cap or cap_none" -v`
Expected: FAIL — `AttributeError: module 'geoecon.regions' has no attribute 'cap_per_country'` / unexpected `country_cap` kwarg.

- [ ] **Step 3: Implement in `geoecon/regions.py`**

Add `cap_per_country` just above `top_per_bucket`:

```python
def cap_per_country(sorted_items, country_of: Callable, cap: int, n: int) -> list:
    """From score-desc *sorted_items*, keep at most *cap* per country (score order
    preserved). If the capped set has >= n items, return its top n. Otherwise backfill
    with the highest-score over-cap surplus so the result still fills to n (never
    shrinks the brief below an uncapped selection — a one-country day still fills)."""
    selected, overflow, per_country = [], [], {}
    for it in sorted_items:
        c = country_of(it)
        if per_country.get(c, 0) < cap:
            selected.append(it)
            per_country[c] = per_country.get(c, 0) + 1
        else:
            overflow.append(it)
    if len(selected) >= n:
        return selected[:n]
    return (selected + overflow)[:n]
```

Replace `top_per_bucket` with:

```python
def top_per_bucket(items, country_of: Callable, score_of: Callable, n: int,
                   active: Iterable[str] = BUCKET_ORDER, country_cap: int | None = None) -> dict[str, list]:
    """group_by_bucket, sort each bucket by score_of desc, keep the top n. When
    country_cap is set (>0), no country takes more than that many of a bucket's n slots
    (see cap_per_country); when falsy, behaviour is the prior plain top-n."""
    grouped = group_by_bucket(items, country_of, active)
    out: dict[str, list] = {}
    for b, v in grouped.items():
        s = sorted(v, key=score_of, reverse=True)
        out[b] = cap_per_country(s, country_of, country_cap, n) if country_cap else s[:n]
    return out
```

`Callable` and `Iterable` are already imported at the top of `regions.py` (`from typing import Callable, Iterable`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_regions.py -v`
Expected: PASS (new tests + all existing region tests, which call `top_per_bucket` without `country_cap` → unchanged).

- [ ] **Step 5: Commit**

```bash
git add geoecon/regions.py tests/unit/test_regions.py
git commit -m "feat(regions): per-country cap helper + top_per_bucket country_cap param"
```

---

## Task 2: `country_cap` config knob

**Files:**
- Modify: `geoecon/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `Config.country_cap: int = 4` (env `COUNTRY_CAP`).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_config.py`:

```python
def test_country_cap_default_and_override():
    assert Config.from_env(dict(_ENV)).country_cap == 4
    assert Config.from_env({**_ENV, "COUNTRY_CAP": "0"}).country_cap == 0
    assert Config.from_env({**_ENV, "COUNTRY_CAP": "3"}).country_cap == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py::test_country_cap_default_and_override -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'country_cap'`.

- [ ] **Step 3: Add the field + env loading**

In `geoecon/config.py`, add the field after `economy_section_boost` (before `limit`):

```python
    economy_section_boost: float = 0.10   # ECONOMY_SECTION_BOOST
    country_cap: int = 4                   # COUNTRY_CAP; max records per country per region (0 disables)
    limit: int | None = None
```

In `from_env`, after the `economy_section_boost=...` line:

```python
            economy_section_boost=float(env.get("ECONOMY_SECTION_BOOST", "0.10")),
            country_cap=int(env.get("COUNTRY_CAP", "4")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add geoecon/config.py tests/unit/test_config.py
git commit -m "feat(config): country_cap knob (default 4, COUNTRY_CAP=0 disables)"
```

---

## Task 3: Wire the cap into render + summary selection

**Files:**
- Modify: `geoecon/stages/render.py`, `geoecon/stages/summary.py`
- Test: full suite (no new test — behavior is covered by Task 1; this wires it in)

**Interfaces:**
- Consumes: `Config.country_cap` (Task 2); `top_per_bucket(..., country_cap=...)` (Task 1).

- [ ] **Step 1: Add the param to both call sites**

In `geoecon/stages/render.py`, find the `regions.top_per_bucket(...)` call inside `run` (it passes `country_of=lambda t: t[1]`, `score_of=ranking.make_score_of(cfg)`, `n=cfg.region_cap`, `active=cfg.buckets`) and add a final argument so it reads:

```python
    grouped = regions.top_per_bucket(
        repo.records_for_buckets(run_id, cfg.rank_by_corroboration), country_of=lambda t: t[1],
        score_of=ranking.make_score_of(cfg), n=cfg.region_cap, active=cfg.buckets,
        country_cap=getattr(cfg, "country_cap", None))
```

In `geoecon/stages/summary.py`, the call in `run` is:

```python
    grouped = regions.top_per_bucket(
        repo.records_for_buckets(run_id, cfg.rank_by_corroboration), country_of=lambda t: t[1],
        score_of=ranking.make_score_of(cfg), n=cfg.region_cap, active=cfg.buckets)
```

Change it to add the same final argument:

```python
    grouped = regions.top_per_bucket(
        repo.records_for_buckets(run_id, cfg.rank_by_corroboration), country_of=lambda t: t[1],
        score_of=ranking.make_score_of(cfg), n=cfg.region_cap, active=cfg.buckets,
        country_cap=getattr(cfg, "country_cap", None))
```

Using `getattr(cfg, "country_cap", None)` (not `cfg.country_cap`) means test `cfg` stubs that don't define the attr fall back to `None` → cap off → their assertions are unchanged. Production `Config` always defines `country_cap` (Task 2), so the cap is active in the live pipeline.

- [ ] **Step 2: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (existing render/summary tests unaffected — their stub `cfg` lacks `country_cap` → `getattr` yields `None` → prior behavior; DB tests SKIP).

- [ ] **Step 3: Commit**

```bash
git add geoecon/stages/render.py geoecon/stages/summary.py
git commit -m "feat(ranking): apply country_cap in render + summary selection"
```

---

## Task 4: Full-suite verification

**Files:** none (validation only)

- [ ] **Step 1: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (DB tests SKIP without `TEST_DATABASE_URL`).

- [ ] **Step 2: Behavior spot-check (no DB needed)**

Run:

```bash
.venv/bin/python -c "
from geoecon import regions
items=[('r','IR',None,100-i) for i in range(9)]+[('r','QA',None,5),('r','AE',None,4),('r','LE',None,3),('r','KU',None,2),('r','BA',None,1)]
g=regions.top_per_bucket(items, country_of=lambda t:t[1], score_of=lambda t:t[3], n=12, active=('middle_east',), country_cap=4)
me=g['middle_east']; print('total',len(me),'IR',sum(1 for t in me if t[1]=='IR'))
"
```

Expected: `total 12 IR 4` (with cap) — confirms a dominant country is capped at 4 when other supply exists.

---

## Rollout (post-merge, separate step)

Merge to `main`, then redeploy so the live 07:00 brief picks it up:
1. Bump `deploy/cronjob.yaml` + `deploy/smoke-job.yaml` image to `20260624-2`; commit + push.
2. Build/push `jalalhussein/geoecon-pipeline:20260624-2` (linux/amd64, amd64builder).
3. `kubectl apply -f deploy/cronjob.yaml` (carries the new image; `country_cap` defaults to 4 in-code, no env needed — optionally add `COUNTRY_CAP` env for an auditable record).
4. Smoke (DRY_RUN) and confirm no region shows >4 records from one country (unless backfilled for thin supply); spot-check MENA now carries non-Iran Gulf/Egypt economics. Reversible at runtime via `COUNTRY_CAP=0`.

---

## Self-Review (completed by plan author)

- **Spec coverage:** §4.1 `cap_per_country` → Task 1; §4.2 `top_per_bucket` param → Task 1; §4.3 config knob → Task 2; §4.4 call sites → Task 3; §7 testing → Task 1/2 tests + Task 4 spot-check; §8 rollout → Rollout section.
- **Placeholder scan:** none — every step has concrete code/commands. The image tag `20260624-2` in Rollout is the concrete next tag.
- **Type consistency:** `cap_per_country(sorted_items, country_of, cap, n)` and `top_per_bucket(..., country_cap=None)` are defined identically in Task 1 and consumed in Task 3; `Config.country_cap` defined in Task 2 is read via `getattr(cfg, "country_cap", None)` in Task 3.
- **Note on backfill vs strict cap:** `cap_per_country` intentionally backfills the dominant country's surplus when supply is thin (Task 1's `test_cap_per_country_backfills_when_supply_thin` asserts 7 IR survive) — this is the approved "never shrink the brief" behavior, not a bug.
