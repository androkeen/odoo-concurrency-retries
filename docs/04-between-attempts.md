# 04 - What happens between attempts

> **Scope:** Odoo 19.0 EE, `odoo/odoo@e6ebad8`.
> **Status:** verified in the source; reproduced by the demo (*Unsafe side effect*, *Safe side effect*, *cr.commit() midway*) and by `tests/test_retrying_facts.py`. Two edge cases at the end are source reading only and marked as such.

## TL;DR

- Between attempts Odoo runs `cr.rollback()`, `env.transaction.reset()`, `registry.reset_changes()`, reloads the session and rewinds uploads, then runs the **whole call** again.
- Undone: everything inside the transaction. Repeated: everything outside it (SMTP send, HTTP call, file write, another cursor, a `cr.commit()`, a sequence number).
- Retry-safe side effects: `self.env.cr.postcommit.add(...)`, queued `mail.mail`, an outbox row delivered by a worker, an idempotency key derived from the business event.
- The COMMIT is after the loop: postcommit hooks run once, after a successful attempt, and a failure at commit time is never answered with a re-run.

<!-- slide -->

```python
# Unsafe: the confirmation leaves on every attempt
order.state = 'confirmed'
self._send_confirmation(order)

# Safe: queued until the attempt commits, dropped by a rollback
order.state = 'confirmed'
self.env.cr.postcommit.add(partial(self._send_confirmation, order.id))
```

Demo, one click, two attempts lost to 40001: **3** calls inline, **1** with `postcommit`.

## Deep dive

### The cleanup, call by call

`odoo/service/model.py:195-206`, run for every caught error:

| Call | Code | What it undoes |
|---|---|---|
| `env.cr.rollback()` | `sql_db.py:570-577` | ORM transaction data (cache, pending writes, fields to recompute) and the precommit queue (`clear()`); the postcommit queue; runs `prerollback` hooks; PostgreSQL `ROLLBACK`; runs `postrollback` hooks |
| `env.transaction.reset()` | `orm/environments.py:610` | takes a fresh `Registry(db_name)` for every environment of the transaction, resets their cached properties, clears the transaction again. 17.0 spelled it `env.reset()`; 19.0 deprecates that alias |
| `env.registry.reset_changes()` | `orm/registry.py:1169-1179` | if the attempt invalidated the registry (a new field, a new model), sets the models up again; if it cleared caches, clears them again (they may have been refilled with rolled-back data) and drops the pending signal to other workers |
| `request.session = request._get_session_and_dbname()[0]` | `http.py:1813-1851` | discards the attempt's session changes (login, logout, context, `request.session[...]`) by reloading the session from the session store |
| upload rewind | `model.py:201-206` | puts every uploaded file back at byte 0 (note 05) |

After the cleanup the loop sleeps (note 06) and calls `func()` again: authentication, `_pre_dispatch`, the controller, `_post_dispatch`, from the top.

### What survives an attempt

The transaction is rolled back; Python memory is not. Things that keep their state into the next attempt:

- the `Request` object (params, headers, `httprequest`), the thread, the cursor object itself (rolled back, not closed);
- module globals, class attributes, `threading.local` values, anything cached outside the ORM;
- `ormcache` entries the attempt filled without invalidating them;
- whatever the attempt already sent, wrote or committed elsewhere.

The demo uses the second point on purpose: its attempt log keys a call on the cursor object, because `retrying()` keeps the same cursor across attempts (`tools/attempt_log.py`).

### Side effects: undone, repeated, or safe

