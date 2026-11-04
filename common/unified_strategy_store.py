#!/usr/bin/env python3
"""
unified_strategy_store.py - Unified Active Options Strategy & Live Telemetry Store.

Manages:
1. `active_strategies.json`: Single source of truth holding both concurrent strategy tracks:
   - `5day_strategy`: Master Tuesday track (Tuesday 09:31 IST to Monday expiry).
   - `3day_strategy`: Parallel Thursday track (Thursday 09:31 IST to Monday expiry).
   Guarantees independent rotation (Tuesday updates 5day, Thursday updates 3day without overwriting each other).
2. Human-Readable Simple Action Mapping:
   Translates cryptic internal labels into plain-English terms:
   - `CALL ONLY EXIT`, `PUT ONLY EXIT`, `BOTH CALL & PUT EXIT`, `20% PEAK PROFIT REACHED`.
3. Post-Discord Exit Signal Persistence:
   Directly called after every Discord channel alert dispatch to update JSON and MongoDB Atlas.
4. MongoDB Atlas Telemetry & Event Streaming:
   - `derivative_active_cycles`: Living snapshot document (_id: "ACTIVE_CYCLES") for O(1) portal queries.
   - `derivative_cycle_live_logs`: High-resolution single-row telemetry feed (5-min ticks and exit events).
"""

import os
import sys
import json
import time
import uuid
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows fallback

# Ensure project root is in sys.path
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_CURRENT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import (
    logger,
    SNAPSHOT_DIR,
    ACTIVE_STRATEGIES_JSON,
    ACTIVE_SNAPSHOT_FILE,
    THURSDAY_SNAPSHOT_FILE,
    MONGODB_URI,
    MONGODB_DATABASE,
    MONGODB_COLLECTION_DERIVATIVE_ACTIVE_CYCLES,
    MONGODB_COLLECTION_DERIVATIVE_CYCLE_LIVE_LOGS,
    NIFTY_LOT_SIZE,
    get_redis_client,
)
from common.fno_keys import build_leg_enabled_key, normalize_expiry

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except ImportError:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Simple Action & Track Normalization Helpers
# ---------------------------------------------------------------------------

def normalize_track(track: Optional[str]) -> str:
    """Normalize input track identifier to '5day_strategy' or '3day_strategy'."""
    if not track:
        return "5day_strategy"
    t = str(track).strip().lower()
    if t in ("thursday", "3day", "3day_strategy", "thu"):
        return "3day_strategy"
    return "5day_strategy"


def get_track_label(track: str) -> str:
    """Return user-friendly label for track."""
    norm = normalize_track(track)
    return "3-Day Thursday Strategy" if norm == "3day_strategy" else "5-Day Tuesday Strategy"


def to_simple_action(action: Optional[str], leg: Optional[str] = None) -> str:
    """
    Convert cryptic action strings into clear, transparent, human-readable terms.

    Examples:
        - 'TAKE_PROFIT_CALL', 'STOP_LOSS_CALL', 'HARVESTED_CALL' -> 'CALL ONLY EXIT'
        - 'TAKE_PROFIT_PUT', 'STOP_LOSS_PUT', 'HARVESTED_PUT' -> 'PUT ONLY EXIT'
        - 'TAKE_PROFIT_BOTH', 'EXIT_BOTH', 'DUAL_EXIT' -> 'BOTH CALL & PUT EXIT'
        - 'PEAK_20PCT' -> '20% PEAK PROFIT REACHED'
    """
    if not action:
        return "HOLDING (NO EXIT)"
    act = str(action).upper().strip()

    if "PEAK" in act:
        return "20% PEAK PROFIT REACHED"

    if "BOTH" in act:
        return "BOTH CALL & PUT EXIT"

    if "CALL" in act:
        return "CALL ONLY EXIT"
    if "PUT" in act:
        return "PUT ONLY EXIT"

    if leg:
        leg_up = str(leg).upper().strip()
        if leg_up == "CALL":
            return "CALL ONLY EXIT"
        if leg_up == "PUT":
            return "PUT ONLY EXIT"
        if leg_up == "BOTH":
            return "BOTH CALL & PUT EXIT"

    return act


def _now_utc_ts() -> int:
    """Return current Unix timestamp in seconds."""
    return int(datetime.now(UTC).timestamp())


def _now_ist_str() -> str:
    """Return current timestamp formatted in IST."""
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _format_ist_ts(epoch_sec: Optional[int]) -> str:
    """Format Unix timestamp as IST string."""
    if not epoch_sec or epoch_sec <= 0:
        return ""
    try:
        dt = datetime.fromtimestamp(int(epoch_sec), tz=UTC).astimezone(IST)
        return dt.strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Default Template Factory
# ---------------------------------------------------------------------------

def create_empty_strategy_record(track: str) -> Dict[str, Any]:
    """Create a pristine strategy record template."""
    norm = normalize_track(track)
    is_thu = norm == "3day_strategy"
    return {
        "cycle_id": None,
        "track": norm,
        "track_label": get_track_label(norm),
        "status": "pending_entry",
        "start_date": None,
        "expiry_date": None,
        "moneyness": "ATM" if is_thu else "ITM",
        "call_strike": None,
        "put_strike": None,
        "day1_locked_price": None,
        "latest_ltp": None,
        "exit_signals": {
            "peak_20pct": {
                "status": "PENDING",
                "triggered": False,
                "display_symbol": "20% PEAK PROFIT REACHED",
                "timestamp": None,
                "time_ist": None,
                "peak_pnl_pct": None,
                "peak_pnl_pts": None,
                "peak_pnl_inr": None,
                "details": None,
            },
            "alpha_fsm": {
                "status": "PENDING",
                "triggered": False,
                "display_symbol": None,
                "timestamp": None,
                "time_ist": None,
                "call_status": "ACTIVE",
                "put_status": "ACTIVE",
                "simple_action": None,
                "realized_pnl_pts": 0.0,
                "realized_pnl_inr": 0.0,
                "details": None,
            },
            "ai_inference": {
                "status": "PENDING",
                "triggered": False,
                "display_symbol": None,
                "timestamp": None,
                "time_ist": None,
                "simple_action": None,
                "matched_rule": None,
                "confidence": None,
                "details": None,
            },
        },
        "updated_at_ist": _now_ist_str(),
        "updated_ts": _now_utc_ts(),
    }


