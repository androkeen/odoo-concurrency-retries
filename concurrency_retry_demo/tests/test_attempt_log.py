import logging
import time

from psycopg2 import errors

from odoo.service.model import retrying
from odoo.tests import TransactionCase, tagged

from ..tools import attempt_log
from .common import patch_retry_clock, pg_error


@tagged("post_install", "-at_install")
class TestAttemptLog(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.scenario = cls.env.ref("concurrency_retry_demo.scenario_serialization")

    def test_log_formats_match_retrying(self):
        # The handler matches retrying()'s lines on their format string.
        patch_retry_clock(self)
        calls = []

        def fail_once():
            calls.append(1)
            if len(calls) == 1:
                raise pg_error(errors.SerializationFailure, "40001")

        with self.assertLogs("odoo.service.model", "INFO") as logs, self.registry.cursor() as cr:
            retrying(fail_once, self.env(cr=cr))
        [record] = logs.records
        self.assertEqual(record.msg, attempt_log.RETRY_LOG_FORMAT)
        self.assertEqual(record.args[:2], ("SERIALIZATION_FAILURE", 4))

    def test_handler_copies_the_backoff_onto_the_attempt(self):
        self.registry_enter_test_mode()  # the "independent" cursor shares the test transaction
        run = self.env["concurrency.demo.run"].create({"scenario_id": self.scenario.id})
        attempt = self.env["concurrency.demo.attempt"].create({"run_id": run.id, "attempt_no": 1})
        self.env.flush_all()
        attempt_log._local.pending = (self.env.cr.dbname, attempt.id, time.monotonic())
        record = logging.LogRecord(
            "odoo.service.model",
            logging.INFO,
            __file__,
            0,
            attempt_log.RETRY_LOG_FORMAT,
            ("SERIALIZATION_FAILURE", 4, 1.2346),
            None,
        )
        attempt_log.RetryLogHandler().handle(record)
        attempt.invalidate_recordset()
        self.assertTrue(attempt.retried)
        self.assertEqual(attempt.wait_ms, 1235)
        self.assertEqual(
            attempt.retry_log, "SERIALIZATION_FAILURE, 4 tries left, try again in 1.2346 sec..."
        )

    def test_rollback_hook_records_the_error_in_flight(self):
        self.registry_enter_test_mode()
        with self.registry.cursor() as cr:
            scenario = self.scenario.with_env(self.env(cr=cr))
            attempt = scenario._begin_attempt("test")
            try:
                raise pg_error(errors.SerializationFailure, "40001")
            except errors.SerializationFailure:
                cr.rollback()  # what retrying() does inside its except block
        row = self.env["concurrency.demo.attempt"].browse(attempt.attempt_id)
        self.assertEqual(row.state, "rolled_back")
        self.assertEqual(row.pgcode, "40001")
        self.assertEqual(row.error_name, "SERIALIZATION_FAILURE")
        self.assertEqual(row.error_type, "SerializationFailure")
