# 01 - Which errors are retried

> **Scope:** Odoo 19.0 EE, `odoo/odoo@e6ebad8`; paths are relative to the odoo repository unless they start with `enterprise/`.
> **Status:** verified in the source, by the demo buttons *Serialization failure*, *Deadlock*, *Lock not available* and *Unique violation*, and by `tests/test_retrying_facts.py`.

## TL;DR

- `retrying()` re-runs a call for three PostgreSQL errors: **40001** `SERIALIZATION_FAILURE`, **40P01** `DEADLOCK_DETECTED`, **55P03** `LOCK_NOT_AVAILABLE`.
- **New in 19.0:** it also retries `odoo.exceptions.ConcurrencyError`, which code raises on purpose to ask for a fresh transaction.
- Everything else is raised at once: other `OperationalError`s, every `UserError` (19.0's `LockError` included), and `IntegrityError`, which becomes a `ValidationError` (note 03).
- 40001: you write a row that changed after your snapshot. 40P01: two transactions wait for each other. 55P03: `NOWAIT` or `lock_timeout` meets a held lock.

## Deep dive

### The code

```python
# odoo/service/model.py:28-30
PG_CONCURRENCY_ERRORS_TO_RETRY = (errorcodes.LOCK_NOT_AVAILABLE, errorcodes.SERIALIZATION_FAILURE, errorcodes.DEADLOCK_DETECTED)
PG_CONCURRENCY_EXCEPTIONS_TO_RETRY = (errors.LockNotAvailable, errors.SerializationFailure, errors.DeadlockDetected)
MAX_TRIES_ON_CONCURRENCY_FAILURE = 5

# odoo/service/model.py:192-221, inside the loop
except (IntegrityError, OperationalError, ConcurrencyError) as exc:
    if env.cr._closed:
        raise
    ...                                   # rollback and resets (note 04), upload rewind (note 05)
    if isinstance(exc, IntegrityError):
        ...
        raise ValidationError(message) from exc          # note 03
    if isinstance(exc, PG_CONCURRENCY_EXCEPTIONS_TO_RETRY):
        error = errorcodes.lookup(exc.pgcode)            # "SERIALIZATION_FAILURE"
    elif isinstance(exc, ConcurrencyError):
        error = repr(exc)                                # "ConcurrencyError('...')"
    else:
        raise                                            # any other OperationalError
```

Read it in order:

1. Only three families enter the handler: `IntegrityError`, `OperationalError` and `ConcurrencyError`. Anything else (a `UserError`, a `KeyError`, a `ReadOnlySqlTransaction`) goes straight to the outer `except Exception`, which resets the transaction and re-raises, without even a rollback (the request closes its cursor later).
2. `env.cr._closed`: if the call closed the cursor, there is nothing to roll back or retry. The RPC endpoints close the request cursor on purpose, which is what stops two retry loops from nesting since 18.0 (note 00).
3. The rollback, the resets and the upload rewind run for **every** caught error, retryable or not.
4. The test is on the exception class since 18.0. `PG_CONCURRENCY_ERRORS_TO_RETRY` (SQLSTATE strings) is still defined and still imported by OCA `queue_job`, but `retrying()` no longer uses it.

| Exception | SQLSTATE | psycopg2 parents | Retried |
|---|---|---|---|
| `SerializationFailure` | 40001 | `TransactionRollbackError`, `OperationalError` | yes |
| `DeadlockDetected` | 40P01 | `TransactionRollbackError`, `OperationalError` | yes |
| `LockNotAvailable` | 55P03 | `OperationalError` | yes |
| `odoo.exceptions.ConcurrencyError` | none | `Exception` | yes, 19.0 |
| `QueryCanceled` (statement_timeout, cancel) | 57014 | `QueryCanceledError`, `OperationalError` | no, raised at once |
| other `OperationalError` (lost connection, disk full, ...) | 08xxx, 53xxx, ... | `OperationalError` | no |
| `IntegrityError` (unique, foreign key, not null, check) | 23xxx | `IntegrityError` | no: `ValidationError` (note 03) |
| `odoo.exceptions.LockError` | none | `UserError` | no, 19.0 |
| `ReadOnlySqlTransaction` | 25006 | `InternalError` | not by `retrying()`; `_serve_db()` re-runs the call on a read/write cursor (note 08) |

### REPEATABLE READ, the part that matters

Every Odoo cursor runs in REPEATABLE READ (`sql_db.py:373`; the reasoning is in the `Cursor` docstring, `sql_db.py:281-344`). Four rules explain all three errors:

1. The snapshot is taken by the **first statement** of the transaction, not by `BEGIN`. In a logged-in request that is the session check made while authenticating (`security.check_session()`, `odoo/service/security.py:13`, which reads `res_users`), before your method starts.
2. Reads never block and never fail.
3. `UPDATE`, `DELETE` and `SELECT ... FOR UPDATE/SHARE` on a row another transaction has locked wait for that transaction to end.
4. If that transaction **changed** the row and committed after your snapshot, your statement fails with `40001 could not serialize access due to concurrent update`. If it only locked the row, or rolled back, your statement proceeds.

So the conflict window of a request is its whole transaction: from the authentication query to the last write. Under READ COMMITTED (PostgreSQL's default, not Odoo's) rule 4 would not exist: the statement would re-read the new row version and go on. Odoo prefers snapshot isolation and pays for it with 40001 and `retrying()`. Note 09 shows the consequence for `UPDATE ... SET x = x + 1`: it still gets 40001.

### 40001 in practice

Two transactions write the same row: a user saves a record a cron is updating, two confirmations touch the same shared record, a stored compute is recomputed on a shared parent, a counter-like row is bumped. The log shows the statement and the table:

```
ERROR odoo.sql_db: bad query: b' UPDATE "concurrency_demo_row" SET "value" = "__tmp"."value"::int4, ...
ERROR: could not serialize access due to concurrent update
INFO odoo.service.model: SERIALIZATION_FAILURE, 4 tries left, try again in 1.2723 sec...
```

Demo *Serialization failure* ([evidence](evidence/run_scenarios.txt)): attempts 1 and 2 waited 1.0 s for the other user's lock, then failed; backoffs 1.4 s and 2.1 s; attempt 3 committed, 5.6 s after the click.

### 40P01 in practice

Two transactions each hold a row lock the other needs. PostgreSQL lets them wait for `deadlock_timeout` (1 s by default), then the session whose own check finds the cycle aborts **itself**: `ERROR: deadlock detected`, with a DETAIL line naming both processes:

```
ERROR: deadlock detected
DETAIL:  Process 19260 waits for ShareLock on transaction 2046832; blocked by process 21519.
Process 21519 waits for ShareLock on transaction 2046833; blocked by process 19260.
```

Only one side fails; the other continues as if nothing happened. If the victim is a cron job or a script, nothing retries it. Sources: two flows that update the same records in opposite orders (one sorts by id, the other by date), or a flow that locks a parent then a child while another locks the child then the parent.

Demo *Deadlock*: the request locks A then B, a background flow locks B then A. The background flow waits 0.3 s before asking for A (30 % of `deadlock_timeout`, read from `pg_settings`), so the request starts waiting first, its check runs first and it is the victim. Attempt 1 failed after 1013 ms with DEADLOCK_DETECTED, attempt 2 committed. In production you do not choose the victim.

### 55P03 in practice

`SELECT ... FOR UPDATE NOWAIT` fails at once instead of waiting; so does any lock wait longer than `lock_timeout`. Odoo 19.0 uses both on purpose:

| Where | What happens on 55P03 |
|---|---|
| `ir.sequence` no-gap numbers, `_update_nogap()` (`odoo/addons/base/models/ir_sequence.py:61`) | retried by `retrying()` |
| Loyalty and coupon codes, `sale_loyalty` (`addons/sale_loyalty/models/sale_order.py:1499-1505`); the comment says NOWAIT is there "to make sure we trigger a serialization error ... and thus, trigger a retry of the transaction" | retried |
| POS session opening, `/pos/ui` (`addons/point_of_sale/controllers/main.py:84-89`) | retried |
| Studio approval rules (`enterprise/web_studio/models/studio_approval.py:655-657`); the comment: "the query will actually be retried by Odoo itself (not PG); the NOWAIT ensures that no deadlock will happen" | retried |
| Website checkout payment (`addons/website_sale/controllers/payment.py:39-50`) | caught: `UserError("Payment is already being processed.")`, no retry |
| Module buttons set `lock_timeout = '3s'` (`odoo/addons/base/models/ir_module.py:611`) | an Install/Upgrade click that cannot get its locks within 3 s raises 55P03 inside the request |
| Filestore garbage collection, `lock_timeout = '10s'` (`odoo/addons/base/models/ir_attachment.py:211`) | caught, tried again at the next run |
| Registry loading, `lock_timeout = '15s'` (`odoo/modules/loading.py:370`) | not in a request |

19.0 moved several call sites from NOWAIT to SKIP LOCKED. The ORM's `lock_for_update()` (`odoo/orm/models.py:5577`) explains why: "Use SKIP LOCKED instead of NOWAIT because the later aborts the transaction and we do not want to use SAVEPOINTS." It raises `LockError("Cannot grab a lock on records")`, a `UserError` with HTTP status 409, which `retrying()` does not catch. `ir.cron` uses it for `write()`, unlink and `toggle()` (`ir_cron.py:697-733`), where 17.0 used NOWAIT plus an early rollback.

Demo *FOR UPDATE NOWAIT*: another transaction holds the lock for 3 s; attempts 1 to 3 failed after about 9 ms each, attempt 4 committed at 6.8 s. Demo *ORM lock_for_update()*: one attempt, `LockError`, no retry.

### ConcurrencyError (19.0)

`odoo/exceptions.py:128`. The docstring calls it low-level, to be used "only if all alternatives are deemed worse". Use it when only a new transaction can see what you need: a row another transaction created after your snapshot. The one production use in 19.0 is OCR partner creation in `enterprise/account_invoice_extract/models/account_invoice.py:362-377`: the partner `create()` hits a `UniqueViolation` because another transaction created the same partner, so the code raises `ConcurrencyError("Duplicated OCR partner")` and the retry finds the partner. `safe_eval` lets it through (`odoo/tools/safe_eval.py:358-366`), so a server action can raise it. The retry log line uses `repr(exc)`:

```
INFO odoo.service.model: ConcurrencyError('Row ORDER-33 was created by a concurrent transaction'), 4 tries left, try again in 0.1655 sec...
```

It only helps where `retrying()` runs: in a cron it is a plain failure, and OCA `queue_job` 19.0.2.1.6 marks the job failed (note 08). Demo *Get or create, ConcurrencyError*: attempt 1 rolled back, attempt 2 found the row and committed.

### Verification

- Tests: `test_exactly_three_sqlstates`, `test_the_three_sqlstates_are_retried`, `test_concurrency_error_is_retried`, `test_other_operational_error_is_raised_at_once`, `test_user_error_is_raised_at_once`.
- Core's own test: `odoo/addons/test_http/tests/test_misc.py` `test_misc8_concurrency_error`.
- Demo runs: [`evidence/run_scenarios.txt`](evidence/run_scenarios.txt).

### Open questions

- Which flows of our databases hit 40001 most? Count it from production logs (note 07) before optimizing anything.
- The `ReadOnlySqlTransaction` re-run only exists with a read replica (`db_replica_host`); it was not reproduced here.
