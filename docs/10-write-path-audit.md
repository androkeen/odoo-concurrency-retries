# 10 - The write-path audit

> **Scope:** the checklist applies to Odoo 14-19 (the numbers are 19.0's); the audit below covers standard Odoo 19.0 modules: `sale_gelato` (community), `iap_extract` and `account_invoice_extract` (enterprise), and the inline email sends of `mail` and `auth_signup`.
> **Status:** findings come from reading the 19.0 source, helped by [`scripts/audit_write_paths.sh`](../scripts/audit_write_paths.sh). None of them was reproduced against the real services (Gelato, IAP); each says what it rests on.

## TL;DR

- **How is it reached?** Button, route or RPC: up to 5 runs (25 on 17.0 XML-RPC). Cron: 1 run, no retry. queue_job: re-run every 5 s.
- **What leaves the transaction?** Email send, HTTP call, file, another cursor, `cr.commit()`, sequence number: each runs once per attempt.
- **Is it deferred or idempotent?** `cr.postcommit`, queued mail, outbox plus worker, idempotency key from the business event.
- **Where can it collide?** Hot rows, lock order, NOWAIT, locks held across calls, long transactions, values read in another transaction.
- **What happens on failure?** No broad `except` that swallows or converts 40001; crons commit per batch; postcommit hooks catch their own errors.

## Deep dive

### The checklist in full

Run it per **write path**: one entry point and everything it calls.

| # | Question | Where to look | Retry-safe answer | Red flag |
|---|---|---|---|---|
| 1 | How is the path reached? | `@route`, `type="object"` buttons, overrides of core actions (`action_confirm`, `button_validate`), public methods called from JS (`orm.call`), RPC clients, `ir.cron` data, `base.automation`, `with_delay()` | you know the multiplier: 5 (HTTP/RPC), 25 (17.0 XML-RPC), 1 (cron, shell), unbounded (queue_job on 40001) | "it is only called from one place": every public method is an RPC entry point |
| 2 | What does it do outside the transaction? | `requests`, `iap_jsonrpc`, SDK clients, `send_mail(force_send=True)`, `.send()`, `registry.cursor()`, `cr.commit()`, `open(..., 'w')`, `next_by_code()` | nothing, or only reads | a write call before the last DB write of the path |
| 3 | Is each outside effect deferred or idempotent? | `postcommit`, `postrollback`, `send_after_commit`, outbox models, idempotency headers or external ids | deferred to the commit, queued, or keyed on the business event | a key generated inside the attempt (`uuid4()`), or none |
| 4 | Is anything committed mid-call? | `cr.commit()`, `_commit_progress()` outside crons, `registry.cursor()` blocks | only in crons and scripts, per batch | any commit in a request path |
| 5 | Where can it collide? | rows every user writes (counters, parents with stored totals, config rows), `FOR UPDATE`, `NOWAIT`, lock order across flows | short transaction, stable order, no hot row | an external call or a sleep between a write and the commit |
| 6 | Does a value cross a transaction? | values from the client, from a cache, read before a commit or through another cursor, written back as absolute numbers | increments computed inside the transaction, or insert-only rows | `write({'qty': value_from_form})` on a shared counter |
| 7 | What happens on a concurrency error? | `except Exception`, `except OperationalError`, savepoints around DB work | the error propagates to `retrying()`, or is converted on purpose (`ConcurrencyError`) | swallowed, logged and continued, or turned into a `UserError`/`AccessError` |
| 8 | If it is a cron or a job: what makes the next run safe? | per-record state, `_commit_progress()`, idempotent batches | each batch can run twice without harm | one transaction for the whole job, or side effects before the state is saved |
| 9 | Do postcommit hooks protect themselves? | the hook body | new cursor, `try`/`except` with logging | a hook that can raise: the user gets an error for saved data, later hooks are skipped |

Verdicts used below:

- **Safe**: database work only, or effects that are deferred, compensated or idempotent.
- **Duplicates**: an outside effect runs once per attempt (email twice, API call twice, credits twice).
- **Breaks atomicity**: a commit inside the call; retries re-apply committed work.
- **No retry**: a concurrency error is swallowed, converted, or happens where nothing retries.

### How to run it

1. `scripts/audit_write_paths.sh path/to/module` lists the candidate lines per question.
2. For every entry point, follow the calls down and note each outside effect with its line.
3. For each effect, answer questions 3-9 and give a verdict.
4. Record each finding with its trigger (which conflict, how likely), its consequence, and the fix.

### Audit 1: `sale_gelato` (19.0, print-on-demand orders)

Entry points found: the sale order **Confirm** button (override of `action_confirm()`), the product template **Synchronize** button, the delivery price computation, and the `/gelato/webhook` route.

| Write path | Outside effects | Under retries | Verdict |
|---|---|---|---|
| Confirm a Gelato order: `action_confirm()` -> `_create_order_on_gelato()` (`models/sale_order.py:68-138`) | POST a **draft** order to Gelato inline; postcommit: PATCH it to confirmed; postrollback: DELETE it | each failed attempt creates a draft and its rollback deletes it; only the committed attempt's draft is confirmed | **Safe**, by compensation, with gaps below |
| Synchronize a template: `action_sync_gelato_template_info()` (`models/product_template.py:51-93`) | GET the template | the GET is repeated; DB work redone | **Safe** |
| Delivery price: `gelato_rate_shipment()` (`models/delivery_carrier.py:59-116`) | POST a quote request | repeated, read-only on the Gelato side (as far as the API name says) | **Safe** |
| Webhook `/gelato/webhook` (`controlers/main.py:19-94`) | none: chatter messages and status emails go through `message_post`, sent after commit | retried, DB only | **Safe** under `retrying()` |

Findings:

1. **The confirmation is at most once.** The PATCH runs in a postcommit hook. If the worker dies between COMMIT and the hook, or the PATCH fails, the sale order is confirmed in Odoo and the Gelato order stays a draft: nothing retries it, only a chatter message records the failure (`sale_order.py:185-189`). *Consequence:* an order that is never produced. *Fix:* a periodic check of confirmed orders whose Gelato order is still a draft (an outbox), or retry the PATCH from a cron.
2. **A non-JSON error response escapes the postcommit hook.** `make_request()` calls `response.json()` before `raise_for_status()` and only catches `ConnectionError` and `Timeout` (`utils.py:30-47`); a non-JSON body raises `requests.exceptions.JSONDecodeError`, which is neither (checked: requests 2.33.1). `_confirm_order_on_gelato()` only catches `UserError`. *Consequence:* after the commit, the user gets an error for an order that is confirmed, and the postcommit hooks queued after this one do not run (note 04): with several orders confirmed together, the remaining orders are not confirmed on Gelato, and `send_after_commit()` emails queued later are not sent. *Fix:* catch `requests.RequestException` and `ValueError` in `make_request()`, and keep postcommit hooks from raising.
3. **The transaction stays open during the Gelato call.** The inline POST runs after the core confirmation work, inside the same transaction, for up to its 10 s timeout. Whatever that work has already written or locked stays locked meanwhile, and the request's conflict window (note 01) grows by the call's duration. Each retry adds a POST in the attempt and a DELETE in the rollback hook, synchronously, before the backoff sleep. *Consequence:* slower confirmations under load, more 40001 on busy orders, and a slow Gelato API turns into lock waits for other users.
4. **Webhook events are not deduplicated.** Nothing records which event was processed; if Gelato re-sends an `in_transit` event after a timeout on its side, the customer gets the status email again. Gelato's re-delivery policy was not checked.
5. *(Concurrency, not retries.)* `_create_attributes_from_gelato_info()` searches then creates attributes and values without a unique key (`product.attribute` and `product.attribute.value` have no unique constraint on the name in 19.0): two synchronizations at the same time can create duplicates.

### Audit 2: `iap_extract` and `account_invoice_extract` (19.0 enterprise, OCR of vendor bills)

Entry points found: the bill's **Digitize document** button and the OCR status widget (JS `orm.call`), the batch action and automatic digitization on upload, **Reload AI Data**, the IAP webhook `/account_invoice_extract/request_done/<uuid>`, and two crons (`check_all_status`, `_cron_validate`).

The core already knows the risk. `_send_batch_for_digitization()` (`iap_extract/models/extract_mixin.py:148-160`) says: "If we were sending them in the current transaction, a rollback would reset the state of documents successfully sent to the OCR server, causing the loss of IAP credits." The batch path follows its own advice; two other paths do not.

| Write path | Outside effects | Under retries | Verdict |
|---|---|---|---|
| Automatic and batch digitization -> `_send_batch_for_digitization()` | IAP `parse` (uses OCR credits) in a postcommit hook, new cursor, commit per document | the upload waits for the commit | **Safe** |
| **Digitize document** button and the status widget -> `action_manual_send_for_digitization()` -> `_upload_to_extract()` (`extract_mixin.py:203-207, 247-300`) | IAP `parse` **inline**; on no credit, `send_mail(force_send=True)` inline (`:330`) | each failed attempt uploads again and its `document_token` is rolled back | **Duplicates**: OCR credits per attempt, "no credit" email per attempt |
| Status widget -> `check_ocr_status()` -> `_try_to_check_ocr_status()` (`extract_mixin.py:217-234, 423-429`) | IAP `get_result` (read); **`cr.commit()` per record inside the HTTP request** | partial commits; a conflict on one bill is swallowed, not retried | **Breaks atomicity** |
| Webhook `request_done` (`account_invoice_extract/controllers/main.py:8-16`) | IAP `get_result`; partner autocomplete for unknown VAT numbers | retried; a duplicate OCR partner raises `ConcurrencyError` and the retry finds it (`models/account_invoice.py:362-377`) | **Safe**; the autocomplete call repeats per attempt |
| **Reload AI Data** -> `action_reload_ai_data()` (`models/account_invoice.py:54-75`) | IAP `get_result`, partner autocomplete | `except Exception` turns any error into `AccessError("Couldn't reload AI data.")` | **No retry** |
| Cron `check_all_status()` -> `_try_to_check_ocr_status()` | IAP `get_result`; commit per record | `except Exception` logs and continues **without a rollback** | **No retry**, degraded run |
| Cron `_cron_validate()` (`extract_mixin.py:104-123`) | IAP `validate` per record, state written at the end | if the final write fails, the next run validates again | **Safe enough**: validation is re-sent, not charged (assumption) |

Findings:

1. **Digitize document can spend OCR credits twice.** The button and the widget's *retry* call `_upload_to_extract()` inside the request. A 40001 after the IAP call (another user saving the same bill, a cron writing it) rolls back `extract_state` and `extract_document_uuid` and uploads again: credits used per attempt, earlier tokens lost. *Fix:* use the batch path (postcommit) for the button too.
2. **The status refresh commits inside a request.** `check_ocr_status()` refreshes the bill, then up to 19 other bills through `_try_to_check_ocr_status()`, which commits after each one (`:427`). The request is no longer one transaction: what came before each commit stays, whatever happens afterwards, and `retrying()` can no longer roll it back. *Fix:* no commit in the request path; leave per-record commits to the cron.
3. **Errors are swallowed without a rollback.** `_try_to_check_ocr_status()` catches every exception and logs a warning (`:428-429`). A 40001 there is never retried. Worse, after any database error (a 40001, or the `UniqueViolation` behind a duplicate OCR partner) the transaction is aborted, and every later bill of the same run fails with `InFailedSqlTransaction`, also swallowed; the request or the cron run then ends with that error or without the remaining updates. The `ConcurrencyError` that `_create_supplier_from_vat()` raises to get a retry is swallowed the same way. *Fix:* one savepoint per record (`with self.env.cr.savepoint():`), and let the next run pick up what failed.
4. **Reload AI Data converts everything.** `action_reload_ai_data()` catches `Exception` and raises `AccessError("Couldn't reload AI data.")` (`:73-75`). A 40001, or the `ConcurrencyError` for a duplicate partner, becomes an access error: the retry that would have succeeded never happens, and the message points the user at permissions. *Fix:* re-raise `OperationalError` and `ConcurrencyError` before the generic handler.
5. **Partner autocomplete repeats per attempt.** `_fetch_autocomplete_values()` calls IAP `enrich_by_vat` inline (`:325-342`). Whether each call is charged was not checked.

### Audit 3: inline email sends in `mail` and `auth_signup`

| Write path | Effect | Under retries | Verdict |
|---|---|---|---|
| `message_post()` notifications (`mail/models/mail_thread.py:3513-3522`) | `mail.mail` rows, sent by `send_after_commit()` | sent once, after commit | **Safe** |
| `template.send_mail(res_id)` (default) | `mail.mail` row, sent by the queue cron | rolled back with the attempt | **Safe** |
| `template.send_mail(res_id, force_send=True)` (`mail/models/mail_template.py:805-806`) | SMTP inside the transaction | one email per attempt | **Duplicates** |
| Password reset / invitation: `res.users._action_reset_password()` (`auth_signup/models/res_users.py:156-230`) | `send_mail(force_send=True)` or `mail.mail.send()`, inline | one email per attempt | **Duplicates**, harmless: the 19.0 token is a stateless signed payload (`res_partner.py:171-190`), so every link works |
| Signup confirmation in `/web/signup` (`auth_signup/controllers/main.py:63`) | `send_mail(force_send=True)` inline | one email per attempt | **Duplicates** |
| OCR "no credit" email (`iap_extract`, above) | `send_mail(force_send=True)` inline | one email per attempt | **Duplicates** |

### Summary

| Module | Finding | Verdict | Likelihood | Impact |
|---|---|---|---|---|
| iap_extract | Digitize document uploads inline | Duplicates | low (needs a conflict on the bill during the call) | OCR credits spent twice |
| iap_extract | Status refresh commits in the request | Breaks atomicity | every refresh with other pending bills commits; harm needs a failure | partial commits, conflicts not retried |
| iap_extract | Broad `except` without rollback (refresh, cron) | No retry | low | one failure fails the rest of the run |
| account_invoice_extract | Reload AI Data converts 40001 and `ConcurrencyError` to `AccessError` | No retry | low | misleading error, lost retry |
| sale_gelato | Confirmation is at most once | Safe, with a gap | low | order never produced |
| sale_gelato | JSON error escapes the postcommit hook | Safe, with a gap | low | error after commit, later hooks skipped |
| sale_gelato | Transaction open during the Gelato call | Contention | medium under load | slower confirmations, more conflicts |
| mail / auth_signup | `force_send=True` in request paths | Duplicates | low | duplicate emails |

None of these needs a code change in this task; they are inputs for upstream reports or for overrides in projects that use these modules heavily.
