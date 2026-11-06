"""Test-session isolation from production state.

The test suite runs on the production VM. Before any project module (and
config.load_dotenv) is imported, point every storage path at a throwaway
directory and disable live trading / external services, so a pytest run can
never write into /home/ubuntu/sqlite/strategies, Redis, MongoDB or Discord.
load_dotenv() does not override variables that are already set.
"""
import os
import tempfile

_SANDBOX_ROOT = tempfile.mkdtemp(prefix="nifty50_pytest_")
_SANDBOX = os.path.join(_SANDBOX_ROOT, "strategies")  # mirrors /home/ubuntu/sqlite/strategies
os.makedirs(_SANDBOX, exist_ok=True)

_ISOLATED_ENV = {
    "SQLITE_DIR": _SANDBOX + "/",
    "SNAPSHOT_DIR": _SANDBOX + "/",
    "SQLITE_BASE_DIR": _SANDBOX_ROOT,
    "SIGNALS_SQLITE_DIR": _SANDBOX_ROOT,
    "ACTIVE_STRATEGIES_JSON": os.path.join(_SANDBOX, "active_strategies.json"),
    "ORDER_ALERT_OUTBOX_DB_PATH": os.path.join(_SANDBOX, "pending_order_alerts.db"),
    "WEEKLY_REPORT_DIR": os.path.join(_SANDBOX, "reports"),
    # Unreachable Redis (connection refused immediately) instead of Redis Cloud
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "1",
    "REDIS_USERNAME": "",
    "REDIS_PASSWORD": "",
    "MONGODB_URI": "",
    "DISCORD_URL": "",
    "DISCORD_WATCHDOG": "",
    "OPENROUTER_API_KEY": "",
    "UPSTOX_ACCESS_TOKEN": "",
    "EMAIL_APP_PASSWORD": "",
    # Pin broker routing to the code defaults so tests never depend on the live .env
    "TUESDAY_BROKER": "upstox",
    "THURSDAY_BROKER": "angelone",
    "LIVE_EXIT_TRIGGER_TUESDAY": "false",
    "LIVE_EXIT_TRIGGER_THURSDAY": "false",
    "LIVE_BUY_TRIGGER_TUESDAY": "false",
    "LIVE_BUY_TRIGGER_THURSDAY": "false",
    "EXIT_IMMEDIATELY_TUESDAY": "false",
    "EXIT_IMMEDIATELY_THURSDAY": "false",
}

os.environ.update(_ISOLATED_ENV)
