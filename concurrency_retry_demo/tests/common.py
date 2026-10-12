from unittest.mock import patch

from odoo.service import model as service_model


def pg_error(error_class, pgcode):
    """A psycopg2 error as the driver raises it, SQLSTATE included."""
    error = error_class()
    error.__setstate__({"pgcode": pgcode})
    return error


class FakeClock:
    """Stands in for the ``random`` and ``time`` modules of odoo.service.model."""

    def __init__(self):
        self.bounds = []
        self.sleeps = []

    def uniform(self, low, high):
        self.bounds.append((low, high))
        return 0.0

    def sleep(self, seconds):
        self.sleeps.append(seconds)


def patch_retry_clock(test_case):
    """Make retrying() record its backoff instead of sleeping."""
    clock = FakeClock()
    test_case.startPatcher(patch.object(service_model, "random", clock))
    test_case.startPatcher(patch.object(service_model, "time", clock))
    return clock