| Effect | Rolled back? | On a retry | Safe pattern |
|---|---|---|---|
| ORM or SQL writes on `env.cr` | yes | done again, correctly | nothing to do |
| `cr.precommit` hooks (mail tracking values, `bus.bus` rows) | queue cleared | run again inside the next attempt, at its flush | nothing to do |
| `cr.postcommit` hooks | queue cleared | run once, after the final COMMIT | the pattern below |
| `cr.postrollback` hooks | run on every rollback, also between attempts | | compensation (sale_gelato deletes a remote draft) |
| `message_post()` notification emails | `mail.mail` rows rolled back | sent once, after commit (`send_after_commit()`, `addons/mail/models/mail_thread.py:3513-3522`) | nothing to do |
| `mail.template.send_mail(..., force_send=True)` or `mail.mail.send()` | no, SMTP is outside | **one email per attempt** (`addons/mail/models/mail_template.py:805-806`) | `force_send=False` (queue) or `send_after_commit()` |
| HTTP or API call inline | no | **one call per attempt** (demo: 3 calls for 1 order) | postcommit, or outbox plus idempotency key |
| Writes through another cursor (`registry.cursor()`) | no, committed | committed again | avoid, or make them idempotent |
| `cr.commit()` in the call | the committed part stays | **committed part applied again** (demo: order confirmed 3 times) | never commit in a request |
| `nextval()`: standard `ir.sequence`, record ids | no | a new number per attempt: gaps | accept gaps, or a no-gap sequence |
| Filestore writes (`ir.attachment` data) | no | nothing new: the path is the content's SHA-1 and `_file_write()` skips an existing file (`odoo/addons/base/models/ir_attachment.py:159-171`) | already safe: each new file is put on a garbage-collection checklist "in case the transaction aborts" |
| Session changes | reloaded from the store | | see the edge case below |

### The postcommit pattern

```python
self.env.cr.postcommit.add(partial(self._push_to_api, record.id))
```

Rules, each from the 19.0 source:

1. **Register inside the attempt.** A rollback clears the queue (`sql_db.py:573`), so only the committed attempt's hooks run.
2. **The callable takes no argument.** Bind data with `functools.partial` (ids, not recordsets bound to the request cursor).
3. **Open a new cursor for database work.** The hook runs right after COMMIT on a cursor whose transaction is over. Core does it every time: `send_after_commit()` (`addons/mail/models/mail_mail.py:674-694`), `google_calendar`'s `after_commit` (`addons/google_calendar/models/google_sync.py:24-49`), `account`'s `after_commit` decorator (`addons/account/tools/after_commit.py`), `sale_gelato`'s `post_commit` wrapper. In the HTTP path the request closes that cursor and rolls back whatever the hook wrote on it.
4. **Catch errors inside the hook.** The data is already committed. An exception escapes `retrying()` (not retried, `signal_changes()` skipped), the user gets an error for a saved record, and the hooks queued after it never run (`Callbacks.run()`, `odoo/tools/misc.py:1170-1176`; tests `test_commit_is_outside_the_loop`, `test_a_failing_postcommit_hook_skips_the_next_ones`). `account`'s `after_commit` logs instead of raising for this reason.
5. **It runs before the response is sent**, in the same worker: a slow API call delays the user.
6. **At most once.** If the worker dies between COMMIT and the hook, or the call fails, nothing tries again. Use an outbox when the effect must happen.
7. **Tests do not run it.** `TestCursor.commit()` clears postcommit hooks (`odoo/tests/test_cursor.py:104`); core code like `send_after_commit()` sends at once when `modules.module.current_test` is set.

Core uses it for external systems for the reason this note is about. `google_sync.py:24-27`: "API requests are sent to Google Calendar after the current transaction ends ... It is particularly important for event creation, otherwise the event might be created twice in Google if the first creation crashed in Odoo."

### Compensation: create, then confirm or delete

`sale_gelato` (`addons/sale_gelato/models/sale_order.py:101-138`) cannot defer its call: confirming the sale order needs the Gelato order id. It creates a **draft** order inline, then registers both outcomes:

```python
data = utils.make_request(api_key, 'order', 'v4', 'orders', payload=payload)   # orderType: draft
self.env.cr.postcommit.add(partial(self._confirm_order_on_gelato, data['id']))
self.env.cr.postrollback.add(partial(self._delete_order_on_gelato, data['id']))
```

