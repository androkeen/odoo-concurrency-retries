# 06 - Backoff

> **Scope:** Odoo 19.0 EE, `odoo/odoo@e6ebad8`.
> **Status:** formula and bounds verified in the source and by `test_five_attempts_four_sleeps_then_the_error`; timings measured with the demo; the time-limit behaviour is source reading; proxy and Odoo.sh timeouts are not verified.

## TL;DR

- After failure n (n = 1 to 4) `retrying()` sleeps `random.uniform(0, 2 ** n)` seconds: bounds **2, 4, 8, 16 s**, no sleep after the fifth failure.
- Per call: **15 s** of sleep on average, **30 s** at most, plus up to five runs of the call itself (lock waits included).
- In prefork mode the sleeps count against `--limit-time-real` (120 s by default; the worker is killed) but not `--limit-time-cpu`. A proxy can time out first: the user gets an error while the worker retries and may still commit.
- Expected extra latency with k retries: 2^k - 1 s on average (1, 3, 7, 15 s).

<!-- slide -->

| Failure | Sleep before the next attempt | Mean | Cumulative mean | Cumulative max |
|---|---|---|---|---|
| 1 | `uniform(0, 2)` | 1 s | 1 s | 2 s |
| 2 | `uniform(0, 4)` | 2 s | 3 s | 6 s |
| 3 | `uniform(0, 8)` | 4 s | 7 s | 14 s |
| 4 | `uniform(0, 16)` | 8 s | 15 s | 30 s |
| 5 | none, the error is raised | | 15 s | 30 s |

## Deep dive

### The code

```python
# odoo/service/model.py:226-228
wait_time = random.uniform(0.0, 2 ** tryno)
_logger.info("%s, %s tries left, try again in %.04f sec...", error, tryleft, wait_time)
time.sleep(wait_time)
```

"Full jitter": the whole interval is random, not just a small noise around a fixed delay. Two requests that failed together do not retry together, which matters on a hot row where every round only lets one writer through. The docstring of `retrying()` describes the same formula (`model.py:174-177`).

### Measured

- Facts test: with `random.uniform` recorded instead of drawn, the bounds were exactly `[(0.0, 2), (0.0, 4), (0.0, 8), (0.0, 16)]` and `time.sleep` was called 4 times.
- Demo *Exhaust retries*, three of the runs: backoffs 1.4, 1.0, 2.2 and 15.7 s (21.9 s in total); 0.8, 0.5, 7.2 and 13.1 s (23.2 s); 0.3, 3.1, 3.7 and 15.5 s (24.2 s, the [evidence](evidence/run_scenarios.txt) run). Each attempt took about 0.3 s.
- Demo *Lock not available*: the lock was held 3 s; depending on the draw the call needed 3 to 5 attempts.

### Worst case of one call

Sleeps: at most 2 + 4 + 8 + 16 = 30 s. Executions: five times the call, and each execution can itself wait: a lock wait until the other transaction ends, `deadlock_timeout` (1 s) per deadlock. A call that takes 10 s and fails five times costs about 50 s of work plus up to 30 s of sleep.

### Against the time limits (19.0 source)

| Mode | Limit | What happens to a request stuck in its retries |
|---|---|---|
| Prefork (`--workers=N`) | `--limit-time-real`, 120 s by default (`odoo/tools/config.py:487`) | the master kills the worker with `SIGKILL` (`odoo/service/server.py:1018-1027`): PostgreSQL rolls back, postcommit hooks never run, the client sees the connection drop |
| Prefork | `--limit-time-cpu`, 60 s by default | sleeping uses no CPU, so backoff does not count; the attempts do |
| Threaded (`--workers=0`) | `--limit-time-real` per thread | the thread is flagged; once no other request is running, or after 60 s, the server dumps the stacks and reloads itself (`odoo/service/server.py:515-540, 740-750`) |
| Cron workers | `--limit-time-real-cron` | not relevant: crons have no retry loop |

### Proxies and users

- A reverse proxy timeout (nginx `proxy_read_timeout` defaults to 60 s) can end the HTTP exchange while the worker keeps retrying. The user sees a 504 and clicks again; the first request may still commit. That is a duplicate action created by retries without a single duplicate retry. Odoo.sh's own front-end timeouts were not checked.
- In the web client a retried button is just a slow button: the spinner stays up for the backoff. One retry adds 1 s on average, two retries 3 s.
- An exhausted call shows the error dialog after ~15-30 s, with the psycopg2 error name (`psycopg2.errors.SerializationFailure: could not serialize access due to concurrent update` in the demo).

### Other timings, for comparison

- ir.cron: no backoff, no retry; a job that committed progress before failing is re-run at once (note 08).
- OCA queue_job 19.0.2.1.6: a fixed 5 s postpone on 40001/40P01/55P03, forever; its dependency update loop uses `uniform(0, 2 ** tries)` with tries starting at 0 (note 08).
