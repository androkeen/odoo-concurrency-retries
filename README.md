# Odoo 19 concurrency retries

What Odoo re-runs when two transactions collide, and what that breaks.

On a busy instance, two transactions writing the same rows raise `could not serialize access due to concurrent update` or `deadlock detected`. Odoo retries the call up to five times, and between attempts it rolls back the database but not the outside world. This repository makes that visible:

- **`docs/`**: one note per topic, each with a TL;DR and a deep dive, checked against the Odoo 19.0 source (`odoo/odoo@e6ebad8`), the demo module, tests and experiments.
- **`concurrency_retry_demo/`**: an Odoo 19 module with one form per concept and an attempt log that survives the rollbacks.
- **`slides/`**: a reveal.js deck generated from the TL;DR sections, published to GitHub Pages.

## What we found

The brief's starting points were partly outdated for 19.0. In short:

| Belief | 19.0 reality |
|---|---|
| Exactly three SQLSTATEs are retried | three SQLSTATEs **plus** `odoo.exceptions.ConcurrencyError` (new) |
| IntegrityError goes through `_as_validation_error()` | gone; `_sql_error_to_message()` builds the message |
| 5 is the multiplier | 5 per retry loop; XML-RPC on **17.0** runs a call up to **25** times (measured) |
| Crons are not retried | true, and since 19.0 "Run Manually" is not retried either (17.0 retried it 5 times) |
| `UPDATE ... SET x = x + 1` avoids conflicts | under REPEATABLE READ it still gets 40001; lost updates come from values read in another transaction |
| queue_job retries a few times | 19.0.2.1.6 postpones on 40001 every 5 s, **forever** (`max_retries` not checked) |

Details in [`docs/00-version-diff.md`](docs/00-version-diff.md); the write-path checklist and an audit of standard modules in [`docs/10-write-path-audit.md`](docs/10-write-path-audit.md).

## Topic notes

| Note | Topic |
|---|---|
| [00](docs/00-version-diff.md) | Version baseline and differences from 17.0 |
| [01](docs/01-which-errors-are-retried.md) | Which errors are retried (40001, 40P01, 55P03, `ConcurrencyError`) |
| [02](docs/02-attempts-vs-retries.md) | Attempts vs retries, `retrying()` line by line, entry points |
| [03](docs/03-integrity-error-never-retried.md) | IntegrityError is never retried |
| [04](docs/04-between-attempts.md) | What happens between attempts, and how to make side effects safe |
| [05](docs/05-uploaded-files.md) | Uploaded files and the rewind check |
| [06](docs/06-backoff.md) | Backoff and time limits |
| [07](docs/07-logging-and-diagnosis.md) | Logging and diagnosis |
| [08](docs/08-where-retry-does-not-apply.md) | Where the retry does not apply: cron, queue_job, shell, own cursors, `cr.commit()` |
| [09](docs/09-reducing-contention.md) | Reducing contention in custom code |
| [10](docs/10-write-path-audit.md) | The write-path audit checklist, applied to `sale_gelato`, `iap_extract`, `account_invoice_extract`, `mail` |

Evidence (raw outputs) is in [`docs/evidence/`](docs/evidence).

## Run the demo

Requirements: Odoo 19.0 (community is enough; tested on 19.0 EE), PostgreSQL, the repository root on the addons path.

```bash
# 1. Install on a fresh database (no demo data needed)
odoo-bin -c odoo.conf -d retry_demo \
    --addons-path=<odoo>/addons,<enterprise>,<this repo> \
    -i concurrency_retry_demo --stop-after-init

# 2. Run the server in threaded mode, one cron thread, this database only
odoo-bin -c odoo.conf -d retry_demo --db-filter='^retry_demo$' \
    --addons-path=<odoo>/addons,<enterprise>,<this repo> \
    --workers=0 --max-cron-threads=1
```

Then open **Retry Demo** (`/odoo/retry-demo`) as an administrator, open a scenario, and click its buttons. Each form explains what to look at; the *Last run* tab shows the attempt log: one row per attempt, with its error, SQLSTATE, duration and the backoff `retrying()` chose.

Why threaded mode: the demo creates contention with background threads, each on its own cursor, inside the server process. `--workers=0` keeps everything in one process and makes the timing predictable. Prefork (`--workers=N`) also works, with care:

- each scenario's threads live in the worker that serves the click; the longest scenario (*Exhaust retries*, up to ~30 s) stays well under `--limit-time-real` (120 s by default);
- *Counter* opens 8 extra database connections at once;
- *Trigger the cron worker* needs a cron thread or cron worker for this database (`--max-cron-threads` > 0, and `-d`/`--db-filter` so the cron processes it).

