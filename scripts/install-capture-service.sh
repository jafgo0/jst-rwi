#!/usr/bin/env bash
# Install the hourly capture job as a macOS LaunchAgent.
#
# Run this yourself: installing a LaunchAgent establishes a persistent
# background service on your machine, which is a decision for you rather than
# for tooling. Everything it does is visible below and reversible with
# --uninstall.
set -euo pipefail

LABEL="com.jusolarteam.jst-rwi.capture"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$PROJECT_DIR/deploy/$LABEL.plist.template"
LOG="$HOME/Library/Logs/jst-rwi-capture.log"
DOMAIN="gui/$(id -u)"

usage() { echo "usage: $0 [--install | --uninstall | --status | --run-now | --log]"; exit 1; }

case "${1:---install}" in
  --install)
    [ -x "$PROJECT_DIR/.venv/bin/python" ] || { echo "no venv; run 'make setup' first"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    sed "s|@PROJECT_DIR@|$PROJECT_DIR|g; s|@HOME@|$HOME|g" "$TEMPLATE" > "$PLIST"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "installed: $PLIST"
    echo "it runs once now, then hourly. log: $LOG"
    echo
    echo "What it will do every hour:"
    echo "  - fetch forecasts from 5 models at 7 sites along the race route"
    echo "  - fetch observations from the Bureau of Meteorology"
    echo "  - store them so forecasts can later be scored against what happened"
    echo "  - about 1,000 Open-Meteo calls a day, inside the free tier's 10,000"
    ;;
  --uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed. the archive and its data are untouched."
    ;;
  --status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state|last exit code|runs =" || echo "not loaded"
    ;;
  --run-now)   launchctl kickstart -k "$DOMAIN/$LABEL"; echo "triggered; see $LOG" ;;
  --log)       tail -n 40 "$LOG" 2>/dev/null || echo "no log yet at $LOG" ;;
  *)           usage ;;
esac
