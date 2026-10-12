"""What retrying() does in Odoo 19.0, checked against the real function.

retrying() commits and rolls back, so each call runs on a separate real
cursor. The functions under test only touch Python state, or fail before
writing, so nothing is written to the database.
"""

from functools import partial

from psycopg2 import errorcodes, errors

from odoo.exceptions import ConcurrencyError, UserError, ValidationError
from odoo.service.model import (
    MAX_TRIES_ON_CONCURRENCY_FAILURE,
    PG_CONCURRENCY_ERRORS_TO_RETRY,
    retrying,
)
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from .common import patch_retry_clock, pg_error

RETRYABLE = [
    (errors.LockNotAvailable, "55P03"),
    (errors.SerializationFailure, "40001"),
    (errors.DeadlockDetected, "40P01"),
]


def failing_then_ok(error, failures=1):
    """A function that raises ``error`` on its first calls, then returns 'ok'."""
    calls = []

    def func():
        calls.append(len(calls) + 1)
        if len(calls) <= failures:
            raise error
        return "ok"

    return func, calls


@tagged("post_install", "-at_install")
class TestRetryingFacts(TransactionCase):
    def setUp(self):
        super().setUp()
        self.clock = patch_retry_clock(self)

    def run_retrying(self, func):
        with self.registry.cursor() as cr:
            return retrying(func, self.env(cr=cr))

    def run_retrying_with_cr(self, func):
        """Same, for functions that need the cursor: ``func(cr)``."""
        with self.registry.cursor() as cr:
            return retrying(partial(func, cr), self.env(cr=cr))

    def test_exactly_three_sqlstates(self):
        self.assertEqual(sorted(PG_CONCURRENCY_ERRORS_TO_RETRY), ["40001", "40P01", "55P03"])

    def test_the_three_sqlstates_are_retried(self):
        for error_class, pgcode in RETRYABLE:
            with self.subTest(pgcode=pgcode):
                func, calls = failing_then_ok(pg_error(error_class, pgcode))
                with self.assertLogs("odoo.service.model", "INFO") as logs:
                    self.assertEqual(self.run_retrying(func), "ok")
                self.assertEqual(calls, [1, 2])
                self.assertIn(f"{errorcodes.lookup(pgcode)}, 4 tries left", logs.output[0])

    def test_concurrency_error_is_retried(self):
        # New in 19.0: an exception raised by Python code, no SQLSTATE.
        func, calls = failing_then_ok(ConcurrencyError("someone was faster"))
        with self.assertLogs("odoo.service.model", "INFO") as logs:
            self.run_retrying(func)
        self.assertEqual(calls, [1, 2])
        self.assertIn("ConcurrencyError('someone was faster'), 4 tries left", logs.output[0])

    def test_other_operational_error_is_raised_at_once(self):
        func, calls = failing_then_ok(pg_error(errors.QueryCanceled, "57014"))
        with self.assertRaises(errors.QueryCanceled):
            self.run_retrying(func)
        self.assertEqual(calls, [1])

    def test_user_error_is_raised_at_once(self):
        func, calls = failing_then_ok(UserError("no"))
        with self.assertRaises(UserError):
            self.run_retrying(func)
        self.assertEqual(calls, [1])

    def test_five_attempts_four_sleeps_then_the_error(self):
        func, calls = failing_then_ok(pg_error(errors.SerializationFailure, "40001"), failures=99)
        with (
            self.assertLogs("odoo.service.model", "INFO") as logs,
            self.assertRaises(errors.SerializationFailure),
        ):
            self.run_retrying(func)
        self.assertEqual(MAX_TRIES_ON_CONCURRENCY_FAILURE, 5)
        self.assertEqual(calls, [1, 2, 3, 4, 5])
        # Full jitter: uniform(0, 2 ** tryno), no sleep after the fifth failure.
        self.assertEqual(self.clock.bounds, [(0.0, 2), (0.0, 4), (0.0, 8), (0.0, 16)])
        self.assertEqual(len(self.clock.sleeps), 4)
        self.assertEqual({record.levelname for record in logs.records}, {"INFO"})
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [
                "SERIALIZATION_FAILURE, 4 tries left, try again in 0.0000 sec...",
                "SERIALIZATION_FAILURE, 3 tries left, try again in 0.0000 sec...",
                "SERIALIZATION_FAILURE, 2 tries left, try again in 0.0000 sec...",
                "SERIALIZATION_FAILURE, 1 tries left, try again in 0.0000 sec...",
                "SERIALIZATION_FAILURE, maximum number of tries reached!",
            ],
        )

    def test_integrity_error_becomes_validation_error_without_retry(self):
        row = self.env.ref("concurrency_retry_demo.row_upload_target")
        calls = []

        def create_duplicate(cr):
            calls.append(1)
            self.env(cr=cr)["concurrency.demo.row"].create(
                {"scenario_id": row.scenario_id.id, "name": "Duplicate", "code": row.code}
            )

        with self.assertRaises(ValidationError) as catcher, mute_logger("odoo.sql_db"):
            self.run_retrying_with_cr(create_duplicate)
        self.assertEqual(calls, [1])
        self.assertEqual(
            str(catcher.exception),
            "The operation cannot be completed: Another row already uses this code.",
        )
        self.assertIsInstance(catcher.exception.__cause__, errors.UniqueViolation)

    def test_pending_orm_writes_are_dropped_between_attempts(self):
        scenario_id = self.env.ref("concurrency_retry_demo.scenario_serialization").id
        seen = []

        def func(cr):
            scenario = self.env(cr=cr)["concurrency.demo.scenario"].browse(scenario_id)
            seen.append(scenario.summary)
            if len(seen) == 1:
                scenario.summary = "changed by the failed attempt"
                raise pg_error(errors.SerializationFailure, "40001")

        with mute_logger("odoo.service.model"):
            self.run_retrying_with_cr(func)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1], seen[0])

    def test_flush_and_precommit_hooks_run_inside_the_loop(self):
        # A conflict that surfaces while flushing (or in a precommit hook) is retried.
        calls = []

        def func(cr):
            calls.append(1)
            if len(calls) == 1:
                cr.precommit.add(self.raise_serialization_failure)

        with mute_logger("odoo.service.model"):
            self.run_retrying_with_cr(func)
        self.assertEqual(calls, [1, 1])

    def test_postcommit_hooks_run_once_after_the_successful_attempt(self):
        log = []

        def func(cr):
            log.append("attempt")
            cr.postcommit.add(partial(log.append, "postcommit"))
            if log.count("attempt") < 3:
                raise pg_error(errors.SerializationFailure, "40001")

        with mute_logger("odoo.service.model"):
            self.run_retrying_with_cr(func)
        self.assertEqual(log, ["attempt", "attempt", "attempt", "postcommit"])

    def test_commit_is_outside_the_loop(self):
        # The data is committed when postcommit hooks run: their errors are never retried.
        calls = []

        def func(cr):
            calls.append(1)
            cr.postcommit.add(self.raise_serialization_failure)

        with self.assertRaises(errors.SerializationFailure):
            self.run_retrying_with_cr(func)
        self.assertEqual(calls, [1])

    def test_a_failing_postcommit_hook_skips_the_next_ones(self):
        log = []

        def func(cr):
            cr.postcommit.add(self.raise_serialization_failure)
            cr.postcommit.add(partial(log.append, "second hook"))

        with self.assertRaises(errors.SerializationFailure):
            self.run_retrying_with_cr(func)
        self.assertEqual(log, [])

    @staticmethod
    def raise_serialization_failure():
        raise pg_error(errors.SerializationFailure, "40001")
