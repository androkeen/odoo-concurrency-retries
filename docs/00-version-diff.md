# 00 - Version baseline and differences from 17.0

> **Compared:** Odoo 19.0 EE at [`odoo/odoo@e6ebad8`](https://github.com/odoo/odoo/tree/e6ebad84b25be6e4dbabc6e803d96ad105b4f485) (2026-10-09) against 17.0 at [`bf7355f`](https://github.com/odoo/odoo/tree/bf7355f1c6a5e8929511f0aaea2ea7e5f0267b76) (2026-10-09, the head of the 17.0 branch; the local 17.0 checkout at `69abc02` has the same code in every file below). 18.0 at `56c7014` (2026-07-14) is only used to date a change.
> **Status:** every row is checked in the source; the rows marked *measured* were also reproduced (demo module, or the probes in [`experiments/`](../experiments)).

## TL;DR

- Same on 17.0 and 19.0: the three SQLSTATEs, 5 attempts, backoff `uniform(0, 2 ** tryno)`, the two INFO log lines, the upload rewind and its `RuntimeError`, the flush inside the loop and the commit after it.
- **19.0** also retries `odoo.exceptions.ConcurrencyError`, and builds the IntegrityError message with `_sql_error_to_message()`: `_as_validation_error()` is gone.
- **18.0+** XML-RPC and `/jsonrpc` get 5 attempts; on **17.0** they get up to **25** (two nested loops, measured).
- **19.0** "Run Manually" runs a cron job on its own cursor: no retry (17.0 and 18.0 retried it 5 times inside the HTTP request). Since 18.0 a failing job is counted and eventually deactivated.
- **19.0** `lock_for_update()` takes SKIP LOCKED locks and raises `LockError`, a `UserError`: no 55P03, no retry.

## Baseline checklist

The brief asked to verify seven points against 19.0 before writing anything.

| # | Point from the brief | 19.0 | Detail |
|---|---|---|---|
| 1 | `retrying()` lives in `odoo/service/model.py`, same callers | **Yes**, callers changed shape | Still [`model.py:160`](https://github.com/odoo/odoo/blob/e6ebad84b25be6e4dbabc6e803d96ad105b4f485/odoo/service/model.py#L160-L241). Callers: `execute_cr()` (`model.py:150`, XML-RPC and `/jsonrpc`) and `Request._serve_db()` at `http.py:2303` (read-only cursor) and `http.py:2329` (read/write cursor). Since 18.0 `_serve_db()` matches the route before the loop and can run two loops for a read-only route that writes (note 08). 17.0 had one call wrapping everything, `_match()` included. |
| 2 | `PG_CONCURRENCY_ERRORS_TO_RETRY` holds exactly three codes | **Yes**, but it is no longer the test | `model.py:28` still lists 55P03, 40001, 40P01. Since 18.0 the loop tests the exception classes `PG_CONCURRENCY_EXCEPTIONS_TO_RETRY` (`model.py:29`), and 19.0 adds `ConcurrencyError` (`model.py:192, 216-221`). |
| 3 | `MAX_TRIES_ON_CONCURRENCY_FAILURE` is 5, same loop shape | **Yes** | `model.py:30`, `for tryno in range(1, MAX + 1)` at `model.py:185`, unchanged since 17.0. |
| 4 | Cleanup between attempts: `rollback`, `env.reset`, `registry.reset_changes`, session re-fetch | **Changed spelling** | `env.cr.rollback()`, **`env.transaction.reset()`**, `env.registry.reset_changes()`, `request.session = request._get_session_and_dbname()[0]` (`model.py:195-200`). `Environment.reset()` is deprecated in 19.0 (`orm/environments.py:61`) and calls the same `Transaction.reset()`. |
| 5 | File rewind and its `RuntimeError` message | **Yes** | `model.py:201-206`, same text. The quoted name is the form field name (`'upload'` in the demo), not the file name. |
| 6 | Backoff formula, log messages and levels | **Yes**, one label change | `random.uniform(0.0, 2 ** tryno)` at `model.py:226`; both lines at INFO (`model.py:223, 227`). The label is `errorcodes.lookup(pgcode)` for psycopg2 errors and `repr(exc)` for a `ConcurrencyError`. |
| 7 | IntegrityError conversion `_as_validation_error()` | **Changed** | The helper is gone. `model.py:207-214` finds the model from `exc.diag.table_name` and raises `ValidationError(_("The operation cannot be completed: %s", model._sql_error_to_message(exc)))`. `_sql_error_to_message()` (`orm/models.py:3276`) uses the `ir.model.constraint` message, then the `models.Constraint` message, then a generic text. Still raised once, still never retried. |

## Differences found against 17.0

| Topic | 17.0 | 19.0 | Since | Evidence |
|---|---|---|---|---|
| Retried errors | `exc.pgcode in PG_CONCURRENCY_ERRORS_TO_RETRY` ([`model.py:173 @17`](https://github.com/odoo/odoo/blob/bf7355f1c6a5e8929511f0aaea2ea7e5f0267b76/odoo/service/model.py#L173)) | `isinstance(exc, PG_CONCURRENCY_EXCEPTIONS_TO_RETRY)` or `ConcurrencyError` | 18.0 (classes), 19.0 (`ConcurrencyError`) | source, test, demo |
| `odoo.exceptions.ConcurrencyError` | does not exist | new, retried, passed through by `safe_eval` (`tools/safe_eval.py:358`) | 19.0 | source, test, demo |
| IntegrityError message | `_as_validation_error()` with `_sql_constraints` and `translate_sql_constraint()` ([`model.py:91 @17`](https://github.com/odoo/odoo/blob/bf7355f1c6a5e8929511f0aaea2ea7e5f0267b76/odoo/service/model.py#L91)) | `model._sql_error_to_message(exc)` | 19.0 | source, test, demo |
| Between-attempt reset | `env.reset()` | `env.transaction.reset()` (same effect) | 19.0 | source |
| XML-RPC `/xmlrpc/2/object`, `/jsonrpc` | the HTTP loop wraps the RPC loop: up to **25 executions** (*measured*) | `_check_request()` closes the request cursor, the HTTP loop re-raises at once: **5** (*measured*) | 18.0 | experiment, source `rpc/controllers/__init__.py:14` |
| RPC endpoints | in `base` | in the auto-installed `rpc` addon, deprecated, removal planned for Odoo 22 | 19.0 | source |
| `--load` / `server_wide_modules` default | `base,web` | `base,rpc,web` (`tools/config.py:30`); `--load=base,web,queue_job` drops XML-RPC (404, *measured*) | 19.0 | experiment |
| Read-only routes and replica cursor | none | `readonly=` routes on a replica; a write raises `ReadOnlySqlTransaction` and the call runs again on a read/write cursor | 18.0 | source |
| JSON-2 API `/json/2/<model>/<method>` | none | one HTTP loop, 5 attempts | 19.0 | source |
| Cron "Run Manually" | runs the server action inside the HTTP request: **5 attempts** (*measured*) | `_process_job()` on its own cursor: **1 attempt**, failure counted, error dialog (*measured*) | 19.0 | experiment, demo |
| Cron failures | logged and rolled back, no counter | `failure_count`, `first_failure_date`; deactivation after 5 failures over more than 7 days (*measured*) | 18.0 | demo |
| Cron progress API | none | `_commit_progress()` (19.0, `_notify_progress()` from 18.0 is deprecated); a failure after committed progress re-runs the job at once (*measured*) | 18.0 / 19.0 | demo |
| Row-lock helpers | `_try_lock()` on ir.cron with `FOR UPDATE NOWAIT` and an early rollback ([`ir_cron.py:413 @17`](https://github.com/odoo/odoo/blob/bf7355f1c6a5e8929511f0aaea2ea7e5f0267b76/odoo/addons/base/models/ir_cron.py#L413)) | `BaseModel.lock_for_update()` / `try_lock_for_update()`: SKIP LOCKED, `LockError` (a `UserError`) | 19.0 | source, demo |
| Core NOWAIT users (community, non-test files) | 12: cron, module, sequences, account, EDI, stock scheduler, ... | 6: sequences no-gap, loyalty codes, POS session, website payment, POS self-order payment, transifex (plus Studio approvals and two more in enterprise) | 19.0 | source |

Two things that did **not** change and are often assumed to have changed: Odoo still runs every cursor in REPEATABLE READ (`sql_db.py:373`), and there is still no optimistic locking of form saves (no `__last_update` check in 17.0 or 19.0).

## Deep dive

### How the comparison was done

1. `retrying()`, `execute_cr()`, `dispatch()` and `Request._serve_db()` read side by side on 17.0, 18.0 and 19.0.
2. The 17.0 files that matter (`odoo/service/model.py`, `odoo/http.py` `_serve_db`, `odoo/addons/base/controllers/rpc.py`, `odoo/addons/base/models/ir_cron.py`) downloaded from the head of the 17.0 branch and diffed against the local checkout: only an unrelated security change in `get_public_method()` and two `group_operator` attributes differ.
3. Each 19.0 behaviour exercised with the demo module, see [`evidence/run_scenarios.txt`](evidence/run_scenarios.txt).
4. The entry-point counts measured on a scratch 17.0 database with the probe in [`experiments/odoo17_entry_points`](../experiments/odoo17_entry_points): a method that always raises 40001 and counts its calls.

### The 17.0 double loop, step by step

1. `Request._serve_db()` (17.0) opens the request cursor and calls `retrying(self._serve_ir_http, self.env)`.
2. The XML-RPC controller calls `dispatch_rpc()`, which hides the request (`borrow_request()`) and calls `model.dispatch()`.
3. `dispatch()` opens a second cursor and `execute_cr()` runs `retrying(call_kw, env)` on it: five attempts.
4. After the fifth failure the `SerializationFailure` propagates through the controller to the outer `retrying()`. The request cursor is still open, so the outer loop rolls it back and re-runs the controller, which runs five more attempts.
5. Five outer attempts times five inner attempts: 25 executions. Measured on 17.0: XML-RPC 25, `/jsonrpc` 25, web `call_kw` 5, "Run Manually" 5.

18.0 and 19.0 call `_check_request()` first, which closes the request cursor; the outer loop then meets `if env.cr._closed: raise` (`model.py:193`) and stops. That `_closed` check is the reason the guard exists.

### Open questions

- When exactly `_check_request()` landed in 18.0: the local 18.0 checkout is shallow, so the commit could not be identified. It is not in 17.0 at `bf7355f`.
- 18.0 was not exercised by experiment; its rows come from the source.
