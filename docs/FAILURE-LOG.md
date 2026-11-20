# Failure log

One entry per night on which a pipeline attempt did not complete, newest first. The aim is
that anyone can see how often each kind of failure happens and whether a brief still went
out, without rebuilding the history from the database.

- Dates and times are **Europe/Prague**. The nightly job has run at 01:00 since
  2026-07-11; before that it ran at 07:00.
- "Auto retry" means a later attempt of the same Job succeeded with nobody involved.
  "Manual" means someone started a run by hand.
- Coverage starts 2026-07-09. Earlier failed runs (11–14 on 06-23, 23 on 06-27, 33–34 on
  07-04, 41 on 07-08) were development and test runs and are left out.
- Last updated 2026-11-20. Add a row whenever a `[GEOECON ALERT]` or `[GEOECON CLEANUP]`
  mail arrives; §4 has the query that lists the candidates.

## 1. Failure classes

| Class | What it is | Nights | Status |
|---|---|---|---|
| **A. Extraction thread crash** | The process aborts with heap corruption inside `extraction`. Cause: `trafilatura.extract()` called from 12 threads. | 26 (36 dead attempts) | Cause found and fixed 2026-11-20 (image `20261120-1`). **Not yet confirmed by scheduled runs** — watch for new class A rows. [Write-up](INCIDENT-2026-11-20-extraction-thread-crash.md) |
| **B. LLM backend unavailable** | The model group behind gate/synthesis is down or erroring. | 2 | Circuit breaker live since image `20260730-1`; model fallback still not implemented. [Write-up](INCIDENT-2026-07-28-gate-hang.md) |
| **C. Mail delivery** | SMTP rejects or drops a send. | 2 | Send retry and resume of an unsent run live since image `20261106-1`. The resume path has not yet been exercised by a real partial send. |
| **D. Killed by deadline or Job cleanup** | The process receives SIGTERM from Kubernetes. | 2 | Side effects of other failures, not independent faults. |

Class A in numbers, 2026-07-09 to 2026-11-20: it hit on **26 of 135 nights (19%)**. On 20
of them an automatic retry rescued the night and the brief went out on time. On the other
six the brief was late or incomplete: on 07-29, 10-10 and 11-20 the crash alone used up
every attempt, and on 07-09, 08-01 and 11-06 it combined with a second failure.

## 2. Log

| Date | Dead attempts (run: stage) | Class | Recovered by | Brief |
|---|---|---|---|---|
| 2026-11-20 | 215, 216, 217: extraction | A | Manual run 218 at 09:03 with `EXTRACT_WORKERS=1` | Late, 09:55 |
| 2026-11-14 | 207, 208: extraction | A | Auto retry, run 209 | On time |
| 2026-11-06 | 197: extraction · 198: send (latam only) · 199: extraction | A + C | Latam re-sent by hand | 2 of 3 on time; latam at 16:46 |
| 2026-10-27 | 185, 186: extraction | A | Auto retry, run 187 | On time |
| 2026-10-22 | 179: extraction | A | Auto retry, run 180 | On time |
| 2026-10-21 | 177: extraction | A | Auto retry, run 178 | On time |
| 2026-10-17 | 172: extraction | A | Auto retry, run 173 | On time |
| 2026-10-13 | 167: extraction | A | Auto retry, run 168 | On time |
| 2026-10-12 | 165: gate (circuit breaker tripped, `gemma4` returning HTTP 500) | B | Auto retry, run 166 | On time |
| 2026-10-11 | 163: extraction | A | Auto retry, run 164 | On time |
| 2026-10-10 | 159, 160, 161: extraction | A | Manual run 162 at 04:01 | Late |
| 2026-10-08 | 156: extraction | A | Auto retry, run 157 | On time |
| 2026-09-30 | 147: extraction | A | Auto retry, run 148 | On time |
| 2026-09-29 | 144, 145: extraction | A | Auto retry, run 146 | On time |
| 2026-09-22 | 135, 136: extraction | A | Auto retry, run 137 | On time |
| 2026-09-19 | 131: extraction | A | Auto retry, run 132 | On time |
| 2026-09-13 | 124: extraction | A | Auto retry, run 125 | On time |
| 2026-09-08 | 118: extraction | A | Auto retry, run 119 | On time |
| 2026-09-04 | 113: extraction | A | Auto retry, run 114 | On time |
| 2026-08-27 | 104: extraction | A | Auto retry, run 105 | On time |
| 2026-08-04 | 80: extraction | A | Auto retry, run 81 | On time |
| 2026-08-01 | 75: extraction · 76: send (all three regions, SMTP `535` credentials rejected) | A + C | Run 77 built the briefs; sent after the mail credential was replaced | Late, 20:44 |
| 2026-07-29 | 69, 70: extraction · 71: discovery (SIGTERM when the Job was deleted) | A + D | Manual run 72 at 04:19 | Late |
| 2026-07-28 | 67: gate (hung 6h50m, `deepseek` backend down) | B | Manual run 68 at 08:16 | Late |
| 2026-07-24 | 62: extraction | A | Auto retry, run 63 | On time |
| 2026-07-17 | 54: extraction | A | Auto retry, run 55 | On time |
| 2026-07-11 | 47: extraction | A | Auto retry, run 48 | On time |
| 2026-07-09 | 42: extraction · 43: synthesis (killed with no error recorded; the notes attribute it to the Job deadline) | A + D | Manual run 44 at 14:53 | Late |

