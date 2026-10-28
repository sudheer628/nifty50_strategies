#!/usr/bin/env bash
# run_5min_options_monitor.sh — 5-minute options telemetry cron wrapper.
#
# Crontab schedule (every 5 mins between 09:15 and 15:30 IST Mon-Fri):
# */5 9-15 * * 1-5 /home/ubuntu/nifty50_strategies/scripts/run_5min_options_monitor.sh >> /home/ubuntu/logs/options_5min_monitor_$(date +\%F).log 2>&1

set -euo pipefail

BASE_DIR="/home/ubuntu/nifty50_strategies"
VENV_PYTHON="${BASE_DIR}/.venv/bin/python"
LOCK_FILE="/tmp/nifty_options_5min_monitor.lock"

# If venv python not found, fallback to system python3
if [ ! -f "${VENV_PYTHON}" ]; then
    VENV_PYTHON="python3"
fi

cd "${BASE_DIR}"

# Acquire lock to prevent duplicate runs
exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
    echo "$(date '+%F %T') [options_5min_monitor] Another instance is running. Skipping."
    exit 0
fi

# Execute poller
${VENV_PYTHON} "${BASE_DIR}/scripts/monitor_live_options_5min.py" "$@"
