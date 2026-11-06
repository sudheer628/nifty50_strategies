"""
Central configuration for nifty50_strategies.

Loads environment variables from .env and provides shared constants
and helper factories used across all strategy modules.
"""

import os
import logging
try:
    import redis
except ImportError:
    redis = None
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("nifty50_strategies")
_log_level_env = os.getenv("LOG_LEVEL")
if _log_level_env:
    logger.setLevel(getattr(logging, _log_level_env.upper(), logging.INFO))
else:
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
STRATEGY_NAME_THURSDAY = "nifty50_thursday_3day_collector"

# ---------------------------------------------------------------------------
# Configurable Broker Routing per Strategy Cycle
# ---------------------------------------------------------------------------
TUESDAY_BROKER = os.getenv("TUESDAY_BROKER", "upstox").lower().strip()
THURSDAY_BROKER = os.getenv("THURSDAY_BROKER", "angelone").lower().strip()
SUPPORTED_BROKERS = ("upstox", "angelone", "kotak")


def normalize_broker_name(broker: Optional[str]) -> str:
    """Normalize broker aliases to canonical names ('upstox', 'angelone', 'kotak')."""
    b = str(broker or "").lower().strip()
    if b in ("angelone", "angel"):
        return "angelone"
    if b == "upstox":
        return "upstox"
    if b in ("kotak", "kotakneo", "kotak_neo", "neo"):
        return "kotak"
    return b


def get_entry_qty_for_track(track: str, redis_client=None) -> int:
    """
    Paired BUY quantity (contracts) for a strategy track.

    Uses the track's own lot allocation (LOTS_TUESDAY / LOTS_THURSDAY x NIFTY_LOT_SIZE, Redis first, then .env)
    so entry size always equals the exit size authorized for that track. Falls back to
    STRATEGY_BUY_QTY / NIFTY_LOT_SIZE only when the track's LOTS_* is not configured.
    """
    t = str(track or "").lower().strip()
    if t in ("5day_strategy", "5day", "weekly", "tuesday", "tue"):
        lots_env = "LOTS_TUESDAY"
    elif t in ("3day_strategy", "3day", "thursday", "thu"):
        lots_env = "LOTS_THURSDAY"
    else:
        raise ValueError(f"Unknown strategy track '{track}'")
    lot_size = get_config_int("NIFTY_LOT_SIZE", NIFTY_LOT_SIZE, redis_client=redis_client)
    if get_config_flag(lots_env, None, redis_client=redis_client) is not None:
        lots = get_config_int(lots_env, 0, redis_client=redis_client)
        if lots <= 0:
            raise ValueError(f"{lots_env} must be a positive integer in Redis or .env")
        return lots * lot_size
    return get_config_int("STRATEGY_BUY_QTY", lot_size, redis_client=redis_client)


def get_broker_for_track(track: str, redis_client=None) -> str:
    """
    Resolve and validate the configured broker for a strategy track.

    Accepts:
      - '5day_strategy', '5day', 'tuesday', 'tue', 'weekly' -> returns TUESDAY_BROKER
      - '3day_strategy', '3day', 'thursday', 'thu' -> returns THURSDAY_BROKER

    Returns:
      Normalized broker name ('upstox', 'angelone', or 'kotak').

    Raises:
      ValueError if track is unrecognized or resolved broker is unsupported.
    """
    norm = str(track or "").lower().strip()
    if norm in ("5day_strategy", "5day", "tuesday", "tue", "weekly"):
        broker_raw = get_config_flag("TUESDAY_BROKER", os.getenv("TUESDAY_BROKER", TUESDAY_BROKER), redis_client=redis_client)
    elif norm in ("3day_strategy", "3day", "thursday", "thu"):
        broker_raw = get_config_flag("THURSDAY_BROKER", os.getenv("THURSDAY_BROKER", THURSDAY_BROKER), redis_client=redis_client)
    else:
        raise ValueError(f"Unknown strategy track '{track}'. Expected '5day_strategy' or '3day_strategy'.")

    broker = normalize_broker_name(broker_raw)
    if broker not in SUPPORTED_BROKERS:
        raise ValueError(
            f"Unsupported broker '{broker}' configured for track '{track}'. "
            f"Must be one of {SUPPORTED_BROKERS}."
        )
    return broker


# ---------------------------------------------------------------------------
# Trading parameters
# ---------------------------------------------------------------------------
STRIKE_STEP = 100                 # 100-point strike grid
COLLECTION_START_HOUR = 9         # 9:30 AM IST is the first collection
COLLECTION_START_MINUTE = 30
COLLECTION_INTERVAL_MINUTES = 60  # Hourly after start
NIFTY_LOT_SIZE = int(os.getenv("NIFTY_LOT_SIZE", "65"))  # Default Nifty contract lot size (revised to 65 by NSE)