## 3. Notes on the entries

- **Where the data comes from.** Run ids, stages, start times and send times are from the
  `runs`, `stage_runs` and `newsletters` tables. Causes for classes B, C and D, and the
  exit codes mentioned below, are from the incident write-ups and from notes taken while
  each incident was investigated.
- **Class A is assigned by signature, not by exit code, for most rows.** A process that
  dies in `extraction` with no error row and no alert is counted as class A. The exit code
  was actually observed on only two nights: 08-01 (run 75) and 10-10 (runs 159–161), both
  code 139 with a glibc abort message. On every other night the pod was gone before anyone
  looked. It is possible that a few of these rows had a different cause.
- **Run 75 (08-01)** is recorded in the database as dying in `extraction`; the notes from
  that day place the abort just after the `discovery_registry` statistics line. Either way
  it was the same native abort.
- **"On time"** means a run of the scheduled Job completed and sent all three regions. It
  does not account for the few extra minutes a retry adds.
- **Most cleanup rows carry a generic error.** Runs closed by the startup reconciliation
  are stored as `abandoned: still 'running' with no progress since …`. That text records
  that the run was found dead, not why.

## 4. Finding nights to add

Every night with at least one attempt that did not complete, with the stage each one
stopped in:

```sql
WITH a AS (
  SELECT r.run_id,
         (r.started_at AT TIME ZONE 'Europe/Prague')::date AS d,
         to_char(r.started_at AT TIME ZONE 'Europe/Prague', 'HH24:MI') AS t,
         r.status,
         (SELECT s.stage_name FROM stage_runs s
           WHERE s.run_id = r.run_id AND s.status IN ('running', 'failed')
           ORDER BY s.started_at DESC LIMIT 1) AS died,
         (SELECT count(*) FROM newsletters n
           WHERE n.run_id = r.run_id AND n.send_status = 'sent') AS sent
    FROM runs r)
SELECT d, count(*) AS attempts,
       string_agg(run_id || '@' || t || ':' || status || coalesce('/' || died, '')
                  || CASE WHEN sent > 0 THEN '/sent' || sent ELSE '' END,
                  ', ' ORDER BY run_id) AS detail
  FROM a
 GROUP BY d
HAVING count(*) FILTER (WHERE status <> 'completed') > 0
 ORDER BY d DESC;
```

Run it with:

```
kubectl -n hussein-ns exec geoecon-db-1 -c postgres -- psql -U postgres -d geoeconomics -c "<query>"
```

The query gives the facts for the first two columns. The class and cause still have to be
established at the time, from the pod's exit code and last log lines — and those are only
available for a few hours after the failure.