Keep the log level at INFO (the default): the attempt log copies the backoff from `retrying()`'s INFO lines. With `--log-level=warn`, add `--log-handler=odoo.service.model:INFO`.

### Click every button from a script

```bash
python scripts/run_scenarios.py --url http://localhost:8069 --db retry_demo --login admin --password admin
python scripts/run_scenarios.py --db retry_demo counter cron     # some scenarios only
```

It uses the same JSON-RPC endpoints as the web client and prints each run's attempts. Point it at a demo database only: it resets every scenario it runs. A full run is in [`docs/evidence/run_scenarios.txt`](docs/evidence/run_scenarios.txt).

### Tests

```bash
odoo-bin -c odoo.conf -d retry_demo --addons-path=... -u concurrency_retry_demo \
    --test-enable --test-tags /concurrency_retry_demo --stop-after-init
```

`tests/test_retrying_facts.py` is the executable version of the notes: it runs the real `retrying()` and checks which errors are retried, the five attempts and four sleeps, the exact log lines, the IntegrityError conversion, the flush inside the loop and the commit after it. Real concurrency cannot run inside a test transaction, so the scenarios themselves are verified with `scripts/run_scenarios.py`.

## The scenarios

| Scenario | Buttons | Shows |
|---|---|---|
| Serialization failure | Run | 40001 on the first attempts, then a commit; backoff per attempt |
| Deadlock | Run | A then B against B then A: 40P01, retried |
| Lock not available | FOR UPDATE NOWAIT, ORM lock_for_update() | 55P03 retried until the lock is gone; 19.0's `LockError` never retried |
| Exhaust retries | Run | a hot row: five attempts, then the error reaches the user |
| Unique violation | Check, then create(); Get or create, ConcurrencyError | 23505 becomes a `ValidationError` at once; `ConcurrencyError` asks for the retry |
| Unsafe side effect | Confirm order; Confirm order, cr.commit() midway | one external call per attempt; a mid-call commit re-applied per attempt |
| Safe side effect | Confirm order (postcommit), Confirm order (outbox), Deliver outbox | one call per click; outbox with idempotency key |
| Counter drift vs atomic update | four variants | 8 concurrent increments: where updates are lost, where they only retry |
| Same failure from cron | Run Manually, Trigger the cron worker, Batched job, Pretend a week of failures | no retry in cron; immediate re-run after committed progress; auto-deactivation |
| Non-seekable upload | Open the upload page | upload rewind, and its `RuntimeError` |

## Slides

```bash
python slides/build.py            # writes slides/dist/index.html
```

Open `slides/dist/index.html` in a browser (it loads reveal.js and mermaid from a CDN). The deck takes the `## TL;DR` section of every note and nothing else, so the notes are the only source to edit:

- a TL;DR is 2-5 bullets;
- `<!-- slide -->` inside a TL;DR starts an extra slide (a code sample, a table, a diagram);
- a fenced `mermaid` block becomes a diagram;
- the order of the deck is the `ORDER` list in `slides/build.py`.

Publishing: in the GitHub repository, *Settings > Pages > Source: GitHub Actions*. The workflow in `.github/workflows/pages.yml` rebuilds and deploys the deck on every push to `main` that touches `docs/` or `slides/`.

## Experiments

[`experiments/`](experiments) holds the two throwaway probes behind the measured cross-version claims:

- `odoo17_entry_points/bp425_probe`: an Odoo 17.0 module with a method that always raises 40001 and counts its calls (XML-RPC, `/jsonrpc`, web `call_kw`, "Run Manually"). Install it on a scratch 17.0 database, serve that database alone (`--db-filter`), call the method through each entry point and read `get_count`.
- `queue_job_19/bp425_probe`: an Odoo 19 module that enqueues one job raising 40001 and one raising `ConcurrencyError`. Run with `--load=base,rpc,web,queue_job` so the job runner starts, call `bp425.probe.enqueue()` and watch the jobs.

Results: [`docs/evidence/probe-entry-points.txt`](docs/evidence/probe-entry-points.txt), [`docs/evidence/probe-queue-job.txt`](docs/evidence/probe-queue-job.txt).

## Layout

```
concurrency_retry_demo/   Odoo 19 module (models, tools/attempt_log.py, tools/contention.py, tests)
docs/00-...10-*.md        topic notes (TL;DR + deep dive), docs/evidence/ raw outputs
slides/                   build.py + template.html -> slides/dist/index.html (GitHub Pages)
scripts/                  run_scenarios.py (clicks every button), audit_write_paths.sh (audit helper)
experiments/              probes for 17.0 and queue_job
```
