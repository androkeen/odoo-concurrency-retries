"""Attempt log written through an independent cursor.

``retrying()`` (odoo/service/model.py) rolls the transaction back between
attempts, so anything written with ``self.env.cr`` disappears with the failed
attempt. This module writes the log with a second cursor that commits at
once: the rows survive the rollbacks, which makes the retries visible.

How one attempt is followed:

* ``begin_attempt()`` writes the attempt row, then registers two hooks on the
  cursor of the call: ``postcommit`` (the attempt committed) and
  ``postrollback`` (the attempt was rolled back). ``retrying()`` rolls back
  inside its ``except`` block, so ``sys.exc_info()`` in the hook still holds
  the error it caught.
* ``retrying()`` then logs "<error>, N tries left, try again in X sec..." on
  the ``odoo.service.model`` logger. ``RetryLogHandler`` copies that line and
  the chosen wait onto the attempt that was just rolled back.
* The attempts of one call form a run. ``retrying()`` re-runs the call on the
  same cursor (it rolls the transaction back, it does not close the cursor),
  so the cursor object identifies the call across its attempts.
"""

import logging
import sys
import threading
import time
import weakref
from functools import partial

from psycopg2 import errorcodes

from odoo import SUPERUSER_ID, api
from odoo.exceptions import ConcurrencyError
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

# The two lines retrying() logs, matched on their format string (record.msg).
RETRY_LOG_FORMAT = "%s, %s tries left, try again in %.04f sec..."
GIVE_UP_LOG_FORMAT = "%s, maximum number of tries reached!"
# retrying() logs right after its rollback; an older pending entry is stale.
PENDING_MAX_AGE = 5.0

# Context keys used to attach the calls of simulated requests to one run.
CTX_RUN_ID = "concurrency_demo_run_id"
CTX_RUN_T0 = "concurrency_demo_run_t0"
CTX_REQUEST_NO = "concurrency_demo_request_no"

_calls = weakref.WeakKeyDictionary()  # cursor -> CallState
_calls_lock = threading.Lock()
_local = threading.local()


class CallState:
    """What one call keeps between its attempts: plain Python memory."""

    def __init__(self, run_id, t0):
        self.run_id = run_id
        self.t0 = t0
        self.attempts = 0
        self.extra = {}


class Attempt:
    """Handle on the attempt in progress, returned to the scenario code."""

    def __init__(self, call, attempt_id, attempt_no, request_no, dbname, started):
        self.call = call
        self.attempt_id = attempt_id
        self.attempt_no = attempt_no
        self.request_no = request_no
        self.dbname = dbname
        self.started = started

    @property
    def run_id(self):
        return self.call.run_id

    def rearm(self, cr):
        """Follow the attempt again after a commit made in the middle of it.

        A commit runs and clears the hooks of the attempt, so its end would go
        unnoticed otherwise.
        """
        _register_hooks(cr, self.dbname, self.attempt_id, self.started)


def independent(dbname, func):
    """Run ``func(env)`` in a transaction of its own and commit it at once.

    This is the "outside world" of the demo: what it writes is not undone when
    the transaction of the request rolls back.
    """
    with Registry(dbname).cursor() as cr:
        return func(api.Environment(cr, SUPERUSER_ID, {}))


def create_run(dbname, scenario_id, *, variant, channel, request_count=1):
    return independent(
        dbname,
        lambda env: env["concurrency.demo.run"]
        .create(
            {
                "scenario_id": scenario_id,
                "variant": variant,
                "channel": channel,
                "request_count": request_count,
            }
        )
        .id,
    )


def update_run(dbname, run_id, vals):
    independent(
        dbname, lambda env: env["concurrency.demo.run"].browse(run_id).write(vals)
    )


def begin_attempt(scenario, *, variant, channel, note=False):
    """Log the start of an attempt and hook its end on the cursor of the call."""
    cr = scenario.env.cr
    dbname = cr.dbname
    context = scenario.env.context
    with _calls_lock:
        call = _calls.get(cr)
        if call is None:
            run_id = context.get(CTX_RUN_ID) or create_run(
                dbname, scenario.id, variant=variant, channel=channel
            )
            call = _calls[cr] = CallState(
                run_id, context.get(CTX_RUN_T0) or time.monotonic()
            )
        call.attempts += 1
        attempt_no = call.attempts
    request_no = context.get(CTX_REQUEST_NO, 1)
    started = time.monotonic()
    attempt_id = independent(
        dbname,
        lambda env: env["concurrency.demo.attempt"]
        .create(
            {
                "run_id": call.run_id,
                "request_no": request_no,
                "attempt_no": attempt_no,
                "offset_ms": _ms(started - call.t0),
                "thread_name": threading.current_thread().name,
                "note": note,
            }
        )
        .id,
    )
    _register_hooks(cr, dbname, attempt_id, started)
    return Attempt(call, attempt_id, attempt_no, request_no, dbname, started)


