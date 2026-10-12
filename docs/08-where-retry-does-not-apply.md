# 08 - Where the retry does not apply

> **Scope:** Odoo 19.0 EE `odoo/odoo@e6ebad8`; OCA `queue_job` 19.0.2.1.6.
> **Status:** ir.cron verified in the source and by the demo (*Same failure from cron*, four buttons); queue_job verified in the source and by experiment ([`evidence/probe-queue-job.txt`](evidence/probe-queue-job.txt)); `odoo shell` by experiment; `cr.commit()` by the demo (*cr.commit() midway*); the self-deadlock case is reasoning only.

## TL;DR

- `retrying()` wraps HTTP routes and RPC calls only. **ir.cron** never retries: the job fails, `failure_count` goes up, the job waits for its next call; 5 failures over more than 7 days deactivate it (18.0+).
- **19.0 "Run Manually"** runs the job on its own cursor: one attempt, an error dialog (17.0 and 18.0 ran it inside the HTTP request: 5 attempts). A batched job that committed progress before failing is re-run **at once**, without backoff.
- **OCA queue_job 19.0.2.1.6**: 40001/40P01/55P03 postpone the job 5 s, **forever** (`max_retries` is not checked on that path); a `ConcurrencyError` fails the job.
- **`odoo shell`**, scripts, code in a `registry.cursor()` block: no retry. **`cr.commit()` inside a request** breaks the model: each retry re-applies the committed part (demo: order confirmed 3 times).

<!-- slide -->

| Path (19.0) | On 40001 |
|---|---|
| Button, controller, JSON-2 | 5 attempts (HTTP loop) |
| XML-RPC, `/jsonrpc` | 5 attempts (RPC loop; 25 on 17.0) |
| Cron worker, Run Manually | 1 attempt, failure counted |
| Cron with committed progress | re-run at once |
| queue_job | postponed 5 s, forever |
| `odoo shell`, scripts | 1 attempt |

## Deep dive

### ir.cron in 19.0

The scheduler never calls `retrying()`. The path (`odoo/addons/base/models/ir_cron.py`):

1. `_process_jobs()` (`:187`) and `_process_jobs_loop()` (`:216`): for each ready job, `_acquire_one_job()` (`:308`) locks the `ir_cron` row with `FOR NO KEY UPDATE SKIP LOCKED`. A 40001 here means another worker just committed this job's new `nextcall`: the worker rolls back and skips the job, by design.
2. `_process_job()` (`:399`) on the scheduler's cursor: clear past triggers, run, count failures, reschedule.
3. `_run_job()` (`:458`) on a **new cursor** (`job_cr`), in a loop of at least 10 runs or 10 s (`MIN_RUNS_PER_JOB`, `MIN_TIME_PER_JOB`): create an `ir.cron.progress` row, commit, call `_callback()`, then decide from `(success, done, remaining)`:

   | Outcome of one run | Status | Next |
   |---|---|---|
   | failed, but this run committed progress and work remains | none yet | **runs again at once** |
   | failed, nothing committed in this run | FAILED | stop |
   | succeeded, nothing remaining (or no progress API used) | FULLY_DONE | stop |
   | succeeded, work remaining, nothing done | PARTIALLY_DONE | stop, rescheduled ASAP |
   | succeeded, work remaining, some done | none yet | runs again |

4. `_callback()` (`:671`): run the server action, flush, signal, commit; on error `reset_changes()`, rollback, re-raise. `_run_job()` logs `Job 'name' (id) server action #N failed` at ERROR with the traceback (`:508`).
5. `_update_failure_count()` (`:571`): FAILED increments `failure_count` and sets `first_failure_date`; at 5 failures (`MIN_FAILURE_COUNT_BEFORE_DEACTIVATION`) **and** more than 7 days since the first one (`MIN_DELTA_BEFORE_DEACTIVATION`) the job is deactivated and `_notify_admin()` logs a WARNING. Any success resets both. Failing jobs are picked last (`ORDER BY failure_count, priority, id`, `:303`).
6. FULLY_DONE and FAILED are rescheduled to the next interval; PARTIALLY_DONE gets an immediate trigger.

Server actions that run code through `safe_eval` let `OperationalError`, `IntegrityError` and `ConcurrencyError` through unchanged (`_BUBBLEUP_EXCEPTIONS`, `odoo/tools/safe_eval.py:358-366`), so the cron sees the real error.

Demo *Same failure from cron* ([evidence](evidence/run_scenarios.txt)):

| Button | Result |
|---|---|
| Run Manually | 1 attempt, 40001, `failure_count` 1, error dialog |
| Trigger the cron worker | the real scheduler thread ran it: 1 attempt, 40001, `failure_count` 1 |
| Batched job | row 1 committed; row 2 failed with 40001; the job ran again about 25 ms later and committed rows 2 and 3; fully done, `failure_count` 0 |
| Pretend a week of failures, then Run Manually | 5th failure, first one 8 days ago: `active=False`, counters reset |

Two 19.0 details the demo exposed:

- **Run Manually no longer gets retries.** `method_direct_trigger()` (`:150`) acquires the job and calls `_process_job()`, so the job runs on its own cursor like the scheduler does, and its failure is counted. It captures ERROR log records and returns a `display_exception` client action. In 17.0 and 18.0 the button ran the server action inside the HTTP request (17.0 `ir_cron.py:102-111`), which `retrying()` wrapped: 5 attempts, measured on 17.0.
- **The error dialog shows even when the job recovered.** The batched job ended fully done, but its first run logged an ERROR, which `method_direct_trigger()` turned into the error dialog.