def create_default_active_strategies() -> Dict[str, Any]:
    """Return an initialized active_strategies dictionary."""
    return {
        "5day_strategy": create_empty_strategy_record("5day_strategy"),
        "3day_strategy": create_empty_strategy_record("3day_strategy"),
    }


# ---------------------------------------------------------------------------
# Atomic File I/O with Process Locks
# ---------------------------------------------------------------------------

def _ensure_dir(path: str) -> None:
    """Create directory if missing."""
    os.makedirs(path, exist_ok=True)


def _atomic_write_json(file_path: str, data: dict) -> None:
    """Write JSON data to an isolated temp file and atomically replace target."""
    _ensure_dir(os.path.dirname(os.path.abspath(file_path)))
    tmp_path = f"{file_path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_path, file_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


_STRATEGY_LOCKS: Dict[str, Dict[str, Any]] = {}
_LOCKS_MUTEX = threading.Lock()


def _get_lock_entry(lock_file: str) -> Dict[str, Any]:
    with _LOCKS_MUTEX:
        if lock_file not in _STRATEGY_LOCKS:
            _STRATEGY_LOCKS[lock_file] = {
                "thread_lock": threading.RLock(),
                "fd": None,
                "depth": 0,
            }
        return _STRATEGY_LOCKS[lock_file]


@contextmanager
def strategy_lock(filepath: Optional[str] = None):
    """
    Re-entrant, thread-safe, and process-safe exclusive lock covering read-modify-write cycles.
    Prevents race conditions between 5-min poller, collector, and exit handlers.
    Safe against self-deadlock within the same process.
    """
    target = os.path.abspath(filepath or ACTIVE_STRATEGIES_JSON)
    lock_file = f"{target}.lock"
    _ensure_dir(os.path.dirname(lock_file))

    entry = _get_lock_entry(lock_file)
    entry["thread_lock"].acquire()
    try:
        if entry["depth"] == 0:
            if fcntl:
                fd = open(lock_file, "w")
                fcntl.flock(fd, fcntl.LOCK_EX)
                entry["fd"] = fd
        entry["depth"] += 1
        yield
    finally:
        entry["depth"] -= 1
        if entry["depth"] == 0:
            fd = entry.get("fd")
            if fcntl and fd:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    fd.close()
                except Exception:
                    pass
                entry["fd"] = None
        entry["thread_lock"].release()


