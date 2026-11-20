# Incident 2026-11-20 — all three attempts crashed in `extraction`; root cause found

**Status:** fixed and deployed on 2026-11-20 in image `20261120-1`; not yet confirmed by a
scheduled nightly run (the first is 2026-11-21 01:00). Today's brief was delivered at 09:55
Prague by a manual rescue run (run 218) with single-threaded extraction — see §6.

This document supersedes the cause given in
[`INCIDENT-2026-07-29-extraction-hard-kill.md`](INCIDENT-2026-07-29-extraction-hard-kill.md)
§2 ("a node-local condition"). That explanation was wrong; see §5.

The full history of this fault is in [`FAILURE-LOG.md`](FAILURE-LOG.md).

---

## 1. Summary

- No brief was sent on schedule; all three regions went out at 09:55 Prague instead. The 01:00 job `geoecon-daily-29918880` made three
  attempts (runs 215, 216, 217). Each crashed inside the `extraction` stage and the Job
  ended with `BackoffLimitExceeded` at 01:07 Prague, seven minutes after it started.
- The crash is heap corruption in native code. `geoecon/stages/extract.py` calls
  `trafilatura.extract()` from a 12-thread pool, and trafilatura 2.1.0 is not safe to call
  that way.
- This is the same fault that has been recurring since July. It killed 36 attempts across
  26 nights before today's rescue (see the failure log).
- The fault was reproduced in the production image: 11 of 40 processes crashed with 12
  concurrent threads, 0 of 40 when the same calls were serialised behind a lock.

## 2. Timeline

All times 2026-11-20. Prague is UTC+1.

| UTC | Prague | Event |
|---|---|---|
| 00:00:17 | 01:00:17 | Job `geoecon-daily-29918880` starts |
| 00:00:24 | 01:00:24 | Run 215 starts |
| 00:02:25.76 | | Run 215 enters `extraction` |
| 00:02:59.66 | | Run 215's process dies, 33.9 s into extraction, 400 of 1581 bodies written |
| 00:03:13 | 01:03:13 | Run 216 starts |
| 00:04:52.61 | | Run 216 enters `extraction` |
| 00:05:05.79 | | Run 216's process dies, 13.2 s in, 231 of 1581 bodies written |
| 00:05:31 | 01:05:31 | Run 217 starts |
| 00:07:12.81 | | Run 217 enters `extraction` |
| 00:07:13.76 | | Run 217's process dies, 0.95 s in, 9 of 1580 bodies written |
| 00:07:16 | 01:07:16 | Job marked `Failed`, reason `BackoffLimitExceeded` |
| 06:30 | 07:30 | Watchdog sends `[GEOECON ALERT] watchdog: no completed run today (latest: running)` |
| ~07:40 | ~08:40 | Investigation starts. The three failed pods have already been deleted |
| 08:03:38 | 09:03:38 | Rescue job `geoecon-rescue-20261120` created with `EXTRACT_WORKERS=1` (run 218) |
| 08:03:45 | | Run 218's startup reconciliation closes runs 215–217; `[GEOECON CLEANUP]` mail sent |
| 08:05:41 | | Run 218 enters `extraction` |
| 08:19:32 | 09:19:32 | Run 218 completes `extraction` (13m51s, 0 failed) and enters `gate` |
| 08:55:46–48 | 09:55 | Run 218 sends all three briefs (latam, middle_east, north_africa) |
| 08:55:48 | | Run 218 completes; Job `geoecon-rescue-20261120` succeeds on its first attempt |

## 3. Evidence

**Death times** come from the Postgres server log, which records each client vanishing
without closing its connection:

```
00:02:59.664 UTC  session_start 00:00:23 UTC  could not receive data from client: Connection reset by peer   (run 215)
00:05:05.788 UTC  session_start 00:03:13 UTC  could not receive data from client: Connection reset by peer   (run 216)
00:07:13.759 UTC  session_start 00:05:31 UTC  could not receive data from client: Connection reset by peer   (run 217)
```

A reset rather than a clean close means the process ended without unwinding. Consistent
with that, none of the three runs has a `stage_runs.error`, and no crash or SIGTERM alert
was sent.