# Moneyness parameters (ITM default per 6-year backtesting data, supports OTM / ATM)
STRIKE_MONEYNESS_MODE = os.getenv("STRIKE_MONEYNESS_MODE", "ITM").upper()
STRIKE_MONEYNESS_MODE_THURSDAY = os.getenv("STRIKE_MONEYNESS_MODE_THURSDAY", "ITM").upper()
TARGET_ITM_DELTA = float(os.getenv("TARGET_ITM_DELTA", "0.68"))  # Target |Delta| ~ 0.65 - 0.70 for ITM
PUT_IV_SPIKE_THRESHOLD = float(os.getenv("PUT_IV_SPIKE_THRESHOLD", "18.0"))  # IV threshold for accelerated trailing stop


# ---------------------------------------------------------------------------
# Peak profit alert parameters
# ---------------------------------------------------------------------------
PEAK_ALERT_MIN_PROFIT_PCT = float(os.getenv("PEAK_ALERT_MIN_PROFIT_PCT", "20.0"))
PEAK_ALERT_HYSTERESIS_PCT = float(os.getenv("PEAK_ALERT_HYSTERESIS_PCT", "5.0"))
PEAK_ALERT_HYSTERESIS_PTS = float(os.getenv("PEAK_ALERT_HYSTERESIS_PTS", "10.0"))
PEAK_ALERT_ENABLED = os.getenv("PEAK_ALERT_ENABLED", "true").lower() in ("true", "1", "yes")
DISCORD_URL = os.getenv("DISCORD_URL", "").strip()
DISCORD_EXIT_3DAY = os.getenv("DISCORD_EXIT_3DAY", os.getenv("DISCORD_URL_3DAY", "")).strip()
DISCORD_EXIT_5DAY = os.getenv("DISCORD_EXIT_5DAY", os.getenv("DISCORD_URL_5DAY", "")).strip()

# ---------------------------------------------------------------------------
# MongoDB Atlas Database Configuration (Derivatives & Predictions)
# ---------------------------------------------------------------------------
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "stock_recommendations")
MONGODB_COLLECTION_DERIVATIVE_STRATEGIES = os.getenv(
    "MONGODB_COLLECTION_DERIVATIVE_STRATEGIES", "derivative_strategies"
)
MONGODB_COLLECTION_DERIVATIVE_MASTER = os.getenv(
    "MONGODB_COLLECTION_DERIVATIVE_MASTER", "derivative_master"
)
MONGODB_COLLECTION_DERIVATIVE_PREDICTIONS = os.getenv(
    "MONGODB_COLLECTION_DERIVATIVE_PREDICTIONS", "derivative_predictions"
)
MONGODB_COLLECTION_DERIVATIVE_ACTIVE_CYCLES = os.getenv(
    "MONGODB_COLLECTION_DERIVATIVE_ACTIVE_CYCLES", "derivative_active_cycles"
)
MONGODB_COLLECTION_DERIVATIVE_CYCLE_LIVE_LOGS = os.getenv(
    "MONGODB_COLLECTION_DERIVATIVE_CYCLE_LIVE_LOGS", "derivative_cycle_live_logs"
)


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

# Active week buy-price snapshot JSON file (Master Tuesday track)
ACTIVE_SNAPSHOT_FILE = os.path.join(
    SNAPSHOT_DIR, "current_week_buy.json"
)

# Active 3-day buy-price snapshot JSON file (Parallel Thursday track)
THURSDAY_SNAPSHOT_FILE = os.path.join(
    SNAPSHOT_DIR, "current_thursday_buy.json"
)

# Unified active strategies JSON file (Master Tuesday & Parallel Thursday tracks)
ACTIVE_STRATEGIES_JSON = os.getenv(
    "ACTIVE_STRATEGIES_JSON",
    os.path.join(SNAPSHOT_DIR, "active_strategies.json")
)


# ---------------------------------------------------------------------------
# Redis connection (shared with existing generate_trading_keys infra)
# ---------------------------------------------------------------------------
def get_redis_client():
    """
    Return a Redis client configured from environment variables.

    Uses the same Redis Cloud instance that stores the Angel One JWT
    under key ``angelone_jwt_feed``.
    """
    if redis is None:
        logger.warning("Redis package not installed.")
        return None
    try:
        return redis.Redis(
            host=os.environ.get("REDIS_HOST", ""),
            port=int(os.environ.get("REDIS_PORT", "12203")),
            username=os.environ.get("REDIS_USERNAME", ""),
            password=os.environ.get("REDIS_PASSWORD", ""),
            decode_responses=True,
            socket_timeout=5,
        )
    except Exception as e:
        logger.warning(f"Failed to create Redis client: {e}")
        return None


