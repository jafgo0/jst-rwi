#!/usr/bin/env bash
# One capture cycle, for a scheduler (launchd, cron, systemd timer).
#
# Deliberately --once rather than the long-running loop: a scheduler that
# restarts the process every hour is far harder to kill silently than a daemon
# that dies at 03:00 and is noticed in March. A missed cycle is a gap; a dead
# daemon is months of missing evidence.
set -uo pipefail

PROJECT_DIR="${RWI_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$PROJECT_DIR" || { echo "cannot enter $PROJECT_DIR"; exit 1; }

PY="$PROJECT_DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "$(date -u +%FT%TZ) FATAL: no interpreter at $PY - run 'make setup'"
  exit 1
fi

echo "$(date -u +%FT%TZ) capture starting (pid $$)"
"$PY" -m rwi.capture --once --prune 2>&1
STATUS=$?
echo "$(date -u +%FT%TZ) capture finished with status $STATUS"

# Exit 0 even when the cycle failed. A provider outage is not a reason for the
# scheduler to treat the job as broken and start backing off: the next hour may
# work, and the failures are already recorded in the log and the archive.
exit 0
