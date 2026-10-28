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
    MONGODB_URI,
    MONGODB_DATABASE,
    MONGODB_COLLECTION_DERIVATIVE_ACTIVE_CYCLES,
    MONGODB_COLLECTION_DERIVATIVE_CYCLE_LIVE_LOGS,
    NIFTY_LOT_SIZE,
)

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
    Insert a single-row telemetry or discrete event record into `derivative_cycle_live_logs`.
    """
    try:
        db = get_mongodb_db()
        if db is None:
            return False

        col = db[MONGODB_COLLECTION_DERIVATIVE_CYCLE_LIVE_LOGS]
        record = dict(log_record)
        if "inserted_ts" not in record:
            record["inserted_ts"] = _now_utc_ts()
        col.insert_one(record)
        return True
    except Exception as exc:
        logger.warning("MongoDB telemetry log insertion failed: %s", exc)
        return False


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

    locked_obj = {
        "call_buy_price": c_buy,
        "put_buy_price": p_buy,
        "combined_buy_price": comb_buy,
        "entry_spot": s_val,
        "locked_at_ist": time_ist,
        "locked_ts": ts,
    }

    with strategy_lock(target):
        active = load_active_strategies(filepath=target)
        strat = active.get(norm, {})
        strat["day1_locked_price"] = locked_obj
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

    # Append 5MIN_TICK row to MongoDB
    append_telemetry_log_to_mongodb({
        "strategy_track": norm,
        "cycle_id": strat.get("cycle_id"),
        "timestamp": ts,
        "time_ist": time_ist,
        "event_type": "5MIN_TICK",
        "nifty_spot": s_val,
        "call_strike": strat.get("call_strike"),
        "call_ltp": c_ltp,
        "call_buy_price": c_buy,
        "call_diff_pts": c_diff,
        "put_strike": strat.get("put_strike"),
        "put_ltp": p_ltp,
        "put_buy_price": p_buy,
        "put_diff_pts": p_diff,
        "combined_ltp": comb_ltp,
        "combined_buy_price": comb_buy,
        "combined_pnl_pts": pnl_pts,
        "combined_pnl_pct": pnl_pct,
        "combined_pnl_inr": pnl_inr,
        "exit_signal": exit_signal_label,
        "exit_details": None,
        "source": "unified_strategy_store:update_latest_ltp",
    })

    return active


def record_exit_signal_and_sync_mongo(
    track: str,
    exit_category: str,
    signal_data: Dict[str, Any],
    filepath: Optional[str] = None,
) -> bool:
    """
    Record an exit alert immediately after Discord dispatch.

    Enforces single-trigger idempotency (first exit event is preserved).
    Saves to active_strategies.json, pushes discrete EXIT row to MongoDB,
    and updates active cycles state.
    """
    norm = normalize_track(track)
    cat = str(exit_category).lower().strip()
    if cat not in ("peak_20pct", "alpha_fsm", "ai_inference"):
        logger.warning("Unknown exit category '%s' provided to record_exit_signal", exit_category)
        return False

    target = filepath or ACTIVE_STRATEGIES_JSON

    with strategy_lock(target):
        active = load_active_strategies(filepath=target)
        strat = active.get(norm, {})
        exits = strat.setdefault("exit_signals", {})
        entry = exits.setdefault(cat, {})

        # Single-alert idempotency: if already triggered, skip re-triggering
        if entry.get("triggered"):
            logger.info(
                "Exit category '%s' already triggered for %s cycle %s. Preserving original exit event.",
                cat, norm, strat.get("cycle_id")
            )
            return False

        ts = signal_data.get("timestamp") or _now_utc_ts()
        time_ist = signal_data.get("time_ist") or _format_ist_ts(ts) or _now_ist_str()

        raw_action = signal_data.get("action") or signal_data.get("action_raw") or cat
        leg = signal_data.get("leg")
        simple_action = signal_data.get("simple_action") or to_simple_action(raw_action, leg=leg)

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
