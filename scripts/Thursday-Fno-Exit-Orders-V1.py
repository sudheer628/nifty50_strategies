#!/usr/bin/env python3
"""
================================================================================
Thursday F&O Exit Manager — Dedicated 3-Day Strategy Trailing Stop Manager (V1)
================================================================================
Cycle:
  Thursday 3-Day Options Strategy (Thursday entry -> Tuesday expiry).

Scope & Operational Rules:
1. Master Switch Gate:
   - Requires BOTH `LIVE_EXIT_TRIGGER_THURSDAY=true` in .env AND `--execute` CLI flag.
   - If `LIVE_EXIT_TRIGGER_THURSDAY` is false/missing, runs strictly in dry-run mode.
2. Fast 1ms Redis Gate:
   - Checks Redis key `ORDERS_LIVE_THU` before querying broker APIs.
   - If inactive (0), exits in 1ms with zero AngelOne API consumption.
3. Multi-Lot & Quantity Isolation:
   - Manages strictly Thursday's allocated lots (`--lots` or `LOTS_THURSDAY`, default: 1 lot = 65 qty).
   - Authoritative quantity in Redis contract key (e.g. `{"enabled": true, "qty": 65}`) takes priority.
4. AngelOne Order Tracking (Redis order_id Live Authority):
   - AngelOne SmartAPI does not accept or forward custom user tags; no `THU-SL` tag is sent in order payloads.
   - Live orders are identified and tracked strictly via broker `orderid` stored in Redis (`fno:angelone:trail:thu:*:order_id`).
   - The code retains an auxiliary `tag="THU-SL"` check as a compatibility fallback across all execution modes (primarily for offline `--mock` data and synthetic test harnesses), while Redis `order_id` remains the authoritative identifier for live market trading.
   - Any open SELL order in the broker order book not matching the stored Redis `orderid` (and carrying no compatibility tag) is treated as a manual sell order and left untouched (`manual_sell_conflict`).
5. Ratchet Trailing SL:
   - Modifies AngelOne SL-M orders upwards only as LTP rises (never lowers stop).
   - Requires minimum ratchet step (default ₹1.00) to prevent broker rate-limit spam.
6. Auto-Cancel Orphan Orders:
   - If Thursday position drops to 0 (closed/filled), automatically requests cancellation of dangling managed
     stop orders to prevent accidental short exposure.
   - Keeps monitoring active until terminal status is confirmed in order book, then purges Redis state.
7. Fail-Closed Safety:
   - Blocked if Redis is offline during `--execute`.
   - Alerts on unexpected short derivative positions.
================================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

try:
    from dotenv import load_dotenv
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    env_path = os.path.join(project_root, ".env")
    if os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path)
    else:
        load_dotenv()
except ImportError:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from common.fno_keys import (
    EXITED_QTY_TTL_SEC,
    build_detailed_enabled_key,
    build_exited_qty_key,
    build_leg_enabled_key,
    get_orders_live_key,
    normalize_cycle,
    normalize_expiry,
    parse_enabled_key,
)
from common.entry_order_manager import (
    dispatch_order_failure_discord,
    clear_order_failure_cooldown,
)
from common.broker_adapters import (
    get_broker_adapter,
    round_to_tick,
    order_filled_quantity,
    order_pending_quantity,
    normalize_order_status,
)
from config import get_broker_for_track, SUPPORTED_BROKERS, THURSDAY_BROKER

try:
    import redis
except ImportError:
    redis = None

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
CYCLE_NAME = "thursday"
CYCLE_TRACK = "3day_strategy"
DEFAULT_TAG = "THU-SL"
OTHER_CYCLE_TAGS = {"TUE-SL", "TRAIL-SL"}

ANGELONE_BASE = os.getenv("ANGELONE_BASE_URL", "https://apiconnect.angelone.in").rstrip("/")
PRODUCT_CARRYFORWARD = "CARRYFORWARD"
PRODUCT_NRML = "CARRYFORWARD"
PRODUCT_MAP = {"angelone": "CARRYFORWARD", "angel": "CARRYFORWARD", "upstox": "D", "kotak": "NRML", "kotakneo": "NRML"}

ORDER_TYPE_SLM = "STOPLOSS_MARKET"
ORDER_TYPE_SL = "STOPLOSS_LIMIT"

VALIDITY_DAY = "DAY"
TICK_SIZE = 0.05
DEFAULT_LOT_SIZE = int(os.getenv("NIFTY_LOT_SIZE", "65"))
DEFAULT_BROKER = os.getenv("THURSDAY_BROKER", "angelone").lower().strip()
DEFAULT_LOCK_KEY = "fno:lock:exit_order_manager:thursday"

OPEN_ORDER_STATUSES = {
    "open", "pending", "modify_pending", "trigger_pending", "open_pending",
    "cancel_pending", "replace_pending", "validation_pending",
    "put_order_req_received", "after_market_order_req_received",
    "partially_filled", "partiallyfilled",
}
TERMINAL_CANCELLED_STATUSES = {"cancelled", "canceled", "rejected", "expired"}
TERMINAL_FILLED_STATUSES = {"complete", "completed", "filled"}
DERIVATIVE_EXCHANGES = ("NFO", "BFO", "NSE_FO", "BSE_FO")
DERIVATIVE_KEY_PREFIXES = ("NSE_FO|", "BSE_FO|")

DEFAULT_TRAIL_PCT = 20.0          # Trail 20% below max
DEFAULT_INITIAL_STOP_PCT = 30.0   # Initial SL 30% below avg before activation
DEFAULT_ACTIVATION_PCT = 0.0      # 0 = trail from start
DEFAULT_MIN_RATCHET_STEP = 1.0    # Min ₹1.00 increase to trigger broker order modify
REQUEST_TIMEOUT = 15
MOCK_TOKEN = "MOCK_TOKEN"
LOG_API = False
_SENSITIVE_HEADERS = ("authorization", "x-api-key", "x-privatekey")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [THU-EXIT] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("thursday_fno_exit")


def set_api_logging(enabled: bool):
    global LOG_API
    LOG_API = bool(enabled)


def _log_api_request(method: str, url: str, headers: Dict[str, Any], payload: Any) -> None:
    safe_headers = {k: ("***" if k.lower() in _SENSITIVE_HEADERS else v) for k, v in (headers or {}).items()}
    logger.debug("[API REQ] %s %s | headers: %s | payload: %s", method, url, safe_headers, payload)


def _log_api_response(method: str, url: str, resp: Any) -> None:
    logger.debug("[API RESP] %s %s | status: %s | text: %s", method, url, getattr(resp, "status_code", None), getattr(resp, "text", "")[:300])


def is_market_hours_ist(ref_dt: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Check if current IST time is within NSE equity derivatives trading hours:
    Monday-Friday, 09:15 to 15:30 IST, excluding exchange trading holidays.
    """
    now = ref_dt or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        now_ist = now.astimezone(ZoneInfo("Asia/Kolkata"))
    except ImportError:
        import pytz
        now_ist = now.astimezone(pytz.timezone("Asia/Kolkata"))

    if now_ist.weekday() >= 5:
        return False, f"Weekend ({now_ist.strftime('%A')}); market is closed"

    # Exchange Holiday Check
    try:
        from common.calendar_utils import check_nse_holiday
        if check_nse_holiday(now_ist.date()):
            return False, f"Exchange Holiday ({now_ist.strftime('%Y-%m-%d %A')}); market is closed"
    except Exception as exc:
        logger.debug("Could not verify holiday calendar: %s", exc)

    # Precision at seconds level: market strictly closes at 15:30:00 IST
    current_time_sec = now_ist.hour * 3600 + now_ist.minute * 60 + now_ist.second
    market_open_sec = 9 * 3600 + 15 * 60      # 09:15:00 IST
    market_close_sec = 15 * 3600 + 30 * 60    # 15:30:00 IST

    if current_time_sec < market_open_sec:
        return False, f"Pre-market ({now_ist.strftime('%H:%M:%S')} IST < 09:15:00 IST); market not open"
    if current_time_sec >= market_close_sec:
        return False, f"Post-market ({now_ist.strftime('%H:%M:%S')} IST >= 15:30:00 IST); market is closed"

    return True, f"Market is open ({now_ist.strftime('%H:%M:%S')} IST)"


@contextmanager
def combined_execution_lock(
    r,
    cycle_name: str,
    broker: str,
    timeout: int = 60,
    retry_delay: float = 0.2,
    max_wait: float = 10.0,
    abort_event: Optional[Any] = None,
):
    """
    Acquire dual distributed locks:
    1. Track-level lock (e.g. fno:lock:exit_order_manager:thursday)
    2. Broker-level lock (e.g. fno:lock:broker:angelone)
    Ensures safe serialization when both tracks share the same broker account.
    """
    cycle_lock_key = f"fno:lock:exit_order_manager:{cycle_name}"
    broker_lock_key = f"fno:lock:broker:{broker}"
    with fno_execution_lock(
        r, lock_key=cycle_lock_key, timeout=timeout, retry_delay=retry_delay, max_wait=max_wait, abort_event=abort_event
    ) as cycle_acquired:
        if not cycle_acquired:
            yield False, f"Track lock '{cycle_lock_key}' is held by another process"
            return
        with fno_execution_lock(
            r, lock_key=broker_lock_key, timeout=timeout, retry_delay=retry_delay, max_wait=max_wait, abort_event=abort_event
        ) as broker_acquired:
            if not broker_acquired:
                yield False, f"Broker lock '{broker_lock_key}' is held by another strategy running on the same account"
                return
            yield True, None


@contextmanager
def fno_execution_lock(
    r,
    lock_key: str = "fno:lock:exit_order_manager:thursday",
    timeout: int = 60,
    retry_delay: float = 0.2,
    max_wait: float = 10.0,
    abort_event: Optional[Any] = None,
):
    """
    Acquire a cross-process distributed lock across Tuesday & Thursday exit managers.
    Prevents race conditions where both cron jobs inspect portfolio at the exact same millisecond
    and over-allocate SELL exit orders.

    Features:
    - Auto-renewal heartbeater daemon thread extends lease while active.
    - Fails closed if not acquired within max_wait.
    - Sets abort_event if lock renewal fails mid-execution so caller aborts immediately.
    - Safe Lua script release prevents deleting other workers' locks.
    """
    if not r:
        yield True
        return

    import threading
    token = str(uuid.uuid4())
    acquired = False
    start_time = time.time()

    while time.time() - start_time < max_wait:
        try:
            if r.set(lock_key, token, nx=True, ex=timeout):
                acquired = True
                break
        except Exception as exc:
            logger.warning("Redis lock attempt failed: %s", exc)
            break
        time.sleep(retry_delay)

    stop_heartbeat = threading.Event()

    def _heartbeat():
        interval = max(0.05, timeout / 3)
        while not stop_heartbeat.wait(interval):
            try:
                lua_extend = """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("expire", KEYS[1], ARGV[2])
                else
                    return 0
                end
                """
                res = r.eval(lua_extend, 1, lock_key, token, timeout)
                if not res:
                    logger.critical("Distributed lock '%s' lease renewal failed (lock lost/expired).", lock_key)
                    if abort_event is not None:
                        abort_event.set()
                    break
            except Exception as exc:
                logger.critical("Distributed lock '%s' heartbeat exception: %s. Flagging lock lost.", lock_key, exc)
                if abort_event is not None:
                    abort_event.set()
                break

    heartbeat_thread = None
    if acquired:
        heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
        heartbeat_thread.start()

    try:
        yield acquired
    finally:
        if acquired and r:
            stop_heartbeat.set()
            if heartbeat_thread:
                heartbeat_thread.join(timeout=1.0)
            try:
                lua_release = """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("del", KEYS[1])
                else
                    return 0
                end
                """
                r.eval(lua_release, 1, lock_key, token)
            except Exception as exc:
                logger.debug("Failed to cleanly release Redis lock %s: %s", lock_key, exc)


def _mask_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    if not headers:
        return {}
    masked = {}
    for k, v in headers.items():
        if str(k).lower() in _SENSITIVE_HEADERS:
            masked[k] = f"***REDACTED*** ({len(str(v))} chars)"
        else:
            masked[k] = v
    return masked


def _headers(token: str) -> Dict[str, str]:
    if token == MOCK_TOKEN:
        return {
            "Authorization": f"Bearer {MOCK_TOKEN}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    api_key = os.environ.get("ANGELONE_API_KEY", "")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": api_key,
    }


def get_redis_client():
    """
    Get Redis client using nifty50_strategies.config credentials (REDIS_HOST/PORT/PW)
    with seamless fallback to REDIS_URL.
    """
    if redis is None:
        return None
    try:
        from config import get_redis_client as config_get_redis
        client = config_get_redis()
        if client:
            client.ping()
            return client
    except Exception:
        pass

    try:
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        client = redis.from_url(url, protocol=2, decode_responses=True, socket_connect_timeout=2)
        client.ping()
        return client
    except Exception as e:
        logger.warning("Redis not available: %s", e)
        return None


def round_to_tick(price: float) -> float:
    if price <= 0:
        return TICK_SIZE
    ticks = round(price / TICK_SIZE)
    return round(ticks * TICK_SIZE, 2)


def _to_float(v: Any) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except Exception:
        return 0.0


def parse_tradingsymbol(tradingsymbol: str) -> Dict[str, str]:
    """
    Robust parser for NSE / Upstox option tradingsymbols:
      - Weekly with 2-digit year at end: NIFTY03NOV2622650CE -> NIFTY, 03NOV26, 22650, CE
      - Monthly/Weekly with 2-digit year first: NIFTY26NOV22650CE -> NIFTY, 26NOV, 22650, CE
      - Compact Upstox format: NIFTY26N0923500CE -> NIFTY, active expiry, 23500, CE
      - Legacy format: NIFTY4AUG24000CE -> NIFTY, 4AUG, 24000, CE
    """
    raw = str(tradingsymbol or "").upper().strip()
    m1 = re.match(r"^([A-Z]+)(\d{1,2}[A-Z]{3}\d{2})(\d{4,5})(CE|PE)$", raw)
    if m1:
        return {"underlying": m1.group(1), "expiry": m1.group(2), "strike": m1.group(3), "option_type": m1.group(4), "raw": raw}
    m2 = re.match(r"^([A-Z]+)(\d{2}[A-Z]{3})(\d{4,5})(CE|PE)$", raw)
    if m2:
        return {"underlying": m2.group(1), "expiry": m2.group(2), "strike": m2.group(3), "option_type": m2.group(4), "raw": raw}
    m3 = re.match(r"^([A-Z]+)(\d{1,2}[A-Z]{3})(\d{4,5})(CE|PE)$", raw)
    if m3:
        return {"underlying": m3.group(1), "expiry": m3.group(2), "strike": m3.group(3), "option_type": m3.group(4), "raw": raw}
    # Compact Upstox / NSE weekly format: two-digit year + single-char month (1-9, O, N, D) + day.
    m_compact = re.match(r"^([A-Z]+)(\d{2}[1-9OND]\d{2})(\d{4,5})(CE|PE)$", raw)
    if m_compact:
        return {"underlying": m_compact.group(1), "expiry": m_compact.group(2), "strike": m_compact.group(3), "option_type": m_compact.group(4), "raw": raw}
    m4 = re.match(r"^(.*?)(\d+)(CE|PE)$", raw)
    if m4:
        return {"underlying": m4.group(1), "expiry": "UNKNOWN", "strike": m4.group(2), "option_type": m4.group(3), "raw": raw}
    return {"underlying": raw[:10] if raw else "UNKNOWN", "expiry": "UNKNOWN", "strike": "0", "option_type": "UNKNOWN", "raw": raw}


