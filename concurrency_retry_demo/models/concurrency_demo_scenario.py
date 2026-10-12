import logging
import threading
import time
from datetime import timedelta
from functools import partial

from psycopg2 import errors

from odoo import api, fields, models
from odoo.exceptions import ConcurrencyError, UserError
from odoo.modules.registry import Registry
from odoo.service.model import MAX_TRIES_ON_CONCURRENCY_FAILURE, retrying
from odoo.tools import SQL

from odoo.addons.base.models.ir_cron import (
    MIN_DELTA_BEFORE_DEACTIVATION,
    MIN_FAILURE_COUNT_BEFORE_DEACTIVATION,
)

from ..tools import attempt_log, contention
from .concurrency_demo_run import OUTCOMES

_logger = logging.getLogger(__name__)

COUNTER_VARIANTS = {
    "orm": "Read-modify-write (ORM)",
    "atomic": "UPDATE ... SET value = value + 1",
    "stale": "Value read by an earlier transaction",
    "ledger": "Insert-only ledger",
}
UPLOAD_MODES = {
    "seekable": "Seekable upload",
    "non_seekable": "Non-seekable upload",
    "non_seekable_integrity": "Non-seekable upload, unique violation",
}
# Seconds of "business logic" between the read and the write of a row.
THINK_TIME = 0.1
# Seconds between the saves of the users in the stale-value variant.
STALE_SAVE_GAP = 0.2
REQUEST_TIMEOUT = 120


