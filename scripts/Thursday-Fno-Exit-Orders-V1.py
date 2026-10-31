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
    build_detailed_enabled_key,
    build_leg_enabled_key,
    get_orders_live_key,
    normalize_cycle,
    normalize_expiry,
    parse_enabled_key,
)

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
PRODUCT_MAP = {"angelone": "CARRYFORWARD", "angel": "CARRYFORWARD"}

ORDER_TYPE_SLM = "STOPLOSS_MARKET"
ORDER_TYPE_SL = "STOPLOSS_LIMIT"

VALIDITY_DAY = "DAY"
TICK_SIZE = 0.05
DEFAULT_LOT_SIZE = int(os.getenv("NIFTY_LOT_SIZE", "65"))
DEFAULT_BROKER = "angelone"
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
    m4 = re.match(r"^(.*?)(\d+)(CE|PE)$", raw)
    if m4:
        return {"underlying": m4.group(1), "expiry": "UNKNOWN", "strike": m4.group(2), "option_type": m4.group(3), "raw": raw}
    return {"underlying": raw[:10] if raw else "UNKNOWN", "expiry": "UNKNOWN", "strike": "0", "option_type": "UNKNOWN", "raw": raw}


def get_active_strategy_expiry(track: str = "3day_strategy") -> Optional[str]:
    """Retrieve active cycle expiry_date (YYYY-MM-DD) from environment or active_strategies.json."""
    env_exp = os.getenv("ACTIVE_EXPIRY_DATE") or os.getenv("EXPIRY_DATE")
    if env_exp:
        return normalize_expiry(env_exp)
    try:
        from common.unified_strategy_store import load_active_strategies
        active = load_active_strategies()
        strat = active.get(track, {})
        exp = strat.get("expiry_date")
        if exp:
            return normalize_expiry(exp)
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
        "max_short": f"{short_base}:max",
        "sl_short": f"{short_base}:sl",
        "avg_short": f"{short_base}:avg",
        "details": details,
        "contract_suffix": contract_suffix,
        "expiry_iso": expiry_iso,
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
      - long_positions: derivative positions with net quantity > 0 (managed for exits)
      - short_positions: derivative positions with net quantity < 0 (unexpected short exposure)
    """
    if not isinstance(raw, dict):
        raise ValueError("Unexpected positions response: expected a JSON object")
    data = raw.get("data")
    if isinstance(data, list):
        source_positions = data
    elif isinstance(data, dict):
        if isinstance(data.get("net"), list):
            source_positions = data["net"]
        elif isinstance(data.get("positions"), list):
            source_positions = data["positions"]
        elif not data:
            source_positions = []
        else:
            raise ValueError(f"Unsupported positions response shape: data keys={list(data.keys())}")
    elif data is None or not data:
        source_positions = []
    else:
        raise ValueError("Unsupported positions payload")

    long_positions = []
    short_positions = []
    for p in source_positions:
        if isinstance(p, dict):
            norm_p = dict(p)
            tok = str(p.get("symboltoken") or p.get("instrument_token") or "").strip()
            sym = str(p.get("tradingsymbol") or p.get("trading_symbol") or "").strip().upper()
            prod = str(p.get("producttype") or p.get("product") or "CARRYFORWARD").strip().upper()
            if prod in ("D", "NRML"):
                prod = "CARRYFORWARD"
            elif prod in ("I", "MIS"):
                prod = "INTRADAY"

            qty = _position_quantity(p)
            if qty == 0:
                buy_q = _to_float(p.get("cfbuyqty") or p.get("buyqty") or 0)
                sell_q = _to_float(p.get("cfsellqty") or p.get("sellqty") or 0)
                if buy_q != 0 or sell_q != 0:
                    qty = buy_q - sell_q

            avg_price = _to_float(p.get("cfbuyavgprice") or p.get("buyavgprice") or p.get("buy_price") or p.get("average_price") or 0.0)

            norm_p["instrument_token"] = tok
            norm_p["symboltoken"] = tok
            norm_p["tradingsymbol"] = sym
            norm_p["trading_symbol"] = sym
            norm_p["product"] = prod
            norm_p["producttype"] = prod
            norm_p["quantity"] = qty
            norm_p["net_quantity"] = qty
            norm_p["netqty"] = qty
            norm_p["buy_price"] = avg_price
            norm_p["average_price"] = avg_price

            if is_derivative_position(norm_p):
                if qty > 0:
                    long_positions.append(norm_p)
                elif qty < 0:
                    short_positions.append(norm_p)
    return long_positions, short_positions


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
    if isinstance(raw, list):
        raw_orders = [o for o in raw if isinstance(o, dict)]
    elif isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list):
            raw_orders = [o for o in data if isinstance(o, dict)]
        elif data is None or data == []:
            raw_orders = []
        else:
            raise ValueError(f"Unexpected order book response shape: data is {type(data).__name__}, expected list")
    elif raw is None:
        raw_orders = []
    else:
        raise ValueError(f"Unexpected order book response shape: {type(raw).__name__}, expected dict or list")

    normalized = []
    for o in raw_orders:
        norm_o = dict(o)
        oid = str(o.get("orderid") or o.get("order_id") or "").strip()
        tok = str(o.get("symboltoken") or o.get("instrument_token") or "").strip()
        sym = str(o.get("tradingsymbol") or o.get("trading_symbol") or "").strip().upper()
        status = str(o.get("orderstatus") or o.get("status") or "").strip()
        ttype = str(o.get("transactiontype") or o.get("transaction_type") or "").strip().upper()
        otype = str(o.get("ordertype") or o.get("order_type") or "").strip().upper()
        ptype = str(o.get("producttype") or o.get("product") or "CARRYFORWARD").strip().upper()
        if ptype in ("D", "NRML"):
            ptype = "CARRYFORWARD"
        elif ptype in ("I", "MIS"):
            ptype = "INTRADAY"

        qty = _to_float(o.get("quantity") or 0)
        filled = _to_float(o.get("filledshares") or o.get("filled_quantity") or 0)
        unfilled = _to_float(o.get("unfilledshares") or o.get("pending_quantity") or max(0, qty - filled))
        trig = _to_float(o.get("triggerprice") or o.get("trigger_price") or 0.0)
        tag = str(o.get("tag") or "").strip()

        norm_o["order_id"] = oid
        norm_o["orderid"] = oid
        norm_o["instrument_token"] = tok
        norm_o["symboltoken"] = tok
        norm_o["trading_symbol"] = sym
        norm_o["tradingsymbol"] = sym
        norm_o["status"] = status
        norm_o["orderstatus"] = status
        norm_o["transaction_type"] = ttype
        norm_o["transactiontype"] = ttype
        norm_o["order_type"] = otype
        norm_o["ordertype"] = otype
        norm_o["product"] = ptype
        norm_o["producttype"] = ptype
        norm_o["quantity"] = qty
        norm_o["filled_quantity"] = filled
        norm_o["pending_quantity"] = unfilled
        norm_o["trigger_price"] = trig
        norm_o["triggerprice"] = trig
        norm_o["tag"] = tag
        normalized.append(norm_o)
    return normalized


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
    broker = (broker or "angelone").lower().strip()
    if broker not in ("angelone", "angel"):
        raise RuntimeError(f"Thursday F&O exit orders strictly support 'angelone' (requested '{broker}')")

    if r is None:
        try:
            r = get_redis_client()
        except Exception:
            r = None

    # 1. Read JWT from Redis key angelone_jwt_feed
    if r is not None:
        try:
            raw = r.get("angelone_jwt_feed")
            if raw:
                try:
                    data = json.loads(raw)
                    jwt = data.get("jwtToken") or data.get("token") or ""
                except Exception:
                    jwt = str(raw).strip()
                if jwt.startswith("Bearer "):
                    jwt = jwt[7:]
                if jwt:
                    return jwt
        except Exception as exc:
            logger.warning("Failed to read angelone_jwt_feed from Redis: %s", exc)

    # 2. Check environment variables
    for env_var in ("ANGELONE_JWT_TOKEN", "ANGELONE_ACCESS_TOKEN", "ANGEL_JWT_TOKEN"):
        val = os.getenv(env_var)
        if val:
            if val.startswith("Bearer "):
                val = val[7:]
            return val

    candidates = ["angelone_jwt_feed (Redis)", "ANGELONE_JWT_TOKEN (.env)", "ANGELONE_ACCESS_TOKEN (.env)"]
    raise RuntimeError(f"No access token found for broker '{broker}'. Checked {candidates}")


def fetch_positions(token: str, broker: str = "angelone") -> Dict[str, Any]:
    broker = (broker or "angelone").lower().strip()
    if broker not in ("angelone", "angel"):
        raise RuntimeError(f"fetch_positions not implemented for broker '{broker}'")
    if token == MOCK_TOKEN:
        return _MOCK_POSITIONS
    url = f"{ANGELONE_BASE}/rest/secure/angelbroking/order/v1/getPosition"
    headers = _headers(token)
    if LOG_API:
        _log_api_request("GET", url, headers, None)
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if LOG_API:
        _log_api_response("GET", url, resp)
    if resp.status_code != 200:
        raise RuntimeError(f"Positions HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if isinstance(body, dict) and body.get("status") is False:
        raise RuntimeError(f"Positions API error: {body.get('message') or body.get('errorcode') or body}")
    return body


def fetch_order_book(token: str, broker: str = "angelone") -> List[Dict[str, Any]]:
    broker = (broker or "angelone").lower().strip()
    if broker not in ("angelone", "angel"):
        raise RuntimeError(f"fetch_order_book not implemented for broker '{broker}'")
    if token == MOCK_TOKEN:
        return parse_order_book(_MOCK_ORDER_BOOK)
    url = f"{ANGELONE_BASE}/rest/secure/angelbroking/order/v1/getOrderBook"
    headers = _headers(token)
    if LOG_API:
        _log_api_request("GET", url, headers, None)
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if LOG_API:
        _log_api_response("GET", url, resp)
    if resp.status_code != 200:
        raise RuntimeError(f"Order book HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if isinstance(body, dict) and body.get("status") is False:
        raise RuntimeError(f"Order book API error: {body.get('message') or body.get('errorcode') or body}")
    return parse_order_book(body)


def fetch_ltp(token: str, instrument_token: str, broker: str = "angelone", trading_symbol: str = "", exchange: str = "NFO") -> Optional[float]:
    broker = (broker or "angelone").lower().strip()
    if broker not in ("angelone", "angel"):
        raise RuntimeError(f"fetch_ltp not implemented for broker '{broker}'")
    if token == MOCK_TOKEN:
        return None
    if not instrument_token:
        return None
    url = f"{ANGELONE_BASE}/rest/secure/angelbroking/market/v1/quote/"
    payload = {
        "mode": "LTP",
        "exchangeTokens": {
            exchange: [str(instrument_token)]
        }
    }
    headers = _headers(token)
    if LOG_API:
        _log_api_request("POST", url, headers, payload)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        if LOG_API:
            _log_api_response("POST", url, resp)
        if resp.status_code != 200 or not resp.text.strip():
            logger.error("Quote HTTP %d: %s", resp.status_code, resp.text[:300])
            return None
        body = resp.json()
        if body.get("status") is not True:
            logger.error("Quote API error: %s", body.get("message") or body)
            return None
        fetched = (body.get("data") or {}).get("fetched", [])
        if fetched and isinstance(fetched, list):
            ltp_val = _to_float(fetched[0].get("ltp"))
            if ltp_val > 0:
                return ltp_val
    except Exception as exc:
        logger.error("fetch_ltp exception for token %s: %s", instrument_token, exc)
    return None


def cancel_order(token: str, order_id: str, broker: str = "angelone", variety: str = "STOPLOSS") -> Dict[str, Any]:
    broker = (broker or "angelone").lower().strip()
    if broker not in ("angelone", "angel"):
        raise RuntimeError(f"Cancel adapter is not implemented for broker '{broker}'")
    if token == MOCK_TOKEN:
        return {"action": "cancel", "order_id": order_id, "success": True, "http_status": 200, "response": {"status": True}}
    url = f"{ANGELONE_BASE}/rest/secure/angelbroking/order/v1/cancelOrder"
    payload = {
        "variety": variety,
        "orderid": str(order_id),
    }
    headers = _headers(token)
    if LOG_API:
        _log_api_request("POST", url, headers, payload)
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    if LOG_API:
        _log_api_response("POST", url, resp)
    try:
        body = resp.json()
    except Exception:
        body = {"raw": getattr(resp, "text", "")[:300]}
    success = (200 <= getattr(resp, "status_code", 0) < 300 and isinstance(body, dict) and body.get("status") is True)
    return {"action": "cancel", "order_id": order_id, "success": success, "http_status": getattr(resp, "status_code", None), "response": body}


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
    raw = str(position.get("product") or position.get("producttype") or "").upper().strip()
    if raw in ("CARRYFORWARD", "NRML", "CNC", "D"):
        return "CARRYFORWARD"
    if raw in ("INTRADAY", "MIS", "I"):
        return "INTRADAY"
    if cli_product:
        cli = str(cli_product).upper().strip()
        if cli in ("CARRYFORWARD", "INTRADAY"):
            return cli
    return "CARRYFORWARD"


def place_exit_order_slm(
    token: str,
    position: Dict[str, Any],
    target_qty: int,
    trigger_price: float,
    product: str = PRODUCT_NRML,
    tag: str = DEFAULT_TAG,
    broker: str = "angelone",
) -> Dict[str, Any]:
    broker = (broker or "angelone").lower().strip()
    if broker not in ("angelone", "angel"):
        raise RuntimeError(f"Live order adapter is not implemented for broker '{broker}'")
    trigger_price = round_to_tick(trigger_price)
    sym = position.get("tradingsymbol") or position.get("trading_symbol")
    tok = str(position.get("symboltoken") or position.get("instrument_token") or "")
    exch = str(position.get("exchange") or "NFO").upper()
    prod = str(product or position.get("producttype") or position.get("product") or "CARRYFORWARD").upper()
    if prod in ("D", "NRML"):
        prod = "CARRYFORWARD"
    elif prod in ("I", "MIS"):
        prod = "INTRADAY"

    payload = {
        "variety": "STOPLOSS",
        "tradingsymbol": sym,
        "symboltoken": tok,
        "transactiontype": "SELL",
        "exchange": exch,
        "ordertype": "STOPLOSS_MARKET",
        "producttype": prod,
        "duration": "DAY",
        "price": "0",
        "triggerprice": f"{trigger_price:.2f}",
        "quantity": str(int(target_qty)),
    }
    if token == MOCK_TOKEN:
        mock_oid = f"mock_{int(time.time()*1000)}"
        return {"action": "place", "request": payload, "success": True, "order_id": mock_oid, "response": {"status": True, "data": {"orderid": mock_oid}}}
    url = f"{ANGELONE_BASE}/rest/secure/angelbroking/order/v1/placeOrder"
    headers = _headers(token)
    if LOG_API:
        _log_api_request("POST", url, headers, payload)
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    if LOG_API:
        _log_api_response("POST", url, resp)
    return _order_response("place", resp, payload)


def place_exit_order_market(
    token: str,
    position: Dict[str, Any],
    target_qty: int,
    product: str = PRODUCT_NRML,
    tag: str = DEFAULT_TAG,
    broker: str = "angelone",
) -> Dict[str, Any]:
    broker = (broker or "angelone").lower().strip()
    if broker not in ("angelone", "angel"):
        raise RuntimeError(f"Live order adapter is not implemented for broker '{broker}'")
    sym = position.get("tradingsymbol") or position.get("trading_symbol")
    tok = str(position.get("symboltoken") or position.get("instrument_token") or "")
    exch = str(position.get("exchange") or "NFO").upper()
    prod = str(product or position.get("producttype") or position.get("product") or "CARRYFORWARD").upper()
    if prod in ("D", "NRML"):
        prod = "CARRYFORWARD"
    elif prod in ("I", "MIS"):
        prod = "INTRADAY"

    payload = {
        "variety": "NORMAL",
        "tradingsymbol": sym,
        "symboltoken": tok,
        "transactiontype": "SELL",
        "exchange": exch,
        "ordertype": "MARKET",
        "producttype": prod,
        "duration": "DAY",
        "price": "0",
        "triggerprice": "0",
        "quantity": str(int(target_qty)),
    }
    if token == MOCK_TOKEN:
        mock_oid = f"mock_{int(time.time()*1000)}"
        return {"action": "place_market", "request": payload, "success": True, "order_id": mock_oid, "response": {"status": True, "data": {"orderid": mock_oid}}}
    url = f"{ANGELONE_BASE}/rest/secure/angelbroking/order/v1/placeOrder"
    headers = _headers(token)
    if LOG_API:
        _log_api_request("POST", url, headers, payload)
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    if LOG_API:
        _log_api_response("POST", url, resp)
    return _order_response("place_market", resp, payload)


def modify_exit_order_slm(
    token: str,
    order: Dict[str, Any],
    target_qty: int,
    trigger_price: float,
    tag: str = DEFAULT_TAG,
    broker: str = "angelone",
) -> Dict[str, Any]:
    broker = (broker or "angelone").lower().strip()
    if broker not in ("angelone", "angel"):
        raise RuntimeError(f"Live order adapter is not implemented for broker '{broker}'")
    trigger_price = round_to_tick(trigger_price)
    total_qty = order_filled_quantity(order) + target_qty
    oid = str(order.get("order_id") or order.get("orderid") or "")
    sym = order.get("trading_symbol") or order.get("tradingsymbol")
    tok = str(order.get("instrument_token") or order.get("symboltoken") or "")
    exch = str(order.get("exchange") or "NFO").upper()
    prod = str(order.get("product") or order.get("producttype") or "CARRYFORWARD").upper()
    if prod in ("D", "NRML"):
        prod = "CARRYFORWARD"

    payload = {
        "variety": "STOPLOSS",
        "orderid": oid,
        "ordertype": "STOPLOSS_MARKET",
        "producttype": prod,
        "duration": "DAY",
        "price": "0",
        "triggerprice": f"{trigger_price:.2f}",
        "quantity": str(int(total_qty)),
        "tradingsymbol": sym,
        "symboltoken": tok,
        "exchange": exch,
    }
    if token == MOCK_TOKEN:
        return {"action": "modify", "request": payload, "success": True, "order_id": oid, "response": {"status": True, "data": {"orderid": oid}}}
    url = f"{ANGELONE_BASE}/rest/secure/angelbroking/order/v1/modifyOrder"
    headers = _headers(token)
    if LOG_API:
        _log_api_request("POST", url, headers, payload)
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    if LOG_API:
        _log_api_response("POST", url, resp)
    return _order_response("modify", resp, payload)


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


def _save_trailing_state(r, keys: Dict[str, str], max_val: float, sl_val: float, avg_val: float, qty_val: int, order_id: str, entry_order_id: Optional[str]) -> bool:
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
        pipe.execute()
        return True
    except Exception as exc:
        logger.critical("Failed to save Redis trailing state: %s", exc)
        return False


def _cleanup_thursday_leg_state(r, keys: Dict[str, str]) -> None:
    """Clean up Redis trailing state and authorization for a closed Thursday leg."""
    if not r:
        return
    try:
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
        if keys.get("max_short"):
            pipe.delete(keys["max_short"])
        if keys.get("sl_short"):
            pipe.delete(keys["sl_short"])
        if keys.get("avg_short"):
            pipe.delete(keys["avg_short"])
        pipe.execute()
        logger.info("Cleared Redis trailing state & authorization for %s", keys["enabled"])
    except Exception as exc:
        logger.error("Failed to clear Redis leg state: %s", exc)


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
                    break

        if has_active_pos:
            continue

        # Check 2: Does an open managed SELL order exist in broker order book for this contract?
        has_open_order = False
        eff_symbol = leg_symbol or f"NIFTY{leg_strike}{leg_opt}"
        eff_token = leg_token or "NOTOKEN"
        leg_keys = build_trail_redis_keys(broker, eff_symbol, eff_token, active_expiry=eff_expiry)
        stored_order_id = _redis_text(r, leg_keys["order_id"]) if r else None

        for o in orders:
            if not is_open_sell_order(o):
                continue
            oid = str(o.get("order_id") or o.get("orderid") or "").strip()
            o_tag = order_tag(o).upper()
            is_managed = (o_tag == tag.upper()) or (stored_order_id and oid and oid == stored_order_id)
            if not is_managed:
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
               or (stored_order_id and oid and oid == stored_order_id):
                has_open_order = True
                break

        if has_open_order:
            continue

        # All checks passed: No short position, long position is 0, AND open order is absent/terminal.
        _cleanup_thursday_leg_state(r, leg_keys)

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
        cleaned.append({"key": enabled_key, "symbol": leg_symbol, "strike": leg_strike, "option_type": leg_opt, "expiry": leg_expiry})

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

    # Authoritative set of Thursday managed order IDs from Redis (needed because AngelOne does not return tags in orderbook)
    managed_order_ids_in_redis = set()
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
            set_ids = r.smembers(f"fno:{broker}:managed_order_ids") or []
            for sid in set_ids:
                s_text = sid.decode("utf-8") if isinstance(sid, bytes) else str(sid)
                if s_text:
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
        order_id = str(order.get("order_id") or "")
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
            _cleanup_thursday_leg_state(r, keys)
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
        manual_sells = [
            o for o in matching_sells
            if o not in managed_sells and (not order_tag(o) or order_tag(o).upper() not in (OTHER_CYCLE_TAGS | {tag.upper()}))
        ]
        other_cycle_sells = [
            o for o in matching_sells
            if o not in managed_sells and order_tag(o).upper() in OTHER_CYCLE_TAGS
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

        entry_order_id, entry_order, entry_id_source = latest_filled_buy_order(pos, orders)
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
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_failed", "order_id": existing_order_id, "mode": "immediate",
                        "error": cancel_res.get("error"), "success": False,
                    }
                    actions.append(base)
                    alerts.append({"severity": "CRITICAL", **base})
                    continue

                # Verify terminal status in orderbook
                time.sleep(0.5)
                fresh_orders = fetch_order_book(token, broker=broker)
                fresh_existing = next((o for o in fresh_orders if str(o.get("order_id") or o.get("orderid") or "") == existing_order_id), None)
                if fresh_existing is None:
                    logger.warning(
                        "[%s] SL-M %s missing from follow-up order book after cancel. Retaining Redis state and deferring market order.",
                        symbol, existing_order_id
                    )
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_missing", "order_id": existing_order_id, "mode": "immediate",
                        "reason": f"SL-M order {existing_order_id} missing from follow-up order book; deferring market order until terminal confirmation",
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
                    logger.warning(
                        "[%s] SL-M %s has unknown/unrecognized status '%s'. Retaining Redis state and deferring market order.",
                        symbol, existing_order_id, fresh_status
                    )
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_unknown_status", "order_id": existing_order_id, "mode": "immediate",
                        "reason": f"SL-M order {existing_order_id} has unrecognized status '{fresh_status}'; deferring market order until confirmed terminal",
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
                    _cleanup_thursday_leg_state(r, keys)
                    continue
                managed_qty = min(remaining_qty, leg_target_qty)

            # If no open managed order was found, but a prior managed SL-M order_id is in Redis:
            # verify that the prior SL-M order is explicitly terminal (cancelled/filled) before placing market order
            if not existing and stored_order_id:
                prior_slm = next((o for o in orders if str(o.get("order_id") or o.get("orderid") or "") == stored_order_id), None)
                if prior_slm is None:
                    logger.warning(
                        "[%s] Prior managed SL-M %s in Redis is missing from broker order book. Retaining Redis state and deferring market order.",
                        symbol, stored_order_id
                    )
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_missing", "order_id": stored_order_id, "mode": "immediate",
                        "reason": f"Prior managed SL-M {stored_order_id} missing from order book; deferring market order until confirmed terminal",
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
                    logger.warning(
                        "[%s] Prior managed SL-M %s has unknown status '%s'. Retaining Redis state and deferring market order.",
                        symbol, stored_order_id, prior_slm_status
                    )
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "cancel_slm_unknown_status", "order_id": stored_order_id, "mode": "immediate",
                        "reason": f"Prior managed SL-M {stored_order_id} has unrecognized status '{prior_slm_status}'; deferring market order",
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

                # Case 2: Prior market order was rejected with 0 fills
                if prior_status == "rejected" and total_market_filled == 0:
                    base = {
                        "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                        "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                        "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                        "status": "market_order_rejected", "order_id": stored_market_id, "mode": "immediate",
                        "error": f"Prior market order {stored_market_id} was rejected", "success": False,
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
                        r.sadd(f"fno:{broker}:managed_order_ids", new_market_id)
                    logger.info("[%s] Successfully placed immediate MARKET order for %d qty (order_id: %s)", symbol, managed_qty, new_market_id)
                else:
                    base["error"] = result.get("error")
                    logger.error("[%s] Market order placement failed: %s", symbol, result.get("error"))
            except Exception as exc:
                base = {
                    "cycle": CYCLE_NAME, "track": CYCLE_TRACK, "broker": broker, "symbol": symbol,
                    "instrument_token": token_key, "tag": tag, "quantity": managed_qty,
                    "target_quantity": leg_target_qty, "total_broker_qty": total_broker_qty,
                    "product": product_for_position, "status": "execution_failed", "error": str(exc), "success": False, "mode": "immediate",
                }
                logger.error("[%s] Exception during market order placement: %s", symbol, exc)
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

        avg_price = _to_float(pos.get("average_price"))
        if avg_price <= 0:
            avg_price = old_avg if old_avg > 0 else ltp

        if lifecycle_changed:
            old_max = avg_price
            old_sl = 0.0
            old_avg = avg_price
            old_qty = managed_qty

        new_max = max(old_max, ltp, avg_price)
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
            "old_sl": round(old_sl, 2) if old_sl else 0, "existing_trigger": round(existing_trigger, 2) if existing_trigger else 0,
            "existing_pending_quantity": existing_pending_qty if existing else None,
            "final_stop": round(final_stop, 2), "phase": phase, "existing_order_id": existing_order_id or None,
        }

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
                _save_trailing_state(r, keys, new_max, final_stop, avg_price, managed_qty, new_order_id, entry_order_id)
                logger.info("[%s] Successfully executed %s order for %d qty at trigger %.2f (order_id: %s)", symbol, base["status"], managed_qty, final_stop, new_order_id)
            else:
                logger.error("[%s] Order execution failed: %s", symbol, result.get("error"))
        except Exception as exc:
            base.update({"status": "execution_failed", "error": str(exc), "success": False})
            logger.error("[%s] Exception during order execution: %s", symbol, exc)

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
    parser.add_argument("--broker", default=DEFAULT_BROKER, help=f"Broker adapter (default: {DEFAULT_BROKER})")
    parser.add_argument("--lots", type=int, default=int(os.getenv("LOTS_THURSDAY", "1")), help="Allocated lots for Thursday strategy (default from LOTS_THURSDAY or 1)")
    parser.add_argument("--lot-size", type=int, default=DEFAULT_LOT_SIZE, help=f"Nifty contract lot size (default {DEFAULT_LOT_SIZE})")
    parser.add_argument("--trail-pct", type=float, default=DEFAULT_TRAIL_PCT, help=f"Trail %% below high-water mark (default {DEFAULT_TRAIL_PCT})")
    parser.add_argument("--initial-stop-pct", type=float, default=DEFAULT_INITIAL_STOP_PCT, help=f"Initial stop %% below average before activation (default {DEFAULT_INITIAL_STOP_PCT})")
    parser.add_argument("--activation-pct", type=float, default=DEFAULT_ACTIVATION_PCT, help="Start trailing after LTP reaches average*(1+activation/100); 0=from start")
    parser.add_argument("--min-ratchet-step", type=float, default=DEFAULT_MIN_RATCHET_STEP, help=f"Minimum ₹ increase to modify order (default ₹{DEFAULT_MIN_RATCHET_STEP})")
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

    # 1. Master Environment Gate: Check LIVE_EXIT_TRIGGER_THURSDAY in .env
    live_env_enabled = os.getenv("LIVE_EXIT_TRIGGER_THURSDAY", "false").strip().lower() in ("true", "1", "yes")
    exit_immediately = args.exit_immediately or (os.getenv("EXIT_IMMEDIATELY_THURSDAY", "false").strip().lower() in ("true", "1", "yes"))
    actual_execute = args.execute and live_env_enabled and not args.dry_run

    if args.execute and not live_env_enabled:
        logger.warning(
            "LIVE_EXIT_TRIGGER_THURSDAY is disabled/false in .env. Enforcing safe DRY-RUN mode (manual square-off mode active)."
        )

    # 2. Fast 1-Millisecond Redis Gate: Check ORDERS_LIVE_THU
    r = None if args.mock else get_redis_client()
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

    # 3. Dedicated Broker Selection (Thursday is strictly pinned to AngelOne)
    requested_broker = (args.broker or DEFAULT_BROKER).lower().strip()
    if requested_broker not in ("angelone", "angel"):
        logger.critical("Broker '%s' is not supported. Thursday F&O exit orders strictly support 'angelone'.", requested_broker)
        print(json.dumps({
            "error": "unsupported_broker",
            "broker": requested_broker,
            "message": "Only AngelOne is supported for Thursday F&O Exit Orders"
        }, indent=2))
        return 1
    active_broker = "angelone"

    # 4. Fetch Positions and Orders under Cycle-Isolated Execution Lock
    abort_event = threading.Event()
    with fno_execution_lock(
        r, lock_key=DEFAULT_LOCK_KEY, timeout=60, max_wait=10.0, abort_event=abort_event
    ) as acquired:
        if not acquired:
            logger.warning(
                "Could not acquire distributed execution lock '%s' within 10s. Another manager is running. Exiting to avoid concurrent order conflict.",
                DEFAULT_LOCK_KEY
            )
            print(json.dumps({
                "status": "lock_busy",
                "cycle": CYCLE_NAME,
                "message": "Distributed lock busy; another manager is running. Skipping this cycle to prevent race condition."
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
                # Check if all legs are closed; if so, reset ORDERS_LIVE_THU = 0
                _check_and_reset_orders_live_thu(r, broker=active_broker)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if summary.get("fatal_error") else 0


if __name__ == "__main__":
    sys.exit(main())