What to do in a cron: commit per batch with `_commit_progress()`, make each batch idempotent (a flag or a state per record, as the demo's `processed` field), and let the next run or the immediate re-run pick up what failed. Catching the 40001 and continuing in the same transaction does not help: the snapshot is still the old one, and the cursor needs a rollback (or a savepoint) before any further query.

### OCA queue_job 19.0.2.1.6

`/queue_job/runjob` is an HTTP route (`controllers/main.py:221`), but `_runjob()` (`:153`) handles concurrency itself:

```python
try:
    cls._try_perform_job(env, job)
except OperationalError as err:
    if err.pgcode not in PG_CONCURRENCY_ERRORS_TO_RETRY:
        raise
    raise RetryableJobError(err.pgerror, seconds=PG_RETRY) from err    # PG_RETRY = 5
...
except RetryableJobError as err:
    retry_postpone(job, str(err), seconds=err.seconds)                  # pending again, eta + 5 s
    env.cr.rollback()
    return
```

- The `max_retries` check lives in `Job.perform()` (`job.py:507-537`) and only sees a `RetryableJobError` raised **by the job function**. The conversion above happens outside `perform()`, so nothing stops it: measured, a job with `max_retries=3` was at `retry=11/3`, still pending, after 50 s. A job that conflicts permanently runs every 5 s until someone notices, repeating its side effects each time.
- The postpone is logged at DEBUG: at INFO the conflicts are invisible (note 07).
- `ConcurrencyError` is not an `OperationalError`: the job is marked failed at its first run. The exception then reaches the HTTP `retrying()` around `/queue_job/runjob`, which retries the request; the second run finds the job no longer enqueued and logs `was requested to run job ..., but it does not exist, or is not in state enqueued` (measured).
- `IntegrityError`: the job is marked failed; the HTTP layer converts the exception, nothing retries.
- Commits inside a job raise `RuntimeError("Commit is forbidden in queue jobs...")` unless the job function allows them (`_prevent_commit()`, `controllers/main.py:30-52`), for the same reason as below: a commit would release the job lock.
- Dependent jobs are enqueued in a separate loop with its own 5 tries and `uniform(0, 2 ** tries)` backoff, tries starting at 0 (`controllers/main.py:117-150`); it logs `retry n/5` at INFO and gives up at ERROR.
- Configuration trap: `--load` replaces the 19.0 default `base,rpc,web` (`odoo/tools/config.py:30`). `server_wide_modules = base,web,queue_job` drops the `rpc` module from the server-wide list, and XML-RPC answered 404 in the experiment. Use `base,rpc,web,queue_job`; a `base,web,queue_job` line carried over from an 18.0 configuration is exactly the trap.

### `odoo shell` and scripts

`odoo shell` opens one cursor, runs the console and rolls back at exit (`odoo/cli/shell.py:130-151`); nothing wraps a call in `retrying()`. Measured: the demo's serialization scenario called from a shell raised `SerializationFailure` after one attempt. Scripts that open `Registry(db).cursor()` themselves behave the same. If a script needs retries it has to call `retrying(func, env)` itself (`from odoo.service.model import retrying`), and then the rest of this note applies to it.

### Code that opens its own cursor

```python
with self.env.registry.cursor() as cr:      # independent transaction, commits on exit
    ...
```

- It commits independently: a rollback of the request does not undo it, and a retry of the request runs it again. It is an external side effect, like an API call. The demo's attempt log uses this on purpose, to survive rollbacks.
- It cannot see the request's uncommitted data.
- *Reasoning, not reproduced:* if it writes a row the request has already written, it waits for the request's lock while the request waits for it in Python. PostgreSQL sees no cycle (one side is not waiting on a lock), so it hangs until a `lock_timeout` or `statement_timeout`, or forever.

### `cr.commit()` inside a request

The house rule is never to commit in a request. Retries are the reason:

```python
order.value += 1
self.env.cr.commit()        # the first half is now permanent
stats.value += 1            # 40001 here
```

`retrying()` rolls back only what came after the commit and runs the whole call again, which confirms the order again. Demo *cr.commit() midway*: three attempts, **order confirmed 3 times** for one click ([evidence](evidence/run_scenarios.txt)). The commit also runs the postcommit hooks registered so far, early, and makes the rest of the attempt a new transaction with a new snapshot.

Core itself commits in some request paths; the audit in note 10 lists one (`iap_extract`'s status refresh).

### Other paths that handle concurrency themselves

- `/payment/status/poll` catches `OperationalError` and `IntegrityError`, rolls back and raises `Exception('retry')`; the browser polls again (`addons/payment/controllers/post_processing.py:51-60`).
- Filestore garbage collection catches `LockNotAvailable` and returns; the next run tries again (`odoo/addons/base/models/ir_attachment.py:211-216`).
- `_acquire_one_job()`: a 40001 means another cron worker processed the job; it is skipped, not retried.

### Open questions

- queue_job's behaviour on 40001 may be on purpose (a conflict is not a job failure), but an unbounded loop with repeated side effects is worth raising upstream or guarding in our job functions.
- 18.0 cron behaviour was read in the source only.