# ---------------------------------------------------------------------------
# Redis-first runtime configuration (Redis -> .env -> default)
# ---------------------------------------------------------------------------
# Execution gates and sizing can be changed remotely by setting a Redis key with the
# same name as the .env variable (e.g. `SET LOTS_THURSDAY 2`); `DEL` the key to revert
# to .env. If Redis is unreachable, or the key is missing/empty/invalid, the .env value
# is used, then the hardcoded default.
REDIS_CONFIG_KEYS = (
    "LIVE_EXIT_TRIGGER_TUESDAY", "EXIT_IMMEDIATELY_TUESDAY", "LIVE_BUY_TRIGGER_TUESDAY",
    "LIVE_EXIT_TRIGGER_THURSDAY", "EXIT_IMMEDIATELY_THURSDAY", "LIVE_BUY_TRIGGER_THURSDAY",
    "LOTS_TUESDAY", "LOTS_THURSDAY", "NIFTY_LOT_SIZE", "STRATEGY_BUY_QTY",
)
_TRUE_VALUES = ("true", "1", "yes", "on")
_FALSE_VALUES = ("false", "0", "no", "off")
_config_redis_state = {"client": None, "unavailable": False, "failed_clients": set()}


def _config_redis(redis_client=None):
    """Return the caller's Redis client, or a lazily created process-wide one (None if unreachable)."""
    if redis_client is not None:
        return None if id(redis_client) in _config_redis_state["failed_clients"] else redis_client
    if _config_redis_state["unavailable"]:
        return None
    if _config_redis_state["client"] is None:
        _config_redis_state["client"] = get_redis_client()
        if _config_redis_state["client"] is None:
            _config_redis_state["unavailable"] = True
    return _config_redis_state["client"]


def _config_candidates(key: str, redis_client=None):
    """Yield (raw_value, source) in precedence order: Redis, then .env."""
    r = _config_redis(redis_client)
    if r is not None:
        try:
            val = r.get(key)
            if isinstance(val, bytes):
                val = val.decode("utf-8")
            if val is not None and str(val).strip() != "":
                yield str(val).strip(), "redis"
        except Exception as exc:
            logger.warning("Redis config lookup failed for %s (%s); falling back to .env", key, exc)
            # Avoid paying the connection timeout again for every remaining flag this run
            if redis_client is None:
                _config_redis_state["unavailable"] = True
            else:
                _config_redis_state["failed_clients"].add(id(redis_client))
    env_val = os.getenv(key)
    if env_val is not None and env_val.strip() != "":
        yield env_val.strip(), "env"


def _log_config(key: str, value, source: str) -> None:
    if source == "redis":
        logger.info("Config %s=%s (source: Redis override)", key, value)
    else:
        logger.debug("Config %s=%s (source: %s)", key, value, source)


def get_config_flag(key: str, default=None, redis_client=None):
    """Raw string value for key: Redis first, then .env, then default."""
    for raw, source in _config_candidates(key, redis_client):
        _log_config(key, raw, source)
        return raw
    return default


def get_config_bool(key: str, default: bool = False, redis_client=None) -> bool:
    """Boolean flag (true/1/yes/on or false/0/no/off): Redis first, then .env, then default.
    An unrecognized value in one tier is logged and the next tier is used."""
    for raw, source in _config_candidates(key, redis_client):
        low = raw.lower()
        if low in _TRUE_VALUES or low in _FALSE_VALUES:
            _log_config(key, low, source)
            return low in _TRUE_VALUES
        logger.warning("Ignoring invalid boolean %s=%r from %s; trying next source", key, raw, source)
    return bool(default)


def get_config_int(key: str, default: int, redis_client=None, min_value: int = 1) -> int:
    """Integer setting (lots / quantities): Redis first, then .env, then default.
    Non-integer or values below min_value in one tier are logged and the next tier is used."""
    for raw, source in _config_candidates(key, redis_client):
        try:
            val = int(raw)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid integer %s=%r from %s; trying next source", key, raw, source)
            continue
        if val < min_value:
            logger.warning("Ignoring %s=%r from %s (must be >= %d); trying next source", key, raw, source, min_value)
            continue
        _log_config(key, val, source)
        return val
    return int(default)


# ---------------------------------------------------------------------------
# Angel One API
# ---------------------------------------------------------------------------
ANGELONE_BASE_URL = "https://apiconnect.angelone.in"

# NIFTY 50 cash-index identifiers from Angel One's OpenAPIScripMaster:
#   token=99926000  symbol="Nifty 50"  exch_seg=NSE
#   instrumenttype=AMXIDX
# Token 2 is a CDS-segment instrument and must not be used with NSE.
NIFTY50_TOKEN = "99926000"
NIFTY50_SYMBOL = "NIFTY"
NIFTY50_TRADING_SYMBOL = "Nifty 50"

# Angel One publishes the current instrument/token catalogue here. Option
# tokens are contract-specific, so they are resolved from this daily file.
ANGELONE_INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)
