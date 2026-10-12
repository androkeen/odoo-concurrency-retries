"""Background transactions that compete with the request.

Each helper starts a daemon thread with its own cursor (``registry.cursor()``):
a second user working on the same rows at the same time. The helpers return
once the competing transaction holds what it needs, so the race is
deterministic.
"""

import logging
import threading
import time

from odoo import SUPERUSER_ID, api
from odoo.tools import SQL

_logger = logging.getLogger(__name__)

READY_TIMEOUT = 10  # seconds to wait for the other transaction to get going
ROW_TABLE = SQL.identifier("concurrency_demo_row")


def run_in_background(registry, name, func):
    """Run ``func(env)`` in a new thread and transaction; commit when it returns."""

    def target():
        threading.current_thread().dbname = registry.db_name
        try:
            with registry.cursor() as cr:
                func(api.Environment(cr, SUPERUSER_ID, {}))
        except Exception:
            _logger.exception("Background transaction %s failed", name)

    thread = threading.Thread(
        target=target, name=f"concurrency-demo-{name}", daemon=True
    )
    thread.start()
    return thread


def wait_for(event, what):
    if not event.wait(READY_TIMEOUT):
        raise RuntimeError(
            f"The background transaction did not {what} within {READY_TIMEOUT}s"
        )


def hold_row(registry, row_id, hold):
    """Another user updates the row and commits ``hold`` seconds later.

    Returns once the row is locked. Under REPEATABLE READ, a transaction
    whose snapshot predates that commit fails with 40001 when it writes
    the row.
    """
    locked = threading.Event()

    def other_user(env):
        env.cr.execute(
            SQL("UPDATE %s SET value = value + 1 WHERE id = %s", ROW_TABLE, row_id)
        )
        locked.set()
        time.sleep(hold)

    run_in_background(registry, "update-row", other_user)
    wait_for(locked, "lock the row")


def lock_row(registry, row_id, hold):
    """Another transaction locks the row without changing it, for ``hold`` seconds."""
    locked = threading.Event()

    def other_user(env):
        env.cr.execute(
            SQL("SELECT id FROM %s WHERE id = %s FOR UPDATE", ROW_TABLE, row_id)
        )
        locked.set()
        time.sleep(hold)

    run_in_background(registry, "lock-row", other_user)
    wait_for(locked, "lock the row")


def lock_b_then_a(registry, row_a_id, row_b_id, delay):
    """The other flow: lock B, wait until the caller holds A, then ask for A.

    Returns an event the caller sets once it holds A. ``delay`` lets the
    caller start waiting for B first, so its deadlock check runs first and
    PostgreSQL picks the caller as the victim.
    """
    b_locked = threading.Event()
    a_locked = threading.Event()

    def other_flow(env):
        env.cr.execute(
            SQL("UPDATE %s SET value = value + 1 WHERE id = %s", ROW_TABLE, row_b_id)
        )
        b_locked.set()
        if not a_locked.wait(READY_TIMEOUT):
            return
        time.sleep(delay)
        env.cr.execute(
            SQL("UPDATE %s SET value = value + 1 WHERE id = %s", ROW_TABLE, row_a_id)
        )

    run_in_background(registry, "lock-b-then-a", other_flow)
    wait_for(b_locked, "lock row B")
    return a_locked


def keep_row_hot(registry, row_id, seconds):
    """Update and commit the row again and again, for at most ``seconds``.

    Returns an event that stops the loop early.
    """
    stop = threading.Event()

    def target():
        threading.current_thread().dbname = registry.db_name
        deadline = time.monotonic() + seconds
        try:
            while not stop.is_set() and time.monotonic() < deadline:
                with registry.cursor() as cr:
                    cr.execute(
                        SQL(
                            "UPDATE %s SET value = value + 1 WHERE id = %s",
                            ROW_TABLE,
                            row_id,
                        )
                    )
                stop.wait(0.05)
        except Exception:
            _logger.exception("Hot-row loop failed")

    threading.Thread(target=target, name="concurrency-demo-hot-row", daemon=True).start()
    return stop


def insert_code(registry, scenario_id, code, hold):
    """Another user creates a row with ``code`` and commits ``hold`` seconds later.

    Returns once the row is inserted (not yet committed).
    """
    inserted = threading.Event()

    def other_user(env):
        env["concurrency.demo.row"].create(
            {
                "scenario_id": scenario_id,
                "name": f"{code} (other user)",
                "code": code,
            }
        )
        inserted.set()
        time.sleep(hold)

    run_in_background(registry, "insert-code", other_user)
    wait_for(inserted, "insert the row")
