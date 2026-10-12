# 07 - Logging and diagnosis

> **Scope:** Odoo 19.0 EE, `odoo/odoo@e6ebad8`.
> **Status:** log formats verified in the source, by `test_five_attempts_four_sleeps_then_the_error` and in the demo server log; logging configuration is source reading; the Odoo.sh paths are from its documentation, not checked here.

## TL;DR

- Two INFO lines on the `odoo.service.model` logger: `<ERROR>, N tries left, try again in X sec...` per retry, and `<ERROR>, maximum number of tries reached!` when it gives up. `<ERROR>` is the SQLSTATE name, or `repr()` of a `ConcurrencyError`.
- Each failed attempt also logs an **ERROR** `odoo.sql_db: bad query: <SQL>` with PostgreSQL's message, so requests that succeeded on a retry still leave ERROR lines. The SQL names the table.
- Grep `tries left|maximum number of tries` (locally, or `~/logs/odoo.log` on Odoo.sh). Above INFO, add `--log-handler=odoo.service.model:INFO`.
- Contention rate = retry lines per hour; `maximum number` lines are failures users saw. Crons and queue_job do not write these lines.

<!-- slide -->

```console
$ grep -E "odoo.service.model: .*(tries left|maximum number of tries)" odoo.log
... INFO db odoo.service.model: SERIALIZATION_FAILURE, 4 tries left, try again in 1.2723 sec...
... INFO db odoo.service.model: DEADLOCK_DETECTED, 4 tries left, try again in 0.6597 sec...
... INFO db odoo.service.model: SERIALIZATION_FAILURE, maximum number of tries reached!

$ grep "tries left" odoo.log | cut -c1-13 | sort | uniq -c    # retries per hour
```

## Deep dive

### What one retried request writes to the log

Demo *Serialization failure*, real server log (threaded server, default log level):

```
12:02:08,054 ERROR bp425_retry_demo odoo.sql_db: bad query: b' UPDATE "concurrency_demo_row"
                    SET "value" = "__tmp"."value"::int4, "write_date" = ...
ERROR: could not serialize access due to concurrent update

12:02:08,058 INFO bp425_retry_demo odoo.service.model: SERIALIZATION_FAILURE, 4 tries left, try again in 1.2723 sec...
12:02:10,347 ERROR bp425_retry_demo odoo.sql_db: bad query: b' UPDATE "concurrency_demo_row" ...
ERROR: could not serialize access due to concurrent update

12:02:10,351 INFO bp425_retry_demo odoo.service.model: SERIALIZATION_FAILURE, 3 tries left, try again in 1.4221 sec...
12:02:11,793 INFO bp425_retry_demo werkzeug: 127.0.0.1 - - "POST /web/dataset/call_button/concurrency.demo.scenario.action_serialization_failure HTTP/1.1" 200 - 44 0.016 4.747
```

Reading it:

- **`bad query` (ERROR, `odoo.sql_db`, `sql_db.py:443`).** Logged by `Cursor.execute()` for any failing statement, before `retrying()` decides anything. The first line of the SQL names the table; for a deadlock, PostgreSQL adds a DETAIL with both processes and transaction ids.
- **`tries left` (INFO, `odoo.service.model`, `model.py:227`).** One line per retry, with the chosen sleep. A non-retryable error (IntegrityError, other OperationalError) never produces it.
- **`maximum number of tries reached!` (INFO, `model.py:223`).** The fifth failure; the error goes on to the user and is logged again by the HTTP layer as `Exception during request handling` with a traceback.
- **The werkzeug access line.** Odoo appends three numbers (`odoo/netsvc.py:121-140`): query count, time spent in SQL, and the rest of the request time. A failed statement is **not** added to the SQL time (the counters are updated after the `try` in `sql_db.py:450-457`), so the lock waits of failed attempts and the backoff sleeps land in the third number: `44 0.016 4.747` is a request that spent 16 ms in successful SQL and 4.7 s elsewhere, mostly waiting.

### What to grep

Locally:

```console
# every retry and every give-up
grep -E "odoo.service.model: .*(tries left|maximum number of tries)" odoo.log

# per kind of conflict
grep -oE "odoo.service.model: [A-Z_]+|odoo.service.model: ConcurrencyError" odoo.log | sort | uniq -c

# which tables: the statement logged just before the PostgreSQL error
grep -A1 "bad query" odoo.log | grep -B1 -E "could not serialize|deadlock detected|could not obtain lock" | grep -oE 'UPDATE "[a-z_]+"|FROM "[a-z_]+"' | sort | uniq -c

# retries per hour
grep "tries left" odoo.log | cut -c1-13 | sort | uniq -c

# requests that failed after five attempts
grep -c "maximum number of tries reached" odoo.log
```

On Odoo.sh the server log is `~/logs/odoo.log` (rotated files next to it), readable from the web shell or the Logs tab; the same greps apply. *Not verified in this task.*

### When the log level hides the lines

The default log level is INFO, so both lines are there. With `--log-level=warn` (or `log_level = warn` in the config file) Odoo sets `odoo:WARNING` (`odoo/netsvc.py:315-342`) and the retry lines disappear, while the `bad query` ERROR lines stay. `--log-handler` entries are applied after the log level, so this brings the retry lines back:

```console
odoo-bin ... --log-level=warn --log-handler=odoo.service.model:INFO
# config file:  log_handler = :INFO,odoo.service.model:INFO
```

The demo's attempt log copies the backoff from these lines; with the logger above INFO its *Backoff (ms)* column stays empty.

### Estimating contention

- **Retries per hour** from the `tries left` lines; plot them against traffic.
- **Give-ups** (`maximum number of tries reached`) are the requests users saw fail; each one is preceded by four retry lines.
- **Per table** from the `bad query` lines: the table tells you which model and flow to look at.
- **Per endpoint**: the werkzeug line of the same thread follows the retry lines; with `--workers` the PID column ties them together.
- **Id gaps.** A rolled-back attempt consumes the ids of the rows it inserted, so retried requests leave gaps in `mail_message`, `account_move`, and other tables.
- **On the PostgreSQL side**: `SELECT deadlocks, xact_rollback FROM pg_stat_database WHERE datname = current_database();` counts deadlocks and rollbacks since the last statistics reset, and `log_lock_waits = on` logs every lock wait longer than `deadlock_timeout`. Standard PostgreSQL, not tested here.

### What is not in these lines

- **Crons** log `Job 'name' (id) server action #N failed` at ERROR with the traceback (`ir_cron.py:508`), with no retry line, because there is no retry (note 08).
- **OCA queue_job** 19.0.2.1.6 catches the three errors itself and logs the postpone at DEBUG: at INFO, a job that conflicts every 5 s for hours leaves only access-log lines. The job's *Retries* counter is the only trace (note 08).
- **Code that swallows the error**: an `except Exception` around a failing statement hides the conflict, and the next statement fails with `current transaction is aborted` (`InFailedSqlTransaction`). That message in a log usually points at a swallowed 40001 just before.
