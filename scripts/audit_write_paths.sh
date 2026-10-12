#!/usr/bin/env bash
# List the lines of an Odoo module that the write-path audit (docs/10-write-path-audit.md)
# asks about: entry points, effects outside the transaction, contention, error handling.
# It only finds candidates; each hit still needs reading.
#
#   scripts/audit_write_paths.sh path/to/module [more/modules ...]

set -u

if [ $# -eq 0 ]; then
    echo "usage: $0 MODULE_PATH [MODULE_PATH ...]" >&2
    exit 2
fi

section() {
    local title=$1 pattern=$2 include=$3
    shift 3
    echo
    echo "## $title"
    grep -rnE --include="$include" "$pattern" "$@" 2>/dev/null \
        | grep -v "/tests/" \
        | sed 's/^/  /' \
        || echo "  (none)"
}

for module in "$@"; do
    echo "# $(basename "$module")"

    section "Entry points: HTTP routes (retried up to 5 times)" \
        '@(http\.)?route\(' '*.py' "$module"
    section "Entry points: buttons calling Python (retried up to 5 times)" \
        'type="object"' '*.xml' "$module"
    section "Entry points: public actions and CRUD methods, often overrides of core buttons" \
        'def (action_[a-z_]+|button_[a-z_]+|create|write|unlink)\(' '*.py' "$module"
    section "Entry points: scheduled actions (never retried)" \
        'model="ir\.cron"|<field name=.code.>' '*.xml' "$module"
    section "Entry points: queue jobs (40001 postponed forever with queue_job 19.0.2.1.6)" \
        'with_delay\(|delayable\(' '*.py' "$module"

    section "Leaves the transaction: HTTP and API calls" \
        'requests\.(get|post|put|patch|delete|request)|urllib|http\.client|httpx|iap_jsonrpc|_contact_iap|make_request\(' '*.py' "$module"
    section "Leaves the transaction: email sent inline" \
        'force_send=True|\.send\(\)|smtplib|send_email\(' '*.py' "$module"
    section "Leaves the transaction: commits, other cursors" \
        'cr\.commit\(\)|registry\.cursor\(|Registry\([^)]*\)\.cursor\(|_commit_progress\(' '*.py' "$module"
    section "Leaves the transaction: files, sequences" \
        "open\([^)]*['\"][wa]|os\.(makedirs|remove|rename)|shutil\.|next_by_(code|id)\(" '*.py' "$module"
    section "Already deferred or compensated" \
        'postcommit|postrollback|send_after_commit|after_commit' '*.py' "$module"

    section "Contention: explicit locks" \
        'FOR (NO KEY )?UPDATE|NOWAIT|SKIP LOCKED|lock_for_update|try_lock_for_update|lock_timeout' '*.py' "$module"
    section "Error handling: broad except (may swallow 40001)" \
        'except( Exception)?( as [a-z_]+)?:' '*.py' "$module"
    section "Error handling: explicit concurrency handling" \
        'OperationalError|SerializationFailure|ConcurrencyError|UniqueViolation|IntegrityError' '*.py' "$module"
    echo
done
