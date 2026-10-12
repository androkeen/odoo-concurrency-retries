# 09 - Reducing contention in custom code

> **Scope:** Odoo 19.0 EE, `odoo/odoo@e6ebad8`, PostgreSQL in Odoo's REPEATABLE READ mode.
> **Status:** the counter results are measured with the demo (*Counter drift vs atomic update*, seven runs of each variant); the other advice follows from notes 01-04.

## TL;DR

- Keep transactions short: a request's conflict window runs from its first query to its last write.
- Lock and write rows in the **same order** everywhere (sort ids); never hold a lock across an external call or a sleep.
- Under REPEATABLE READ, `UPDATE ... SET x = x + 1` **still gets 40001**: 5-6 retries for 8 concurrent increments, against 8-10 for read-modify-write, and neither lost an update. Updates are lost when the value comes from **another transaction** (a form loaded earlier): +1 instead of +8, with no error.
- Remove the hot row: insert-only rows summed when read (0 retries), or a standard `ir.sequence` (never blocks, leaves gaps). Move heavy work to crons or jobs that commit in batches.

<!-- slide -->

8 requests add 1 to the same counter at the same moment, each through the real `retrying()`:

| Variant | Retries (7 runs) | Counter | Errors logged |
|---|---|---|---|
| Read-modify-write, ORM | 8-10 | **+8** | 40001, retried |
| `SET value = value + 1` | 5-6 | **+8** | 40001, retried |
| Value read in an earlier transaction | 0 | **+1** | none |
| Insert-only ledger | 0 | **+8** | none |

## Deep dive

### The counter experiment

Each button of the *Counter drift vs atomic update* scenario starts 8 threads behind a barrier. Each thread opens its own cursor and environment and calls the real `odoo.service.model.retrying()`, exactly like `execute_cr()` does for an RPC call. Each attempt first reads the session user, as a real request does during authentication, which takes the REPEATABLE READ snapshot. Then:

- **Read-modify-write (ORM):** `value = counter.value`, 0.1 s of "business logic", `counter.value = value + 1`.
- **`SET value = value + 1`:** one `UPDATE` statement.
- **Stale form value:** each user read the value in an earlier transaction ("opened the form"), then the users save one after the other, 0.2 s apart, writing `value = read + 1`, which is what a form save sends.
- **Insert-only ledger:** each request inserts a `concurrency.demo.ledger` row with `amount = 1`; the total is computed when read.

Seven runs of each variant ([one of them in full](evidence/run_scenarios.txt)):

| Variant | Attempts | Retries | Duration | Counter |
|---|---|---|---|---|
| Read-modify-write | 16-18 | 8-10 | 2.2-6.1 s | +8 every time |
| `SET value = value + 1` | 13-14 | 5-6 | 1.3-2.2 s | +8 every time |
| Stale form value | 8 | 0 | 1.6 s | **+1 every time** |
| Insert-only ledger | 8 | 0 | 0.1 s | +8 every time |

### Why the atomic UPDATE still conflicts

REPEATABLE READ refuses to change a row that another transaction changed after your snapshot (note 01), whatever the statement: `SET value = value + 1` is still an UPDATE of a changed row. All 8 requests took their snapshot together; the first one to update won, and the others waited for its lock, then failed with 40001 when it committed. Retries spread them out with random backoff. The atomic form only shortens each attempt's window (no 0.1 s between read and write), hence fewer retries.

What the atomic form does buy:

- it does not depend on a value read earlier, so it cannot write back a stale one;
- it keeps the time between taking the row lock and committing short.

What it does not buy, in Odoo: freedom from 40001. That would need READ COMMITTED, which Odoo does not use.

### Where updates are really lost

Inside one transaction, REPEATABLE READ protects read-modify-write: the second writer gets 40001, retries, re-reads and adds its 1. Updates are lost when the value crossed a transaction boundary:

- a **form** loaded at 10:00 and saved at 10:05 writes back the value it displayed. Odoo has no optimistic lock on form saves (no `__last_update` check in 17.0 or 19.0);
- a value read, then written after a **`cr.commit()`**;
- a value read through **another cursor**, or kept in a cache or a global between requests;
- a value computed by a **client or an integration** and sent as an absolute number.

In the demo, 7 of 8 increments vanished, and nothing in the log says so. That is the "counter drifting" symptom: no error, no retry, wrong number.

### Removing the hot row

- **Insert-only rows.** One row per event, summed when read (or summarised by a cron). Inserts of different rows never conflict: 0 retries in the demo.
- **`ir.sequence`, standard implementation.** `next_by_code()` uses a PostgreSQL sequence (`nextval()`): it never blocks and never conflicts, but it is not transactional, so every rolled-back attempt, including each retry, burns a number. Gaps are normal.
- **`ir.sequence`, no-gap implementation.** `_update_nogap()` locks the sequence row with `FOR UPDATE NOWAIT` (`odoo/addons/base/models/ir_sequence.py:59-64`): no gaps, but every creator is serialized on that row and concurrent ones get 55P03 and retry (note 01). Use it only where the law asks for it.
- **Stored computes on a shared parent.** A total stored on a parent and recomputed by every child write turns the parent into a hot row. Consider not storing it, or recomputing it in a batch.

### Ordering and holding locks

- Two flows that update the same records in different orders deadlock (note 01). Sort before writing: `records.sorted('id')`, or lock explicitly in a deterministic order: `SELECT id FROM t WHERE id = ANY(%s) ORDER BY id FOR UPDATE`.
- A lock is held until COMMIT. An external call made inside the transaction keeps every row written so far locked for the call's whole duration, and widens the request's conflict window: `sale_gelato` keeps its confirmation transaction open during its Gelato request (note 10). Make the call before the writes, or after the commit.
- Batch jobs can step around each other with 19.0's `try_lock_for_update()` (`odoo/orm/models.py:5605`): lock what is free with SKIP LOCKED, skip the rest, as ir.cron does to share jobs between workers.

### Shorter transactions, fewer requests in the hot path

- Every write is a conflict candidate: avoid writes on read paths ("last seen" timestamps, counters bumped on display).
- Move heavy work out of the request: a cron with `_commit_progress()` per batch, or a queue job per record. A conflict then costs one batch, not the user's whole click (and no retries, so handle it per note 08).
- Take the lock first when the work is expensive: `SELECT ... FOR UPDATE` at the start of the transaction fails at once (40001 or a wait) instead of after the work is done.