A retried confirmation creates one draft per attempt and deletes each rolled-back one, because `retrying()`'s rollback runs postrollback hooks. Exactly one draft is confirmed. The audit (note 10) lists the remaining gaps.

### Queued mail

`template.send_mail(res_id)` (the default `force_send=False`) creates a `mail.mail` row in the transaction; the mail queue cron sends it later. A rolled-back attempt takes its row with it. `force_send=True` sends inside the transaction: one email per attempt. `message_post()` is safe: its notification emails go through `send_after_commit()`.

### Outbox and idempotency keys

The outbox is the robust version: the message is a row written in the business transaction, so it commits or rolls back with the business data, and a worker delivers it afterwards, at least once. At-least-once means duplicates, so the receiver needs a key to drop them:

- derive the key from the business event (`order-42-confirmation`), never from a value generated inside the attempt: a `uuid4()` per attempt changes on every retry and protects nothing;
- send it the way the API expects (an `Idempotency-Key` header, an external reference field);
- or check the remote side by external id before creating.

Demo *Safe side effect*: *Confirm order (outbox)* lost two attempts and left exactly one pending message; *Deliver outbox* with *Crash after delivery* ticked delivered it and failed before marking it sent; the second delivery sent it again and the receiver ignored it as a duplicate ([evidence](evidence/run_scenarios.txt)).

### Why the COMMIT is after the loop

1. **Conflicts surface inside the loop.** `env.cr.flush()` (`model.py:190`) is inside the `try`: it executes every pending ORM write and runs the precommit hooks (`sql_db.py:176-187`), so their 40001s are retried. Under REPEATABLE READ row conflicts are raised by statements, not by COMMIT, so once the flush succeeds the COMMIT has nothing retryable left. (Test: `test_flush_and_precommit_hooks_run_inside_the_loop`.)
2. **COMMIT runs the postcommit hooks** (`sql_db.py:560-568`). Inside the `try`, a hook failure would roll back nothing (the data is committed) and run the whole call again: everything applied twice. Outside, it propagates once. (Tests: `test_commit_is_outside_the_loop`, `test_postcommit_hooks_run_once_after_the_successful_attempt`.)
3. **A failed COMMIT has an unknown outcome.** A dropped connection during COMMIT does not tell you whether it happened; re-running the call blindly could apply it twice.
4. **Signals follow the commit.** `signal_changes()` runs after COMMIT and tells other workers about committed registry and cache changes only; a failed attempt drops its signal with `reset_changes()` instead.

### Edge cases (source reading, not reproduced)

- **Session saved before a failing flush.** `_post_dispatch()` saves a dirty session (`Dispatcher.post_dispatch()`, `http.py:2450-2457`) inside the loop, before `retrying()`'s flush. If that flush fails, the reloaded session already holds the failed attempt's changes.
- **`ormcache` filled from rolled-back data.** `reset_changes()` only clears caches the attempt invalidated. A cache filled from data the attempt wrote with raw SQL, without invalidating, keeps the rolled-back value.
- **Read-only route that writes** (with a replica): the call runs once on the replica, fails with `ReadOnlySqlTransaction`, and runs again on the primary (`http.py:2298-2311`): side effects of that first run happen too.

### Verification

- Demo, one click each ([evidence](evidence/run_scenarios.txt)): *Confirm order* 3 attempts, 3 external calls; *Confirm order (postcommit)* 3 attempts, 1 call; *Confirm order (outbox)* 3 attempts, 1 message; *cr.commit() midway* 3 attempts, order confirmed 3 times.
- Tests: `test_pending_orm_writes_are_dropped_between_attempts`, `test_flush_and_precommit_hooks_run_inside_the_loop`, `test_postcommit_hooks_run_once_after_the_successful_attempt`, `test_commit_is_outside_the_loop`, `test_a_failing_postcommit_hook_skips_the_next_ones`.
