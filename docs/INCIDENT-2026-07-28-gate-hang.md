# Incident 2026-07-28 — run 67 hung 6h50m in `gate`; hardening plan

**Status:** incident resolved (job killed, run 67 marked failed).

> **Update 2026-07-29.** deepseek is *still* down and has degraded from HTTP 500 to HTTP 400
> `"no healthy deployments"`; the gemma4 workaround stays. P0-a (client logging), P0-c (null-gate
> guard) and P1-a (tighten the hang-guard) have since been **implemented** — P1-a is live,
> the code fixes await an image rebuild. P0-b (circuit breaker), P1-b (model fallback) and
> P2-a (`find_resumable_run`, now confirmed dead code) remain open. A *second, unrelated*
> incident followed the next night — see
> [`INCIDENT-2026-07-29-extraction-hard-kill.md`](INCIDENT-2026-07-29-extraction-hard-kill.md),
> which supersedes the status of the items below.

---

## 1. What happened

| | |
|---|---|
| Run | 67, started 2026-07-27 23:00:03Z (01:00 Europe/Prague) |
| Hung stage | `gate`, started 23:04:55Z, still `running` at 05:55Z |
| Detected by | `geoecon-watchdog` — correctly alerted, but could not name a failed stage |
| Outcome | 0 articles kept of 1602. No brief produced. Job killed manually 06:05Z |

**Root cause — upstream, not ours.** The e-INFRA LiteLLM gateway (`llm.ai.e-infra.cz`) stayed
healthy (`/v1/models` → 200 in 0.2s), but the vLLM backend behind the **`deepseek` model group**
was unreachable for the entire stage:

```
http 500: litellm.InternalServerError: InternalServerError: Hosted_vllmException -
Cannot connect to host frps-proxy-tunnel.vllm-ns.svc.cluster.local:8001
[Connect call failed ('10.43.233.61', 8001)]. Received Model Group=deepseek
```

Verified during the incident: `deepseek` and `deepseek-v4-pro` both 500; **`kimi`, `glm-5`,
`qwen3.5` all responded normally.** `GATE_MODEL`, `SYNTHESIS_MODEL` and `DEDUP_MODEL` are all
set to `deepseek`, so a single upstream backend took out the whole pipeline.

**Why a clean upstream 500 became a silent 8-hour hang** — four of our own faults multiplied:

1. **HTTP 500 is retryable** (`client.py:89`), so every call burned all 5 attempts
   (`max_retries=4`) plus ~15s of backoff before giving up — ~40s per `complete()`.
2. **The re-gate cascade multiplies that.** For one 15-article batch:
   top-level `_judge(retries=1)` = 2 calls → `_judge_subbatches` = 3 chunks × 2 = 6 calls →
   `_judge_per_article` = 15 calls. **23 `complete()` calls = up to 115 HTTP requests per batch**,
   ~15 min wall-clock. Across 107 batches that is ~12,300 doomed requests.
3. **The client logs nothing.** `geoecon/llm/client.py` does not import `logging` at all. Every
   failure at `client.py:126`, `:131`, `:137` is swallowed silently. The pod's last log line was
   `extraction: {...}` at 23:04:55 — 6h50m of total failure produced **zero** output.
4. **Gate treats "everything dropped unjudged" as success.** All 1500 verdicts were
   `gate_unparsed_dropped`. The `>5%` warning at `gate.py:194` only fires *after* the whole stage
   finishes, and only as a `log.warning` — the stage would still have returned `completed` with
   `gate_kept=0`, handing an empty article set to `cluster`/`synthesis`.

**The hang-guard could not have saved it either.** `activeDeadlineSeconds=28800` (8h) from a
23:00Z start fires at 07:00Z = **09:00 Prague — two hours after the 07:00 Prague delivery target.**

---

## 2. Proposed fixes, in priority order

### P0-a — Make the LLM client observable
`geoecon/llm/client.py` — add a module logger and log every failure path.

- `log.warning` on each retryable failure: model, attempt `n/max`, status code, truncated body, latency.
- `log.error` on final give-up (`:137`) and on non-retryable error (`:133`).

Alone, this would have turned 6h50m of silence into an obvious, greppable failure within seconds.

