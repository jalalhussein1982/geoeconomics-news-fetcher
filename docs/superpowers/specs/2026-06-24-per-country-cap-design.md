# Per-country slot cap in region ranking — design

**Date:** 2026-06-24
**Status:** Draft for review

## 1. Goal

Stop a single country from dominating a regional brief. In the 2026-06-24 deploy's
validation run (run_id 20), Iran took **9 of 12** Middle East slots (US-Iran / Hormuz
event from many angles) and Argentina dominated LATAM (MSCI story ×4). Each event is
already one record, so the repetition is *many distinct events about the same country*.
The fix is a **per-country cap within a region**: no country may take more than `N`
of a region's slots, with the country's surplus demoted and used only to backfill if
the region would otherwise under-fill.

This is a ranking/selection change only — no change to discovery, gate, clustering,
synthesis, or the curated-source / economy-first nudge from the prior change.

## 2. Non-goals

- No cross-country topical dedup (collapsing different countries' takes on one topic).
  That needs a similarity/LLM step and is explicitly out of scope (decided in design).
- No new LLM/embedding calls, no schema change, no migration.
- No change to per-region `region_cap` (still 12), market-demote, or the curated nudge.

## 3. Background (current selection)

`geoecon/regions.py` `top_per_bucket` is the single selection point, consumed by both
`render.run` and `summary.run`:

```python
def top_per_bucket(items, country_of, score_of, n, active=BUCKET_ORDER):
    grouped = group_by_bucket(items, country_of, active)
    return {b: sorted(v, key=score_of, reverse=True)[:n] for b, v in grouped.items()}
```

Call sites (both pass `country_of=lambda t: t[1]` (country_fips), `score_of=ranking.make_score_of(cfg)`, `n=cfg.region_cap`):
- `geoecon/stages/render.py` — selects the records shown in the brief.
- `geoecon/stages/summary.py` — selects the records fed to the region-summary LLM.

`ranking.make_score_of(cfg)` already encodes market-demote (−1000) and the curated /
economy-section boosts, so the `score_of` order the cap sees already reflects them.

## 4. Design

### 4.1 New helper — `cap_per_country`

A pure function in `geoecon/regions.py`:

```python
def cap_per_country(sorted_items, country_of, cap, n):
    """From score-desc `sorted_items`, keep <= `cap` per country, score order preserved.
    If the capped set has >= n items, return its top n. Otherwise backfill with the
    highest-score over-cap surplus so the result still has up to n items (never shrinks
    the brief below what an uncapped selection would yield)."""
    selected, overflow, per_country = [], [], {}
    for it in sorted_items:                      # already score-desc
        c = country_of(it)
        if per_country.get(c, 0) < cap:
            selected.append(it)
            per_country[c] = per_country.get(c, 0) + 1
        else:
            overflow.append(it)
    if len(selected) >= n:
        return selected[:n]
    return (selected + overflow)[:n]             # overflow is also score-desc
```

Properties:
- **Score order preserved** in the output (both `selected` and `overflow` are slices of
  the already-sorted input).
- **Strict refinement**: when no country exceeds `cap`, `overflow` is empty and
  `selected[:n]` equals `sorted(v)[:n]` — byte-identical to today.
- **Never shrinks**: backfill fills up to `n` from the surplus when distinct-country
  supply is thin (a genuinely one-country day still fills, capped concentration aside).

### 4.2 Wire into `top_per_bucket`

Add an optional `country_cap` param (default `None` = off):

```python
def top_per_bucket(items, country_of, score_of, n, active=BUCKET_ORDER, country_cap=None):
    grouped = group_by_bucket(items, country_of, active)
    out = {}
    for b, v in grouped.items():
        s = sorted(v, key=score_of, reverse=True)
        out[b] = cap_per_country(s, country_of, country_cap, n) if country_cap else s[:n]
    return out
```

`country_cap` falsy (None or 0) → current behavior exactly.

### 4.3 Config knob

`geoecon/config.py`, after the curated knobs:

| Field | Default | Env | Purpose |
|---|---|---|---|
| `country_cap` | `4` | `COUNTRY_CAP` | max records per country per region; `0` disables |

Loaded as `country_cap=int(env.get("COUNTRY_CAP", "4"))`. Ships ON at 4; `COUNTRY_CAP=0`
reverts ranking to byte-identical prior behavior.

### 4.4 Call-site changes

Both `render.run` and `summary.run` add `country_cap=cfg.country_cap` to their existing
`regions.top_per_bucket(...)` call — so the brief body and the region-summary input see
the same diverse set and stay coherent.

## 5. Composition (no conflicts)

- **Market-demote**: market roundups already carry −1000, so they sort last → land in
  `overflow`, surfacing only if a region is starved. Unchanged.
- **Curated / economy nudge**: adjusts `score_of`, deciding *which* of a country's
  stories rank in its top-`cap`. Nudge picks which; cap limits how many. Compose cleanly.
- **Egypt dual-bucket**: Egypt is capped independently in `middle_east` and
  `north_africa` (the cap is per country *per bucket*, since `top_per_bucket` processes
  buckets separately).

## 6. Files touched

- `geoecon/regions.py` — new `cap_per_country`; `top_per_bucket` gains `country_cap`.
- `geoecon/config.py` — `country_cap` field + env loading.
- `geoecon/stages/render.py` — pass `country_cap=cfg.country_cap`.
- `geoecon/stages/summary.py` — pass `country_cap=cfg.country_cap`.
- Tests under `tests/unit/` for `cap_per_country`, `top_per_bucket` with cap, config knob.

## 7. Testing

- **`cap_per_country`**:
  - cap kicks in — 9 same-country items, cap 4, n 12, plus 8 other-country items →
    result has exactly 4 of the dominant country, rest are others, score order preserved.
  - backfill — 4 dominant + 5 others (9 distinct under-cap), n 12 → fills to 9, then
    backfills dominant surplus to reach min(n, total).
  - no-op — no country exceeds cap → identical to `sorted[:n]`.
  - disabled — `country_cap=0`/`None` path returns `sorted[:n]`.
- **`top_per_bucket`** with `country_cap=4`: a bucket with a dominant country returns at
  most 4 of it; with `country_cap=None` returns today's result.
- **config**: `country_cap` defaults to 4; `COUNTRY_CAP=0` and `COUNTRY_CAP=3` override.
- Full `pytest` green; existing ranking/region/render/summary tests unchanged (cap
  defaults off in any stub `cfg` lacking the attr — call sites pass `cfg.country_cap`,
  and `top_per_bucket`'s param defaults `None`).

## 8. Rollout

Land the change, run the suite, then redeploy (new image, e.g. `20260624-2`) so the live
07:00 brief picks it up. Validate on a smoke/real run: no region shows >4 records from one
country (unless backfilled for lack of supply); spot-check that MENA now carries non-Iran
Gulf/Egypt economics. Reversible at runtime via `COUNTRY_CAP=0`.