def load_active_strategies(filepath: Optional[str] = None) -> Dict[str, Any]:
    """
    Load the unified active strategies JSON file under a safe process lock.

    Returns default template if the file does not exist or is empty.
    """
    target = filepath or ACTIVE_STRATEGIES_JSON
    if not os.path.exists(target):
        return create_default_active_strategies()

    with strategy_lock(target):
        try:
            with open(target, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            # Ensure both top-level keys exist
            if "5day_strategy" not in data:
                data["5day_strategy"] = create_empty_strategy_record("5day_strategy")
            if "3day_strategy" not in data:
                data["3day_strategy"] = create_empty_strategy_record("3day_strategy")
            return data

        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not read %s (%s). Returning default template.", target, e)
            return create_default_active_strategies()


def get_active_leg_entry_details(
    track: str,
    strike: int,
    option_type: str,
    expiry: Optional[str] = None,
    filepath: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the entry order ID and fill basis owned by one track/option leg.

    Broker position APIs report net positions, which can combine both tracks
    when they use the same broker and contract. Prefer the per-cycle BUY order
    ID and locked fill price; never infer this leg's entry from the most recent
    BUY for the shared symbol.
    """
    norm = normalize_track(track)
    opt = str(option_type or "").upper().strip()
    if opt not in ("CE", "PE"):
        return {}
    prefix = "call" if opt == "CE" else "put"
    strike_field = f"{prefix}_strike"

    active = load_active_strategies(filepath=filepath)
    strategy = active.get(norm) or {}
    active_strike = strategy.get(strike_field)
    if active_strike is not None and int(active_strike) != int(strike):
        strategy = {}

    locked = strategy.get("day1_locked_price") or {}
    details: Dict[str, Any] = {
        "cycle_id": strategy.get("cycle_id"),
        "expiry": strategy.get("expiry_date") or strategy.get("expiry"),
        "broker": strategy.get("broker") or locked.get("broker"),
        "entry_status": strategy.get("entry_status") or locked.get("entry_status"),
        "entry_order_id": locked.get(f"{prefix}_order_id") or strategy.get(f"{prefix}_order_id"),
        "entry_price": locked.get(f"{prefix}_buy_price") or strategy.get(f"{prefix}_buy_price"),
    }

    # The active snapshot is a compatibility source for cycles restored before
    # the unified store was initialized or for snapshots not yet bootstrapped.
    if not details.get("entry_order_id") or not details.get("entry_price") or not details.get("broker"):
        snapshot_path = ACTIVE_SNAPSHOT_FILE if norm == "5day_strategy" else THURSDAY_SNAPSHOT_FILE
        try:
            with open(snapshot_path, "r", encoding="utf-8") as snapshot_file:
                snapshot = json.load(snapshot_file)
            if int(snapshot.get(strike_field) or 0) == int(strike):
                details["cycle_id"] = details.get("cycle_id") or snapshot.get("cycle_id")
                details["expiry"] = details.get("expiry") or snapshot.get("expiry_date") or snapshot.get("expiry")
                details["broker"] = details.get("broker") or snapshot.get("broker")
                details["entry_status"] = details.get("entry_status") or snapshot.get("entry_status")
                details["entry_order_id"] = details.get("entry_order_id") or snapshot.get(f"{prefix}_order_id")
                details["entry_price"] = details.get("entry_price") or snapshot.get(f"{prefix}_buy_price")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    try:
        details["entry_price"] = float(details.get("entry_price") or 0.0)
    except (TypeError, ValueError):
        details["entry_price"] = 0.0
    details["entry_order_id"] = str(details.get("entry_order_id") or "")

    current_status = str(details.get("entry_status") or "").upper().strip()
    details["entry_status"] = current_status

    # If both live tracks own this exact contract in the same account, the
    # broker's net average price and latest BUY are not track-specific.
    other_track = "3day_strategy" if norm == "5day_strategy" else "5day_strategy"
    other = active.get(other_track) or {}
    other_locked = other.get("day1_locked_price") or {}
    other_entry_status = str(
        other.get("entry_status") or other_locked.get("entry_status") or ""
    ).upper().strip()
    other_broker = other.get("broker") or other_locked.get("broker")
    other_active_strike = other.get(strike_field)
    current_broker = details.get("broker")
    current_expiry = normalize_expiry(expiry or details.get("expiry"))
    other_expiry = normalize_expiry(other.get("expiry_date") or other.get("expiry"))
    same_expiry = (
        current_expiry == other_expiry
        if current_expiry != "UNKNOWN" and other_expiry != "UNKNOWN"
        else True
    )
    if not current_broker or not other_broker:
        try:
            from config import get_broker_for_track
            current_broker = current_broker or get_broker_for_track(norm)
            other_broker = other_broker or get_broker_for_track(other_track)
        except ValueError:
            pass

    other_status = str(other.get("status") or "").lower().strip()
    details["same_broker_contract_overlap"] = bool(
        current_status == "FILLED"
        and other_entry_status == "FILLED"
        and other_status not in ("closed", "aborted", "skipped", "pending_entry")
        and other_broker
        and current_broker
        and str(other_broker).lower() == str(current_broker).lower()
        and other_active_strike is not None
        and int(other_active_strike) == int(strike)
        and same_expiry
    )
    details["ambiguous_entry_attribution"] = bool(
        details["same_broker_contract_overlap"] and not details["entry_order_id"]
    )
    return details


def resolve_cycle_entry_qty(track: str, strategy_data: Dict[str, Any], snapshot: Optional[Dict[str, Any]] = None) -> int:
    """
    Quantity (contracts) a cycle bought per leg.

    Order: recorded entry_qty (unified record / locked entry) -> same-cycle entry snapshot
    entry_qty -> LOTS_TUESDAY / LOTS_THURSDAY x NIFTY_LOT_SIZE (legacy cycles).
    """
    norm = normalize_track(track)
    locked = (strategy_data or {}).get("day1_locked_price") or {}
    raw = (strategy_data or {}).get("entry_qty") or locked.get("entry_qty")
    if not raw and snapshot and snapshot.get("cycle_id") and snapshot.get("cycle_id") == (strategy_data or {}).get("cycle_id"):
        raw = snapshot.get("entry_qty")
    try:
        qty = int(raw or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty > 0:
        return qty
    lot_size = int(os.getenv("NIFTY_LOT_SIZE", str(NIFTY_LOT_SIZE)))
    lots_env = "LOTS_TUESDAY" if norm == "5day_strategy" else "LOTS_THURSDAY"
    default_lots = 2 if norm == "5day_strategy" else 1
    lots = int((strategy_data or {}).get("lots") or os.getenv(lots_env, str(default_lots)))
    return lots * lot_size


def get_track_remaining_owned_qty(
    track: str,
    strike: int,
    option_type: str,
    expiry: Optional[str],
    broker: str,
    redis_client: Optional[Any] = None,
    filepath: Optional[str] = None,
    filled_today: int = 0,
) -> int:
    """
    Quantity of one contract that `track` still owns on `broker`
    (entry qty minus quantity already exited by its manager or marked as a manual exit).

    Returns 0 when the track holds no live FILLED position in this exact contract on this broker.
    Used by the exit managers to reserve the other track's lot on a shared broker contract.
    """
    norm = normalize_track(track)
    opt = str(option_type or "").upper().strip()
    if opt not in ("CE", "PE"):
        return 0
    strat = load_active_strategies(filepath=filepath).get(norm) or {}
    locked = strat.get("day1_locked_price") or {}
    entry_status = str(strat.get("entry_status") or locked.get("entry_status") or "").upper().strip()
    if entry_status != "FILLED":
        return 0
    if str(strat.get("status") or "").lower().strip() in ("closed", "aborted", "skipped", "pending_entry"):
        return 0
    strat_broker = str(strat.get("broker") or locked.get("broker") or "").lower().strip()
    if not strat_broker or strat_broker != str(broker or "").lower().strip():
        return 0
    held_strike = strat.get("call_strike" if opt == "CE" else "put_strike")
    if held_strike is None or int(held_strike) != int(strike):
        return 0
    held_expiry = normalize_expiry(strat.get("expiry_date") or strat.get("expiry"))
    if held_expiry != normalize_expiry(expiry):
        return 0

    snapshot = None
    try:
        snap_file = ACTIVE_SNAPSHOT_FILE if norm == "5day_strategy" else THURSDAY_SNAPSHOT_FILE
        with open(snap_file, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except Exception:
        snapshot = None
    owned = resolve_cycle_entry_qty(norm, strat, snapshot)

    exited = 0
    if redis_client is not None:
        from common.fno_keys import build_exited_qty_key
        cycle = "tue" if norm == "5day_strategy" else "thu"
        try:
            exited = int(float(redis_client.get(build_exited_qty_key(cycle, held_expiry, strike, opt, broker)) or 0))
        except Exception as exc:
            logger.warning("Could not read exited qty for %s %s%s: %s", norm, strike, opt, exc)
    # filled_today: the track's filled SELLs visible in today's broker order book, which its own
    # manager may not have recorded yet (e.g. filled seconds ago).
    return max(0, owned - max(exited, int(filled_today or 0)))


def save_active_strategies(data: Dict[str, Any], filepath: Optional[str] = None) -> None:
    """
    Save the unified active strategies JSON file under an exclusive file lock.
    """
    target = filepath or ACTIVE_STRATEGIES_JSON
    with strategy_lock(target):
        _atomic_write_json(target, data)


# ---------------------------------------------------------------------------
# MongoDB Atlas Persistence Helper
# ---------------------------------------------------------------------------

_MONGO_CLIENT = None
_MONGO_DB = None


def get_mongodb_db(uri: Optional[str] = None, db_name: Optional[str] = None):
    """Return MongoDB Database instance (or None if unconfigured / unavailable)."""
    global _MONGO_CLIENT, _MONGO_DB
    if _MONGO_DB is not None:
        return _MONGO_DB

    mongo_uri = uri or MONGODB_URI
    if not mongo_uri or not mongo_uri.strip():
        return None

    target_db_name = db_name or MONGODB_DATABASE or "stock_recommendations"
    try:
        from pymongo import MongoClient
        _MONGO_CLIENT = MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=4000,
            connectTimeoutMS=4000,
        )
        _MONGO_DB = _MONGO_CLIENT[target_db_name]
        return _MONGO_DB
    except Exception as e:
        logger.debug("Failed to connect to MongoDB Atlas (%s): %s", target_db_name, e)
        return None


def sync_active_cycles_to_mongodb(active_data: Dict[str, Any]) -> bool:
    """
    Upsert the full active strategies state into collection `derivative_active_cycles`.
    """
    try:
        db = get_mongodb_db()
        if db is None:
            return False

        col = db[MONGODB_COLLECTION_DERIVATIVE_ACTIVE_CYCLES]
        doc = dict(active_data)
        doc["_id"] = "ACTIVE_CYCLES"
        doc["updated_at"] = _now_ist_str()
        doc["updated_ts"] = _now_utc_ts()

        col.replace_one({"_id": "ACTIVE_CYCLES"}, doc, upsert=True)
        return True
    except Exception as exc:
        logger.warning("MongoDB active cycles sync failed: %s", exc)
        return False


def append_telemetry_log_to_mongodb(log_record: Dict[str, Any]) -> bool:
    """
    Deprecated: Telemetry logging to MongoDB derivative_cycle_live_logs has been removed.
    Preserved as no-op for backward compatibility.
    """
    return True


def reset_cycle_live_logs_in_mongodb(track: str) -> bool:
    """
    Purge telemetry logs from prior cycles for this track in `derivative_cycle_live_logs`.
    Optimizes MongoDB Atlas storage by ensuring only the active cycle's logs are retained.
    """
    try:
        db = get_mongodb_db()
        if db is None:
            return False

        col = db[MONGODB_COLLECTION_DERIVATIVE_CYCLE_LIVE_LOGS]
        norm = normalize_track(track)
        result = col.delete_many({"strategy_track": norm})
        logger.info(
            "MongoDB live logs reset for track '%s': deleted %d prior cycle records.",
            norm, result.deleted_count
        )
        return True
    except Exception as exc:
        logger.warning("MongoDB live logs reset failed for '%s': %s", track, exc)
        return False


# ---------------------------------------------------------------------------
# Core Strategy Lifecycle Mutation APIs
# ---------------------------------------------------------------------------

def init_or_rotate_cycle(
    track: str,
    cycle_info: Dict[str, Any],
    filepath: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Initialize or rotate a strategy cycle under its designated track.

    Guarantees independent rotation:
    - If track is '5day_strategy', rotates '5day_strategy' and leaves '3day_strategy' untouched.
    - If track is '3day_strategy', rotates '3day_strategy' and leaves '5day_strategy' untouched.
    """
    norm = normalize_track(track)
    target = filepath or ACTIVE_STRATEGIES_JSON

    with strategy_lock(target):
        active = load_active_strategies(filepath=target)

        new_strat = create_empty_strategy_record(norm)
        new_strat.update(cycle_info)
        new_strat["track"] = norm
        new_strat["track_label"] = get_track_label(norm)
        new_strat["status"] = "active"
        new_strat["updated_at_ist"] = _now_ist_str()
        new_strat["updated_ts"] = _now_utc_ts()

        active[norm] = new_strat
        save_active_strategies(active, filepath=target)

    sync_active_cycles_to_mongodb(active)

    # Purge prior cycle live logs for this track from MongoDB Atlas (data size optimization)
    reset_cycle_live_logs_in_mongodb(norm)

    # Log initial cycle creation event
    append_telemetry_log_to_mongodb({
        "strategy_track": norm,
        "cycle_id": new_strat.get("cycle_id"),
        "timestamp": _now_utc_ts(),
        "time_ist": _now_ist_str(),
        "event_type": "CYCLE_INIT",
        "nifty_spot": cycle_info.get("entry_spot"),
        "call_strike": cycle_info.get("call_strike"),
        "call_ltp": None,
        "call_buy_price": None,
        "call_diff_pts": 0.0,
        "put_strike": cycle_info.get("put_strike"),
        "put_ltp": None,
        "put_buy_price": None,
        "put_diff_pts": 0.0,
        "combined_ltp": None,
        "combined_buy_price": None,
        "combined_pnl_pts": 0.0,
        "combined_pnl_pct": 0.0,
        "combined_pnl_inr": 0.0,
        "exit_signal": None,
        "exit_details": f"Cycle {new_strat.get('cycle_id')} initialized for {get_track_label(norm)}",
        "source": "unified_strategy_store:init_or_rotate_cycle",
    })

    logger.info("Rotated and initialized %s cycle: %s", norm, new_strat.get("cycle_id"))
    return active


def update_locked_prices(
    track: str,
    call_buy: float,
    put_buy: float,
    spot: Optional[float] = None,
    timestamp: Optional[int] = None,
    filepath: Optional[str] = None,
    call_order_id: Optional[str] = None,
    put_order_id: Optional[str] = None,
    call_ref_ltp: Optional[float] = None,
    put_ref_ltp: Optional[float] = None,
    broker: Optional[str] = None,
    entry_status: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Record Day 1 locked BUY order prices (baseline anchor) under the active track.
    """
    norm = normalize_track(track)
    target = filepath or ACTIVE_STRATEGIES_JSON

    ts = timestamp or _now_utc_ts()
    time_ist = _format_ist_ts(ts) or _now_ist_str()
    c_buy = round(float(call_buy), 2) if call_buy is not None else 0.0
    p_buy = round(float(put_buy), 2) if put_buy is not None else 0.0
    comb_buy = round(c_buy + p_buy, 2)
    s_val = round(float(spot), 2) if spot is not None else None

    is_filled_or_paper = (entry_status in ("FILLED", "PAPER_ENTRY")) if entry_status else (c_buy > 0 and p_buy > 0)

    if not is_filled_or_paper:
        status_norm = "skipped" if (entry_status and "SKIPPED" in entry_status) else "aborted"
        with strategy_lock(target):
            active = load_active_strategies(filepath=target)
            strat = active.get(norm, {})
            strat["day1_locked_price"] = None
            strat["status"] = status_norm
            if call_order_id:
                strat["call_order_id"] = str(call_order_id)
            if put_order_id:
                strat["put_order_id"] = str(put_order_id)
            if broker:
                strat["broker"] = str(broker)
            if entry_status:
                strat["entry_status"] = str(entry_status)
            strat["updated_at_ist"] = _now_ist_str()
            strat["updated_ts"] = _now_utc_ts()
            save_active_strategies(active, filepath=target)

        sync_active_cycles_to_mongodb(active)

        event_type = f"ENTRY_{entry_status}" if entry_status else "ENTRY_ABORTED"
        append_telemetry_log_to_mongodb({
            "strategy_track": norm,
            "cycle_id": strat.get("cycle_id"),
            "timestamp": ts,
            "time_ist": time_ist,
            "event_type": event_type,
            "nifty_spot": s_val,
            "call_strike": strat.get("call_strike"),
            "call_ltp": None,
            "call_buy_price": None,
            "call_diff_pts": 0.0,
            "put_strike": strat.get("put_strike"),
            "put_ltp": None,
            "put_buy_price": None,
            "put_diff_pts": 0.0,
            "combined_ltp": None,
            "combined_buy_price": None,
            "combined_pnl_pts": 0.0,
            "combined_pnl_pct": 0.0,
            "combined_pnl_inr": 0.0,
            "exit_signal": None,
            "exit_details": f"Day 1 BUY Order Not Filled: Status={entry_status or 'ABORTED'} ({broker or 'broker'})",
            "source": "unified_strategy_store:update_locked_prices",
        })
        logger.info("Updated %s unified store with non-entry status '%s'", norm, entry_status)
        return active

    locked_obj = {
        "call_buy_price": c_buy,
        "put_buy_price": p_buy,
        "combined_buy_price": comb_buy,
        "entry_spot": s_val,
        "locked_at_ist": time_ist,
        "locked_ts": ts,
    }
    if call_order_id:
        locked_obj["call_order_id"] = str(call_order_id)
    if put_order_id:
        locked_obj["put_order_id"] = str(put_order_id)
    if call_ref_ltp is not None:
        locked_obj["call_ref_ltp"] = round(float(call_ref_ltp), 2)
    if put_ref_ltp is not None:
        locked_obj["put_ref_ltp"] = round(float(put_ref_ltp), 2)
    if broker:
        locked_obj["broker"] = str(broker)
    if entry_status:
        locked_obj["entry_status"] = str(entry_status)

    with strategy_lock(target):
        active = load_active_strategies(filepath=target)
        strat = active.get(norm, {})
        strat["day1_locked_price"] = locked_obj
        if call_order_id:
            strat["call_order_id"] = str(call_order_id)
        if put_order_id:
            strat["put_order_id"] = str(put_order_id)
        if broker:
            strat["broker"] = str(broker)
        if entry_status:
            strat["entry_status"] = str(entry_status)
        strat["updated_at_ist"] = _now_ist_str()
        strat["updated_ts"] = _now_utc_ts()
        save_active_strategies(active, filepath=target)

    sync_active_cycles_to_mongodb(active)

    # Log LOCKED_PRICE event
    append_telemetry_log_to_mongodb({
        "strategy_track": norm,
        "cycle_id": strat.get("cycle_id"),
        "timestamp": ts,
        "time_ist": time_ist,
        "event_type": "LOCKED_PRICE",
        "nifty_spot": s_val,
        "call_strike": strat.get("call_strike"),
        "call_ltp": c_buy,
        "call_buy_price": c_buy,
        "call_diff_pts": 0.0,
        "put_strike": strat.get("put_strike"),
        "put_ltp": p_buy,
        "put_buy_price": p_buy,
        "put_diff_pts": 0.0,
        "combined_ltp": comb_buy,
        "combined_buy_price": comb_buy,
        "combined_pnl_pts": 0.0,
        "combined_pnl_pct": 0.0,
        "combined_pnl_inr": 0.0,
        "exit_signal": None,
        "exit_details": f"Day 1 BUY Order Locked: CE ₹{c_buy:.2f} + PE ₹{p_buy:.2f} = ₹{comb_buy:.2f}",
        "source": "unified_strategy_store:update_locked_prices",
    })

    logger.info("Recorded Day 1 locked prices for %s: CE=%.2f, PE=%.2f", norm, c_buy, p_buy)
    return active


def update_latest_ltp(
    track: str,
    spot: float,
    call_ltp: float,
    put_ltp: float,
    timestamp: Optional[int] = None,
    filepath: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Record periodic 5-minute live LTP telemetry, compute real-time P&L,
    update active_strategies.json, and stream single-row log to MongoDB.
    """
    norm = normalize_track(track)
    target = filepath or ACTIVE_STRATEGIES_JSON

    ts = timestamp or _now_utc_ts()
    time_ist = _format_ist_ts(ts) or _now_ist_str()
    c_ltp = round(float(call_ltp), 2) if call_ltp is not None else 0.0
    p_ltp = round(float(put_ltp), 2) if put_ltp is not None else 0.0
    comb_ltp = round(c_ltp + p_ltp, 2)
    s_val = round(float(spot), 2) if spot is not None else 0.0

    with strategy_lock(target):
        active = load_active_strategies(filepath=target)
        strat = active.get(norm, {})

        locked = strat.get("day1_locked_price") or {}
        c_buy = locked.get("call_buy_price")
        p_buy = locked.get("put_buy_price")
        comb_buy = locked.get("combined_buy_price")

        c_diff = round(c_ltp - c_buy, 2) if c_buy is not None else 0.0
        p_diff = round(p_ltp - p_buy, 2) if p_buy is not None else 0.0

        if comb_buy and comb_buy > 0:
            pnl_pts = round(comb_ltp - comb_buy, 2)
            pnl_pct = round((pnl_pts / comb_buy) * 100.0, 2)
            pnl_inr = round(pnl_pts * NIFTY_LOT_SIZE, 2)
        else:
            pnl_pts = 0.0
            pnl_pct = 0.0
            pnl_inr = 0.0

        latest_obj = {
            "timestamp": ts,
            "time_ist": time_ist,
            "nifty_spot": s_val,
            "call_ltp": c_ltp,
            "put_ltp": p_ltp,
            "combined_ltp": comb_ltp,
            "combined_pnl_pts": pnl_pts,
            "combined_pnl_pct": pnl_pct,
            "combined_pnl_inr": pnl_inr,
        }
        strat["latest_ltp"] = latest_obj
        strat["updated_at_ist"] = _now_ist_str()
        strat["updated_ts"] = _now_utc_ts()

        save_active_strategies(active, filepath=target)

    sync_active_cycles_to_mongodb(active)

    # Determine if any exit has already triggered for this cycle to tag the single-row log
    exits = strat.get("exit_signals", {})
    exit_signal_label = None
    if exits.get("peak_20pct", {}).get("triggered"):
        exit_signal_label = exits["peak_20pct"].get("display_symbol") or "20% PEAK PROFIT REACHED"
    elif exits.get("alpha_fsm", {}).get("triggered"):
        exit_signal_label = exits["alpha_fsm"].get("display_symbol")
    elif exits.get("ai_inference", {}).get("triggered"):
        exit_signal_label = exits["ai_inference"].get("display_symbol")

    return active


def publish_exit_authorization_to_redis(
    track: str,
    call_exit: bool,
    put_exit: bool,
    strategy_data: Dict[str, Any],
    redis_client: Optional[Any] = None,
    broker: Optional[str] = None,
) -> List[str]:
    """
    Publish canonical Redis authorization keys and activate the fast Redis gate
    for the specified strategy cycle legs.

    Keys written:
      fno:{broker}:enabled:{cycle}:{expiry_iso}:{strike}:{option_type} -> {"enabled": true, "qty": target_qty}
      ORDERS_LIVE_TUE -> "1" (for 5day / Tuesday)
      ORDERS_LIVE_THU -> "1" (for 3day / Thursday)
    TTL: 7 days (604,800s) to safeguard against multi-day holds while allowing
    reconciliation to cleanly retire the key when positions close.
    """
    published_keys = []
    r = redis_client
    if r is None:
        try:
            r = get_redis_client()
        except Exception as e_redis:
            logger.warning("Could not connect to Redis: %s", e_redis)
            return []

    if not r:
        return []

    cycle = "tue" if normalize_track(track) == "5day_strategy" else "thu"
    locked_entry = strategy_data.get("day1_locked_price") or {}
    entry_status = str(
        strategy_data.get("entry_status")
        or locked_entry.get("entry_status")
        or strategy_data.get("cycle_status")
        or ""
    ).upper().strip()
    if entry_status == "PAPER_ENTRY":
        logger.info(
            "Track %s cycle %s has status 'PAPER_ENTRY'. Refusing live exit authorization publishing.",
            track, strategy_data.get("cycle_id")
        )
        return []

    from config import get_broker_for_track, SUPPORTED_BROKERS

    if not broker:
        broker = strategy_data.get("broker") or locked_entry.get("broker")
        if not broker:
            broker = get_broker_for_track(track)

    broker = str(broker or "").lower().strip()
    if broker in ("paper", "simulation"):
        logger.info(
            "Track %s cycle %s uses paper trading broker '%s'. Refusing live exit authorization publishing.",
            track, strategy_data.get("cycle_id"), broker
        )
        return []

    if broker not in SUPPORTED_BROKERS:
        logger.error(
            "Cannot publish exit authorization: broker '%s' is not supported (%s). Failing closed.",
            broker, SUPPORTED_BROKERS
        )
        return []
    lot_size = int(os.getenv("NIFTY_LOT_SIZE", str(NIFTY_LOT_SIZE)))
    lots_env = "LOTS_TUESDAY" if cycle == "tue" else "LOTS_THURSDAY"
    default_lots = 2 if cycle == "tue" else 1
    lots = int(strategy_data.get("lots") or os.getenv(lots_env, str(default_lots)))
    target_qty = lots * lot_size

    expiry_raw = strategy_data.get("expiry_date") or strategy_data.get("expiry")
    call_strike_raw = strategy_data.get("call_strike")
    put_strike_raw = strategy_data.get("put_strike")

    # The Alpha FSM signal is computed on the strikes in the cycle entry snapshot
    # (current_week_buy.json / current_thursday_buy.json). If the unified store
    # disagrees for the same cycle, authorize the snapshot contract that was evaluated.
    snapshot_file = ACTIVE_SNAPSHOT_FILE if cycle == "tue" else THURSDAY_SNAPSHOT_FILE
    try:
        with open(snapshot_file, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:
        snap = {}
    if snap and snap.get("cycle_id") and snap.get("cycle_id") == strategy_data.get("cycle_id"):
        mismatches = []
        for field, current in (("call_strike", call_strike_raw), ("put_strike", put_strike_raw)):
            snap_val = snap.get(field)
            if snap_val is not None and (current is None or int(current) != int(snap_val)):
                mismatches.append(f"{field}: unified={current} snapshot={snap_val}")
        snap_expiry = snap.get("expiry_date")
        if snap_expiry and normalize_expiry(snap_expiry) != normalize_expiry(expiry_raw):
            mismatches.append(f"expiry: unified={expiry_raw} snapshot={snap_expiry}")
        if mismatches:
            logger.critical(
                "Exit authorization contract mismatch for %s cycle %s (%s). Using entry snapshot %s.",
                track, strategy_data.get("cycle_id"), "; ".join(mismatches), snapshot_file,
            )
            call_strike_raw = snap.get("call_strike", call_strike_raw)
            put_strike_raw = snap.get("put_strike", put_strike_raw)
            expiry_raw = snap_expiry or expiry_raw

    # Authorize exactly the quantity this cycle actually bought (recorded at entry), so a
    # track can never exit more than its own lot on a contract shared with the other track.
    # LOTS_* above remains the fallback for cycles entered before entry_qty was recorded.
    entry_qty = resolve_cycle_entry_qty(track, strategy_data, snap)
    if entry_qty > 0:
        if entry_qty != target_qty:
            logger.info(
                "Exit authorization qty for %s cycle %s uses recorded entry_qty=%d (LOTS-derived qty=%d).",
                track, strategy_data.get("cycle_id"), entry_qty, target_qty,
            )
        target_qty = entry_qty

    expiry_iso = normalize_expiry(expiry_raw)

    try:
        if call_exit and call_strike_raw:
            c_strike = int(call_strike_raw)
            c_key = build_leg_enabled_key(cycle, expiry_iso, c_strike, "CE", broker=broker)
            c_val = json.dumps({"enabled": True, "qty": target_qty})
            r.set(c_key, c_val, ex=604800)
            published_keys.append(c_key)
            logger.info("Published Redis authorization key: %s (qty=%d)", c_key, target_qty)

        if put_exit and put_strike_raw:
            p_strike = int(put_strike_raw)
            p_key = build_leg_enabled_key(cycle, expiry_iso, p_strike, "PE", broker=broker)
            p_val = json.dumps({"enabled": True, "qty": target_qty})
            r.set(p_key, p_val, ex=604800)
            published_keys.append(p_key)
            logger.info("Published Redis authorization key: %s (qty=%d)", p_key, target_qty)

        if published_keys:
            gate_key = "ORDERS_LIVE_TUE" if cycle == "tue" else "ORDERS_LIVE_THU"
            r.set(gate_key, "1")
            alias_key = f"fno:orders_live:{'tuesday' if cycle == 'tue' else 'thursday'}"
            r.set(alias_key, "1")
            logger.info("Activated fast Redis safety gate: %s = 1", gate_key)
    except Exception as exc:
        logger.error("Failed to publish F&O exit authorization to Redis: %s", exc)

    return published_keys


def record_exit_signal_and_sync_mongo(
    track: str,
    exit_category: str,
    signal_data: Dict[str, Any],
    filepath: Optional[str] = None,
    redis_client: Optional[Any] = None,
) -> bool:
    """
    Record an exit alert and authorize live F&O exit orders in Redis.

    Enforces leg-specific idempotency (CALL exit does not block subsequent PUT exit).
    Saves to active_strategies.json, publishes canonical keys to Redis, pushes
    discrete EXIT row to MongoDB, and updates active cycles state.
    """
    norm = normalize_track(track)
    cat = str(exit_category).lower().strip()
    if cat not in ("peak_20pct", "alpha_fsm", "ai_inference"):
        logger.warning("Unknown exit category '%s' provided to record_exit_signal", exit_category)
        return False

    raw_action = signal_data.get("action") or signal_data.get("action_raw") or cat
    leg = signal_data.get("leg")
    simple_action = signal_data.get("simple_action") or to_simple_action(raw_action, leg=leg)

    # Determine which leg(s) are exiting
    call_exit = False
    put_exit = False
    raw_upper = str(raw_action or "").upper()
    simple_upper = str(simple_action or "").upper()
    leg_upper = str(leg or "").upper()

    if "CALL" in raw_upper or "CALL" in simple_upper or leg_upper == "CALL":
        call_exit = True
    if "PUT" in raw_upper or "PUT" in simple_upper or leg_upper == "PUT":
        put_exit = True
    if "BOTH" in raw_upper or "BOTH" in simple_upper or leg_upper == "BOTH" or raw_upper in ("EXIT", "EXIT_BOTH"):
        call_exit = True
        put_exit = True
    if cat == "peak_20pct":
        call_exit = True
        put_exit = True

    target = filepath or ACTIVE_STRATEGIES_JSON

    with strategy_lock(target):
        active = load_active_strategies(filepath=target)
        strat = active.get(norm, {})
        exits = strat.setdefault("exit_signals", {})
        entry = exits.setdefault(cat, {})

        # Leg-specific idempotency for Alpha FSM
        if cat == "alpha_fsm":
            call_trig = bool(entry.get("call_triggered"))
            put_trig = bool(entry.get("put_triggered"))
            if call_exit and not put_exit:
                if call_trig:
                    logger.info(
                        "Alpha FSM CALL exit already triggered for %s cycle %s. Preserving original exit event.",
                        norm, strat.get("cycle_id")
                    )
                    return False
                entry["call_triggered"] = True
            elif put_exit and not call_exit:
                if put_trig:
                    logger.info(
                        "Alpha FSM PUT exit already triggered for %s cycle %s. Preserving original exit event.",
                        norm, strat.get("cycle_id")
                    )
                    return False
                entry["put_triggered"] = True
            elif call_exit and put_exit:
                if call_trig and put_trig:
                    logger.info(
                        "Alpha FSM BOTH exits already triggered for %s cycle %s. Preserving original exit event.",
                        norm, strat.get("cycle_id")
                    )
                    return False
                entry["call_triggered"] = True
                entry["put_triggered"] = True
        else:
            # Single-alert idempotency: if already triggered, skip re-triggering
            if entry.get("triggered"):
                logger.info(
                    "Exit category '%s' already triggered for %s cycle %s. Preserving original exit event.",
                    cat, norm, strat.get("cycle_id")
                )
                return False

        ts = signal_data.get("timestamp") or _now_utc_ts()
        time_ist = signal_data.get("time_ist") or _format_ist_ts(ts) or _now_ist_str()

        entry["status"] = "TRIGGERED"
        entry["triggered"] = True
        entry["display_symbol"] = signal_data.get("symbol") or simple_action
        entry["simple_action"] = simple_action
        entry["timestamp"] = ts
        entry["time_ist"] = time_ist
        entry["details"] = signal_data.get("details") or signal_data.get("rationale") or simple_action

        if cat == "peak_20pct":
            entry["peak_pnl_pct"] = signal_data.get("peak_pnl_pct")
            entry["peak_pnl_pts"] = signal_data.get("peak_pnl_pts")
            entry["peak_pnl_inr"] = signal_data.get("peak_pnl_inr")
        elif cat == "alpha_fsm":
            entry["call_status"] = signal_data.get("call_status", "ACTIVE")
            entry["put_status"] = signal_data.get("put_status", "ACTIVE")
            entry["realized_pnl_pts"] = signal_data.get("realized_pnl_pts", 0.0)
            entry["realized_pnl_inr"] = round(signal_data.get("realized_pnl_pts", 0.0) * NIFTY_LOT_SIZE, 2)
        elif cat == "ai_inference":
            entry["matched_rule"] = signal_data.get("matched_rule")
            entry["confidence"] = signal_data.get("confidence")

        strat["updated_at_ist"] = _now_ist_str()
        strat["updated_ts"] = _now_utc_ts()

        save_active_strategies(active, filepath=target)

    sync_active_cycles_to_mongodb(active)

    # Publish Redis exit authorization strictly for Alpha FSM exits
    if cat == "alpha_fsm":
        strat_broker = strat.get("broker") or (strat.get("day1_locked_price") or {}).get("broker")
        publish_exit_authorization_to_redis(
            track=norm,
            call_exit=call_exit,
            put_exit=put_exit,
            strategy_data=strat,
            redis_client=redis_client,
            broker=strat_broker,
        )

    # Get latest pricing context for event log row
    latest = strat.get("latest_ltp") or {}
    locked = strat.get("day1_locked_price") or {}

    event_type_map = {
        "peak_20pct": "EXIT_SIGNAL_PEAK",
        "alpha_fsm": "EXIT_SIGNAL_FSM",
        "ai_inference": "EXIT_SIGNAL_AI",
    }

    # Append discrete EXIT_SIGNAL event to MongoDB
    append_telemetry_log_to_mongodb({
        "strategy_track": norm,
        "cycle_id": strat.get("cycle_id"),
        "timestamp": ts,
        "time_ist": time_ist,
        "event_type": event_type_map.get(cat, "EXIT_SIGNAL"),
        "nifty_spot": latest.get("nifty_spot"),
        "call_strike": strat.get("call_strike"),
        "call_ltp": latest.get("call_ltp"),
        "call_buy_price": locked.get("call_buy_price"),
        "call_diff_pts": round(latest.get("call_ltp", 0.0) - locked.get("call_buy_price", 0.0), 2) if locked.get("call_buy_price") else 0.0,
        "put_strike": strat.get("put_strike"),
        "put_ltp": latest.get("put_ltp"),
        "put_buy_price": locked.get("put_buy_price"),
        "put_diff_pts": round(latest.get("put_ltp", 0.0) - locked.get("put_buy_price", 0.0), 2) if locked.get("put_buy_price") else 0.0,
        "combined_ltp": latest.get("combined_ltp"),
        "combined_buy_price": locked.get("combined_buy_price"),
        "combined_pnl_pts": latest.get("combined_pnl_pts", 0.0),
        "combined_pnl_pct": latest.get("combined_pnl_pct", 0.0),
        "combined_pnl_inr": latest.get("combined_pnl_inr", 0.0),
        "exit_signal": entry["display_symbol"],
        "exit_details": entry["details"],
        "source": f"post_discord_hook:{cat}",
    })

    logger.info("Successfully persisted exit alert [%s: %s] for %s cycle %s", cat, simple_action, norm, strat.get("cycle_id"))
    return True