class ConcurrencyDemoScenario(models.Model):
    """One concept of the demo: a form, its rows, and the buttons that trigger it."""

    _name = "concurrency.demo.scenario"
    _description = "Concurrency Demo Scenario"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    kind = fields.Selection(
        selection=[
            ("serialization", "Serialization failure"),
            ("deadlock", "Deadlock"),
            ("lock_not_available", "Lock not available"),
            ("exhaust", "Exhaust retries"),
            ("unique_violation", "Unique violation"),
            ("unsafe_side_effect", "Unsafe side effect"),
            ("safe_side_effect", "Safe side effect"),
            ("counter", "Counter drift vs atomic update"),
            ("cron", "Same failure from cron"),
            ("upload", "Non-seekable upload"),
        ],
        required=True,
    )
    summary = fields.Char()
    description = fields.Html()
    failing_attempts = fields.Integer(
        default=1,
        help="How many attempts meet a concurrent transaction before one can succeed.",
    )
    hold_seconds = fields.Float(
        string="Hold (seconds)",
        default=1.0,
        help="How long the competing transaction keeps its lock before it commits.",
    )
    concurrent_requests = fields.Integer(
        default=8, help="Counter scenario: requests sent at the same moment."
    )
    simulate_crash = fields.Boolean(
        string="Crash after delivery",
        help="Outbox delivery fails after the external call, before the message is "
        "marked as sent.",
    )
    row_ids = fields.One2many("concurrency.demo.row", "scenario_id", string="Rows")
    run_ids = fields.One2many("concurrency.demo.run", "scenario_id", string="Runs")
    external_call_ids = fields.One2many(
        "concurrency.demo.external.call", "scenario_id", string="External calls"
    )
    outbox_ids = fields.One2many("concurrency.demo.outbox", "scenario_id", string="Outbox")
    last_run_id = fields.Many2one(
        "concurrency.demo.run", compute="_compute_last_run", string="Last run"
    )
    last_run_attempt_ids = fields.One2many(
        "concurrency.demo.attempt", compute="_compute_last_run", string="Last run attempts"
    )
    last_run_outcome = fields.Selection(
        selection=OUTCOMES, compute="_compute_last_run", string="Outcome"
    )
    last_run_attempt_count = fields.Integer(compute="_compute_last_run", string="Attempts")
    last_run_retry_count = fields.Integer(compute="_compute_last_run", string="Retries")
    last_run_external_call_count = fields.Integer(
        compute="_compute_last_run", string="Calls to the outside world"
    )
    last_run_note = fields.Char(compute="_compute_last_run", string="Result")
    cron_id = fields.Many2one("ir.cron", string="Scheduled action")
    batched_cron_id = fields.Many2one("ir.cron", string="Batched scheduled action")
    cron_active = fields.Boolean(related="cron_id.active", string="Job active")
    cron_failure_count = fields.Integer(related="cron_id.failure_count", string="Job failure count")
    cron_first_failure_date = fields.Datetime(
        related="cron_id.first_failure_date", string="Job first failure"
    )
    cron_nextcall = fields.Datetime(related="cron_id.nextcall", string="Job next call")
    cron_lastcall = fields.Datetime(related="cron_id.lastcall", string="Job last call")
    batched_cron_failure_count = fields.Integer(
        related="batched_cron_id.failure_count", string="Batched job failure count"
    )

    @api.depends("run_ids")
    def _compute_last_run(self):
        for scenario in self:
            run = scenario.run_ids[:1]
            scenario.last_run_id = run
            scenario.last_run_attempt_ids = run.attempt_ids
            scenario.last_run_outcome = run.outcome
            scenario.last_run_attempt_count = run.attempt_count
            scenario.last_run_retry_count = run.retry_count
            scenario.last_run_external_call_count = run.external_call_count
            scenario.last_run_note = run.note

    # ------------------------------------------------------------------
    # Serialization failure (40001)
    # ------------------------------------------------------------------

    def action_serialization_failure(self):
        """A concurrent update fails the first attempts with 40001; a retry succeeds."""
        self.ensure_one()
        attempt = self._begin_attempt("Concurrent update")
        row = self._row("target")
        if attempt.attempt_no <= self.failing_attempts:
            # Another user updates the row and commits hold_seconds later. Our
            # snapshot was taken by the first query of this request, so their
            # commit stays invisible to this attempt.
            contention.hold_row(self.env.registry, row.id, self.hold_seconds)
        row.value += 100
        # UPDATE now: it waits for the other user's lock, then fails with
        # "could not serialize access due to concurrent update" (40001).
        row.flush_recordset(["value"])
        return True

    # ------------------------------------------------------------------
    # Deadlock (40P01)
    # ------------------------------------------------------------------

    def action_deadlock(self):
        """Two flows lock rows A and B in opposite orders."""
        self.ensure_one()
        attempt = self._begin_attempt("A then B, against B then A")
        row_a, row_b = self._row("a"), self._row("b")
        if attempt.attempt_no > self.failing_attempts:
            row_a.value += 1
            row_b.value += 1
            return True
        a_locked = contention.lock_b_then_a(
            self.env.registry, row_a.id, row_b.id, self._deadlock_head_start()
        )
        row_a.value += 1
        row_a.flush_recordset(["value"])  # we hold A, the other flow holds B
        a_locked.set()
        row_b.value += 1
        # We wait for B while the other flow waits for A. After deadlock_timeout
        # PostgreSQL aborts one of them: us, because we started waiting first.
        row_b.flush_recordset(["value"])
        return True

    def _deadlock_head_start(self):
        """Delay before the other flow asks for row A, in seconds.

        PostgreSQL runs the deadlock check in the session that has waited for
        deadlock_timeout, and aborts that session. Waiting first makes the
        request the victim, which is the case retrying() handles.
        """
        self.env.cr.execute(
            SQL("SELECT setting::int FROM pg_settings WHERE name = 'deadlock_timeout'")
        )
        timeout_ms = self.env.cr.fetchone()[0]
        return min(0.3, timeout_ms / 1000 * 0.3)

    # ------------------------------------------------------------------
    # Lock not available (55P03)
    # ------------------------------------------------------------------

    def action_lock_not_available(self):
        """FOR UPDATE NOWAIT against a held lock: 55P03, retried until the lock is gone."""
        self.ensure_one()
        attempt = self._begin_attempt("FOR UPDATE NOWAIT")
        row = self._row("target")
        if attempt.attempt_no == 1:
            # Another transaction locks the row, without changing it.
            contention.lock_row(self.env.registry, row.id, self.hold_seconds)
        # Fail at once instead of waiting: "could not obtain lock on row" (55P03).
        self.env.cr.execute(
            SQL(
                "SELECT id FROM %s WHERE id = %s FOR UPDATE NOWAIT",
                SQL.identifier(row._table),
                row.id,
            )
        )
        row.value += 1
        return True

    def action_lock_for_update(self):
        """The 19.0 ORM helper uses SKIP LOCKED and raises LockError: never retried."""
        self.ensure_one()
        attempt = self._begin_attempt("ORM lock_for_update()")
        row = self._row("target")
        if attempt.attempt_no == 1:
            contention.lock_row(self.env.registry, row.id, self.hold_seconds)
        # LockError is a UserError: retrying() does not catch it, the user gets
        # "Cannot grab a lock on records" from the first attempt.
        row.lock_for_update()
        row.value += 1
        return True

    # ------------------------------------------------------------------
    # Exhaust retries
    # ------------------------------------------------------------------

    def action_exhaust_retries(self):
        """A row that stays hot longer than the whole retry budget."""
        self.ensure_one()
        attempt = self._begin_attempt("Hot row")
        row = self._row("target")
        if attempt.attempt_no == 1:
            attempt.call.extra["stop_hot_row"] = contention.keep_row_hot(
                self.env.registry, row.id, self.hold_seconds
            )
        if attempt.attempt_no >= MAX_TRIES_ON_CONCURRENCY_FAILURE:
            # Last attempt: stop the background writer, whatever happens.
            stop = attempt.call.extra["stop_hot_row"]
            self.env.cr.postrollback.add(stop.set)
            self.env.cr.postcommit.add(stop.set)
        value = row.value
        time.sleep(THINK_TIME * 3)  # business logic between the read and the write
        row.value = value + 1
        # The row changed since our snapshot: 40001, on every attempt.
        row.flush_recordset(["value"])
        return True

    # ------------------------------------------------------------------
    # Unique violation (23505)
    # ------------------------------------------------------------------

    def action_unique_violation(self):
        """Two users create the same code: an IntegrityError, never retried."""
        self.ensure_one()
        attempt = self._begin_attempt("Check, then create()")
        code = f"ORDER-{attempt.run_id}"
        if attempt.attempt_no == 1:
            # Another user inserts the same code and commits hold_seconds later.
            contention.insert_code(self.env.registry, self.id, code, self.hold_seconds)
        Row = self.env["concurrency.demo.row"]
        # Our snapshot cannot see the other row, so the check passes...
        if not Row.search_count([("code", "=", code)], limit=1):
            # ...and the INSERT waits for the other transaction, then fails with
            # UniqueViolation (23505). retrying() turns it into a ValidationError
            # at once: an IntegrityError is never retried.
            Row.create({"scenario_id": self.id, "name": code, "code": code})
        return True

    def action_unique_get_or_create(self):
        """Same race, but the code asks for a retry with ConcurrencyError (19.0)."""
        self.ensure_one()
        attempt = self._begin_attempt("Get or create, ConcurrencyError")
        code = f"ORDER-{attempt.run_id}"
        if attempt.attempt_no == 1:
            contention.insert_code(self.env.registry, self.id, code, self.hold_seconds)
        Row = self.env["concurrency.demo.row"]
        row = Row.search([("code", "=", code)], limit=1)
        if not row:
            try:
                row = Row.create({"scenario_id": self.id, "name": code, "code": code})
            except errors.UniqueViolation as exc:
                # The other row exists, but only a new transaction can see it.
                # Same pattern as account_invoice_extract (enterprise).
                raise ConcurrencyError(
                    f"Row {code} was created by a concurrent transaction"
                ) from exc
        row.value += 1
        return True

    # ------------------------------------------------------------------
    # Side effects
    # ------------------------------------------------------------------

    def action_unsafe_side_effect(self):
        """The confirmation leaves inside the transaction: one per attempt."""
        self.ensure_one()
        attempt = self._begin_attempt("Inline call")
        order = self._row("order")
        if attempt.attempt_no <= self.failing_attempts:
            contention.hold_row(self.env.registry, order.id, self.hold_seconds)
        order.value += 1  # confirm the order: the ORM writes it at the flush
        self._call_outside_world(attempt, "inline", f"{order.name}: confirmation email")
        # retrying() flushes after this method returns: the UPDATE meets the
        # other user's commit and fails with 40001, after the email was sent.
        return True

    def action_commit_midway(self):
        """cr.commit() inside the call: each retry re-runs work already committed."""
        self.ensure_one()
        attempt = self._begin_attempt("cr.commit() midway")
        order, stats = self._row("order"), self._row("stats")
        order.value += 1  # confirm the order
        # Deliberate anti-pattern, the point of this button: a commit inside a
        # request makes the first half of every attempt permanent.
        self.env.cr.commit()
        attempt.rearm(self.env.cr)
        attempt_log.note_attempt(
            attempt.dbname, attempt.attempt_id, f"{order.name} confirmed and committed"
        )
        if attempt.attempt_no <= self.failing_attempts:
            contention.hold_row(self.env.registry, stats.id, self.hold_seconds)
        stats.value += 1
        # 40001: retrying() rolls back the second half only, then confirms the
        # order again.
        stats.flush_recordset(["value"])
        return True

    def action_safe_side_effect(self):
        """Same flow, the call waits for the commit: exactly one per click."""
        self.ensure_one()
        attempt = self._begin_attempt("cr.postcommit")
        order = self._row("order")
        if attempt.attempt_no <= self.failing_attempts:
            contention.hold_row(self.env.registry, order.id, self.hold_seconds)
        order.value += 1
        # Runs only if this attempt commits: the rollback of a failed attempt
        # clears the postcommit queue (Cursor.rollback()).
        self.env.cr.postcommit.add(
            partial(
                self._call_outside_world,
                attempt,
                "postcommit",
                f"{order.name}: confirmation email",
            )
        )
        return True

    def action_outbox_side_effect(self):
        """The message is a row of the business transaction: it rolls back with it."""
        self.ensure_one()
        attempt = self._begin_attempt("Outbox row")
        order = self._row("order")
        if attempt.attempt_no <= self.failing_attempts:
            contention.hold_row(self.env.registry, order.id, self.hold_seconds)
        order.value += 1
        self.env["concurrency.demo.outbox"].create(
            {
                "scenario_id": self.id,
                "run_id": attempt.run_id,
                "payload": f"{order.name}: confirmation email",
                "idempotency_key": f"order-{order.id}-confirmation-{attempt.run_id}",
            }
        )
        return True

    def action_process_outbox(self):
        """What a worker does with the outbox: deliver each pending message, at least once."""
        self.ensure_one()
        attempt = self._begin_attempt("Outbox delivery")
        for message in self.outbox_ids.filtered(lambda m: m.state == "pending"):
            self._call_outside_world(
                attempt, "outbox", message.payload, message.idempotency_key
            )
            if self.simulate_crash:
                raise UserError(
                    self.env._(
                        "Simulated crash after delivering %s: the message stays "
                        "pending and the next delivery sends it again.",
                        message.idempotency_key,
                    )
                )
            message.write({"state": "sent", "sent_at": fields.Datetime.now()})
        return True

    def _call_outside_world(self, attempt, channel, payload, idempotency_key=False):
        """Stand-in for an SMTP send or an API call: the outside world keeps it."""
        return attempt_log.record_external_call(
            self.env.cr.dbname,
            run_id=attempt.run_id,
            attempt_no=attempt.attempt_no,
            channel=channel,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------------
    # Counter drift vs atomic update
    # ------------------------------------------------------------------

    def action_counter_orm(self):
        return self._run_counter("orm")

    def action_counter_atomic(self):
        return self._run_counter("atomic")

    def action_counter_stale(self):
        return self._run_counter("stale")

    def action_counter_ledger(self):
        return self._run_counter("ledger")

    def _run_counter(self, variant):
        """Send concurrent_requests requests at the same moment.

        Each request runs in its own thread, cursor and environment, through
        the real retrying(), the way execute_cr() runs an RPC call.
        """
        self.ensure_one()
        counter = self._row("counter")
        dbname = self.env.cr.dbname
        requests = max(self.concurrent_requests, 1)
        run_id = attempt_log.create_run(
            dbname,
            self.id,
            variant=COUNTER_VARIANTS[variant],
            channel="rpc",
            request_count=requests,
        )
        before = self._read_counter_total(dbname, counter.id)
        barrier = threading.Barrier(requests)
        t0 = time.monotonic()
        threads = [
            threading.Thread(
                target=_simulated_request,
                args=(dbname, self.env.uid, self.id, counter.id, variant),
                kwargs={"run_id": run_id, "t0": t0, "request_no": number, "barrier": barrier},
                name=f"concurrency-demo-request-{number}",
                daemon=True,
            )
            for number in range(1, requests + 1)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(REQUEST_TIMEOUT)
        after = self._read_counter_total(dbname, counter.id)
        attempt_log.update_run(
            dbname,
            run_id,
            {
                "value_before": before,
                "value_after": after,
                "note": f"expected +{requests}, got {after - before:+d}",
            },
        )
        return True

    @staticmethod
    def _read_counter_total(dbname, row_id):
        # A fresh transaction: this request's own snapshot predates the others.
        return attempt_log.independent(
            dbname, lambda env: env["concurrency.demo.row"].browse(row_id).total
        )

    def _counter_increment(self, variant, counter_id, stale_value=None):
        """One attempt of one simulated request."""
        self._begin_attempt(COUNTER_VARIANTS[variant], channel="rpc")
        # A real request reads the session user before it dispatches: that
        # first query pins the REPEATABLE READ snapshot of the attempt.
        self.env.cr.execute(SQL("SELECT login FROM res_users WHERE id = %s", self.env.uid))
        counter = self.env["concurrency.demo.row"].browse(counter_id)
        if variant == "orm":
            value = counter.value
            time.sleep(THINK_TIME)
            counter.value = value + 1
        elif variant == "atomic":
            self.env.cr.execute(
                SQL(
                    "UPDATE %s SET value = value + 1 WHERE id = %s",
                    SQL.identifier(counter._table),
                    counter.id,
                )
            )
        elif variant == "stale":
            # What a form sends on save: an absolute value, read earlier.
            counter.value = stale_value + 1
        else:
            self.env["concurrency.demo.ledger"].create(
                {"row_id": counter.id, "run_id": self.env.context.get(attempt_log.CTX_RUN_ID)}
            )

    # ------------------------------------------------------------------
    # Same failure from cron
    # ------------------------------------------------------------------

    def action_cron_run_manually(self):
        """The 'Run Manually' button of the scheduled action."""
        self.ensure_one()
        return self.cron_id.method_direct_trigger()

    def action_cron_trigger(self):
        """Ask the cron worker to run the job; the run appears once it has picked it up."""
        self.ensure_one()
        self.cron_id._trigger()
        return True

    def action_cron_batched(self):
        """'Run Manually' on a job that commits its progress after each row."""
        self.ensure_one()
        return self.batched_cron_id.method_direct_trigger()

    def action_cron_fake_failure_history(self):
        """Pretend the job has failed for a week: its next failure deactivates it."""
        self.ensure_one()
        self.cron_id.write(
            {
                "failure_count": MIN_FAILURE_COUNT_BEFORE_DEACTIVATION - 1,
                "first_failure_date": fields.Datetime.now()
                - MIN_DELTA_BEFORE_DEACTIVATION
                - timedelta(days=1),
            }
        )
        return True

    @api.model
    def _cron_contended_job(self):
        """Scheduled action: the conflict of the first scenario, with no retry."""
        scenario = self.env.ref("concurrency_retry_demo.scenario_cron")
        scenario._begin_attempt("Scheduled action", channel="cron")
        row = scenario._row("target")
        contention.hold_row(self.env.registry, row.id, scenario.hold_seconds)
        row.value += 1
        # 40001 here fails the job: ir.cron logs it, counts the failure and
        # reschedules the job. Nothing runs it again before its next call.
        row.flush_recordset(["value"])

    @api.model
    def _cron_contended_batched_job(self):
        """Scheduled action that commits its progress after each row (19.0 API)."""
        scenario = self.env.ref("concurrency_retry_demo.scenario_cron")
        IrCron = self.env["ir.cron"]
        rows = scenario.row_ids.filtered(
            lambda r: (r.key or "").startswith("batch") and not r.processed
        )
        IrCron._commit_progress(remaining=len(rows))
        progress_id = self.env.context.get("ir_cron_progress_id")
        for row in rows:
            attempt = scenario._begin_attempt("Batched job", channel="cron")
            executions = attempt.call.extra.setdefault("progress_ids", [])
            if progress_id not in executions:
                executions.append(progress_id)
            attempt_log.note_attempt(
                self.env.cr.dbname,
                attempt.attempt_id,
                f"{row.name}, execution {executions.index(progress_id) + 1} of the job",
            )
            if row.key == "batch_2" and not attempt.call.extra.get("contended"):
                attempt.call.extra["contended"] = True
                contention.hold_row(self.env.registry, row.id, scenario.hold_seconds)
            row.write({"value": row.value + 1, "processed": True})
            row.flush_recordset(["value", "processed"])
            IrCron._commit_progress(1)

    # ------------------------------------------------------------------
    # Non-seekable upload
    # ------------------------------------------------------------------

    def action_open_upload_page(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": "/concurrency_retry_demo/upload",
            "target": "new",
        }

    def _upload_attempt(self, mode, size):
        """Called by the upload controller on every attempt, after it read the file."""
        self.ensure_one()
        attempt = self._begin_attempt(
            UPLOAD_MODES[mode], note=f"read {size} bytes from the upload"
        )
        row = self._row("target")
        if attempt.attempt_no == 1 and mode == "non_seekable_integrity":
            # A non-retryable error: the code of the target row exists already.
            self.env["concurrency.demo.row"].create(
                {"scenario_id": self.id, "name": "Duplicate", "code": row.code}
            )
        if attempt.attempt_no == 1:
            contention.hold_row(self.env.registry, row.id, self.hold_seconds)
        row.value += 1
        row.flush_recordset(["value"])

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def action_reset(self):
        """Delete the runs of the scenario and put its rows back to zero."""
        self.ensure_one()
        self.run_ids.unlink()
        self.outbox_ids.unlink()
        self.row_ids.ledger_ids.unlink()
        self.row_ids.filtered(lambda r: not r.key).unlink()
        self.row_ids.write({"value": 0, "processed": False})
        crons = self.cron_id | self.batched_cron_id
        if crons:
            crons.write({"failure_count": 0, "first_failure_date": False, "active": True})
        return True

    def _row(self, key):
        row = self.row_ids.filtered(lambda r: r.key == key)[:1]
        if not row:
            raise UserError(
                self.env._(
                    "Scenario %(scenario)s has no row with key %(key)s.",
                    scenario=self.name,
                    key=key,
                )
            )
        return row

    def _begin_attempt(self, variant, channel="http", note=False):
        return attempt_log.begin_attempt(self, variant=variant, channel=channel, note=note)


def _simulated_request(dbname, uid, scenario_id, counter_id, variant, *, run_id, t0, request_no, barrier):
    """One RPC request: its own cursor, its own environment, the real retrying()."""
    threading.current_thread().dbname = dbname
    stale_value = None
    if variant == "stale":
        # The user opened the form earlier: the value comes from an older transaction.
        stale_value = attempt_log.independent(
            dbname, lambda env: env["concurrency.demo.row"].browse(counter_id).value
        )
    try:
        barrier.wait(REQUEST_TIMEOUT)
    except threading.BrokenBarrierError:
        _logger.warning("Simulated request %s did not start", request_no)
        return
    if variant == "stale":
        # The users save one after the other: no overlap, hence no conflict.
        time.sleep(request_no * STALE_SAVE_GAP)
    context = {
        attempt_log.CTX_RUN_ID: run_id,
        attempt_log.CTX_RUN_T0: t0,
        attempt_log.CTX_REQUEST_NO: request_no,
    }
    try:
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, uid, context)
            env.transaction.default_env = env  # as execute_cr() does
            scenario = env["concurrency.demo.scenario"].browse(scenario_id)
            retrying(
                partial(scenario._counter_increment, variant, counter_id, stale_value), env
            )
    except Exception:  # noqa: BLE001
        # The request gave up: its errors are already in the attempt log.
        _logger.info("Simulated request %s gave up", request_no, exc_info=True)
