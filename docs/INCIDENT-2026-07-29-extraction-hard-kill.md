# Incident 2026-07-29 — nightly job burned all 3 attempts to uncatchable kills in `extraction`

**Status:** mitigated. Today's brief delivered by manual run 72. Deploy fixes applied
(`deploy/cronjob.yaml`). Code fixes still **proposed, not implemented** — see §6.

Companion doc: [`INCIDENT-2026-07-28-gate-hang.md`](INCIDENT-2026-07-28-gate-hang.md) (yesterday).

---

## 1. TL;DR

Two different incidents on consecutive nights, with one thing in common — **the pipeline
failed in a way that produced no usable diagnostic signal.**

| | 2026-07-28 (run 67) | 2026-07-29 (runs 69/70/71) |
|---|---|---|
| Symptom | hung 6h50m in `gate` | 3 hard kills in `extraction`, job dead in 5m52s |
| Cause | upstream deepseek/vLLM backend down | pod killed by SIGKILL (uncatchable) |
| Alert quality | watchdog fired, could not name a stage | SIGTERM email named the **wrong run and wrong stage** |
| Evidence left | logs (silent), stage row | **none — pod was destroyed** |

The alert email you received ("run_id: 71, stage: discovery, most likely the
activeDeadlineSeconds hang-guard") was **misleading on every point**. There was no hang:
the job lived 5 minutes 52 seconds, not 8 hours. Run 71 was the third of three attempts
and barely existed. The real failures were runs 69 and 70, and they died **silently in
`extraction`** — you were never told about them at all.

---

## 2. What actually happened last night

The CronJob fired at 23:00:01Z (01:00 Prague) as `geoecon-daily-29754660`. Because the pod
template used `restartPolicy: OnFailure`, Kubernetes recycled **one pod in place** three
times. Each container start created a *new* run row:

| Run | started | discovery | discovery_registry | extraction started | process died | bodies written |
|---|---|---|---|---|---|---|
| 69 | 23:00:07 | ✅ 60s | ✅ 108s | 23:02:56.55 | **23:03:00.08** | 43 / 1605 |
| 70 | 23:03:00 | ✅ 40s | ✅ 116s | 23:05:37.33 | **23:05:38.68** | 11 / 1632 |
| 71 | 23:05:52 | started 23:05:52.97 | — | never reached | 23:05:53 (SIGTERM) | 0 |

At 23:05:53 the Job hit `BackoffLimitExceeded` and the controller **deleted the pod**. That
deletion is what sent SIGTERM to run 71's process mid-`discovery` — which is the only
reason you got an email at all.

### The kill was a SIGKILL, not a deadline

Proven from the Postgres server log (the client's own logs died with it):

```
23:03:00.082 UTC  session_start_time 23:00:07 UTC  →  run 69
  "could not receive data from client: Connection reset by peer"   sql_state 08006
23:05:38.677 UTC  session_start_time 23:03:00 UTC  →  run 70
  "could not receive data from client: Connection reset by peer"   sql_state 08006
```

A TCP **RST** rather than a clean close means the client process vanished without unwinding.
That is corroborated by three independent facts:

1. **No `stage_runs.error` row.** `orchestrator.py:94` catches `Exception` and calls
   `repo.fail_stage`. It never ran → no Python-level exception occurred.
2. **No crash email.** `main()`'s handler (`orchestrator.py:146`) never ran either.
3. **No SIGTERM email for 69/70.** The handler at `orchestrator.py:21` *does* fire on
   SIGTERM (it fired for run 71). Its silence for 69/70 rules out SIGTERM/eviction-with-grace
   and leaves **SIGKILL**.

The database was healthy throughout — it logged these disconnects as a bystander, and its
own checkpoints ran normally before, during and after.

### It is not "extraction needs more RAM"

I tested that and it does not hold up. Run 72 (today's manual run, same image, same env, 1597
articles) was wrapped in a cgroup sampler, so this is the kernel's own high-water mark for
the whole run, not a sampled estimate:

```
MEMORY sampled_peak_bytes = 2,054,086,656   (~1958 MiB)
MEMORY cgroup_memory.peak = 2,057,797,632   (~1962 MiB)   ← whole-run high-water mark
```

Stage by stage: ~1.84 GiB at the end of `discovery_registry`, 1.96 GiB peak during
`extraction` (extraction itself adds only ~120 MiB), flat ~1.95 GiB through gate/synthesis.

**Peak was 1.92 GiB against a 4 GiB limit** — the pipeline never came close to the cgroup
ceiling. A plain "extraction ran out of its 4 GiB" story is therefore **not supported**.

But the same number is damning in a different way: the pod's memory **request was 2 GiB**, and
it peaked at **1.92 GiB — 96% of its request, all run long**. For a Burstable pod that is the
worst possible place to sit, because kubelet ranks node-pressure eviction by usage relative to
request. This is the single most likely mechanism, and it is what §5.3 fixes.

I also chased the "one pathological giant document" theory, because extraction submits work
in `ORDER BY article_id` (`repo.py:110`) — so **all three attempts fetched the same URLs in
the same order**, which would explain three deaths at the same place. Run 69's first 60
articles contain exactly 43 `extracted` (= its 43 bodies) and run 70's first 30 contain
exactly 11 (= its 11 bodies), confirming they died within the first ~30–60 URLs. Measuring
the still-`pending` URLs from that window:

| URL | bytes |
|---|---|
| finance.yahoo.com/…lam-research… | 831,480 |
| tradearabia.com/…Saudia-Cargo…/IND | 321,579 |
| tradearabia.com/…Jeddah-Tower… | 319,892 |
| theepochtimes.com/…stock-indexes… | 185,124 |
| mondaq.com/…forced-labor-tariffs… | 74,583 |

Largest is 831 KB. **No oversized payload exists.** Hypothesis refuted.

### Most likely cause: a node-local condition, made inescapable by `restartPolicy: OnFailure`

This is the part I want to be precise about, because the evidence to close it was destroyed.

`restartPolicy: OnFailure` restarts the container **inside the same pod**, which stays bound
to **the same node** for the life of the Job. So all three attempts ran on one node. A
node-local condition — memory pressure from a noisy neighbour, kubelet eviction, a node OOM
killer, a transient kernel/CSI fault — would kill every attempt identically, which is exactly
the observed pattern. Supporting detail: the pod was QoS **Burstable** requesting 2Gi while
actually using ~1.95Gi, i.e. sitting *at* its request, so any spike pushed it above its
request and into kubelet's node-pressure eviction ranking.

I could not confirm the precise reason (`OOMKilled` vs `Evicted` vs `Error`) because the pod
— and with it `lastState.terminated.reason`, all three attempts' logs, and the pod events —
was deleted by the Job controller before anyone looked. **That is the real bug**, and §5 fixes it.

### This has been happening for five weeks and nobody ever saw it

Deploying the reconciliation (§6.1) and running it against production turned up far more than
last night. **8 of the 50 runs since 2026-06-27 — 16% — died silently and were never
noticed**, each left frozen in `status='running'` forever:

| run | started | died in |
|---|---|---|
| 23 | 2026-06-27 03:29 | gate |
| 33 | 2026-07-04 02:19 | — (before any stage failed) |
| 34 | 2026-07-04 02:20 | — |
| 42 | 2026-07-09 05:00 | **extraction** |
| 43 | 2026-07-09 05:04 | synthesis |
| 47 | 2026-07-10 23:00 | **extraction** |
| 54 | 2026-07-16 23:00 | **extraction** |
| 62 | 2026-07-23 23:00 | **extraction** |

**Five of the eight died in `extraction`**, on 07-09, 07-10, 07-16, 07-23 and then twice on
07-28/29 (runs 69 and 70). That is a recurring fault on a roughly weekly cadence, and it has
been accelerating.

**No brief was ever actually missed, though — every one of those days was rescued by a
retry:**

| abandoned run | Prague day | other runs that day | briefs sent that day |
|---|---|---|---|
| 23 | 06-27 | 24 completed | 3 |
| 33, 34 | 07-04 | 35, 36 completed | 6 |
| 42, 43 | 07-09 | 44, 45 completed | 3 |
| 47 | 07-11 | 48 completed | 3 |
| 54 | 07-17 | 55 completed | 3 |
| 62 | 07-24 | 63 completed | 3 |

Run 62's pod (`geoecon-daily-29747460-tlj7g`) shows `RESTARTS 1`, and run 63 started 4m35s
later and completed. So the correct reading is **not** "eight lost briefs" — it is that the
system has been running on retry luck for five weeks, and 2026-07-29 was the first night the
retry budget ran out before the fault did.

⚠️ **Do not infer the cause from an orphaned row.** A `running` row proves only that the
process ended without unwinding. *Both* SIGKILL **and** SIGTERM leave one — the SIGTERM
handler raises `SystemExit(143)`, a `BaseException` that bypasses `run_pipeline`'s
`except Exception`, so `fail_stage` never runs there either. Run 43 is the worked example:
`cronjob.yaml` records it as "killed at 93% of synthesis by the old 4h deadline", i.e. a
SIGTERM deadline kill, not an OOM. The first version of the reconciliation alert called all
eight "uncatchable SIGKILL" and had to be corrected — see §6.1.

So this incident is not "a bad node one night". It is a **known-frequency defect that was
invisible by construction** — which is exactly why §5.1 (retain the evidence) and §6.1 (notice
the death) matter more than any single tuning change.

---

## 3. Why the alert misled you

`orchestrator.py:25` hardcodes a guess into the email body:

```python
f"The pipeline pod received SIGTERM — most likely the "
f"activeDeadlineSeconds hang-guard killing a stuck run "
f"(or node maintenance).\n\n"
```

The deadline was 8h; the job died in 5m52s. The handler has no idea *why* it got SIGTERM and
should not speculate — it should report elapsed wall-clock, which alone would have falsified
"hang-guard" instantly. Worse, the one run it *could* name (71) was the least informative of
the three, and the two that mattered produced no alert at all.

---

## 4. The 8h hang-guard could never have protected a send

Independently of last night: `activeDeadlineSeconds: 28800` from a 23:00Z start fires at
07:00Z = **09:00 Prague, two hours after the 07:00 delivery target.** It was incapable of
saving a delivery — as run 67 demonstrated when it hung 6h50m and the guard never mattered.
(Flagged as P1-a yesterday, not yet done — now done, see §5.)

---

## 5. Fixes APPLIED (deploy-only — no image rebuild needed)

All in `deploy/cronjob.yaml`.

**5.1 `restartPolicy: OnFailure` → `Never`.** The highest-value change, and it does two
things at once:
- *Resilience:* each attempt gets a **new pod**, which the scheduler can place on a
  **different node** — so a bad node is escaped instead of retried into three times.
- *Diagnosability:* failed pods are **retained**, so `kubectl get pods` /
  `kubectl logs` show the termination reason and every attempt's output. The next
  occurrence is diagnosable in one command instead of being unfalsifiable.

**5.2 `activeDeadlineSeconds: 28800` → `19800` (5.5h).** Fires 04:30Z / 06:30 Prague — 30 min
*before* the delivery target instead of two hours after. Healthy runs are 19–137 min, so the
margin is still ~2.4× the worst observed.

**5.3 `requests.memory: 2Gi → 3Gi`, `limits.memory: 4Gi → 6Gi`.** Measured steady state is
~1.95Gi, i.e. *at* the old 2Gi request. Raising the request puts real usage safely **below**
it, which is what actually determines kubelet's node-pressure eviction ranking for a
Burstable pod. Namespace quota is untouched by this (limits.memory 8Gi used / 24Gi hard).

**5.4 deepseek re-probe recorded.** As of 02:18Z today deepseek is *still* down and has
degraded from HTTP 500 to HTTP 400 `"no healthy deployments for this model"` — the backend is
now fully deregistered. `gemma4` answers in 0.3s with 0 reasoning tokens. `kimi`/`glm-5`/
`qwen3.5` are up but are reasoning models (empty `content`, non-zero `reasoning_tokens`) and
are not drop-in. **gemma4 stays.** One upside: 400 is not in `LLMClient.RETRYABLE`, so a
deepseek revert would now fail fast rather than hang like run 67.

---

## 6. Code fixes WRITTEN — but NOT yet live (need an image rebuild + push)

⚠️ These are committed to the working tree with tests (229 unit tests pass), but the cluster
still runs `20260709-1`, which does **not** contain them. Tonight's protection comes entirely
from §5. Docker Desktop was not running this session, so the image could not be built.
To ship: `docker build -t jalalhussein/geoecon-pipeline:<tag> . && docker push …`, then bump
the tag in **both** `deploy/cronjob.yaml` and `deploy/watchdog-cronjob.yaml` (they must stay
in lockstep — the alert hooks live in this image).

**6.1 — Detect the silent kill.** `orchestrator.reconcile_abandoned_runs()`, called at
startup. A SIGKILLed process runs no handler, so it cannot report its own death — the *next*
process has to. Marks any run left `running` for >6h as `failed` (6h > the 5.5h deadline, so
a legitimately-running pipeline is never touched) and sends one alert listing them, with the
exact `kubectl` command to read the retained pod's termination reason. Best-effort by
contract: it can never block a run. *This is the fix for the class of failure that had zero
signal.* (`repo.stale_running_runs`, `repo.mark_run_failed`)

> **Corrected after first live use (2026-07-29 03:41Z).** The first version of this alert
> asserted the runs died of "an uncatchable SIGKILL (OOM / node-pressure eviction / node
> loss)" and that each "produced nothing and told no one". Both claims were wrong, and wrong
> in exactly the way §3 criticises the old SIGTERM alert for: it diagnosed a cause it cannot
> observe. An orphaned row does not distinguish SIGKILL from a SIGTERM deadline kill (run 43
> was the latter), and it does not imply a missed delivery (all eight days were delivered).
> The alert now states only what is known, names *both* candidate causes and why neither is
> recorded, and tells the reader how to check delivery and the retained pod. Regression test:
> `test_reconcile_alert_does_not_assert_a_single_cause`.

**6.2 ⟳ — LLM client is now observable.** `geoecon/llm/client.py` now has a module logger and
logs every failure path: a `WARNING` per retryable attempt (model, attempt n/max, status,
truncated body, latency), an `ERROR` on non-retryable, and an `ERROR` on final exhaustion.
Yesterday's 6h50m of total silence becomes greppable within seconds.

**6.4 ⟳ — A null gate now fails loudly.** `gate.NullGateError` is raised at end of stage when
a real run judged nothing, instead of returning `completed` with `gate_kept=0`. Two guards,
both floored by article count so `--limit`/smoke runs are unaffected:
`gate_unparsed_dropped > 50%` of ≥20 articles (unambiguous — parseable-but-negative verdicts
are a real answer, unparsed output is a broken model), or `gate_kept == 0` on ≥200 articles
(historical keep rate is 19–22%; today's run 72 kept 343/1597 = 21.5%). Verdicts are still
persisted before the raise, so the evidence survives.

**6.5 — Honest SIGTERM alert.** The "most likely the activeDeadlineSeconds hang-guard" guess
is gone. The handler now reports **elapsed wall-clock** — the one fact it actually owns, and
the one that would have falsified "hang-guard" instantly last night — tells the reader how to
compare it against the deadline, and explicitly warns that sibling attempts killed by SIGKILL
send no alert at all.

### Residual gap, stated plainly

6.1 uses a **6h** cutoff so it can never mistake a legitimately-running pipeline for a dead
one. That means a kill at 23:03 is not reported *by reconciliation* until the next run, ~24h
later. Same-night coverage comes from three other places: the retained failed pod (§5.1) shows
the termination reason immediately, the retry now lands on a **different node** so it is
likely to just succeed, and the 07:30 watchdog still reports a missing brief. A tighter
same-night detector ("an earlier run for today's slot is still `running`") was considered and
rejected for now — it would false-positive against manual runs like today's run 72, which
overlap by design.

### Still outstanding (not attempted this session)

**6.3 ⟳ — Circuit breaker on systemic LLM failure.** After N consecutive fully-exhausted
`complete()` calls, raise `SystemicLLMFailure` so it propagates to `fail_stage` + a crash
alert. (Counter must be lock-guarded — shared across gate workers.) 6.2 makes the outage
*visible*; this makes it *fast*. Lower priority now that 6.4 refuses to ship an empty brief.

**6.6 ⟳ — `find_resumable_run` is dead code.** Confirmed: `repo.py:24` matches
`window_start=%s AND window_end=%s`, but `orchestrator.py:77` sets `window_end = now` — a
fresh microsecond timestamp every process start, so it can **never** match. Consequence last
night: each of the 3 attempts redid discovery + registry from scratch (~2.5 min each),
burning the entire retry budget re-doing work that had already succeeded, and never getting
further than the stage that was killing it. Match on `scheduled_for::date` instead.
*Left alone deliberately:* turning resume on changes run semantics and deserves its own
change, not a 4am hotfix — and with `restartPolicy: Never` each attempt now at least gets a
different node.

**6.7 ⟳ — Model fallback.** A single upstream backend can still take out the whole pipeline;
deepseek has now been down for two days.

---

## 7. Housekeeping

- Runs **62, 69, 70, 71** are still `status='running'` in the DB and will never complete.
  Harmless to the watchdog (it keys off the *latest* run in the day window, which is run 72),
  ~~but it skews any "successful runs" query.~~ **Done** — 6.1 shipped and was run against
  production at 03:41Z, which closed out **8** abandoned runs (23, 33, 34, 42, 43, 47, 54, 62)
  and sent one alert naming them. Runs **69/70/71 were deliberately skipped**: at 03:42Z they
  were only 4.7h old, inside the 6h safety cutoff, so the guard correctly refused to declare a
  possibly-live run dead. Tonight's 23:00Z run will close them (they will be ~24h old) and
  send a second, smaller alert. Both alerts are the fix working, not new failures.
- `deploy/send-run68-job.yaml` is a completed one-off, kept in the repo as the record of how
  run 68 was salvaged (rendered under `--dry-run`, inspected, then sent). Safe to delete once
  that recovery pattern is no longer wanted as a reference.
- `deploy/cronjob.yaml` + `deploy/watchdog-cronjob.yaml` image bump to `20260709-1` was
  uncommitted drift (P2-b yesterday); it is included in this change set.
