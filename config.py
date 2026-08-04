"""
Central configuration for nifty50_strategies.

Loads environment variables from .env and provides shared constants
and helper factories used across all strategy modules.
"""

import os
import logging
import redis
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("nifty50_strategies")
logger.setLevel(logging.DEBUG if os.getenv("DEBUG") else logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
))
logger.handlers = [_handler]
logger.propagate = False

# Silence noisy third-party loggers
for _lib in ("redis", "urllib3", "requests"):
    logging.getLogger(_lib).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Strategy identity
# ---------------------------------------------------------------------------
STRATEGY_NAME = "nifty50_weekly_option_collector"


# ---------------------------------------------------------------------------
# Trading parameters
# ---------------------------------------------------------------------------
STRIKE_STEP = 100                 # 100-point strike grid
COLLECTION_START_HOUR = 9         # 9:30 AM IST is the first collection
COLLECTION_START_MINUTE = 30
COLLECTION_INTERVAL_MINUTES = 60  # Hourly after start


# ---------------------------------------------------------------------------
# Storage paths (Ubuntu VM target)
# ---------------------------------------------------------------------------
SQLITE_DIR = os.getenv(
    "SQLITE_DIR",
    "/home/ubuntu/sqlite/strategies/"
)
SNAPSHOT_DIR = os.getenv(
    "SNAPSHOT_DIR",
    "/home/ubuntu/sqlite/strategies/"
)

# Active week buy-price snapshot JSON file
ACTIVE_SNAPSHOT_FILE = os.path.join(
    SNAPSHOT_DIR, "current_week_buy.json"
)


# ---------------------------------------------------------------------------
# Redis connection (shared with existing generate_trading_keys infra)
# ---------------------------------------------------------------------------
def get_redis_client() -> redis.Redis:
    """
    Return a Redis client configured from environment variables.

    Uses the same Redis Cloud instance that stores the Angel One JWT
    under key ``angelone_jwt_feed``.
    """
    return redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ.get("REDIS_PORT", "12203")),
        username=os.environ.get("REDIS_USERNAME", ""),
        password=os.environ.get("REDIS_PASSWORD", ""),
        decode_responses=True,
        socket_timeout=5,
    )


# ---------------------------------------------------------------------------
# Angel One API
# ---------------------------------------------------------------------------
ANGELONE_BASE_URL = "https://apiconnect.angelone.in"

# NIFTY50 token (index token for spot LTP queries)
NIFTY50_TOKEN = "99926000"
NIFTY50_SYMBOL = "NIFTY"
NIFTY50_TRADING_SYMBOL = "NIFTY 50"