def get_active_strategy_expiry(track: str = "3day_strategy") -> Optional[str]:
    """Retrieve active cycle expiry_date (YYYY-MM-DD) from environment, active_strategies.json, or snapshot file."""
    env_exp = os.getenv("ACTIVE_EXPIRY_DATE") or os.getenv("EXPIRY_DATE")
    if env_exp:
        return normalize_expiry(env_exp)
    try:
        from common.unified_strategy_store import load_active_strategies
        active = load_active_strategies()
        strat = active.get(track, {})
        exp = strat.get("expiry_date") or (strat.get("day1_locked_price") or {}).get("expiry_date")
        if exp:
            return normalize_expiry(exp)
    except Exception:
        pass
    try:
        from config import ACTIVE_SNAPSHOT_FILE, THURSDAY_SNAPSHOT_FILE
        target_file = THURSDAY_SNAPSHOT_FILE if str(track).startswith("3") or "thu" in str(track).lower() else ACTIVE_SNAPSHOT_FILE
        if os.path.exists(target_file):
            with open(target_file, "r", encoding="utf-8") as f:
                snap = json.load(f)
                exp = snap.get("expiry_date") or snap.get("expiry")
                if exp:
                    return normalize_expiry(exp)
    except Exception:
        pass
    return None


def get_active_strategy_broker(track: str = "3day_strategy") -> Optional[str]:
    """Retrieve active cycle broker from active_strategies.json or snapshot file."""
    try:
        from common.unified_strategy_store import load_active_strategies
        active = load_active_strategies()
        strat = active.get(track, {})
        broker = strat.get("broker") or (strat.get("day1_locked_price") or {}).get("broker")
        if broker:
            return str(broker).lower().strip()
    except Exception:
        pass
    try:
        from config import THURSDAY_SNAPSHOT_FILE
        if os.path.exists(THURSDAY_SNAPSHOT_FILE):
            with open(THURSDAY_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                snap = json.load(f)
                b = snap.get("broker")
                if b:
                    return str(b).lower().strip()
    except Exception:
        pass
    return None


def build_trail_redis_keys(
    broker: str,
    tradingsymbol: str,
    instrument_token: str,
    active_expiry: Optional[str] = None,
) -> Dict[str, str]:
    """
    Build exact Thursday-scoped authorization and trailing-state Redis keys.
    Strictly isolated to Thursday cycle (no generic cross-cycle fallbacks).
    """
    broker = (broker or "angelone").lower().strip()
    details = parse_tradingsymbol(tradingsymbol)
    if str(details.get("expiry") or "").upper() in ("UNKNOWN", "NONE", "N/A") and active_expiry:
        details["expiry"] = normalize_expiry(None, active_expiry=active_expiry)
    safe_token = str(instrument_token or "NOTOKEN").replace("|", "_").replace(":", "_").upper()
    safe_symbol = str(tradingsymbol or "NOSYMBOL").upper().replace(":", "_").replace("|", "_")
    contract_suffix = (
        f"{details['underlying']}:{details['expiry']}:{details['strike']}:"
        f"{details['option_type']}:{safe_symbol}:{safe_token}"
    )

    base = f"fno:{broker}:trail:thu:{contract_suffix}"
    short_base = f"fno_trail:{broker}:thu:{safe_symbol}:{safe_token}"

    expiry_iso = normalize_expiry(details.get("expiry"), active_expiry=active_expiry)
    canonical_enabled = build_leg_enabled_key("thu", expiry_iso, details["strike"], details["option_type"], broker)
    detailed_enabled = build_detailed_enabled_key("thu", contract_suffix, broker)

    return {
        "enabled": detailed_enabled,
        "enabled_canonical": canonical_enabled,
        "max": f"{base}:max",
        "sl": f"{base}:sl",
        "avg": f"{base}:avg",
        "qty": f"{base}:qty",
        "order_id": f"{base}:order_id",
        "entry_order_id": f"{base}:entry_order_id",
        "mode": f"{base}:mode",
        "market_order_id": f"{base}:market_order_id",
        "exit_filled_qty": f"{base}:exit_filled_qty",
        "max_short": f"{short_base}:max",
        "sl_short": f"{short_base}:sl",
        "avg_short": f"{short_base}:avg",
        "details": details,
        "contract_suffix": contract_suffix,
        "expiry_iso": expiry_iso,
        "symbol": tradingsymbol,
        "tradingsymbol": tradingsymbol,
    }


def build_trailing_state_keys(
    tradingsymbol: str,
    instrument_token: str,
    active_expiry: Optional[Union[str, date, datetime]] = None,
    broker: str = "angelone",
) -> Dict[str, Any]:
    return build_trail_redis_keys(broker, tradingsymbol, instrument_token, active_expiry=active_expiry)


def _position_quantity(pos: Dict[str, Any]) -> float:
    for field in ("quantity", "net_quantity", "netQty", "netqty"):
        if field in pos and pos.get(field) not in (None, ""):
            return _to_float(pos.get(field))
    return _to_float(pos.get("day_buy_quantity", 0))


def is_derivative_position(pos: Dict[str, Any]) -> bool:
    exch = str(pos.get("exchange") or "").upper()
    token = str(pos.get("instrument_token") or pos.get("symboltoken") or "").upper()
    inst_type = str(pos.get("instrumenttype") or "").upper()
    if exch in DERIVATIVE_EXCHANGES:
        return True
    if inst_type in ("OPTIDX", "FUTIDX", "OPTSTK", "FUTSTK"):
        return True
    return any(token.startswith(prefix) for prefix in DERIVATIVE_KEY_PREFIXES)


def parse_positions(raw: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Parse AngelOne / Upstox positions response.
    Returns: (long_positions, short_positions)
    """
    from common.broker_adapters import parse_positions as adapter_parse_positions
    return adapter_parse_positions(raw)


def normalize_order_status(status: Any) -> str:
    raw = str(status or "").strip().lower()
    return raw.replace(" ", "_")


def is_open_sell_order(order: Dict[str, Any]) -> bool:
    if not isinstance(order, dict):
        return False
    ttype = str(order.get("transaction_type") or order.get("transactiontype") or "").upper()
    if ttype != "SELL":
        return False
    status = normalize_order_status(order.get("status") or order.get("orderstatus"))
    return status in OPEN_ORDER_STATUSES


def parse_order_book(raw: Any) -> List[Dict[str, Any]]:
    """Parse Upstox or AngelOne order book response into normalized order dicts."""
    from common.broker_adapters import parse_order_book as adapter_parse_order_book
    return adapter_parse_order_book(raw)


def order_tag(order: Dict[str, Any]) -> str:
    return str(order.get("tag") or "").strip()


def order_pending_quantity(order: Dict[str, Any]) -> int:
    for field in ("pending_quantity", "quantity_remaining", "open_quantity", "unfilledshares"):
        if field in order and order.get(field) not in (None, ""):
            return int(_to_float(order.get(field)))
    total = int(_to_float(order.get("quantity", 0)))
    filled = int(_to_float(order.get("filled_quantity", order.get("filledshares", 0))))
    return max(0, total - filled)


def order_filled_quantity(order: Dict[str, Any]) -> int:
    for field in ("filled_quantity", "filled_shares", "traded_quantity", "filledshares"):
        if field in order and order.get(field) not in (None, ""):
            f = int(_to_float(order.get(field)))
            if f > 0:
                return f
    # Fallback to total quantity - pending quantity / unfilled shares / cancel size
    total = _to_float(order.get("quantity"))
    for pfield in ("pending_quantity", "unfilledshares", "cancelsize"):
        if pfield in order and order.get(pfield) not in (None, ""):
            return int(max(0.0, total - _to_float(order.get(pfield))))
    return 0


def order_matches_position(order: Dict[str, Any], position: Dict[str, Any]) -> bool:
    o_token = str(order.get("instrument_token") or order.get("symboltoken") or "").strip()
    p_token = str(position.get("instrument_token") or position.get("symboltoken") or "").strip()
    if o_token and p_token and o_token == p_token:
        return True
    o_sym = str(order.get("trading_symbol") or order.get("tradingsymbol") or "").strip().upper()
    p_sym = str(position.get("tradingsymbol") or position.get("trading_symbol") or "").strip().upper()
    return bool(o_sym and p_sym and o_sym == p_sym)


def matching_open_sell_orders(position: Dict[str, Any], orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [o for o in orders if is_open_sell_order(o) and order_matches_position(o, position)]


def latest_filled_buy_order(position: Dict[str, Any], orders: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    matching_buys = []
    for o in orders:
        ttype = str(o.get("transaction_type") or o.get("transactiontype") or "").upper()
        if ttype != "BUY":
            continue
        if not order_matches_position(o, position):
            continue
        status = normalize_order_status(o.get("status") or o.get("orderstatus"))
        filled = order_filled_quantity(o)
        if status in ("complete", "filled") or filled > 0:
            matching_buys.append(o)

    if not matching_buys:
        return None, None, "none"

    def _sort_key(order: Dict[str, Any]):
        for f in ("order_timestamp", "order_time", "exchange_timestamp"):
            if order.get(f):
                return str(order.get(f))
        return str(order.get("order_id") or order.get("orderid") or "")

    matching_buys.sort(key=_sort_key, reverse=True)
    best = matching_buys[0]
    return str(best.get("order_id") or best.get("orderid") or ""), best, "matched_filled_buy"


def get_access_token(broker: str = "angelone", r: Optional[Any] = None) -> str:
    adapter = get_broker_adapter(broker)
    client = r if r is not None else get_redis_client()
    return adapter.get_access_token(redis_client=client)


def fetch_positions(token: str, broker: str = "angelone") -> Dict[str, Any]:
    return get_broker_adapter(broker).fetch_positions(token)


def fetch_order_book(token: str, broker: str = "angelone") -> List[Dict[str, Any]]:
    return get_broker_adapter(broker).fetch_order_book(token)


def fetch_ltp(token: str, instrument_token: str, broker: str = "angelone", trading_symbol: str = "", exchange: str = "NFO") -> Optional[float]:
    return get_broker_adapter(broker).fetch_ltp(token, instrument_token, trading_symbol=trading_symbol, exchange=exchange)


def cancel_order(token: str, order_id: str, broker: str = "angelone", variety: str = "STOPLOSS") -> Dict[str, Any]:
    return get_broker_adapter(broker).cancel_order(token, order_id, variety=variety)


def compute_trailing_sl(max_price: float, trail_pct: float) -> float:
    sl = max_price * (1.0 - trail_pct / 100.0)
    if sl <= 0:
        sl = TICK_SIZE
    return round_to_tick(sl)


def compute_initial_sl(avg_price: float, initial_stop_pct: float) -> float:
    sl = avg_price * (1.0 - initial_stop_pct / 100.0)
    if sl <= 0:
        sl = TICK_SIZE
    return round_to_tick(sl)


def get_exit_product(position: Dict[str, Any], broker: str = "angelone", cli_product: Optional[str] = None) -> Optional[str]:
    return get_broker_adapter(broker).get_exit_product(position, cli_product=cli_product)


def place_exit_order_slm(
    token: str,
    position: Dict[str, Any],
    target_qty: int,
    trigger_price: float,
    product: Optional[str] = None,
    tag: str = DEFAULT_TAG,
    broker: str = "angelone",
) -> Dict[str, Any]:
    return get_broker_adapter(broker).place_exit_order_slm(token, position, target_qty, trigger_price, product=product, tag=tag)


def place_exit_order_market(
    token: str,
    position: Dict[str, Any],
    target_qty: int,
    product: Optional[str] = None,
    tag: str = DEFAULT_TAG,
    broker: str = "angelone",
) -> Dict[str, Any]:
    return get_broker_adapter(broker).place_exit_order_market(token, position, target_qty, product=product, tag=tag)


def modify_exit_order_slm(
    token: str,
    order: Dict[str, Any],
    target_qty: int,
    trigger_price: float,
    tag: str = DEFAULT_TAG,
    broker: str = "angelone",
) -> Dict[str, Any]:
    return get_broker_adapter(broker).modify_exit_order_slm(token, order, target_qty, trigger_price, tag=tag)


def _order_response(action: str, resp, payload: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"action": action, "request": payload}
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": str(getattr(resp, "text", ""))[:300]}
    result["http_status"] = getattr(resp, "status_code", None)
    result["response"] = body
    is_success = (
        isinstance(body, dict)
        and 200 <= int(getattr(resp, "status_code", 0) or 0) < 300
        and (body.get("status") is True or body.get("status") == "success")
    )
    if is_success:
        result["success"] = True
        data = body.get("data") or {}
        result["order_id"] = data.get("orderid") or data.get("order_id") if isinstance(data, dict) else None
    else:
        result["success"] = False
        result["error"] = body.get("message") or body.get("errorcode") or body.get("errors") or body if isinstance(body, dict) else body
    return result


TRUE_AUTH_VALUES = {"1", "true", "yes", "on", "enabled"}


def _get_redis_leg_authorization(r, key: str) -> Tuple[bool, Optional[int]]:
    """
    Check if a leg is authorized in Redis and extract optional quantity.
    Returns (is_enabled, optional_qty).
    Supports:
      - JSON string: {"enabled": true, "qty": 65} or {"quantity": 65}
      - Boolean / flag string: "1", "true", "yes"
    Fail-closed: only whitelisted truthy values are accepted; malformed JSON, typos,
    unrecognized strings, or non-positive quantities are strictly rejected.
    """
    if r is None:
        return False, None
    try:
        value = r.get(key)
        if value is None:
            return False, None
        val_str = str(value).strip()

        # Check if JSON structure
        if val_str.startswith("{"):
            try:
                data = json.loads(val_str)
            except Exception as exc:
                logger.error("Malformed JSON in Redis authorization key %s: %s (value: %s)", key, exc, val_str)
                return False, None

            if not isinstance(data, dict):
                logger.error("JSON in Redis authorization key %s is not a dictionary (type: %s)", key, type(data))
                return False, None

            # Strict whitelist validation for enabled field
            raw_enabled = data.get("enabled", True)
            if isinstance(raw_enabled, bool):
                enabled = raw_enabled
            elif isinstance(raw_enabled, (int, float)):
                enabled = int(raw_enabled) == 1
            elif isinstance(raw_enabled, str):
                enabled = raw_enabled.strip().lower() in TRUE_AUTH_VALUES
            else:
                enabled = False

            if not enabled:
                return False, None

            # Strict quantity validation if specified in JSON
            qty_raw = data.get("qty") if "qty" in data else (data.get("quantity") if "quantity" in data else data.get("target_qty"))
            if qty_raw is not None:
                try:
                    qty = int(qty_raw)
                    if qty <= 0:
                        logger.error("Non-positive quantity (%s) in Redis authorization key %s", qty_raw, key)
                        return False, None
                    return True, qty
                except (ValueError, TypeError) as exc:
                    logger.error("Invalid quantity type (%s) in Redis authorization key %s: %s", qty_raw, key, exc)
                    return False, None

            return True, None

        # Non-JSON string: require explicit whitelist truthy flag ("1", "true", "yes", etc.)
        if val_str.lower() in TRUE_AUTH_VALUES:
            return True, None

        logger.warning("Unrecognized authorization value in Redis key %s: '%s' (rejecting fail-closed)", key, val_str)
        return False, None
    except Exception as exc:
        logger.critical("Redis authorization lookup failed for key %s: %s", key, exc)
        return False, None


def _redis_truthy_key(r, key: str) -> bool:
    is_enabled, _ = _get_redis_leg_authorization(r, key)
    return is_enabled


def _redis_float(r, key: str) -> float:
    if not r:
        return 0.0
    try:
        val = r.get(key)
        return _to_float(val)
    except Exception:
        return 0.0


def _redis_text(r, key: str) -> str:
    if not r:
        return ""
    try:
        val = r.get(key)
        return str(val or "").strip()
    except Exception:
        return ""


def _save_trailing_state(
    r,
    keys: Dict[str, str],
    max_val: float,
    sl_val: float,
    avg_val: float,
    qty_val: int,
    order_id: str,
    entry_order_id: Optional[str],
    broker: str = "angelone",
) -> bool:
    if not r:
        return False
    try:
        pipe = r.pipeline()
        pipe.set(keys["max"], round(max_val, 2))
        pipe.set(keys["sl"], round(sl_val, 2))
        pipe.set(keys["avg"], round(avg_val, 2))
        pipe.set(keys["qty"], int(qty_val))
        pipe.set(keys["order_id"], str(order_id or ""))
        if entry_order_id:
            pipe.set(keys["entry_order_id"], str(entry_order_id))
        if order_id:
            pipe.sadd(f"fno:{broker}:managed_order_ids:thu", str(order_id))
            pipe.sadd(f"fno:{broker}:managed_order_ids", str(order_id))
        pipe.execute()
        return True
    except Exception as exc:
        logger.critical("Failed to save Redis trailing state: %s", exc)
        return False


def _cleanup_thursday_leg_state(r, keys: Dict[str, str], broker: str = "angelone", symbol: Optional[str] = None) -> None:
    """Clean up Redis trailing state and authorization for a closed Thursday leg."""
    if not r:
        return
    try:
        stored_oid = _redis_text(r, keys.get("order_id")) if keys.get("order_id") else None
        stored_mkt_id = _redis_text(r, keys.get("market_order_id")) if keys.get("market_order_id") else None

        pipe = r.pipeline()
        if keys.get("enabled_canonical"):
            pipe.delete(keys["enabled_canonical"])
        pipe.delete(keys["enabled"])
        pipe.delete(keys["max"])
        pipe.delete(keys["sl"])
        pipe.delete(keys["avg"])
        pipe.delete(keys["qty"])
        pipe.delete(keys["order_id"])
        pipe.delete(keys["entry_order_id"])
        if keys.get("mode"):
            pipe.delete(keys["mode"])
        if keys.get("market_order_id"):
            pipe.delete(keys["market_order_id"])
        if keys.get("exit_filled_qty"):
            pipe.delete(keys["exit_filled_qty"])
        if keys.get("max_short"):
            pipe.delete(keys["max_short"])
        if keys.get("sl_short"):
            pipe.delete(keys["sl_short"])
        if keys.get("avg_short"):
            pipe.delete(keys["avg_short"])

        for oid in (stored_oid, stored_mkt_id):
            if oid:
                pipe.srem(f"fno:{broker}:managed_order_ids:thu", str(oid))
                pipe.srem(f"fno:{broker}:managed_order_ids", str(oid))

        pipe.execute()
        logger.info("Cleared Redis trailing state & authorization for %s", keys["enabled"])
    except Exception as exc:
        logger.error("Failed to clear Redis leg state: %s", exc)

    # Position confirmed flat/closed: clear failure alert cooldown
    try:
        clean_sym = symbol or keys.get("symbol") or keys.get("tradingsymbol") or (keys.get("details") or {}).get("tradingsymbol")
        clear_order_failure_cooldown(r, broker=broker, track=CYCLE_TRACK, symbol=clean_sym)
    except Exception as exc:
        logger.warning("Failed to clear order failure cooldown for closed leg: %s", exc)


def _check_and_reset_orders_live_thu(r, broker: str = "angelone", has_short_positions: bool = False) -> bool:
    """
    If no active Thursday legs remain enabled in Redis, reset ORDERS_LIVE_THU to 0.
    NEVER resets if an active short position exists.
    """
    if has_short_positions:
        logger.critical("Cannot reset ORDERS_LIVE_THU: account has open short derivative positions!")
        return False
    if not r:
        return False
    try:
        pattern = f"fno:{broker}:enabled:thu:*"
        remaining = [k for k in r.scan_iter(pattern, count=20) if _redis_truthy_key(r, k)]
        if not remaining:
            r.set("ORDERS_LIVE_THU", "0")
            r.set("fno:orders_live:thursday", "0")
            logger.info("No active Thursday legs remain in Redis. Reset ORDERS_LIVE_THU = 0.")
            return True
    except Exception as exc:
        logger.error("Failed to check/reset ORDERS_LIVE_THU: %s", exc)
    return False


def _unmanaged_thu_long_positions(
    r,
    positions: List[Dict[str, Any]],
    broker: str = "angelone",
    active_expiry: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Return open LONG broker positions for the active Thursday expiry that have NO
    enabled authorization key in Redis. The exit loop never manages these legs, so
    ORDERS_LIVE_THU must stay active while any of them is open.
    Fail-closed: any lookup error counts the position as unmanaged.
    """
    target_expiry = normalize_expiry(active_expiry, active_expiry=active_expiry) if active_expiry else None
    unmanaged: List[Dict[str, Any]] = []
    for pos in positions or []:
        if _position_quantity(pos) <= 0:
            continue
        symbol = pos.get("tradingsymbol") or pos.get("instrument_token") or "UNKNOWN"
        try:
            if target_expiry:
                raw_expiry = parse_tradingsymbol(symbol).get("expiry")
                if str(raw_expiry or "").upper() in ("", "UNKNOWN", "NONE", "N/A"):
                    pos_expiry = target_expiry
                else:
                    pos_expiry = normalize_expiry(raw_expiry, active_expiry=active_expiry)
                if pos_expiry != target_expiry:
                    continue  # other track / other expiry: not this cycle's concern
            token_key = pos.get("instrument_token") or "NOTOKEN"
            keys = build_trail_redis_keys(broker, symbol, token_key, active_expiry=active_expiry)
            canonical_key = keys.get("enabled_canonical")
            if canonical_key and r.get(canonical_key) is not None:
                enabled, _ = _get_redis_leg_authorization(r, canonical_key)
            else:
                enabled, _ = _get_redis_leg_authorization(r, keys["enabled"])
        except Exception as exc:
            logger.warning("Could not verify authorization for %s; treating as unmanaged: %s", symbol, exc)
            enabled = False
        if not enabled:
            unmanaged.append(pos)
    return unmanaged


THU_FSM_LEG_BY_OPTION_TYPE = {"CE": "call_leg", "PE": "put_leg"}


def _load_thursday_fsm_snapshot() -> Optional[Dict[str, Any]]:
    """Load the ongoing Thursday cycle snapshot (Alpha FSM state). None if absent/unreadable/closed."""
    try:
        from config import THURSDAY_SNAPSHOT_FILE
        if not os.path.exists(THURSDAY_SNAPSHOT_FILE):
            return None
        with open(THURSDAY_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            snap = json.load(f)
        if not isinstance(snap, dict) or str(snap.get("status") or "").lower() != "ongoing":
            return None
        return snap
    except Exception as exc:
        logger.warning("Could not read Thursday FSM snapshot: %s", exc)
        return None


def _fsm_leg_for_symbol(
    snap: Optional[Dict[str, Any]],
    symbol: str,
    active_expiry: Optional[str] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Map an option tradingsymbol to its Alpha FSM leg ('call_leg'/'put_leg') in the ongoing
    Thursday snapshot by strike, option type and expiry. Returns (None, None) if no match.
    """
    if not snap:
        return None, None
    details = parse_tradingsymbol(symbol)
    leg_name = THU_FSM_LEG_BY_OPTION_TYPE.get(details.get("option_type"))
    if not leg_name:
        return None, None
    leg = (snap.get("alpha_fsm") or {}).get(leg_name)
    if not isinstance(leg, dict):
        return None, None
    try:
        if int(_to_float(leg.get("strike"))) != int(_to_float(details.get("strike"))):
            return None, None
        snap_expiry = snap.get("expiry_date")
        if snap_expiry and active_expiry:
            raw_expiry = details.get("expiry")
            if str(raw_expiry or "").upper() in ("", "UNKNOWN", "NONE", "N/A"):
                pos_expiry = normalize_expiry(active_expiry, active_expiry=active_expiry)
            else:
                pos_expiry = normalize_expiry(raw_expiry, active_expiry=active_expiry)
            if pos_expiry != normalize_expiry(snap_expiry, active_expiry=active_expiry):
                return None, None
    except Exception as exc:
        logger.warning("FSM leg match failed for %s: %s", symbol, exc)
        return None, None
    return leg_name, leg


def _fsm_peak_for_symbol(snap: Optional[Dict[str, Any]], symbol: str, active_expiry: Optional[str] = None) -> float:
    """Highest premium the Alpha FSM has observed for this leg (0.0 if unknown)."""
    _, leg = _fsm_leg_for_symbol(snap, symbol, active_expiry=active_expiry)
    return _to_float(leg.get("peak_ltp")) if leg else 0.0


def _fsm_closed_legs_with_open_positions(
    snap: Optional[Dict[str, Any]],
    positions: List[Dict[str, Any]],
    active_expiry: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Reconciliation check: legs the Alpha FSM has already closed (non-ACTIVE) that the broker
    still holds as an open long. Such legs are unprotected and need manual review.
    """
    mismatches: List[Dict[str, Any]] = []
    if not snap:
        return mismatches
    for pos in positions or []:
        if _position_quantity(pos) <= 0:
            continue
        symbol = pos.get("tradingsymbol") or pos.get("instrument_token") or "UNKNOWN"
        leg_name, leg = _fsm_leg_for_symbol(snap, symbol, active_expiry=active_expiry)
        if not leg:
            continue
        fsm_status = str(leg.get("status") or "").upper()
        if fsm_status in ("", "ACTIVE"):
            continue
        mismatches.append({
            "symbol": symbol,
            "fsm_leg": leg_name,
            "fsm_status": fsm_status,
            "fsm_exit_price": leg.get("exit_price"),
            "fsm_exit_reason": leg.get("exit_reason"),
            "broker_quantity": _position_quantity(pos),
        })
    return mismatches


def reconcile_cycle_legs(
    r,
    positions: List[Dict[str, Any]],
    orders: List[Dict[str, Any]],
    broker: str = "angelone",
    cycle: str = "thu",
    tag: str = "THU-SL",
    short_positions: Optional[List[Dict[str, Any]]] = None,
    active_expiry: Optional[Union[str, date, datetime]] = None,
) -> List[Dict[str, Any]]:
    """
    Reconcile all enabled Redis legs for this cycle against live broker positions & open orders.
    If a leg has:
      - NO open short position in broker portfolio, AND
      - NO open long position in the broker portfolio, AND
      - NO open/pending SELL order in the broker order book (i.e. order is confirmed terminal or never placed)
    Then that leg's Redis trailing state & authorization is cleaned up.
    Returns list of cleaned up legs.
    """
    if not r:
        return []
    cleaned = []
    if active_expiry is None:
        active_expiry = get_active_strategy_expiry("3day_strategy" if cycle == "thu" else "5day_strategy")

    pattern = f"fno:{broker}:enabled:{cycle}:*"
    try:
        active_keys = [k for k in r.scan_iter(pattern, count=30) if _redis_truthy_key(r, k)]
    except Exception as exc:
        logger.error("Failed to scan enabled keys for reconciliation: %s", exc)
        return []

    cycle_managed_order_ids = set()
    try:
        raw_ids = r.smembers(f"fno:{broker}:managed_order_ids:thu") or set()
        for raw_id in raw_ids:
            cycle_managed_order_ids.add(
                raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
            )
    except Exception as exc:
        logger.warning("Failed to scan Thursday managed order IDs during reconciliation: %s", exc)

    for enabled_key in active_keys:
        parsed = parse_enabled_key(enabled_key, broker=broker, cycle=cycle)
        if not parsed:
            continue

        leg_expiry = parsed.get("expiry")
        leg_strike = parsed.get("strike")
        leg_opt = parsed.get("option_type")
        leg_symbol = parsed.get("tradingsymbol") or ""
        leg_token = parsed.get("instrument_token") or ""
        eff_expiry = leg_expiry or active_expiry

        # Check 0 (CRITICAL SAFETY): Does a matching SHORT position exist in broker portfolio?
        has_short_pos = False
        for sp in (short_positions or []):
            sp_sym = str(sp.get("tradingsymbol") or "").upper().replace(":", "_").replace("|", "_")
            sp_tok = str(sp.get("instrument_token") or "").upper().replace(":", "_").replace("|", "_")
            sp_details = parse_tradingsymbol(sp.get("tradingsymbol") or "")
            sp_strike = int(sp_details.get("strike") or 0) if sp_details.get("strike") else 0
            sp_opt = sp_details.get("option_type")
            sp_exp = normalize_expiry(sp_details.get("expiry"), active_expiry=eff_expiry) if sp_details.get("expiry") else None
            exp_match = (not leg_expiry) or (sp_exp == leg_expiry)

            if (leg_strike and leg_opt and sp_strike == int(leg_strike) and sp_opt == leg_opt and exp_match) \
               or (leg_token and leg_token != "NOTOKEN" and leg_token == sp_tok) \
               or (leg_symbol and leg_symbol == sp_sym):
                has_short_pos = True
                break

        if has_short_pos:
            logger.critical(
                "Cannot retire leg %s: active SHORT position exists in broker portfolio (net qty < 0)!",
                enabled_key
            )
            continue

        # Check 1: Does an active long position exist for this contract in portfolio?
        has_active_pos = False
        for p in positions:
            p_sym = str(p.get("tradingsymbol") or "").upper().replace(":", "_").replace("|", "_")
            p_tok = str(p.get("instrument_token") or "").upper().replace(":", "_").replace("|", "_")
            p_details = parse_tradingsymbol(p.get("tradingsymbol") or "")
            p_strike = int(p_details.get("strike") or 0) if p_details.get("strike") else 0
            p_opt = p_details.get("option_type")
            p_exp = normalize_expiry(p_details.get("expiry"), active_expiry=eff_expiry) if p_details.get("expiry") else None
            exp_match = (not leg_expiry) or (p_exp == leg_expiry)

            if (leg_strike and leg_opt and p_strike == int(leg_strike) and p_opt == leg_opt and exp_match) \
               or (leg_token and leg_token != "NOTOKEN" and leg_token == p_tok) \
               or (leg_symbol and leg_symbol == p_sym):
                if _position_quantity(p) > 0:
                    has_active_pos = True
                    matched_pos = p
                    break

        own_exit_complete = False
        own_keys = None
        if has_active_pos:
            # Shared contract: if this track already filled its full authorized exit quantity,
            # the remaining broker quantity belongs to the other track -> retire this leg.
            try:
                _auth_ok, auth_qty = _get_redis_leg_authorization(r, enabled_key)
                own_keys = build_trail_redis_keys(
                    broker,
                    matched_pos.get("tradingsymbol") or matched_pos.get("instrument_token") or "UNKNOWN",
                    str(matched_pos.get("instrument_token") or ""),
                    active_expiry=eff_expiry,
                )
                own_filled_qty = int(_redis_float(r, own_keys["exit_filled_qty"]))
                if leg_strike and leg_opt:
                    own_filled_qty = max(own_filled_qty, int(_redis_float(
                        r, build_exited_qty_key("thu", leg_expiry or eff_expiry, leg_strike, leg_opt, broker))))
                own_exit_complete = bool(auth_qty and own_filled_qty >= int(auth_qty))
            except Exception as exc:
                logger.warning("Own-exit check failed for %s: %s", enabled_key, exc)
                own_exit_complete = False
            if not own_exit_complete:
                continue
            logger.info(
                "Thursday exit for %s fully filled (%d/%d); remaining broker qty belongs to another track. Retiring leg.",
                enabled_key, own_filled_qty, int(auth_qty),
            )

        # Check 2: Does an open managed SELL order exist in broker order book for this contract?
        has_open_order = False
        eff_symbol = leg_symbol or f"NIFTY{leg_strike}{leg_opt}"
        eff_token = leg_token or "NOTOKEN"
        leg_keys = build_trail_redis_keys(broker, eff_symbol, eff_token, active_expiry=eff_expiry)
        if own_exit_complete and own_keys:
            leg_keys = own_keys
            eff_symbol = own_keys.get("symbol") or eff_symbol
        stored_order_ids = {
            _redis_text(r, leg_keys.get(key))
            for key in ("order_id", "market_order_id")
            if leg_keys.get(key)
        }
        stored_order_ids.discard("")

        for o in orders:
            order_id = str(o.get("order_id") or o.get("orderid") or "").strip()
            is_managed = (
                order_tag(o).upper() == tag.upper()
                or order_id in stored_order_ids
                or order_id in cycle_managed_order_ids
            )
            if not is_open_sell_order(o) or not is_managed:
                continue
            o_sym = str(o.get("trading_symbol") or o.get("tradingsymbol") or "").upper().replace(":", "_").replace("|", "_")
            o_tok = str(o.get("instrument_token") or o.get("symboltoken") or "").upper().replace(":", "_").replace("|", "_")
            o_details = parse_tradingsymbol(o.get("trading_symbol") or o.get("tradingsymbol") or "")
            o_strike = int(o_details.get("strike") or 0) if o_details.get("strike") else 0
            o_opt = o_details.get("option_type")
            o_exp = normalize_expiry(o_details.get("expiry"), active_expiry=eff_expiry) if o_details.get("expiry") else None
            exp_match = (not leg_expiry) or (o_exp == leg_expiry)

            if (leg_strike and leg_opt and o_strike == int(leg_strike) and o_opt == leg_opt and exp_match) \
               or (leg_token and leg_token != "NOTOKEN" and leg_token == o_tok) \
               or (leg_symbol and leg_symbol == o_sym) \
               or (order_id in stored_order_ids):
                has_open_order = True
                break

        if has_open_order:
            continue

        # Check 3: Was this authorization actually executed by the manager (filled managed SELL)?
        # If not, the Alpha exit never reached the broker (e.g. strike/symbol mismatch or manual close).
        exit_executed = own_exit_complete
        for o in orders:
            order_id = str(o.get("order_id") or o.get("orderid") or "").strip()
            is_managed = (
                order_tag(o).upper() == tag.upper()
                or order_id in stored_order_ids
                or order_id in cycle_managed_order_ids
            )
            if not is_managed or str(o.get("transaction_type") or o.get("transactiontype") or "").upper() != "SELL":
                continue
            if normalize_order_status(o.get("status") or o.get("orderstatus")) not in TERMINAL_FILLED_STATUSES:
                continue
            o_details = parse_tradingsymbol(o.get("trading_symbol") or o.get("tradingsymbol") or "")
            o_strike = int(o_details.get("strike") or 0) if o_details.get("strike") else 0
            o_exp = normalize_expiry(o_details.get("expiry"), active_expiry=eff_expiry) if o_details.get("expiry") else None
            if (leg_strike and o_strike == int(leg_strike) and o_details.get("option_type") == leg_opt
                    and ((not leg_expiry) or o_exp == leg_expiry)) or order_id in stored_order_ids:
                exit_executed = True
                break

        if not exit_executed:
            reason = (
                f"Alpha exit authorization {enabled_key} is being retired WITHOUT any filled managed SELL: "
                f"no open broker position matched {leg_strike} {leg_opt} (expiry {leg_expiry}). "
                "Check for strike/symbol mismatch or a manual close."
            )
            logger.critical(reason)
            dispatch_order_failure_discord(
                broker=broker,
                action="SELL_AUTH_UNMATCHED",
                track=CYCLE_TRACK,
                symbol=f"{leg_strike}{leg_opt}",
                quantity=0,
                error_reason=reason,
                details={"enabled_key": enabled_key, "open_positions": [p.get("tradingsymbol") for p in positions]},
                redis_client=r,
                cooldown_seconds=3600,
            )

        # All checks passed: No short position, long position is 0, AND open order is absent/terminal.
        _cleanup_thursday_leg_state(r, leg_keys, broker=broker, symbol=eff_symbol)

        # Comprehensive trailing & detailed keys purge restricted strictly to this leg AND expiry
        try:
            r.delete(enabled_key)
            trail_pattern = f"fno:{broker}:trail:{cycle}:*:{leg_strike}:{leg_opt}:*"
            detailed_pattern = f"fno:{broker}:enabled:{cycle}:*:{leg_strike}:{leg_opt}:*"
            keys_to_purge = set()
            for k in r.scan_iter(trail_pattern, count=30):
                k_parts = k.split(":")
                # Format: fno:{broker}:trail:{cycle}:{underlying}:{expiry}:{strike}:{opt}:{symbol}:{token}:{field}
                if len(k_parts) >= 8:
                    k_exp = normalize_expiry(k_parts[5], active_expiry=eff_expiry)
                    if leg_expiry and k_exp != leg_expiry:
                        continue  # Belongs to another expiry; preserve it!
                keys_to_purge.add(k)

            for k in r.scan_iter(detailed_pattern, count=30):
                k_parts = k.split(":")
                if len(k_parts) >= 8:
                    k_exp = normalize_expiry(k_parts[5], active_expiry=eff_expiry)
                    if leg_expiry and k_exp != leg_expiry:
                        continue  # Belongs to another expiry; preserve it!
                keys_to_purge.add(k)

            if leg_symbol:
                short_pattern = f"fno_trail:{broker}:{cycle}:{leg_symbol}:*"
                for k in r.scan_iter(short_pattern, count=20):
                    keys_to_purge.add(k)

            if keys_to_purge:
                p_pipe = r.pipeline()
                for k in keys_to_purge:
                    p_pipe.delete(k)
                p_pipe.execute()
                logger.info("Purged %d Redis trailing/detailed keys for %s (expiry: %s)", len(keys_to_purge), enabled_key, leg_expiry)
        except Exception as exc:
            logger.error("Failed to purge trailing keys for %s: %s", enabled_key, exc)

        logger.info(
            "Leg %s confirmed closed (no position, no open %s orders). Retired Redis state.",
            enabled_key, tag
        )
        cleaned.append({"key": enabled_key, "symbol": leg_symbol, "strike": leg_strike, "option_type": leg_opt, "expiry": leg_expiry, "exit_executed": exit_executed})

    return cleaned


# --------------------------------------------------------------------------
# Main Thursday Exit Manager
# --------------------------------------------------------------------------
def manage_thursday_exit_orders(
    token: str,
    positions: List[Dict[str, Any]],
    orders: List[Dict[str, Any]],
    broker: str = DEFAULT_BROKER,
    target_lots: int = 1,
    lot_size: int = DEFAULT_LOT_SIZE,
    trail_pct: float = DEFAULT_TRAIL_PCT,
    initial_stop_pct: float = DEFAULT_INITIAL_STOP_PCT,
    activation_pct: float = DEFAULT_ACTIVATION_PCT,
    min_ratchet_step: float = DEFAULT_MIN_RATCHET_STEP,
    execute: bool = False,
    auto_cancel_orphans: bool = True,
    product: Optional[str] = None,
    tag: str = DEFAULT_TAG,
    force: bool = False,
    redis_client: Any = None,
    short_positions: Optional[List[Dict[str, Any]]] = None,
    active_expiry: Optional[str] = None,
    abort_event: Optional[Any] = None,
    exit_immediately: bool = False,
) -> Dict[str, Any]:
    broker = (broker or DEFAULT_BROKER).lower().strip()
    r = redis_client if redis_client is not None else (None if token == MOCK_TOKEN else get_redis_client())
    actions: List[Dict[str, Any]] = []
    alerts: List[Dict[str, Any]] = []
    cycle_target_qty = target_lots * lot_size
    if active_expiry is None:
        active_expiry = get_active_strategy_expiry("3day_strategy")

    # Authoritative set of Thursday managed order IDs and other-cycle order IDs from Redis
    managed_order_ids_in_redis = set()
    other_cycle_order_ids_in_redis = set()
    if r:
        try:
            for k in r.scan_iter(f"fno:{broker}:trail:thu:*:order_id", count=100):
                val = _redis_text(r, k)
                if val:
                    managed_order_ids_in_redis.add(str(val))
            for k in r.scan_iter(f"fno:{broker}:trail:thu:*:market_order_id", count=100):
                val = _redis_text(r, k)
                if val:
                    managed_order_ids_in_redis.add(str(val))
            set_ids = r.smembers(f"fno:{broker}:managed_order_ids:thu") or []
            for sid in set_ids:
                s_text = sid.decode("utf-8") if isinstance(sid, bytes) else str(sid)
                if s_text:
                    managed_order_ids_in_redis.add(s_text)

            # Symmetrically scan Tuesday (other cycle) orders on the same broker
            for k in r.scan_iter(f"fno:{broker}:trail:tue:*:order_id", count=100):
                val = _redis_text(r, k)
                if val:
                    other_cycle_order_ids_in_redis.add(str(val))
            for k in r.scan_iter(f"fno:{broker}:trail:tue:*:market_order_id", count=100):
                val = _redis_text(r, k)
                if val:
                    other_cycle_order_ids_in_redis.add(str(val))
            set_ids_tue = r.smembers(f"fno:{broker}:managed_order_ids:tue") or []
            for sid in set_ids_tue:
                s_text = sid.decode("utf-8") if isinstance(sid, bytes) else str(sid)
                if s_text and s_text not in managed_order_ids_in_redis:
                    other_cycle_order_ids_in_redis.add(s_text)

            # Legacy fallback
            set_ids_legacy = r.smembers(f"fno:{broker}:managed_order_ids") or []
            for sid in set_ids_legacy:
                s_text = sid.decode("utf-8") if isinstance(sid, bytes) else str(sid)
                if s_text and s_text not in other_cycle_order_ids_in_redis:
                    managed_order_ids_in_redis.add(s_text)
        except Exception as exc:
            logger.warning("Failed to scan active Thursday managed order IDs from Redis: %s", exc)

    # 0. Check for unexpected short derivative positions
    if short_positions:
        short_symbols = [p.get("tradingsymbol") or p.get("instrument_token") for p in short_positions]
        logger.critical("Unexpected SHORT derivative positions detected in account: %s! Manual intervention required.", short_symbols)
        alerts.append({
            "severity": "CRITICAL",
            "status": "unexpected_short_positions",
            "symbols": short_symbols,
            "short_positions": short_positions,
            "reason": "Script only manages LONG options exit orders. Short derivative positions detected in account.",
        })

    # 1. Filter authorized positions specifically for Thursday cycle
    authorized: List[Dict[str, Any]] = []
    for pos in positions:
        symbol = pos.get("tradingsymbol") or pos.get("instrument_token") or "UNKNOWN"
        token_key = pos.get("instrument_token") or "NOTOKEN"
        keys = build_trail_redis_keys(broker, symbol, token_key, active_expiry=active_expiry)

        canonical_key = keys.get("enabled_canonical")
        canonical_val = r.get(canonical_key) if (r and canonical_key) else None

        if canonical_val is not None:
            # Canonical key is present in Redis and is authoritative (no fall-through)
            enabled, redis_qty = _get_redis_leg_authorization(r, canonical_key)
        else:
            # Migration fallback: canonical key not set; check detailed key
            enabled, redis_qty = _get_redis_leg_authorization(r, keys["enabled"])
        if force or (token == MOCK_TOKEN) or enabled:
            pos_copy = dict(pos)
            if redis_qty and redis_qty > 0:
                pos_copy["_redis_target_qty"] = redis_qty
            authorized.append(pos_copy)
            # Refresh TTL safeguard: While position is active & authorized, keep key alive (7 days)
            if r and enabled:
                try:
                    if canonical_val is not None and canonical_key:
                        r.expire(canonical_key, 604800)
                    elif keys.get("enabled"):
                        r.expire(keys["enabled"], 604800)
                except Exception:
                    pass
        else:
            reason = "missing/disabled Redis authorization key for Thursday cycle"
            actions.append({
                "symbol": symbol, "instrument_token": token_key, "broker": broker,
                "status": "authorization_skipped", "reason": reason, "required_key": keys["enabled"]
            })
            logger.info("[%s] skipped: %s (%s)", symbol, reason, keys["enabled"])

    # 2. Check for orphan THU-SL managed orders
    orphan_managed_orders: List[Dict[str, Any]] = []
    for order in orders:
        order_id = str(order.get("order_id") or order.get("orderid") or "").strip()
        is_managed = (order_id in managed_order_ids_in_redis or order_tag(order).upper() == tag.upper())
        if not is_open_sell_order(order) or not is_managed:
            continue
        matched_positions = [p for p in positions if order_matches_position(order, p)]
        symbol = order.get("trading_symbol") or order.get("tradingsymbol")

        if not matched_positions:
            if abort_event is not None and abort_event.is_set():
                logger.critical("Lock lost before cancelling orphan orders. Aborting for safety.")
                return {
                    "cycle": CYCLE_NAME, "positions": len(positions), "authorized_positions": 0,
                    "actions": actions, "orphan_managed_orders": orphan_managed_orders,
                    "alerts": alerts, "fatal_error": "lock_lost", "dry_run": not execute, "broker": broker
                }
            item = {
                "order_id": order_id, "symbol": symbol, "instrument_token": order.get("instrument_token"),
                "tag": tag, "reason": "no active matching long position in portfolio (position closed or filled)",
            }
            if execute and auto_cancel_orphans and token != MOCK_TOKEN:
                if not force:
                    is_open, open_reason = is_market_hours_ist()
                    if not is_open:
                        logger.warning("[%s] Market closed (%s) before orphan cancellation. Aborting mutation.", symbol, open_reason)
                        item["action_taken"] = "cancel_aborted_market_closed"
                        item["is_terminal"] = False
                        orphan_managed_orders.append(item)
                        alerts.append({"severity": "CRITICAL", **item})
                        return {
                            "cycle": CYCLE_NAME, "positions": len(positions), "authorized_positions": 0,
                            "actions": actions, "orphan_managed_orders": orphan_managed_orders,
                            "alerts": alerts, "fatal_error": "market_closed", "dry_run": not execute, "broker": broker
                        }

                if abort_event is not None and abort_event.is_set():
                    logger.critical("[%s] Distributed lock lost immediately before cancelling orphan order %s! Aborting.", symbol, order_id)
                    item["action_taken"] = "cancel_aborted_lock_lost"
                    item["is_terminal"] = False
                    orphan_managed_orders.append(item)
                    alerts.append({"severity": "CRITICAL", **item})
                    return {
                        "cycle": CYCLE_NAME, "positions": len(positions), "authorized_positions": 0,
                        "actions": actions, "orphan_managed_orders": orphan_managed_orders,
                        "alerts": alerts, "fatal_error": "lock_lost", "dry_run": not execute, "broker": broker
                    }
                cancel_res = cancel_order(token, order_id, broker=broker)
                cancel_ok = bool(cancel_res.get("success"))
                item["action_taken"] = "cancel_requested" if cancel_ok else "cancel_failed"
                item["cancel_result"] = cancel_res
                item["is_terminal"] = False
                if cancel_ok:
                    logger.warning(
                        "[%s] Orphan %s order %s cancellation requested; keeping state active until terminal confirmation in order book",
                        symbol, tag, order_id
                    )
                else:
                    logger.critical(
                        "[%s] Orphan %s order %s cancellation FAILED: %s. Keeping Redis state and ORDERS_LIVE_THU active for retry.",
                        symbol, tag, order_id, cancel_res.get("error")
                    )
            else:
                item["action_required"] = "cancel this orphan THU-SL order manually"
                item["is_terminal"] = False
                logger.critical("Orphan managed SELL order %s for %s: position is closed", order_id, symbol)
            orphan_managed_orders.append(item)
            alerts.append({"severity": "CRITICAL", **item})

    if execute and token != MOCK_TOKEN and r is None:
        message = "Redis is unavailable; live Thursday order management is blocked (fail-closed)."
        logger.critical(message)
        alerts.append({"severity": "CRITICAL", "reason": message})
        return {
            "cycle": CYCLE_NAME, "positions": len(positions), "authorized_positions": 0,
            "actions": actions, "orphan_managed_orders": orphan_managed_orders,
            "alerts": alerts, "fatal_error": "redis_unavailable", "dry_run": False, "broker": broker
        }

    # 3. Process each authorized Thursday position
    for pos in authorized:
        if abort_event is not None and abort_event.is_set():
            logger.critical("Lock heartbeat lost mid-execution. Aborting Thursday order loop.")
            return {
                "cycle": CYCLE_NAME, "positions": len(positions), "authorized_positions": len(authorized),
                "actions": actions, "orphan_managed_orders": orphan_managed_orders,
                "alerts": alerts, "fatal_error": "lock_lost", "dry_run": not execute, "broker": broker
            }
        symbol = pos.get("tradingsymbol") or pos.get("instrument_token") or "UNKNOWN"
        token_key = str(pos.get("instrument_token") or "")
        keys = build_trail_redis_keys(broker, symbol, token_key, active_expiry=active_expiry)
        total_broker_qty = int(_to_float(pos.get("quantity")))
        if total_broker_qty <= 0:
            _cleanup_thursday_leg_state(r, keys, broker=broker, symbol=symbol)
            _check_and_reset_orders_live_thu(r, broker=broker)
            continue

        # Immediate Square-Off Mode check
        is_immediate = bool(exit_immediately)
        if not is_immediate and r:
            is_immediate = (_redis_text(r, keys["mode"]).lower() == "immediate")

        # Read Redis state
        try:
            old_max = _redis_float(r, keys["max"]) if r else 0.0
            old_sl = _redis_float(r, keys["sl"]) if r else 0.0
            old_avg = _redis_float(r, keys["avg"]) if r else 0.0
            old_qty = int(_redis_float(r, keys["qty"])) if r else 0
            old_entry_id = _redis_text(r, keys["entry_order_id"])
            stored_order_id = _redis_text(r, keys["order_id"])
        except Exception as exc:
            item = {
                "symbol": symbol, "instrument_token": token_key, "status": "redis_state_read_failed",
                "reason": f"Redis state read failed: {exc}", "redis_keys": keys
            }
            actions.append(item)
            alerts.append({"severity": "CRITICAL", **item})
            continue

        matching_sells = matching_open_sell_orders(pos, orders)
        managed_sells = [
            o for o in matching_sells
            if (stored_order_id and str(o.get("order_id") or o.get("orderid") or "") == stored_order_id)
            or str(o.get("order_id") or o.get("orderid") or "") in managed_order_ids_in_redis
            or order_tag(o).upper() == tag.upper()
        ]
        other_cycle_sells = [
            o for o in matching_sells
            if o not in managed_sells and (
                order_tag(o).upper() in OTHER_CYCLE_TAGS
                or str(o.get("order_id") or o.get("orderid") or "") in other_cycle_order_ids_in_redis
            )
        ]
        other_cycle_sells_all = [
            o for o in orders
            if order_matches_position(o, pos)
            and str(o.get("transaction_type") or o.get("transactiontype") or "").upper() == "SELL"
            and (
                order_tag(o).upper() in OTHER_CYCLE_TAGS
                or str(o.get("order_id") or o.get("orderid") or "") in other_cycle_order_ids_in_redis
            )
        ]
        manual_sells = [
            o for o in matching_sells
            if o not in managed_sells and o not in other_cycle_sells
        ]

        if manual_sells:
            item = {
                "symbol": symbol, "instrument_token": token_key, "status": "manual_sell_conflict",
                "reason": f"an untagged/manual open SELL order exists in {broker.title()}; script leaves it untouched and will not create a duplicate",
                "manual_order_ids": [o.get("order_id") or o.get("orderid") for o in manual_sells], "redis_keys": keys,
                "mode": "immediate" if is_immediate else "trailing"
            }
            actions.append(item)
            alerts.append({"severity": "HIGH", **item})
            logger.error("[%s] manual SELL conflict; leaving position untouched", symbol)
            continue

        if len(managed_sells) > 1:
            item = {
                "symbol": symbol, "instrument_token": token_key, "status": "multiple_managed_sell_conflict",
                "reason": f"multiple open orders match this contract for {tag}; script cannot choose safely",
                "managed_order_ids": [o.get("order_id") or o.get("orderid") for o in managed_sells], "redis_keys": keys
            }
            actions.append(item)
            alerts.append({"severity": "CRITICAL", **item})
            logger.critical("[%s] multiple %s SELL orders exist; stopping for safety", symbol, tag)
            continue

        # Reserve pending quantities from other cycles (e.g. TUE-SL)
        other_pending_qty = sum(int(order_pending_quantity(o)) for o in other_cycle_sells)
        available_for_thursday = max(0, total_broker_qty - other_pending_qty)

        if available_for_thursday <= 0:
            item = {
                "symbol": symbol, "instrument_token": token_key, "status": "quantity_exhausted_by_other_cycle",
                "reason": f"Total position ({total_broker_qty}) is already fully covered by other cycle orders ({other_pending_qty})",
                "other_cycle_order_ids": [o.get("order_id") or o.get("orderid") for o in other_cycle_sells], "redis_keys": keys
            }
            actions.append(item)
            alerts.append({"severity": "CRITICAL", **item})
            logger.critical("[%s] No available quantity for Thursday: total=%d, other_pending=%d", symbol, total_broker_qty, other_pending_qty)
            continue

        # Target quantity slicing: use authoritative Redis quantity if present, otherwise cycle_target_qty
        leg_target_qty = pos.get("_redis_target_qty") or cycle_target_qty
        managed_qty = min(available_for_thursday, leg_target_qty)
        if available_for_thursday < leg_target_qty:
            logger.warning("[%s] Thursday target qty %d reduced to %d due to other cycle orders (reserved: %d)",
                           symbol, leg_target_qty, managed_qty, other_pending_qty)

        existing = managed_sells[0] if managed_sells else None
        existing_order_id = str((existing or {}).get("order_id") or (existing or {}).get("orderid") or "")

        # Own-exit accounting for shared broker contracts: the net position may also hold the
        # other strategy track's lot. Count this track's filled managed SELLs (today's order book)
        # and the quantity persisted from earlier days (broker order books reset overnight), and
        # never exit beyond this track's authorized quantity.
        own_filled_today = 0
        for o in orders:
            if not order_matches_position(o, pos):
                continue
            if str(o.get("transaction_type") or o.get("transactiontype") or "").upper() != "SELL":
                continue
            o_id = str(o.get("order_id") or o.get("orderid") or "")
            if not (order_tag(o).upper() == tag.upper() or o_id in managed_order_ids_in_redis
                    or (stored_order_id and o_id == stored_order_id)):
                continue
            o_status = normalize_order_status(o.get("status") or o.get("orderstatus"))
            if o_status in TERMINAL_FILLED_STATUSES:
                own_filled_today += order_filled_quantity(o)
            else:
                own_filled_today += int(max(0.0, _to_float(o.get("filled_quantity") or o.get("filledshares"))))
        persisted_filled = int(_redis_float(r, keys["exit_filled_qty"])) if r else 0
        contract = keys.get("details") or {}
        exited_key = None
        try:
            if contract.get("strike") and contract.get("option_type"):
                exited_key = build_exited_qty_key("thu", keys.get("expiry_iso"), contract["strike"], contract["option_type"], broker)
        except (TypeError, ValueError):
            exited_key = None
        canonical_exited = int(_redis_float(r, exited_key)) if (r and exited_key) else 0
        own_filled = max(own_filled_today, persisted_filled, canonical_exited)
        if r and execute and own_filled_today > persisted_filled:
            try:
                r.set(keys["exit_filled_qty"], str(own_filled_today), ex=604800)
            except Exception as exc:
                logger.warning("[%s] Failed to persist Thursday filled exit qty: %s", symbol, exc)
        if r and execute and exited_key and own_filled_today > canonical_exited:
            try:
                r.set(exited_key, str(own_filled_today), ex=EXITED_QTY_TTL_SEC)
            except Exception as exc:
                logger.warning("[%s] Failed to persist Thursday exited qty: %s", symbol, exc)
        if existing is None and own_filled > 0:
            remaining_own = max(0, leg_target_qty - own_filled)
            if remaining_own <= 0:
                item = {
                    "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                    "instrument_token": token_key, "tag": tag, "quantity": 0,
                    "total_broker_qty": total_broker_qty,
                    "status": "market_order_already_filled" if is_immediate else "own_exit_already_filled",
                    "own_filled_qty": own_filled, "mode": "immediate" if is_immediate else "trailing",
                    "success": True,
                    "reason": f"Thursday exit already filled its authorized quantity ({own_filled}/{leg_target_qty}); "
                              f"remaining broker qty {total_broker_qty} is not owned by this track",
                }
                actions.append(item)
                logger.info("[%s] %s exit already filled (%d/%d); not exiting remaining broker qty %d",
                            symbol, "Thursday", own_filled, leg_target_qty, total_broker_qty)
                continue
            if remaining_own < managed_qty:
                logger.info("[%s] %s exit qty reduced %d -> %d (own filled %d/%d)",
                            symbol, "Thursday", managed_qty, remaining_own, own_filled, leg_target_qty)
                managed_qty = remaining_own

        # Shared broker contract: reserve the quantity the Tuesday track still owns (not only
        # its pending SELL orders). If the net position is smaller than both tracks' remaining
        # holdings, part of it was sold outside the managers (typically a manual square-off) and
        # the owner cannot be determined from the broker's net position -> fail closed and alert.
        other_remaining = 0
        try:
            if contract.get("strike") and contract.get("option_type"):
                other_filled_today = 0
                for o in other_cycle_sells_all:
                    o_status = normalize_order_status(o.get("status") or o.get("orderstatus"))
                    if o_status in TERMINAL_FILLED_STATUSES:
                        other_filled_today += order_filled_quantity(o)
                    else:
                        other_filled_today += int(max(0.0, _to_float(o.get("filled_quantity") or o.get("filledshares"))))
                from common.unified_strategy_store import get_track_remaining_owned_qty
                other_remaining = get_track_remaining_owned_qty(
                    "5day_strategy", int(float(contract["strike"])), str(contract["option_type"]),
                    keys.get("expiry_iso"), broker, redis_client=r, filled_today=other_filled_today,
                )
        except Exception as exc:
            logger.warning("[%s] Could not compute Tuesday track owned qty: %s", symbol, exc)
            other_remaining = 0
        if other_remaining > 0:
            own_remaining = max(0, leg_target_qty - own_filled)
            if total_broker_qty < own_remaining + other_remaining:
                reason = (
                    f"Shared contract {symbol}: broker net qty {total_broker_qty} is below Thursday remaining "
                    f"{own_remaining} + Tuesday remaining {other_remaining}. Part of the position was sold "
                    "outside the exit managers (manual square-off?) and its owner is unknown. No order placed. "
                    "Record the manual exit with scripts/mark_manual_exit.py for the track you sold."
                )
                logger.critical("[%s] %s", symbol, reason)
                dispatch_order_failure_discord(
                    broker=broker,
                    action="SHARED_CONTRACT_SHORTFALL",
                    track=CYCLE_TRACK,
                    symbol=symbol,
                    quantity=own_remaining,
                    error_reason=reason,
                    details={"broker_net_qty": total_broker_qty, "own_remaining": own_remaining,
                             "other_track_remaining": other_remaining},
                    redis_client=r,
                    cooldown_seconds=3600,
                )
                item = {
                    "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                    "instrument_token": token_key, "tag": tag, "quantity": 0,
                    "total_broker_qty": total_broker_qty, "status": "shared_contract_shortfall",
                    "own_remaining": own_remaining, "other_track_remaining": other_remaining,
                    "reason": reason, "success": False,
                }
                actions.append(item)
                alerts.append({"severity": "CRITICAL", **item})
                continue
            cap_for_this_track = total_broker_qty - other_remaining
            if cap_for_this_track < managed_qty:
                logger.info("[%s] %s exit qty capped %d -> %d (reserving %d for Tuesday)",
                            symbol, "Thursday", managed_qty, cap_for_this_track, other_remaining)
                managed_qty = cap_for_this_track

        entry_details = {}
        symbol_details = parse_tradingsymbol(symbol)
        if symbol_details.get("strike") and symbol_details.get("option_type"):
            try:
                from common.unified_strategy_store import get_active_leg_entry_details
                entry_details = get_active_leg_entry_details(
                    CYCLE_TRACK,
                    int(symbol_details["strike"]),
                    str(symbol_details["option_type"]),
                    expiry=symbol_details.get("expiry") or active_expiry,
                )
            except Exception as exc:
                logger.warning("[%s] Could not load Thursday cycle BUY attribution: %s", symbol, exc)

        if entry_details.get("ambiguous_entry_attribution"):
            item = {
                "symbol": symbol,
                "instrument_token": token_key,
                "status": "entry_order_attribution_ambiguous",
                "reason": "Tuesday and Thursday share this broker contract, but the Thursday cycle has no persisted BUY order ID; refusing to infer ownership from the latest BUY.",
                "redis_keys": keys,
            }
            actions.append(item)
            alerts.append({"severity": "CRITICAL", **item})
            logger.critical("[%s] Cannot safely attribute BUY entry to Thursday cycle", symbol)
            continue

        persisted_entry_id = str(entry_details.get("entry_order_id") or "")
        if persisted_entry_id:
            entry_order_id = persisted_entry_id
            entry_order = next(
                (
                    o for o in orders
                    if str(o.get("order_id") or o.get("orderid") or "") == persisted_entry_id
                    and str(o.get("transaction_type") or o.get("transactiontype") or "").upper() == "BUY"
                ),
                None,
            )
            entry_id_source = "active_cycle"
        else:
            entry_order_id, entry_order, entry_id_source = latest_filled_buy_order(pos, orders)

        cycle_entry_price = _to_float(entry_details.get("entry_price"))
        if cycle_entry_price <= 0 and entry_order:
            cycle_entry_price = _to_float(
                entry_order.get("average_price") or entry_order.get("averageprice") or entry_order.get("avgprice")
            )
        if entry_details.get("same_broker_contract_overlap") and cycle_entry_price <= 0:
            item = {
                "symbol": symbol,
                "instrument_token": token_key,
                "status": "entry_price_attribution_missing",
                "reason": "The shared broker position has no track-specific Thursday fill price; refusing to use the net position average for trailing calculations.",
                "redis_keys": keys,
            }
            actions.append(item)
            alerts.append({"severity": "CRITICAL", **item})
            logger.critical("[%s] Cannot safely attribute entry price to Thursday cycle", symbol)
            continue

        lifecycle_changed = bool(old_entry_id and entry_order_id and old_entry_id != entry_order_id)
        if lifecycle_changed and existing:
            item = {
                "symbol": symbol, "instrument_token": token_key, "status": "stale_managed_order_after_new_entry",
                "reason": f"new BUY entry detected ({entry_order_id}) but old {tag} SELL {existing_order_id} remains open",
                "managed_order_id": existing_order_id, "redis_keys": keys
            }
            actions.append(item)
            alerts.append({"severity": "CRITICAL", **item})
            logger.critical("[%s] stale %s SELL order %s after new entry", symbol, tag, existing_order_id)
            continue

        # Immediate Square-Off Mode
        if is_immediate:
            if execute and r:
                try:
                    r.set(keys["mode"], "immediate")
                except Exception:
                    pass

            product_for_position = get_exit_product(pos, broker=broker, cli_product=product)
            existing_type = str((existing or {}).get("order_type") or (existing or {}).get("ordertype") or "").upper()

            # Case A: Market exit order already in-flight in broker order book
            if existing and existing_type == "MARKET":
                base = {
                    "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                    "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                    "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                    "status": "market_order_in_flight", "order_id": existing_order_id, "mode": "immediate",
                    "reason": f"Market exit order {existing_order_id} is already in-flight",
                    "product": product_for_position,
                }
                actions.append(base)
                logger.info("[%s] Market exit order %s is already in-flight", symbol, existing_order_id)
                continue

            # Case B: Managed SL-M order exists; must cancel before submitting market sell order
            if existing:
                if not execute:
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_for_immediate_exit", "dry_run": True, "action_needed": True,
                        "order_id": existing_order_id, "mode": "immediate", "product": product_for_position,
                        "reason": f"Existing {existing_type} order {existing_order_id} must be cancelled before immediate market exit",
                    }
                    actions.append(base)
                    continue

                if token == MOCK_TOKEN:
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "mock_execute_blocked", "reason": "mock token cannot place real orders",
                        "mode": "immediate", "success": False,
                    }
                    actions.append(base)
                    continue

                if abort_event is not None and abort_event.is_set():
                    logger.critical("[%s] Distributed lock lost immediately before cancelling SL-M order %s! Aborting.", symbol, existing_order_id)
                    base = {"status": "lock_lost", "error": "Distributed lock lost before broker mutation", "success": False, "mode": "immediate"}
                    actions.append(base)
                    alerts.append({"severity": "CRITICAL", **base})
                    return {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker,
                        "positions": len(positions), "authorized_positions": len(authorized),
                        "actions": actions, "orphan_managed_orders": orphan_managed_orders,
                        "alerts": alerts, "fatal_error": "lock_lost", "dry_run": not execute,
                    }

                if not force:
                    is_open, open_reason = is_market_hours_ist()
                    if not is_open:
                        logger.warning("[%s] Market closed (%s) before cancelling SL-M order %s. Aborting.", symbol, open_reason, existing_order_id)
                        base = {"status": "market_closed", "error": f"Market closed before broker mutation: {open_reason}", "success": False, "mode": "immediate"}
                        actions.append(base)
                        alerts.append({"severity": "CRITICAL", **base})
                        return {
                            "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker,
                            "positions": len(positions), "authorized_positions": len(authorized),
                            "actions": actions, "orphan_managed_orders": orphan_managed_orders,
                            "alerts": alerts, "fatal_error": "market_closed", "dry_run": not execute,
                        }

                cancel_res = cancel_order(token, existing_order_id, broker=broker, variety="STOPLOSS")
                if not cancel_res.get("success"):
                    cancel_err = cancel_res.get("error")
                    dispatch_order_failure_discord(
                        broker=broker,
                        action="SELL_SLM_CANCEL_FAILED",
                        track=CYCLE_TRACK,
                        symbol=symbol,
                        quantity=managed_qty,
                        error_reason=f"Failed to cancel existing SL-M order {existing_order_id} before market exit: {cancel_err}",
                        details={"order_id": existing_order_id, "error": cancel_err, "result": cancel_res},
                        redis_client=r,
                        cooldown_seconds=600,
                    )
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_failed", "order_id": existing_order_id, "mode": "immediate",
                        "error": cancel_err, "success": False,
                    }
                    actions.append(base)
                    alerts.append({"severity": "CRITICAL", **base})
                    continue

                # Verify terminal status in orderbook
                time.sleep(0.5)
                fresh_orders = fetch_order_book(token, broker=broker)
                fresh_existing = next((o for o in fresh_orders if str(o.get("order_id") or o.get("orderid") or "") == existing_order_id), None)
                if fresh_existing is None:
                    missing_msg = f"SL-M order {existing_order_id} missing from follow-up order book; deferring market order until terminal confirmation"
                    logger.warning(
                        "[%s] SL-M %s missing from follow-up order book after cancel. Retaining Redis state and deferring market order.",
                        symbol, existing_order_id
                    )
                    dispatch_order_failure_discord(
                        broker=broker,
                        action="IMMEDIATE_EXIT_BLOCKED",
                        track=CYCLE_TRACK,
                        symbol=symbol,
                        quantity=managed_qty,
                        error_reason=missing_msg,
                        details={"order_id": existing_order_id, "status": "cancel_slm_missing"},
                        redis_client=r,
                        cooldown_seconds=600,
                    )
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_missing", "order_id": existing_order_id, "mode": "immediate",
                        "reason": missing_msg,
                        "success": False,
                    }
                    actions.append(base)
                    alerts.append({"severity": "HIGH", **base})
                    continue

                fresh_status = normalize_order_status(fresh_existing.get("status"))
                if fresh_status in OPEN_ORDER_STATUSES:
                    logger.warning("[%s] SL-M %s cancel requested but still pending (%s). Next cron run will place market order.", symbol, existing_order_id, fresh_status)
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_pending", "order_id": existing_order_id, "mode": "immediate",
                        "reason": f"SL-M cancellation in-flight ({fresh_status}); will place market order on next run",
                        "success": True,
                    }
                    actions.append(base)
                    continue
                elif fresh_status in TERMINAL_FILLED_STATUSES:
                    logger.info("[%s] SL-M %s filled before cancellation. Position flat.", symbol, existing_order_id)
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "slm_already_filled", "order_id": existing_order_id, "mode": "immediate", "success": True,
                    }
                    actions.append(base)
                    continue
                elif fresh_status not in TERMINAL_CANCELLED_STATUSES:
                    unknown_msg = f"SL-M order {existing_order_id} has unrecognized status '{fresh_status}'; deferring market order until confirmed terminal"
                    logger.warning(
                        "[%s] SL-M %s has unknown/unrecognized status '%s'. Retaining Redis state and deferring market order.",
                        symbol, existing_order_id, fresh_status
                    )
                    dispatch_order_failure_discord(
                        broker=broker,
                        action="IMMEDIATE_EXIT_BLOCKED",
                        track=CYCLE_TRACK,
                        symbol=symbol,
                        quantity=managed_qty,
                        error_reason=unknown_msg,
                        details={"order_id": existing_order_id, "status": "cancel_slm_unknown_status", "broker_status": fresh_status},
                        redis_client=r,
                        cooldown_seconds=600,
                    )
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_unknown_status", "order_id": existing_order_id, "mode": "immediate",
                        "reason": unknown_msg,
                        "success": False,
                    }
                    actions.append(base)
                    alerts.append({"severity": "HIGH", **base})
                    continue

                # Re-fetch positions to determine exact remaining long quantity
                fresh_positions, _ = parse_positions(fetch_positions(token, broker=broker))
                fresh_pos = next((p for p in fresh_positions if order_matches_position({"instrument_token": token_key, "tradingsymbol": symbol}, p)), None)
                remaining_qty = int(_position_quantity(fresh_pos)) if fresh_pos else 0
                if remaining_qty <= 0:
                    logger.info("[%s] Position flat after SL-M cancellation. Cleaning up.", symbol)
                    _cleanup_thursday_leg_state(r, keys, broker=broker, symbol=symbol)
                    continue
                managed_qty = min(remaining_qty, leg_target_qty)

            # If no open managed order was found, but a prior managed SL-M order_id is in Redis:
            # verify that the prior SL-M order is explicitly terminal (cancelled/filled) before placing market order
            if not existing and stored_order_id:
                prior_slm = next((o for o in orders if str(o.get("order_id") or o.get("orderid") or "") == stored_order_id), None)
                if prior_slm is None:
                    prior_missing_msg = f"Prior managed SL-M {stored_order_id} missing from order book; deferring market order until confirmed terminal"
                    logger.warning(
                        "[%s] Prior managed SL-M %s in Redis is missing from broker order book. Retaining Redis state and deferring market order.",
                        symbol, stored_order_id
                    )
                    dispatch_order_failure_discord(
                        broker=broker,
                        action="IMMEDIATE_EXIT_BLOCKED",
                        track=CYCLE_TRACK,
                        symbol=symbol,
                        quantity=managed_qty,
                        error_reason=prior_missing_msg,
                        details={"order_id": stored_order_id, "status": "cancel_slm_missing"},
                        redis_client=r,
                        cooldown_seconds=600,
                    )
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_missing", "order_id": stored_order_id, "mode": "immediate",
                        "reason": prior_missing_msg,
                        "success": False,
                    }
                    actions.append(base)
                    alerts.append({"severity": "HIGH", **base})
                    continue

                prior_slm_status = normalize_order_status(prior_slm.get("status"))
                if prior_slm_status in OPEN_ORDER_STATUSES:
                    logger.warning("[%s] Prior managed SL-M %s is still pending (%s). Deferring market order.", symbol, stored_order_id, prior_slm_status)
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_pending", "order_id": stored_order_id, "mode": "immediate",
                        "reason": f"Prior managed SL-M {stored_order_id} is still in-flight ({prior_slm_status}); deferring market order",
                        "success": True,
                    }
                    actions.append(base)
                    continue
                elif prior_slm_status in TERMINAL_FILLED_STATUSES:
                    logger.info("[%s] Prior managed SL-M %s was already filled. Position flat.", symbol, stored_order_id)
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "slm_already_filled", "order_id": stored_order_id, "mode": "immediate", "success": True,
                    }
                    actions.append(base)
                    continue
                elif prior_slm_status not in TERMINAL_CANCELLED_STATUSES:
                    prior_unknown_msg = f"Prior managed SL-M {stored_order_id} has unrecognized status '{prior_slm_status}'; deferring market order"
                    logger.warning(
                        "[%s] Prior managed SL-M %s has unknown status '%s'. Retaining Redis state and deferring market order.",
                        symbol, stored_order_id, prior_slm_status
                    )
                    dispatch_order_failure_discord(
                        broker=broker,
                        action="IMMEDIATE_EXIT_BLOCKED",
                        track=CYCLE_TRACK,
                        symbol=symbol,
                        quantity=managed_qty,
                        error_reason=prior_unknown_msg,
                        details={"order_id": stored_order_id, "status": "cancel_slm_unknown_status", "broker_status": prior_slm_status},
                        redis_client=r,
                        cooldown_seconds=600,
                    )
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_unknown_status", "order_id": stored_order_id, "mode": "immediate",
                        "reason": prior_unknown_msg,
                        "success": False,
                    }
                    actions.append(base)
                    alerts.append({"severity": "HIGH", **base})
                    continue

            # Check if a market order was already placed previously and is still in-flight or partially filled
            stored_market_id = _redis_text(r, keys["market_order_id"]) if r else ""
            if stored_market_id:
                managed_ids_set = set(managed_order_ids_in_redis)
                if stored_market_id:
                    managed_ids_set.add(stored_market_id)

                managed_market_sells = [
                    o for o in orders
                    if order_matches_position(o, pos)
                    and str(o.get("transaction_type") or o.get("transactiontype") or "").upper() == "SELL"
                    and str(o.get("order_type") or o.get("ordertype") or "").upper() in ("MARKET", "STOPLOSS_MARKET")
                    and (
                        str(o.get("order_id") or o.get("orderid") or "") in managed_ids_set
                        or order_tag(o).upper() == tag.upper()
                    )
                ]
                prior_order = next((o for o in orders if str(o.get("order_id") or o.get("orderid") or "") == stored_market_id), None)
                if prior_order and prior_order not in managed_market_sells:
                    managed_market_sells.append(prior_order)

                # Check if any managed market sell order is currently in-flight
                in_flight_market = next((o for o in managed_market_sells if is_open_sell_order(o)), None)
                if in_flight_market:
                    in_flight_id = str(in_flight_market.get("order_id") or in_flight_market.get("orderid") or "")
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "market_order_in_flight", "order_id": in_flight_id, "mode": "immediate",
                        "reason": f"Market order {in_flight_id} is already in-flight",
                        "product": product_for_position,
                    }
                    actions.append(base)
                    logger.info("[%s] Market exit order %s is already in-flight", symbol, in_flight_id)
                    continue

                # Compute cumulative filled quantity across all managed market sell orders
                total_market_filled = sum(order_filled_quantity(o) for o in managed_market_sells)
                if not total_market_filled and prior_order:
                    total_market_filled = order_filled_quantity(prior_order)

                prior_status = normalize_order_status((prior_order or {}).get("status")) if prior_order else ""
                remaining_authorized = max(0, leg_target_qty - total_market_filled)

                # Case 1: Prior market order(s) already filled the entire authorized quantity
                if total_market_filled > 0 and remaining_authorized <= 0:
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "market_order_already_filled", "order_id": stored_market_id,
                        "mode": "immediate", "success": True,
                        "reason": f"Market orders already filled authorized quantity ({total_market_filled}/{leg_target_qty} qty); awaiting position settlement",
                        "product": product_for_position,
                    }
                    actions.append(base)
                    logger.info("[%s] Market orders already filled authorized qty (%d/%d); awaiting flat position confirmation", symbol, total_market_filled, leg_target_qty)
                    continue

                # Case 2: Prior market order was rejected/cancelled with 0 fills
                if prior_status in TERMINAL_CANCELLED_STATUSES and total_market_filled == 0:
                    reject_reason = (
                        (prior_order or {}).get("text")
                        or (prior_order or {}).get("rejection_reason")
                        or (prior_order or {}).get("status_message")
                        or (prior_order or {}).get("message")
                        or f"Prior market order {stored_market_id} terminated with status {prior_status}"
                    )
                    logger.critical("[%s] In-flight market exit order %s was rejected/cancelled by broker: %s (%s)",
                                    symbol, stored_market_id, prior_status, reject_reason)
                    dispatch_order_failure_discord(
                        broker=broker,
                        action="SELL_MARKET_TERMINAL_REJECTED",
                        track=CYCLE_TRACK,
                        symbol=symbol,
                        quantity=managed_qty,
                        error_reason=f"Order {stored_market_id} ({prior_status}): {reject_reason}",
                        details={
                            "order_id": stored_market_id,
                            "broker_status": prior_status,
                            "symbol": symbol,
                            "quantity": managed_qty,
                            "rejection_reason": reject_reason,
                            "order_data": prior_order,
                        },
                        redis_client=r,
                        cooldown_seconds=600,
                    )
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "market_order_rejected", "order_id": stored_market_id, "mode": "immediate",
                        "error": f"Prior market order {stored_market_id} was {prior_status}: {reject_reason}", "success": False,
                        "product": product_for_position,
                    }
                    actions.append(base)
                    alerts.append({"severity": "CRITICAL", **base})
                    continue

                # Case 3: Partial fill reconciliation (e.g. 40 filled, 25 cancelled)
                # Reconcile remaining authorized quantity against current available broker position snapshot
                if total_market_filled > 0:
                    reconciled_qty = min(managed_qty, remaining_authorized)
                    if reconciled_qty <= 0:
                        base = {
                            "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                            "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                            "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                            "status": "market_order_already_filled", "order_id": stored_market_id,
                            "mode": "immediate", "success": True,
                            "reason": f"Market orders already filled authorized quantity ({total_market_filled}/{leg_target_qty} qty); awaiting position settlement",
                            "product": product_for_position,
                        }
                        actions.append(base)
                        continue

                    if reconciled_qty != managed_qty:
                        logger.info(
                            "[%s] Reconciled immediate market exit qty from %d to %d (prior filled: %d, authorized target: %d, broker snapshot: %d)",
                            symbol, managed_qty, reconciled_qty, total_market_filled, leg_target_qty, total_broker_qty
                        )
                        managed_qty = reconciled_qty

            # Dry-run immediate exit
            if not execute:
                base = {
                    "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                    "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                    "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                    "product": product_for_position, "status": "place_market", "dry_run": True,
                    "action_needed": True, "mode": "immediate",
                    "reason": f"Authorized leg will be immediately squared off with MARKET order ({managed_qty} qty)",
                }
                actions.append(base)
                continue

            if token == MOCK_TOKEN:
                mock_oid = f"mock_{int(time.time()*1000)}"
                if r:
                    r.set(keys["mode"], "immediate")
                    r.set(keys["market_order_id"], mock_oid)
                    r.set(keys["order_id"], mock_oid)
                    r.sadd(f"fno:{broker}:managed_order_ids:thu", mock_oid)
                    r.sadd(f"fno:{broker}:managed_order_ids", mock_oid)
                base = {
                    "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                    "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                    "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                    "product": product_for_position, "status": "place_market", "order_id": mock_oid,
                    "success": True, "mode": "immediate",
                }
                actions.append(base)
                continue

            if abort_event is not None and abort_event.is_set():
                logger.critical("[%s] Distributed lock lost immediately before market order placement! Aborting.", symbol)
                base = {"status": "lock_lost", "error": "Distributed lock lost before broker mutation", "success": False, "mode": "immediate"}
                actions.append(base)
                alerts.append({"severity": "CRITICAL", **base})
                return {
                    "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker,
                    "positions": len(positions), "authorized_positions": len(authorized),
                    "actions": actions, "orphan_managed_orders": orphan_managed_orders,
                    "alerts": alerts, "fatal_error": "lock_lost", "dry_run": not execute,
                }

            if not force:
                is_open, open_reason = is_market_hours_ist()
                if not is_open:
                    logger.warning("[%s] Market closed (%s) before market order placement. Aborting.", symbol, open_reason)
                    base = {"status": "market_closed", "error": f"Market closed before broker mutation: {open_reason}", "success": False, "mode": "immediate"}
                    actions.append(base)
                    alerts.append({"severity": "CRITICAL", **base})
                    return {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker,
                        "positions": len(positions), "authorized_positions": len(authorized),
                        "actions": actions, "orphan_managed_orders": orphan_managed_orders,
                        "alerts": alerts, "fatal_error": "market_closed", "dry_run": not execute,
                    }

            try:
                result = place_exit_order_market(token, pos, managed_qty, product=product_for_position, tag=tag, broker=broker)
                base = {
                    "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                    "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                    "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                    "product": product_for_position, "status": "place_market", "mode": "immediate",
                    "result": result, "success": result.get("success", False),
                }
                if result.get("success"):
                    new_market_id = result.get("order_id")
                    base["order_id"] = new_market_id
                    if r and new_market_id:
                        r.set(keys["mode"], "immediate")
                        r.set(keys["market_order_id"], new_market_id)
                        r.set(keys["order_id"], new_market_id)
                        r.sadd(f"fno:{broker}:managed_order_ids:thu", new_market_id)
                        r.sadd(f"fno:{broker}:managed_order_ids", new_market_id)
                    logger.info("[%s] Successfully placed immediate MARKET order for %d qty (order_id: %s)", symbol, managed_qty, new_market_id)
                else:
                    base["error"] = result.get("error")
                    logger.error("[%s] Market order placement failed: %s", symbol, result.get("error"))
                    dispatch_order_failure_discord(
                        broker=broker,
                        action="SELL_MARKET",
                        track=CYCLE_TRACK,
                        symbol=symbol,
                        quantity=managed_qty,
                        error_reason=str(result.get("error")),
                        details=result,
                        redis_client=r,
                        cooldown_seconds=600,
                    )
            except Exception as exc:
                base = {
                    "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                    "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                    "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                    "product": product_for_position, "status": "execution_failed", "error": str(exc), "success": False, "mode": "immediate",
                }
                logger.error("[%s] Exception during market order placement: %s", symbol, exc)
                dispatch_order_failure_discord(
                    broker=broker,
                    action="SELL_MARKET",
                    track=CYCLE_TRACK,
                    symbol=symbol,
                    quantity=managed_qty,
                    error_reason=str(exc),
                    redis_client=r,
                    cooldown_seconds=600,
                )
            actions.append(base)
            continue

        # Fetch live LTP
        if token == MOCK_TOKEN:
            ltp = _to_float(pos.get("last_price") or 0)
        else:
            try:
                fresh_ltp = fetch_ltp(token, token_key, broker=broker)
            except Exception as exc:
                fresh_ltp = None
                logger.error("[%s] quote request raised an exception: %s", symbol, exc)
            if not fresh_ltp or fresh_ltp <= 0:
                item = {
                    "symbol": symbol, "status": "skipped", "reason": "no fresh quote; leaving broker orders untouched",
                    "broker": broker, "redis_keys": keys
                }
                actions.append(item)
                logger.warning("[%s] no fresh LTP; keeping existing protection", symbol)
                continue
            ltp = fresh_ltp

        avg_price = cycle_entry_price if cycle_entry_price > 0 else _to_float(pos.get("average_price"))
        if avg_price <= 0:
            avg_price = old_avg if old_avg > 0 else ltp

        if lifecycle_changed:
            old_max = avg_price
            old_sl = 0.0
            old_avg = avg_price
            old_qty = managed_qty

        # Seed/floor the trail max with the Alpha FSM's observed peak for this leg. While a leg is
        # unauthorized the Redis max is never written, so without this the trail restarts from entry.
        fsm_peak = _fsm_peak_for_symbol(_load_thursday_fsm_snapshot(), symbol, active_expiry=active_expiry)
        new_max = max(old_max, ltp, avg_price, fsm_peak)
        activation_price = avg_price * (1.0 + activation_pct / 100.0)
        if activation_pct > 0 and ltp < activation_price and old_max < activation_price:
            new_stop = compute_initial_sl(avg_price, initial_stop_pct)
            phase = "phase1_initial"
        else:
            new_stop = compute_trailing_sl(new_max, trail_pct)
            phase = "phase2_trailing"

        existing_trigger = _to_float((existing or {}).get("trigger_price") or 0)
        existing_pending_qty = order_pending_quantity(existing) if existing else 0
        quantity_mismatch = bool(existing and existing_pending_qty != managed_qty)

        # Ratchet Up Only
        floor_candidates = [x for x in (old_sl, existing_trigger) if x > 0]
        final_stop = max([new_stop] + floor_candidates)
        product_for_position = get_exit_product(pos, broker=broker, cli_product=product)

        base: Dict[str, Any] = {
            "cycle": CYCLE_NAME, "symbol": symbol, "instrument_token": token_key, "broker": broker,
            "tag": tag, "quantity": managed_qty, "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
            "product": product_for_position, "avg_price": round(avg_price, 2), "ltp": round(ltp, 2),
            "old_max": round(old_max, 2) if old_max else 0, "new_max": round(new_max, 2),
            "fsm_peak": round(fsm_peak, 2),
            "old_sl": round(old_sl, 2) if old_sl else 0, "existing_trigger": round(existing_trigger, 2) if existing_trigger else 0,
            "existing_pending_quantity": existing_pending_qty if existing else None,
            "final_stop": round(final_stop, 2), "phase": phase, "existing_order_id": existing_order_id or None,
        }

        # Check if prior SL-M protection order was rejected/cancelled in broker order book
        if not existing and stored_order_id:
            prior_slm = next((o for o in orders if str(o.get("order_id") or o.get("orderid") or "") == stored_order_id), None)
            if prior_slm:
                prior_status = normalize_order_status(prior_slm.get("status"))
                prior_filled = order_filled_quantity(prior_slm)
                if prior_status in TERMINAL_CANCELLED_STATUSES and prior_filled == 0:
                    reject_reason = (
                        prior_slm.get("text")
                        or prior_slm.get("rejection_reason")
                        or prior_slm.get("status_message")
                        or prior_slm.get("message")
                        or f"Prior SL-M protection order {stored_order_id} terminated with status {prior_status}"
                    )
                    logger.critical(
                        "[%s] In-flight SL-M protection order %s was rejected/cancelled by broker: %s (%s)",
                        symbol, stored_order_id, prior_status, reject_reason
                    )
                    dispatch_order_failure_discord(
                        broker=broker,
                        action="SELL_SLM_TERMINAL_REJECTED",
                        track=CYCLE_TRACK,
                        symbol=symbol,
                        quantity=managed_qty,
                        error_reason=f"Protection order {stored_order_id} ({prior_status}): {reject_reason}",
                        details={
                            "order_id": stored_order_id,
                            "broker_status": prior_status,
                            "symbol": symbol,
                            "quantity": managed_qty,
                            "rejection_reason": reject_reason,
                            "order_data": prior_slm,
                        },
                        redis_client=r,
                        cooldown_seconds=600,
                    )

        # Stop Breached check
        if ltp <= final_stop:
            base.update({
                "status": "breached_existing_order_needs_review" if existing else "breached_needs_immediate_exit",
                "reason": f"LTP {ltp:.2f} <= required stop {final_stop:.2f}; immediate manual review required",
                "requires_immediate_attention": True
            })
            actions.append(base)
            alerts.append({"severity": "CRITICAL", **base})
            logger.critical("[%s] STOP BREACHED: LTP %.2f <= stop %.2f", symbol, ltp, final_stop)
            continue

        trigger_needs_modify = bool(existing and (final_stop >= existing_trigger + min_ratchet_step))

        if not execute:
            base["status"] = "modify" if existing and (trigger_needs_modify or quantity_mismatch) else ("place" if not existing else "unchanged")
            base["dry_run"] = True
            base["action_needed"] = (not existing) or trigger_needs_modify or quantity_mismatch
            actions.append(base)
            continue

        if token == MOCK_TOKEN:
            base.update({"status": "mock_execute_blocked", "reason": "mock token cannot place real orders", "success": False})
            actions.append(base)
            continue

        # Check lock validity immediately before state save or broker order placement/modification
        if abort_event is not None and abort_event.is_set():
            logger.critical("[%s] Distributed lock lost immediately before broker order mutation! Aborting.", symbol)
            base.update({"status": "lock_lost", "error": "Distributed lock lost before broker mutation", "success": False})
            actions.append(base)
            alerts.append({"severity": "CRITICAL", **base})
            summary = {
                "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker,
                "positions": len(positions), "authorized_positions": len(authorized),
                "actions": actions, "orphan_managed_orders": orphan_managed_orders,
                "alerts": alerts, "fatal_error": "lock_lost", "dry_run": not execute
            }
            return summary

        # Check market hours immediately before broker order mutation (unless force or mock)
        if execute and token != MOCK_TOKEN and not force:
            is_open, open_reason = is_market_hours_ist()
            if not is_open:
                logger.warning("[%s] Market closed (%s) before order mutation. Aborting.", symbol, open_reason)
                base.update({"status": "market_closed", "error": f"Market closed before broker mutation: {open_reason}", "success": False})
                actions.append(base)
                alerts.append({"severity": "CRITICAL", **base})
                return {
                    "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker,
                    "positions": len(positions), "authorized_positions": len(authorized),
                    "actions": actions, "orphan_managed_orders": orphan_managed_orders,
                    "alerts": alerts, "fatal_error": "market_closed", "dry_run": not execute
                }

        if existing and not trigger_needs_modify and not quantity_mismatch:
            state_saved = _save_trailing_state(r, keys, new_max, max(final_stop, existing_trigger), avg_price, managed_qty, existing_order_id, entry_order_id)
            clear_order_failure_cooldown(r, broker=broker, track=CYCLE_TRACK, symbol=symbol)
            base.update({"status": "unchanged", "success": True, "state_saved": state_saved, "reason": "trigger already meets required stop"})
            actions.append(base)
            continue

        try:
            if existing:
                result = modify_exit_order_slm(token, existing, managed_qty, final_stop, tag=tag, broker=broker)
                base["status"] = "modify"
            else:
                result = place_exit_order_slm(token, pos, managed_qty, final_stop, product=product_for_position, tag=tag, broker=broker)
                base["status"] = "place"

            base["result"] = result
            base["success"] = result.get("success", False)
            if result.get("success"):
                new_order_id = result.get("order_id") or existing_order_id
                _save_trailing_state(r, keys, new_max, final_stop, avg_price, managed_qty, new_order_id, entry_order_id, broker=broker)
                logger.info("[%s] Successfully executed %s order for %d qty at trigger %.2f (order_id: %s)", symbol, base["status"], managed_qty, final_stop, new_order_id)
            else:
                logger.error("[%s] Order execution failed: %s", symbol, result.get("error"))
                act = "SELL_SLM_MODIFY" if existing else "SELL_SLM_PLACE"
                dispatch_order_failure_discord(
                    broker=broker,
                    action=act,
                    track=CYCLE_TRACK,
                    symbol=symbol,
                    quantity=managed_qty,
                    error_reason=str(result.get("error")),
                    details=result,
                    redis_client=r,
                    cooldown_seconds=600,
                )
        except Exception as exc:
            base.update({"status": "execution_failed", "error": str(exc), "success": False})
            logger.error("[%s] Exception during order execution: %s", symbol, exc)
            act = "SELL_SLM_MODIFY" if existing else "SELL_SLM_PLACE"
            dispatch_order_failure_discord(
                broker=broker,
                action=act,
                track=CYCLE_TRACK,
                symbol=symbol,
                quantity=managed_qty,
                error_reason=str(exc),
                redis_client=r,
                cooldown_seconds=600,
            )

        actions.append(base)

    placed_cnt = sum(1 for a in actions if a.get("status") in ("place", "place_market") and a.get("success"))
    modified_cnt = sum(1 for a in actions if a.get("status") == "modify" and a.get("success"))
    failed_cnt = sum(1 for a in actions if a.get("success") is False)

    return {
        "cycle": CYCLE_NAME,
        "track": CYCLE_TRACK,
        "broker": broker,
        "positions": len(positions),
        "authorized_positions": len(authorized),
        "target_lots": target_lots,
        "cycle_target_qty": cycle_target_qty,
        "placed": placed_cnt,
        "modified": modified_cnt,
        "failed": failed_cnt,
        "actions": actions,
        "orphan_managed_orders": orphan_managed_orders,
        "alerts": alerts,
        "dry_run": not execute,
    }