### P0-b — Circuit breaker on systemic LLM failure
Add a thread-safe consecutive-total-failure counter to `LLMClient` (it is shared by all 4 gate
workers, so the counter must be lock-guarded; reset on any success).

- After **N consecutive fully-exhausted `complete()` calls** (suggest N=5), raise
  `SystemicLLMFailure` instead of returning an error response.
- It propagates out of `_gate_batch` → `fut.result()` (`gate.py:191`) → `run_pipeline`'s
  `except Exception` (`orchestrator.py:94`) → `repo.fail_stage` + crash alert naming the real cause.

**Effect: an 8-hour silent hang becomes a ~2-minute clean failure with an accurate alert** — and the
watchdog's "Failed stages" section would have named `gate` instead of "(no failed stage recorded)".

### P0-c — Never let a null gate pass as success
In `gate.run`, after the loop: if `gate_total > 0` and (`gate_kept == 0` or
`gate_unparsed_dropped > 50%` of total), **raise** rather than `log.warning`. A run that judged
nothing must fail loudly, not emit an empty newsletter. Replaces the too-late warning at `gate.py:194`.

### P1-a — Tighten the hang-guard below the delivery window
`deploy/cronjob.yaml`: `activeDeadlineSeconds: 28800` → **`19800` (5.5h)**, firing at 04:30Z /
06:30 Prague — 30 min before the delivery target, leaving room to react. Healthy runs take
19–137 min (runs 63–66), so 5.5h is still a very wide margin.

### P1-b — Model fallback
Single-model dependency is what made an upstream blip fatal. Make `GATE_MODEL` etc. accept an
ordered list (e.g. `deepseek,kimi,glm-5`); on `SystemicLLMFailure` for the primary, fail over to the
next and log loudly. **Caveat:** gate/synthesis prompt behaviour has only ever been validated
against `deepseek` — a fallback model needs a validation run before it is trusted for a live send.

### P2-a — `find_resumable_run` is dead code
`geoecon/state/repo.py:22` matches `window_start=%s AND window_end=%s` exactly, but
`orchestrator.py:77` sets `window_end = now` — a fresh microsecond timestamp on every invocation.
**It can never match**, so every start creates a new run and redoes discovery → extraction from
scratch. Had it worked, killing run 67 would have been cheap. Suggest matching on the scheduled
day/slot (`scheduled_for::date`) rather than exact window equality. *Needs a design decision —
flagging rather than prescribing.*

### P2-b — Commit the image drift
Cluster runs `jalalhussein/geoecon-pipeline:20260709-1`; the local `deploy/*.yaml` edits match the
cluster but are **uncommitted**, so git HEAD still says `20260708-1`. Commit them so HEAD reflects
production.

### P3 — Retry ladder is deeper than it looks (note only)
`_judge` retries the *same* messages with `temperature=0, seed=12345` (`gate.py:47`). For a
deterministic parse failure the retry is guaranteed to reproduce it; it only helps for transient
errors, which `client.complete` already retries. Worth revisiting when touching the cascade, but
harmless on its own.

---

## 3. Suggested test coverage

- `LLMClient` returns errors and logs on 500; breaker raises `SystemicLLMFailure` after N
  consecutive exhausted calls; **breaker counter is thread-safe under concurrent workers**;
  a success resets the counter.
- `gate.run` raises when every verdict is `gate_unparsed_dropped`, and when `kept == 0`.
- A stage raising `SystemicLLMFailure` produces a `stage_runs` row with `status='failed'` and the
  cause in `error` (the behaviour the watchdog reports on).

## 4. Operational follow-up

- **Not our bug.** Worth reporting the `frps-proxy-tunnel.vllm-ns` outage to e-INFRA support.
- Re-check before the next 23:00Z run:
  ```
  kubectl -n hussein-ns create job deepseek-probe --from=cronjob/geoecon-watchdog   # or any pod with the key
  # then: POST {"model":"deepseek","messages":[...],"max_tokens":10} to https://llm.ai.e-infra.cz/v1/chat/completions
  ```
  If `deepseek` is still 500 at 23:00Z, run 68 will fail exactly the same way — P1-b is the fix
  that would carry it through.