def _register_hooks(cr, dbname, attempt_id, started):
    cr.postcommit.add(partial(_attempt_committed, dbname, attempt_id, started))
    cr.postrollback.add(partial(_attempt_rolled_back, dbname, attempt_id, started))


def note_attempt(dbname, attempt_id, note):
    _write_attempt(dbname, attempt_id, {"note": note})


def describe_error(exc):
    """Error columns of an attempt row for the exception ``exc``."""
    if exc is None:
        return {}
    vals = {
        "error_type": type(exc).__name__,
        "error_message": str(exc).strip(),
    }
    pgcode = getattr(exc, "pgcode", None)
    if pgcode:
        vals["pgcode"] = pgcode
        try:
            vals["error_name"] = errorcodes.lookup(pgcode)
        except KeyError:
            vals["error_name"] = pgcode
    elif isinstance(exc, ConcurrencyError):
        vals["error_name"] = "ConcurrencyError"
    return vals


def record_external_call(
    dbname, *, run_id, attempt_no, channel, payload, idempotency_key=False
):
    """The fake external system: it cannot be rolled back, and it ignores a
    call whose idempotency key it has already applied.

    Return True when the call was a duplicate.
    """

    def receive(env):
        Call = env["concurrency.demo.external.call"]
        duplicate = bool(idempotency_key) and bool(
            Call.search_count(
                [
                    ("idempotency_key", "=", idempotency_key),
                    ("is_duplicate", "=", False),
                ],
                limit=1,
            )
        )
        Call.create(
            {
                "run_id": run_id,
                "attempt_no": attempt_no,
                "channel": channel,
                "payload": payload,
                "idempotency_key": idempotency_key,
                "is_duplicate": duplicate,
            }
        )
        return duplicate

    return independent(dbname, receive)


def _ms(seconds):
    return round(seconds * 1000)


def _write_attempt(dbname, attempt_id, vals):
    independent(
        dbname,
        lambda env: env["concurrency.demo.attempt"].browse(attempt_id).write(vals),
    )


def _attempt_committed(dbname, attempt_id, started):
    # A failing hook would break the commit sequence of the request: log only.
    try:
        _write_attempt(
            dbname,
            attempt_id,
            {"state": "committed", "duration_ms": _ms(time.monotonic() - started)},
        )
    except Exception:
        _logger.exception("Could not mark demo attempt %s as committed", attempt_id)


def _attempt_rolled_back(dbname, attempt_id, started):
    # cr.rollback() runs this hook. retrying() rolls back inside its except
    # block, so the error it caught is still the exception being handled.
    vals = {"state": "rolled_back", "duration_ms": _ms(time.monotonic() - started)}
    vals.update(describe_error(sys.exc_info()[1]))
    # A failing hook would replace the error retrying() is handling: log only.
    try:
        _write_attempt(dbname, attempt_id, vals)
    except Exception:
        _logger.exception("Could not mark demo attempt %s as rolled back", attempt_id)
    # The retrying() log line for this failure, if any, comes right after.
    _local.pending = (dbname, attempt_id, time.monotonic())


class RetryLogHandler(logging.Handler):
    """Copy retrying()'s INFO lines onto the attempt that was just rolled back.

    The handler only sees them when the ``odoo.service.model`` logger lets INFO
    through, which is Odoo's default log level.
    """

    def emit(self, record):
        if record.msg not in (RETRY_LOG_FORMAT, GIVE_UP_LOG_FORMAT):
            return
        pending = getattr(_local, "pending", None)
        _local.pending = None
        if not pending or time.monotonic() - pending[2] > PENDING_MAX_AGE:
            return
        dbname, attempt_id, _rolled_back_at = pending
        vals = {"retry_log": record.getMessage()}
        if record.msg == RETRY_LOG_FORMAT:
            vals.update(retried=True, wait_ms=_ms(record.args[2]))
        else:
            vals["gave_up"] = True
        try:
            _write_attempt(dbname, attempt_id, vals)
        except Exception:
            _logger.exception("Could not copy the retry log line to attempt %s", attempt_id)


def install_retry_log_handler():
    """Attach RetryLogHandler to the logger of retrying(), once per process."""
    logger = logging.getLogger("odoo.service.model")
    for handler in list(logger.handlers):
        if type(handler).__name__ == RetryLogHandler.__name__:
            logger.removeHandler(handler)
    logger.addHandler(RetryLogHandler())