**What is missing.** The three failed pods were gone by the time anyone looked, about 7.5
hours after they died, so their exit codes and last log lines were not captured. The Job
object and its `failed: 3` count survived; the pods did not. Something on the cluster now
removes finished pods within hours (the previous night's completed pod was gone as well),
so `restartPolicy: Never` no longer preserves this evidence for long.

The identification of today's crash therefore rests on three things, not on today's exit
codes:

1. The signature matches earlier occurrences where the pod *was* inspected. On 2026-08-01
   (run 75) and 2026-10-10 (runs 159–161) the container exited with code 139 and its last
   log line was a glibc abort: `double free or corruption (out)`, `free(): invalid pointer`,
   `malloc_consolidate(): unaligned fastbin chunk detected`. The 10-10 attempts ran on two
   different nodes.
2. All three of today's attempts died inside the threaded part of `extraction`, at
   different points (after 9, 231 and 400 bodies), which rules out one specific bad page.
3. The reproduction in §4.

## 4. Root cause

`geoecon/stages/extract.py` submits `extract_one` to a `ThreadPoolExecutor` with
`EXTRACT_WORKERS` threads (default 12). Each call fetches a page and then runs
`trafilatura.extract()` on it.

trafilatura 2.1.0 parses every document through one module-level parser:

```python
# trafilatura/utils.py
HTML_PARSER = HTMLParser(collect_ids=False, default_doctype=False, encoding="utf-8", ...)
tree = fromstring(htmlobject, parser=HTML_PARSER)
```

lxml allows a parser to be shared between threads and serialises the parsing itself. The
problem is what comes after: every tree made by that parser shares the parser's libxml2
string dictionary, and trafilatura then edits those trees from several threads at once.
The dictionary is not thread-safe, so the heap gets corrupted and glibc aborts the process.

This is a known upstream bug: adbar/trafilatura issue #925, fixed upstream by PR #1092,
which replaces the shared parser with a new parser per call. Upstream's stated workaround
for affected versions is a process-wide lock around `extract()`.

Versions in image `20261106-1` (unchanged from `20260730-1`, since the later image was a
code-only overlay): Python 3.12.13, trafilatura 2.1.0, lxml 6.1.1, libxml2 2.14.6.

### Reproduction

Run inside `jalalhussein/geoecon-pipeline:20261106-1` on the build VM. Each trial is a
fresh Python process that starts 12 threads behind a barrier and has each thread call
`trafilatura.extract()` on generated HTML, with the same arguments the pipeline uses.

| Mode | Trials | Crashed | How they ended |
|---|---|---|---|
| 12 threads, concurrent | 40 | **11** | 5× `munmap_chunk(): invalid pointer`, 4× `double free or corruption (out)`, 2× `free(): invalid pointer` |
| 12 threads, one `extract()` at a time behind a lock | 40 | **0** | — |

Limits of this test: the HTML was synthetic, not live pages, and it is 40 trials per mode.
The crash rate it produced (about 27% per process) is close to what production shows.

The script is in the appendix.

## 5. What this corrects

The 2026-07-29 write-up concluded that the most likely cause was a node-local condition
(memory pressure or kubelet eviction against a pod sitting at its memory request). The
changes made on that basis — memory request 2→3 GiB, limit 4→6 GiB, `restartPolicy: Never`
— did not stop the crashes. They continued at the same rate afterwards, including on
different nodes within one night.

Those changes are harmless and `restartPolicy: Never` remains useful, but memory and node
placement are not the cause and should not be investigated further for this fault.

Two things in that write-up still stand: the processes die without unwinding, and no
oversized page is responsible.

## 6. Mitigation applied today

A one-off job, `geoecon-rescue-20261120`, was created from the `geoecon-daily` CronJob
with one change: `EXTRACT_WORKERS=1`. With a single worker thread nothing runs
`extract()` concurrently, so the race cannot occur. No image rebuild was needed, because
`config.py` reads `EXTRACT_WORKERS` from the environment.

Cost: extraction took 13m51s in run 218 (1421 extracted, 128 headline-only, 0 failed),
against 2–3 minutes with 12 threads.

**Outcome of run 218:** completed on its first attempt in 52 minutes (08:03:46–08:55:48
UTC). All three briefs were sent, 12 records each.

| Stage | Duration | Result |
|---|---|---|
| discovery | 0m40s | 347 discovered |
| discovery_registry | 1m15s | 1202 discovered |
| extraction | 13m51s | 1421 extracted, 128 headline-only, 0 failed |
| gate | 8m39s | 274 kept of 1549, 0 unparsed |
| cluster | 0m46s | 195 clusters |
| synthesis | 26m30s | 177 records ok, 11 held |
| summary, render, send | 0m21s | 3 summaries, 3 briefs sent |

One clean single-threaded run does not prove the mitigation by itself — with 12 threads
roughly three attempts in four succeed anyway. The reason to trust it is the mechanism (one
thread cannot race with itself) and the 0-of-40 result for serialised calls in §4.

**Stopgap on the nightly job.** After run 218 succeeded, the same setting was applied to
the `geoecon-daily` CronJob at about 10:25 Prague on 2026-11-20, and mirrored in
`deploy/cronjob.yaml` so that re-applying the manifest does not undo it:

```
kubectl -n hussein-ns set env cronjob/geoecon-daily EXTRACT_WORKERS=1
```

It was removed again about 45 minutes later, when image `20261120-1` with the code fix was
deployed (§7), so no scheduled run ever used it. It remains available as a no-rebuild
fallback if the crash ever returns.

## 7. The fix, and what is still open

### Fix (commit `0dc0e65`, image `20261120-1`)

`stages/extract.py` takes a module-level lock (`_EXTRACT_LOCK`) around
`trafilatura.extract()`. The page fetches stay threaded: trafilatura's download path does
not use lxml. Two tests in `tests/unit/test_extract.py` hold this in place — one fails if
two `extract()` calls ever overlap, the other fails if the fetches stop running in parallel.

**Synthetic check, before building.** The real `extract.run()` with 12 workers over 96
generated documents, 40 fresh processes per version, in image `20261106-1`:

| `stages/extract.py` | Trials | Crashed |
|---|---|---|
| As shipped in `20261106-1` (no lock) | 40 | **22** — `free(): invalid pointer`, `munmap_chunk(): invalid pointer`, `double free or corruption` |
| With the lock | 40 | **0** |

**Live-page check, on the built image.** `extract.run()` from `20261120-1`, 12 workers,
2 CPUs, over the 1549 real article URLs of run 218:

| Trial | Result | Time |
|---|---|---|
| 1 | 1442 extracted, 107 headline-only, 0 failed | 117 s |
| 2 | 1439 extracted, 110 headline-only, 0 failed | 110 s |
| 3 | 1444 extracted, 105 headline-only, 0 failed | 106 s |

So the lock costs nothing measurable: about two minutes, the same as before the fix.
Three clean live runs are weak evidence taken alone (the unfixed code survives about three
runs in four); the 0-of-40 synthetic result is the stronger one.

**Image.** `20261120-1` is an overlay on `20261106-1` (`Dockerfile.overlay`), built with
`--platform linux/amd64`. `pip freeze` is identical between the two, and `extract.py` is
the only file in the package that differs. Registry digest
`sha256:880da952…7a5945`; an in-cluster throwaway job pulled it on `kub-b13` and ran it.

**Deployed** to `geoecon-daily` and `geoecon-watchdog` on 2026-11-20 at about 11:10 Prague,
with `EXTRACT_WORKERS=1` removed, so the nightly job is back to 12 fetch threads.

### Still open

1. **Confirmation in production.** The first scheduled run on `20261120-1` is 2026-11-21
   01:00. Given the previous rate of about one bad night in five, it takes a few weeks
   without a class A row in the failure log before the fix can be called confirmed.
2. **Evidence retention.** Failed pods are now deleted within hours. If exit codes and last
   log lines matter for a future fault, they have to be captured some other way, for
   example by having the watchdog record the termination state of the day's pods.
3. **Upgrading trafilatura** to a release containing PR #1092 would make the lock
   unnecessary, but `pyproject.toml` pins nothing, so a rebuild from the main `Dockerfile`
   would change lxml, glibc and everything else at the same time. Not worth doing for
   this alone; when a dependency refresh happens anyway, the lock can stay as a guard.

---

## Appendix — reproduction script

Pipe this into the image; the argument is the number of trials per mode. It takes about
18 minutes for 40.

```
docker run --rm -i --platform linux/amd64 --entrypoint python \
  jalalhussein/geoecon-pipeline:20261106-1 - 40 < repro_driver.py
```

```python
import subprocess, sys, textwrap, collections, time
WORKER = textwrap.dedent('''
    import sys, random, threading
    from concurrent.futures import ThreadPoolExecutor
    import trafilatura
    mode, seed = sys.argv[1], int(sys.argv[2])
    def doc(i):
        r = random.Random(seed * 1000 + i)
        w = lambda: "".join(r.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(r.randint(4, 11)))
        paras = "".join(
            f'<p class="{w()} {w()}" data-{w()}="{w()}" id="{w()}">' + " ".join(w() for _ in range(90)) +
            f' <a href="/{w()}" rel="{w()}">{w()}</a> <span data-{w()}="{w()}">{w()}</span>.</p>'
            for _ in range(25))
        junk = "".join(f'<div class="{w()}" data-{w()}="1"><{w()}-{w()} {w()}="{w()}">{w()}</{w()}></div>' for _ in range(40))
        return (f'<html><head><title>{w()} {w()}</title><meta name="{w()}" content="{w()}"></head><body>'
                f'<nav>{junk}</nav><article><h1>{w()} {w()} {w()}</h1>{paras}</article><footer>{junk}</footer></body></html>')
    docs = [doc(i) for i in range(48)]
    lock = threading.Lock()
    start = threading.Barrier(12)
    def work(k):
        start.wait()
        for d in docs[k::12] * 2:
            if mode == "locked":
                with lock:
                    trafilatura.extract(d, include_comments=False, favor_recall=True, output_format="json", with_metadata=True)
            else:
                trafilatura.extract(d, include_comments=False, favor_recall=True, output_format="json", with_metadata=True)
    with ThreadPoolExecutor(12) as ex:
        list(ex.map(work, range(12)))
''')
open("/tmp/w.py", "w").write(WORKER)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
for mode in ("threads", "locked"):
    res = collections.Counter(); last = collections.Counter(); t = time.time()
    for s in range(N):
        p = subprocess.run([sys.executable, "/tmp/w.py", mode, str(s)], capture_output=True, text=True)
        res[p.returncode] += 1
        if p.returncode != 0:
            tail = (p.stderr.strip().splitlines() or ["<no stderr>"])[-1][:90]
            last[tail] += 1
    print(f"mode={mode} runs={N} exit_codes={dict(res)} secs={time.time()-t:.0f}")
    for k, v in last.most_common(6): print(f"    {v}x  {k}")
```
