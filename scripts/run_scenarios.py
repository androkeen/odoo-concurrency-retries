#!/usr/bin/env python3
"""Click the buttons of concurrency_retry_demo and print what each run did.

    python scripts/run_scenarios.py --url http://localhost:19169 \
        --db bp425_retry_demo --login admin --password admin [scenario ...]

It talks to a running server the way the web client does (JSON-RPC on
/web/dataset/call_button), so every click goes through the real HTTP retry
loop. Point it at a demo database only: it resets each scenario it runs.

Scenarios: serialization deadlock lock exhaust unique unsafe safe counter cron
upload (default: all of them). Needs the ``requests`` package, which every
Odoo virtualenv has.
"""

import argparse
import re
import sys
import time

import requests

MODEL = "concurrency.demo.scenario"


class RpcError(Exception):
    def __init__(self, error):
        data = error.get("data") or {}
        self.name = data.get("name") or error.get("message")
        self.message = data.get("message") or error.get("message")
        super().__init__(f"{self.name}: {self.message}")


class Client:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.request_id = 0

    def rpc(self, path, params):
        self.request_id += 1
        response = self.session.post(
            self.url + path,
            json={"jsonrpc": "2.0", "method": "call", "params": params, "id": self.request_id},
            timeout=300,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RpcError(payload["error"])
        return payload["result"]

    def login(self, db, login, password):
        result = self.rpc(
            "/web/session/authenticate", {"db": db, "login": login, "password": password}
        )
        if not result.get("uid"):
            sys.exit("Login failed")

    def call(self, model, method, *args, **kwargs):
        return self.rpc(
            f"/web/dataset/call_kw/{model}.{method}",
            {"model": model, "method": method, "args": list(args), "kwargs": kwargs},
        )

    def button(self, record_id, method):
        """Click a button of a scenario form. Return (result, error)."""
        started = time.monotonic()
        try:
            result = self.rpc(
                f"/web/dataset/call_button/{MODEL}.{method}",
                {"model": MODEL, "method": method, "args": [[record_id]], "kwargs": {}},
            )
            error = None
        except RpcError as exc:
            result, error = None, exc
        return result, error, time.monotonic() - started


class Runner:
    def __init__(self, client):
        self.client = client

    def scenario_id(self, xmlid):
        return self.client.call(
            "ir.model.data", "check_object_reference", "concurrency_retry_demo", xmlid
        )[1]

    def reset(self, scenario_id):
        self.client.button(scenario_id, "action_reset")

    def click(self, scenario_id, method, title):
        result, error, elapsed = self.client.button(scenario_id, method)
        print(f"\n### {title} [{method}] ({elapsed:.1f} s)")
        if error:
            print(f"    user sees: {error.name}: {first_line(error.message)}")
        elif isinstance(result, dict) and result.get("tag") == "display_exception":
            # method_direct_trigger() wraps the job's error in a RuntimeError;
            # the job's own exception ends the traceback of the cause.
            debug = result["params"]["data"].get("debug") or ""
            cause = debug.split("The above exception was the direct cause")[0]
            lines = [line for line in cause.splitlines() if line.strip()]
            print(f"    user sees: error dialog, {lines[-1] if lines else 'no details'}")
        self.print_last_run(scenario_id)
        return result, error

    def print_last_run(self, scenario_id):
        [run] = self.client.call(
            "concurrency.demo.run",
            "search_read",
            [("scenario_id", "=", scenario_id)],
            fields=[
                "variant", "outcome", "request_count", "attempt_count", "retry_count",
                "external_call_count", "value_before", "value_after", "note",
            ],
            limit=1,
        ) or [None]
        if not run:
            print("    no run recorded")
            return
        print(
            f"    run #{run['id']} {run['variant']}: {run['outcome']}, "
            f"{run['attempt_count']} attempt(s), {run['retry_count']} retry(ies), "
            f"{run['external_call_count']} external call(s)"
            + (f", {run['note']}" if run["note"] else "")
        )
        attempts = self.client.call(
            "concurrency.demo.attempt",
            "search_read",
            [("run_id", "=", run["id"])],
            fields=[
                "request_no", "attempt_no", "offset_ms", "duration_ms", "state",
                "error_name", "error_type", "pgcode", "wait_ms", "retried", "gave_up", "note",
            ],
            order="request_no, attempt_no",
        )
        if run["request_count"] > 1:
            self.print_request_summary(attempts)
            return
        print("    att  start ms  dur ms  state        error                    sqlstate  backoff ms  note")
        for a in attempts:
            backoff = a["wait_ms"] if a["retried"] else ("gave up" if a["gave_up"] else "")
            print(
                f"    {a['attempt_no']:>3}  {a['offset_ms']:>8}  {a['duration_ms']:>6}  "
                f"{a['state']:<11}  {a['error_name'] or a['error_type'] or '':<23}  {a['pgcode'] or '':<8}  "
                f"{backoff!s:>10}  {a['note'] or ''}"
            )

    @staticmethod
    def print_request_summary(attempts):
        by_request = {}
        for a in attempts:
            by_request.setdefault(a["request_no"], []).append(a)
        for request_no, rows in sorted(by_request.items()):
            states = " ".join(
                "ok" if a["state"] == "committed" else (a["error_name"] or a["error_type"] or a["state"])
                for a in rows
            )
            print(f"    request {request_no}: {len(rows)} attempt(s): {states}")

    def external_calls(self, scenario_id):
        calls = self.client.call(
            "concurrency.demo.external.call",
            "search_read",
            [("scenario_id", "=", scenario_id)],
            fields=["run_id", "attempt_no", "channel", "idempotency_key", "is_duplicate"],
            order="id",
        )
        print(f"    outside world received {len(calls)} call(s):")
        for call in calls:
            print(
                f"      run {call['run_id'][0]} attempt {call['attempt_no']} via {call['channel']}"
                + (f", key {call['idempotency_key']}" if call["idempotency_key"] else "")
                + (", ignored as duplicate" if call["is_duplicate"] else "")
            )

    # --- scenarios -----------------------------------------------------

    def serialization(self):
        sid = self.scenario_id("scenario_serialization")
        self.reset(sid)
        self.click(sid, "action_serialization_failure", "Serialization failure")

    def deadlock(self):
        sid = self.scenario_id("scenario_deadlock")
        self.reset(sid)
        self.click(sid, "action_deadlock", "Deadlock")

    def lock(self):
        sid = self.scenario_id("scenario_lock_not_available")
        self.reset(sid)
        self.click(sid, "action_lock_not_available", "Lock not available")
        self.click(sid, "action_lock_for_update", "Lock not available, ORM lock_for_update()")

    def exhaust(self):
        sid = self.scenario_id("scenario_exhaust")
        self.reset(sid)
        self.click(sid, "action_exhaust_retries", "Exhaust retries")

    def unique(self):
        sid = self.scenario_id("scenario_unique_violation")
        self.reset(sid)
        self.click(sid, "action_unique_violation", "Unique violation")
        self.click(sid, "action_unique_get_or_create", "Unique violation, ConcurrencyError")

    def unsafe(self):
        sid = self.scenario_id("scenario_unsafe_side_effect")
        self.reset(sid)
        self.click(sid, "action_unsafe_side_effect", "Unsafe side effect")
        self.external_calls(sid)
        self.reset(sid)
        self.click(sid, "action_commit_midway", "Unsafe side effect, cr.commit() midway")
        self.print_rows(sid)

    def print_rows(self, scenario_id):
        rows = self.client.call(
            "concurrency.demo.row",
            "search_read",
            [("scenario_id", "=", scenario_id), ("key", "!=", False)],
            fields=["name", "value"],
        )
        print("    rows: " + ", ".join(f"{row['name']} = {row['value']}" for row in rows))

    def safe(self):
        sid = self.scenario_id("scenario_safe_side_effect")
        self.reset(sid)
        self.click(sid, "action_safe_side_effect", "Safe side effect, postcommit")
        self.click(sid, "action_outbox_side_effect", "Safe side effect, outbox row")
        self.client.call(MODEL, "write", [sid], {"simulate_crash": True})
        self.click(sid, "action_process_outbox", "Outbox delivery that crashes after sending")
        self.client.call(MODEL, "write", [sid], {"simulate_crash": False})
        self.click(sid, "action_process_outbox", "Outbox delivery, second try")
        self.external_calls(sid)

    def counter(self):
        sid = self.scenario_id("scenario_counter")
        self.reset(sid)
        for method, title in [
            ("action_counter_orm", "Counter, read-modify-write"),
            ("action_counter_atomic", "Counter, SET value = value + 1"),
            ("action_counter_stale", "Counter, stale form value"),
            ("action_counter_ledger", "Counter, insert-only ledger"),
        ]:
            self.click(sid, method, title)

    def cron(self):
        sid = self.scenario_id("scenario_cron")
        self.reset(sid)
        self.click(sid, "action_cron_run_manually", "Cron, Run Manually")
        self.print_cron(sid)
        self.click(sid, "action_cron_batched", "Cron, batched job with _commit_progress()")
        self.print_cron(sid)
        self.client.button(sid, "action_cron_fake_failure_history")
        self.click(sid, "action_cron_run_manually", "Cron, failure after a week of failures")
        self.print_cron(sid)
        self.reset(sid)
        self.client.button(sid, "action_cron_trigger")
        print("\n### Cron, triggered for the cron worker [action_cron_trigger]")
        for _second in range(60):
            if self.client.call("concurrency.demo.run", "search_count", [("scenario_id", "=", sid)]):
                time.sleep(3)  # let the job finish
                break
            time.sleep(1)
        self.print_last_run(sid)
        self.print_cron(sid)

    def print_cron(self, scenario_id):
        [values] = self.client.call(
            MODEL,
            "read",
            [scenario_id],
            ["cron_active", "cron_failure_count", "cron_first_failure_date", "batched_cron_failure_count"],
        )
        print(
            f"    job: active={values['cron_active']} failure_count={values['cron_failure_count']} "
            f"first_failure={values['cron_first_failure_date']} "
            f"batched failure_count={values['batched_cron_failure_count']}"
        )

    def upload(self):
        sid = self.scenario_id("scenario_upload")
        self.reset(sid)
        page = self.client.session.get(self.client.url + "/concurrency_retry_demo/upload", timeout=60)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        for mode in ("seekable", "non_seekable", "non_seekable_integrity"):
            response = self.client.session.post(
                self.client.url + "/concurrency_retry_demo/upload",
                data={"csrf_token": token, "mode": mode},
                files={"upload": ("hello.txt", b"Hello world! " * 100, "text/plain")},
                allow_redirects=False,
                timeout=120,
            )
            print(f"\n### Upload, {mode}")
            print(f"    HTTP {response.status_code}" + (" (redirect: success)" if response.status_code == 303 else ""))
            self.print_last_run(sid)


def first_line(text):
    return (text or "").strip().splitlines()[0] if text else ""


SCENARIOS = ["serialization", "deadlock", "lock", "exhaust", "unique", "unsafe", "safe", "counter", "cron", "upload"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:19169")
    parser.add_argument("--db", required=True)
    parser.add_argument("--login", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("scenarios", nargs="*", metavar="scenario", help=", ".join(SCENARIOS))
    args = parser.parse_args()
    unknown = set(args.scenarios) - set(SCENARIOS)
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(sorted(unknown))}")
    client = Client(args.url)
    client.login(args.db, args.login, args.password)
    runner = Runner(client)
    for name in args.scenarios or SCENARIOS:
        getattr(runner, name)()


if __name__ == "__main__":
    main()