# Mock data for offline verification
_MOCK_POSITIONS = {
    "status": "success",
    "data": {
        "net": [
            {"tradingsymbol": "NIFTY03NOV2622650CE", "exchange": "NFO", "instrument_token": "NSE_FO|53750", "product": "NRML", "quantity": 65, "average_price": 105.50, "last_price": 122.00},
            {"tradingsymbol": "NIFTY03NOV2622800PE", "exchange": "NFO", "instrument_token": "NSE_FO|53795", "product": "NRML", "quantity": 65, "average_price": 150.00, "last_price": 140.00},
        ]
    },
}
_MOCK_ORDER_BOOK = [
    {"exchange": "NFO", "product": "NRML", "price": 0, "quantity": 65, "status": "open", "tag": "THU-SL", "instrument_token": "NSE_FO|53750", "trading_symbol": "NIFTY03NOV2622650CE", "order_type": "SL-M", "validity": "DAY", "trigger_price": 90.00, "transaction_type": "SELL", "filled_quantity": 0, "pending_quantity": 65, "order_id": "990108010918002"},
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Thursday F&O Exit Manager — Dedicated 3-Day Strategy Trailing Stop Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--execute", action="store_true", help="Place/modify real orders (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry-run mode (default behavior when --execute is omitted)")
    parser.add_argument("--mock", action="store_true", help="Use built-in offline mock data")
    parser.add_argument("--force", action="store_true", help="Bypass Redis authorization / ORDERS_LIVE checks (testing only)")
    parser.add_argument("--broker", default=None, help=f"Broker adapter (default: track configured broker from THURSDAY_BROKER or '{DEFAULT_BROKER}')")
    parser.add_argument("--lots", type=int, default=None, help="Allocated lots for Thursday strategy (default: LOTS_THURSDAY from Redis, then .env, then 1)")
    parser.add_argument("--lot-size", type=int, default=None, help="Nifty contract lot size (default: NIFTY_LOT_SIZE from Redis, then .env, then 65)")
    parser.add_argument("--trail-pct", type=float, default=DEFAULT_TRAIL_PCT, help=f"Trail %% below high-water mark (default {DEFAULT_TRAIL_PCT})")
    parser.add_argument("--initial-stop-pct", type=float, default=DEFAULT_INITIAL_STOP_PCT, help=f"Initial stop %% below average before activation (default {DEFAULT_INITIAL_STOP_PCT})")
    parser.add_argument("--activation-pct", type=float, default=DEFAULT_ACTIVATION_PCT, help="Start trailing after LTP reaches average*(1+activation/100); 0=from start")
    parser.add_argument("--min-ratchet-step", type=float, default=DEFAULT_MIN_RATCHET_STEP, help=f"Minimum Rs. increase to modify order (default Rs. {DEFAULT_MIN_RATCHET_STEP})")
    parser.add_argument("--tag", default=DEFAULT_TAG, help=f"Order tag (default {DEFAULT_TAG})")
    parser.add_argument("--auto-cancel-orphans", action="store_true", default=True, help="Auto-cancel orphan THU-SL orders when position is closed (default: True to prevent naked shorts)")
    parser.add_argument("--no-auto-cancel-orphans", action="store_false", dest="auto_cancel_orphans", help="Disable auto-cancellation of orphan orders (leaves orphan orders open for manual cancel)")
    parser.add_argument("--product", default=None, help="Explicit product fallback (I or D)")
    parser.add_argument("--exit-immediately", action="store_true", help="Immediately square off authorized legs with MARKET sell orders instead of trailing SL")
    parser.add_argument("--debug", action="store_true", help="Verbose DEBUG logging")
    parser.add_argument("--log-api", action="store_true", help="Enable raw broker API dump")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
    if args.log_api:
        set_api_logging(True)

    # 0. Ensure logs directory exists
    try:
        os.makedirs(os.path.join(project_root, "logs"), exist_ok=True)
    except Exception:
        pass

    # Redis client first: execution gates and sizing are read Redis-first (fallback: .env, then default)
    if args.mock:
        r = None
        from config import _config_redis_state
        _config_redis_state["unavailable"] = True
    else:
        r = get_redis_client()
    from config import get_config_bool, get_config_int
    # 1. Master Gate: LIVE_EXIT_TRIGGER_THURSDAY (Redis -> .env -> false)
    live_env_enabled = get_config_bool("LIVE_EXIT_TRIGGER_THURSDAY", default=False, redis_client=r)
    exit_immediately = args.exit_immediately or get_config_bool("EXIT_IMMEDIATELY_THURSDAY", default=False, redis_client=r)
    if args.lots is None:
        args.lots = get_config_int("LOTS_THURSDAY", 1, redis_client=r)
    if args.lot_size is None:
        args.lot_size = get_config_int("NIFTY_LOT_SIZE", DEFAULT_LOT_SIZE, redis_client=r)
    actual_execute = args.execute and live_env_enabled and not args.dry_run

    if args.execute and not live_env_enabled:
        logger.warning(
            "LIVE_EXIT_TRIGGER_THURSDAY is disabled/false (Redis/.env). Enforcing safe DRY-RUN mode (manual square-off mode active)."
        )

    # 2. Fast 1-Millisecond Redis Gate: Check ORDERS_LIVE_THU
    if not args.mock and not args.force:
        if r is None:
            logger.error("Redis is offline and --force was not specified. Aborting.")
            print(json.dumps({"error": "redis_unavailable", "message": "Redis is required for live tracking"}, indent=2))
            return 1

        orders_live = r.get("ORDERS_LIVE_THU") or r.get("fno:orders_live:thursday")
        if str(orders_live or "").strip().lower() not in ("1", "true", "yes"):
            logger.info("ORDERS_LIVE_THU is inactive in Redis. Exiting immediately in 1ms.")
            print(json.dumps({
                "status": "idle",
                "cycle": CYCLE_NAME,
                "orders_live": 0,
                "message": "No active exit signal for Thursday strategy in Redis"
            }, indent=2))
            return 0

        # Market Hours Guard: Exit immediately outside 09:15 - 15:30 IST on weekdays
        is_open, market_reason = is_market_hours_ist()
        if not is_open:
            logger.info("Market closed: %s. Exiting cleanly.", market_reason)
            print(json.dumps({
                "status": "market_closed",
                "cycle": CYCLE_NAME,
                "reason": market_reason,
            }, indent=2))
            return 0

    # 3. Dynamic Broker Selection per Strategy Cycle
    configured_broker = (args.broker or get_broker_for_track(CYCLE_TRACK)).lower().strip()
    stored_broker = get_active_strategy_broker(CYCLE_TRACK)

    if stored_broker:
        if configured_broker != stored_broker:
            logger.critical(
                "Configured broker '%s' contradicts active cycle persisted broker '%s'. "
                "Failing closed to avoid managing positions under the wrong account.",
                configured_broker, stored_broker
            )
            print(json.dumps({
                "error": "broker_mismatch",
                "configured_broker": configured_broker,
                "persisted_broker": stored_broker,
                "message": f"Active cycle requires broker '{stored_broker}', but '{configured_broker}' is configured. Failing closed."
            }, indent=2))
            return 1
        active_broker = stored_broker
    else:
        active_broker = configured_broker

    if active_broker not in SUPPORTED_BROKERS:
        logger.critical("Broker '%s' is not supported. Supported brokers are %s.", active_broker, SUPPORTED_BROKERS)
        print(json.dumps({
            "error": "unsupported_broker",
            "broker": active_broker,
            "message": f"Only brokers {SUPPORTED_BROKERS} are supported for Thursday F&O Exit Orders"
        }, indent=2))
        return 1

    # 4. Fetch Positions and Orders under Cycle & Broker Execution Locks
    abort_event = threading.Event()
    with combined_execution_lock(
        r, cycle_name=CYCLE_NAME, broker=active_broker, timeout=60, max_wait=10.0, abort_event=abort_event
    ) as (acquired, lock_reason):
        if not acquired:
            logger.warning(
                "Could not acquire distributed execution lock (%s) within 10s. Another manager is running. Exiting to avoid concurrent order conflict.",
                lock_reason
            )
            print(json.dumps({
                "status": "lock_busy",
                "cycle": CYCLE_NAME,
                "broker": active_broker,
                "message": f"Distributed lock busy ({lock_reason}). Skipping this cycle to prevent race condition."
            }, indent=2))
            return 0

        if abort_event.is_set():
            logger.critical("Distributed lock lost immediately after acquisition. Aborting for safety.")
            return 1

        try:
            if args.mock:
                raw_pos = _MOCK_POSITIONS
                orders = parse_order_book(_MOCK_ORDER_BOOK)
                token = MOCK_TOKEN
            else:
                token = get_access_token(broker=active_broker)
                raw_pos = fetch_positions(token, broker=active_broker)
                orders = fetch_order_book(token, broker=active_broker)
            positions, short_positions = parse_positions(raw_pos)
        except Exception as exc:
            logger.error("Fetch failed: %s", exc)
            print(json.dumps({"error": "fetch_failed", "cycle": CYCLE_NAME, "message": str(exc)}, indent=2))
            return 1

        if abort_event.is_set():
            logger.critical("Distributed lock lost during portfolio fetch. Aborting order execution.")
            return 1

        # 5. Run Exit Order Management
        active_expiry = get_active_strategy_expiry("3day_strategy")
        for mismatch in _fsm_closed_legs_with_open_positions(
            _load_thursday_fsm_snapshot(), positions, active_expiry=active_expiry
        ):
            logger.critical(
                "[THU-RECON] Alpha FSM marked %s as %s (exit %s, reason: %s) but broker still holds %s x%s. Position is UNPROTECTED; manual review required.",
                mismatch["fsm_leg"], mismatch["fsm_status"], mismatch["fsm_exit_price"],
                mismatch["fsm_exit_reason"], mismatch["symbol"], mismatch["broker_quantity"],
            )
        summary = manage_thursday_exit_orders(
            token=token,
            positions=positions,
            orders=orders,
            broker=active_broker,
            target_lots=args.lots,
            lot_size=args.lot_size,
            trail_pct=args.trail_pct,
            initial_stop_pct=args.initial_stop_pct,
            activation_pct=args.activation_pct,
            min_ratchet_step=args.min_ratchet_step,
            execute=actual_execute,
            auto_cancel_orphans=args.auto_cancel_orphans,
            product=args.product,
            tag=args.tag,
            force=args.force,
            short_positions=short_positions,
            active_expiry=active_expiry,
            abort_event=abort_event,
            exit_immediately=exit_immediately,
        )

        if abort_event.is_set() or summary.get("fatal_error") == "lock_lost":
            logger.critical("Distributed lock lost during order execution. Aborting cleanup/reconciliation.")
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 1

        # 6. Leg-by-Leg Reconciliation & Cleanup:
        # Safely retire individual legs that have no active position and no open managed orders,
        # and NEVER reset ORDERS_LIVE_THU if an unconfirmed orphan SELL order remains open!
        if not args.mock and not args.force and r:
            retired_legs = reconcile_cycle_legs(
                r,
                positions,
                orders,
                broker=active_broker,
                cycle="thu",
                tag=args.tag,
                short_positions=short_positions,
                active_expiry=active_expiry,
            )
            if retired_legs:
                logger.info("Reconciled and retired %d closed Thursday leg(s) in Redis.", len(retired_legs))

            unconfirmed_orphans = [
                o for o in summary.get("orphan_managed_orders", [])
                if not o.get("is_terminal", False)
            ]
            if unconfirmed_orphans:
                logger.critical(
                    "Cannot reset ORDERS_LIVE_THU: %d orphan %s orders remain open/pending in broker. Keeping ORDERS_LIVE_THU active for verification.",
                    len(unconfirmed_orphans), args.tag
                )
            elif len(short_positions) > 0:
                logger.critical(
                    "Cannot reset ORDERS_LIVE_THU: %d unexpected SHORT derivative positions exist in broker portfolio. Manual review required.",
                    len(short_positions)
                )
            elif len(positions) == 0:
                # Entire broker portfolio has 0 open long AND 0 open short derivative positions, and 0 orphan orders
                logger.info("Broker portfolio has 0 open derivative positions and 0 orphan orders. Resetting ORDERS_LIVE_THU = 0.")
                for k in list(r.scan_iter(f"fno:{active_broker}:enabled:thu:*", count=20)):
                    r.delete(k)
                r.set("ORDERS_LIVE_THU", "0")
                r.set("fno:orders_live:thursday", "0")
            else:
                # Reset ORDERS_LIVE_THU = 0 only when no enabled legs remain AND no open
                # Thursday long is left without an authorization key (it would be unmanaged).
                unmanaged_longs = _unmanaged_thu_long_positions(
                    r, positions, broker=active_broker, active_expiry=active_expiry
                )
                if unmanaged_longs:
                    logger.critical(
                        "Cannot reset ORDERS_LIVE_THU: %d open Thursday long position(s) have no authorization key: %s. Keeping gate active; manual review required.",
                        len(unmanaged_longs),
                        [p.get("tradingsymbol") or p.get("instrument_token") for p in unmanaged_longs],
                    )
                else:
                    _check_and_reset_orders_live_thu(r, broker=active_broker)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if summary.get("fatal_error") else 0


if __name__ == "__main__":
    sys.exit(main())
