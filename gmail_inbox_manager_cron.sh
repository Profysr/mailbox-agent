#!/bin/sh
# gmail-inbox-manager — cron launcher (watchdog pattern).
#
# stdout IS the notification message: empty stdout = silent run (nothing
# changed), non-empty stdout = digest, exit 1 = failure alert.
#
# Everything is driven by the config file. Point CONFIG at it, or leave
# CONFIG unset and put config.json next to this script.
#
# Usage:
#   CONFIG=/path/to/config.json ./gmail_inbox_manager_cron.sh
#   CONFIG=/path/to/config.json ./gmail_inbox_manager_cron.sh --dry-run
#
# Set PYTHON to the interpreter that has google-api-python-client installed.
# Default: python3 on PATH.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG=${CONFIG:-"$HERE/config.json"}
export CONFIG

PYTHON=${PYTHON:-python3}
SCRIPT=${SCRIPT:-"$HERE/gmail_inbox_manager.py"}

# The script reads CONFIG itself; --dry-run/extra flags are passed through.
# Single-instance lock: if a previous run is still going (long backfill),
# the new run exits immediately instead of racing it on the state files.
# flock exits 1 when the lock is taken — that is the cron failure alert
# for an overrun run, by design.
exec flock -n "$HERE/.run.lock" "$PYTHON" "$SCRIPT" --config "$CONFIG" "$@"
