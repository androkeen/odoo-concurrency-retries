# 03 - IntegrityError is never retried

> **Scope:** Odoo 19.0 EE, `odoo/odoo@e6ebad8`.
> **Status:** verified in the source, by `test_integrity_error_becomes_validation_error_without_retry`, and by the demo (*Unique violation*).
> **See also:** the constraints entry, which owns the conversion of database errors into user messages (`models.Constraint`, `ir.model.constraint`). *Link to add when that entry is published.*

## TL;DR

- An `IntegrityError` (unique, foreign key, not null, check) is rolled back, converted into a `ValidationError` and raised at the **first** attempt. It is never retried.
- 19.0 builds the message with `model._sql_error_to_message(exc)`: the constraint's own message if there is one, else a generic text. 17.0's `_as_validation_error()` is gone.
- Two users racing on a unique key get **23505, not 40001**: the retry that would have found the other row never happens.
- When a retry is the right answer, catch the violation and raise `ConcurrencyError` (19.0), like the OCR partner creation does.

## Deep dive

### The code

```python
# odoo/service/model.py:207-214
if isinstance(exc, IntegrityError):
    model = env['base']
    for rclass in env.registry.values():
        if exc.diag.table_name == rclass._table:
            model = env[rclass._name]
            break
    message = env._("The operation cannot be completed: %s", model._sql_error_to_message(exc))
    raise ValidationError(message) from exc
```

- It runs after `env.cr.rollback()`, so `_sql_error_to_message()` can still query (`odoo/orm/models.py:3276-3291`: "the cursor on self has to be in a valid state").
- The model is found from the table named in the PostgreSQL diagnostics; unknown tables fall back to `base`.
- `raise ... from exc` keeps the original psycopg2 error as `__cause__`: the traceback in the log still shows the constraint name and the conflicting key.
- With a non-seekable upload in the request, the rewind check (note 05) raises `RuntimeError` before this code runs.

### Where the message comes from (19.0)

`_sql_error_to_message(exc)`:

1. If the constraint is one of the model's table objects (`models.Constraint`, `models.UniqueIndex`), use its message from `ir.model.constraint` (translatable), else the message declared in the code.
2. Otherwise `_sql_error_to_message_generic()` (`odoo/orm/models.py:3294`):
   - not null: "Missing required value for the field ..." with the model and field;
   - foreign key: "Another model is using the record you are trying to delete ... How about archiving the record instead?";
   - unique: "The value for 'code' (Code) already exists. Detail: Key (code)=(...) already exists.";
   - check: no specific text, PostgreSQL's message.

17.0 did the same job in `_as_validation_error()` from `_sql_constraints` and `translate_sql_constraint()`.

### The race on a unique key

Demo *Unique violation*, button *Check, then create()* ([evidence](evidence/run_scenarios.txt)):

1. Another user inserts `ORDER-32` and keeps the transaction open for 1 s.
2. The request searches for `ORDER-32`: the row is not in its snapshot, the search returns nothing.
3. The request inserts `ORDER-32`. PostgreSQL checks the unique index against **all** row versions, sees the other user's uncommitted row and waits.
4. The other user commits. The request's INSERT fails with `23505 duplicate key value violates unique constraint`.
5. `retrying()` rolls back and raises `ValidationError("The operation cannot be completed: Another row already uses this code.")`. One attempt, 1013 ms, no retry.

Under REPEATABLE READ this is a unique violation, not a serialization failure, so it is not retried, although a second attempt would have found the row. In SERIALIZABLE mode PostgreSQL may report such cases as 40001, but Odoo never uses SERIALIZABLE.

### Asking for the retry: get or create with `ConcurrencyError`

```python
row = Row.search([('code', '=', code)], limit=1)
if not row:
    try:
        row = Row.create({'code': code, ...})
    except psycopg2.errors.UniqueViolation as exc:
        # The other row exists, but only a new transaction can see it.
        raise ConcurrencyError(f"Row {code} was created by a concurrent transaction") from exc
row.value += 1
```

`create()` executes its INSERT immediately, so the violation is raised inside the method. The transaction is aborted at that point; raising at once is fine, using the cursor again is not. Demo button *Get or create, ConcurrencyError*: attempt 1 rolled back, backoff 0.2 s, attempt 2 found the other row and committed. The same pattern in 19.0 enterprise: `account_invoice_extract` `_create_supplier_from_vat()` (`enterprise/account_invoice_extract/models/account_invoice.py:362-377`).

Before 19.0 there is no `ConcurrencyError`. A savepoint plus a second search does not help either: the second search uses the same snapshot and still cannot see the row. Only a new transaction can.

### Verification

- Test `test_integrity_error_becomes_validation_error_without_retry`: one call, the exact message, `UniqueViolation` as the cause.
- Demo: *Unique violation* (both buttons).

### Open questions

- 17.0 and 18.0 have no `ConcurrencyError`. What do our older modules do when a get-or-create races? A user-visible `ValidationError` is the likely answer; not checked.
