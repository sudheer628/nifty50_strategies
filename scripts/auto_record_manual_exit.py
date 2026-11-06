#!/usr/bin/env python3
"""
auto_record_manual_exit.py - Automatically detect and record manual F&O closes.

This script is called by the exit manager (Tuesday/Thursday) when it detects
a position that was sold manually (quantity=0, day_sell_quantity>0, no managed order).

It performs the same cleanup as mark_manual_exit.py but is invoked automatically:
1. Marks the leg MANUAL_CLOSED in the Alpha FSM state
2. Records exited quantity in Redis for shared contract reconciliation
3. Cleans up authorization keys and trailing state
4. Updates active_strategies.json and MongoDB Atlas

Called by:
  - Tuesday-Fno-Exit-Orders-V1.py (reconcile_cycle_legs)
  - Thursday-Fno-Exit-Orders-V1.py (reconcile_cycle_legs)
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

try:
    from config import (
        logger,
        ACTIVE_SNAPSHOT_FILE,
        THURSDAY_SNAPSHOT_FILE,
        get_redis_client,
        get_broker_for_track,
    )
    from common.fno_keys import (
        EXITED_QTY_TTL_SEC,
        build_exited_qty_key,
        build_leg_enabled_key,
        normalize_expiry,
    )
    from common.fsm_strategy import evaluate_fsm_tick
    from common.storage import update_active_fsm_state, load_active_snapshot
    from common import unified_strategy_store as uss
except ImportError:
    # Allow standalone testing
    logger = logging.getLogger("auto_manual_exit")
    logger.setLevel(logging.INFO)


IST_OFFSET = 5.5  # hours from UTC


def _ts_to_ist_str(ts: int) -> str:
    """Convert Unix timestamp to IST string."""
    from datetime import timedelta
    ist = timezone(timedelta(hours=IST_OFFSET))
    return datetime.fromtimestamp(ts, tz=ist).strftime("%Y-%m-%d %H:%M:%S IST")


def auto_record_manual_exit(
    track: str,
    broker: str,
    strike: int,
    option_type: str,
    expiry_iso: str,
    sell_price: float,
    sell_ts: int,
    sell_qty: int,
    redis_client: Optional[Any] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Automatically record a manual exit detected from broker position data.

    Args:
        track: '5day_strategy' or '3day_strategy'
        broker: 'upstox', 'angelone', etc.
        strike: Strike price (e.g., 24500)
        option_type: 'CE' or 'PE'
        expiry_iso: Expiry in ISO format 'YYYY-MM-DD'
        sell_price: Fill price from broker position
        sell_ts: Unix timestamp of the sale
        sell_qty: Quantity sold
        redis_client: Redis client instance
        dry_run: If True, return plan without executing

    Returns:
        Dict with action details and success status
    """
    track = uss.normalize_track(track)
    cycle = "tue" if track == "5day_strategy" else "thu"
    leg = "CALL" if option_type == "CE" else "PUT"
    
    result = {
        "track": track,
        "cycle": cycle,
        "broker": broker,
        "strike": strike,
        "option_type": option_type,
        "contract": f"{strike}{option_type} exp {expiry_iso}",
        "sell_price": sell_price,
        "sell_qty": sell_qty,
        "sell_time": _ts_to_ist_str(sell_ts),
        "dry_run": dry_run,
        "success": False,
    }

    # Load the correct snapshot file
    snapshot_file = ACTIVE_SNAPSHOT_FILE if track == "5day_strategy" else THURSDAY_SNAPSHOT_FILE
    
    try:
        snap = load_active_snapshot(filepath=snapshot_file)
    except Exception as e:
        result["error"] = f"Failed to load snapshot: {e}"
        logger.error("[%s] Failed to load snapshot %s: %s", track, snapshot_file, e)
        return result

    if not snap:
        result["error"] = f"No active snapshot at {snapshot_file}"
        return result

    # Verify this is the correct cycle/contract
    snap_call_strike = snap.get("call_strike")
    snap_put_strike = snap.get("put_strike")
    snap_expiry = normalize_expiry(snap.get("expiry_date") or snap.get("expiry"))
    
    if snap_expiry != expiry_iso:
        result["warning"] = f"Snapshot expiry {snap_expiry} differs from position expiry {expiry_iso}"
        logger.warning("[%s] Expiry mismatch: snapshot=%s, position=%s", track, snap_expiry, expiry_iso)
        # Continue anyway - might be stale position from prior cycle
    
    # Determine which leg this is
    leg_key = None
    fsm_leg_key = None
    if option_type == "CE" and snap_call_strike == strike:
        leg_key = "call"
        fsm_leg_key = "call_leg"
    elif option_type == "PE" and snap_put_strike == strike:
        leg_key = "put"
        fsm_leg_key = "put_leg"
    else:
        result["error"] = f"Strike {strike}{option_type} doesn't match snapshot (call={snap_call_strike}, put={snap_put_strike})"
        logger.warning("[%s] %s", track, result["error"])
        return result

    # Check FSM state
    fsm = snap.get("alpha_fsm") or {}
    fsm_leg = fsm.get(fsm_leg_key) or {}
    current_status = fsm_leg.get("status", "UNKNOWN")
    
    result["fsm_status_before"] = current_status
    result["fsm_buy_price"] = fsm_leg.get("buy_price")
    
    if current_status in ("HARVESTED_CLOSED", "TRAILING_STOP_CLOSED", "SALVAGED_CLOSED", "MANUAL_CLOSED"):
        result["info"] = f"Leg already closed with status {current_status}"
        logger.info("[%s] %s %s already closed (%s)", track, strike, option_type, current_status)
        result["success"] = True  # Already handled
        return result

    # Get the buy price for P&L calculation
    buy_price = float(fsm_leg.get("buy_price") or 0.0)
    if buy_price <= 0:
        result["error"] = f"No buy_price in FSM leg state"
        logger.error("[%s] %s %s has no buy_price in FSM", track, strike, option_type)
        return result

    realized_pnl = round(sell_price - buy_price, 2)

    if dry_run:
        result["plan"] = {
            "action": "Would mark MANUAL_CLOSED",
            "realized_pnl_pts": realized_pnl,
            "realized_pnl_pct": round((realized_pnl / buy_price) * 100, 2) if buy_price > 0 else 0,
            "redis_exited_key": build_exited_qty_key(cycle, expiry_iso, strike, option_type, broker),
            "redis_enabled_key": build_leg_enabled_key(cycle, expiry_iso, strike, option_type, broker=broker),
        }
        return result

    # === Execute the updates ===
    
    r = redis_client
    if r is None:
        try:
            r = get_redis_client()
        except Exception as e:
            result["redis_error"] = f"Redis unavailable: {e}"
            logger.error("[%s] Redis unavailable for manual exit recording: %s", track, e)
            # Continue without Redis - we can still update FSM state

    # 1. Update FSM state
    import copy
    new_fsm = copy.deepcopy(fsm)
    new_leg = new_fsm[fsm_leg_key]
    new_leg.update({
        "status": "MANUAL_CLOSED",
        "exit_price": round(sell_price, 2),
        "exit_ts": sell_ts,
        "exit_reason": f"AUTO_DETECTED manual sell at ₹{sell_price:.2f} (qty={sell_qty}, broker={broker})",
        "realized_pnl": realized_pnl,
    })
    
    # Recompute overall FSM state
    new_fsm, events = evaluate_fsm_tick(new_fsm, None, None, sell_ts)
    result["fsm_state_after"] = new_fsm.get("state")
    result["fsm_status_after"] = new_leg.get("status")
    result["fsm_events"] = events

    # Write FSM state
    try:
        if not update_active_fsm_state(new_fsm, filepath=snapshot_file):
            result["error"] = "Failed to write FSM state"
            return result
        logger.info("[%s] Updated FSM state for %s %s: MANUAL_CLOSED at ₹%.2f", track, strike, option_type, sell_price)
    except Exception as e:
        result["error"] = f"Failed to write FSM state: {e}"
        logger.error("[%s] Failed to write FSM state: %s", track, e)
        return result

    # 2. Record exited quantity in Redis
    if r is not None:
        try:
            exited_key = build_exited_qty_key(cycle, expiry_iso, strike, option_type, broker)
            prev_qty = 0
            try:
                prev_qty = int(float(r.get(exited_key) or 0))
            except (TypeError, ValueError):
                prev_qty = 0
            
            # Take the max in case of multiple partial fills
            effective_qty = max(prev_qty, sell_qty)
            r.set(exited_key, str(effective_qty), ex=EXITED_QTY_TTL_SEC)
            result["redis_exited_qty"] = effective_qty
            result["redis_exited_key"] = exited_key
            logger.info("[%s] Recorded exited qty %d in Redis: %s", track, effective_qty, exited_key)
        except Exception as e:
            result["redis_exited_error"] = str(e)
            logger.error("[%s] Failed to record exited qty in Redis: %s", track, e)

    # 3. Clean up authorization keys
    if r is not None:
        try:
            # Delete the canonical authorization key
            canonical_key = build_leg_enabled_key(cycle, expiry_iso, strike, option_type, broker=broker)
            r.delete(canonical_key)
            
            # Delete any detailed authorization keys
            pattern = f"fno:{broker}:enabled:{cycle}:*:{strike}:{option_type}"
            for key in r.scan_iter(pattern, count=10):
                r.delete(key)
            
            result["redis_auth_cleaned"] = True
            logger.info("[%s] Cleaned up authorization keys for %s %s", track, strike, option_type)
        except Exception as e:
            result["redis_auth_error"] = str(e)
            logger.error("[%s] Failed to clean authorization keys: %s", track, e)

    # 4. Clean up trailing state keys
    if r is not None:
        try:
            # Pattern for trailing state: fno:{broker}:trail:{cycle}:*:{strike}:{option_type}:*
            trail_pattern = f"fno:{broker}:trail:{cycle}:*:{strike}:{option_type}:*"
            deleted_count = 0
            for key in r.scan_iter(trail_pattern, count=30):
                r.delete(key)
                deleted_count += 1
            
            # Also clean managed_order_ids sets
            managed_ids_key = f"fno:{broker}:managed_order_ids:{cycle}"
            # We don't know the exact order_id, but we can scan and remove
            # The order book reconciliation will clean up orphan order IDs
            
            result["redis_trail_keys_deleted"] = deleted_count
            logger.info("[%s] Cleaned up %d trailing state keys for %s %s", track, deleted_count, strike, option_type)
        except Exception as e:
            result["redis_trail_error"] = str(e)
            logger.error("[%s] Failed to clean trailing keys: %s", track, e)

    # 5. Update active_strategies.json
    try:
        active_file = uss.ACTIVE_STRATEGIES_JSON
        active = uss.load_active_strategies(filepath=active_file)
        strat = active.get(track) or {}
        
        if strat.get("cycle_id") == snap.get("cycle_id"):
            # Update exit_signals
            exits = strat.setdefault("exit_signals", {})
            alpha = exits.setdefault("alpha_fsm", {})
            alpha[f"{leg_key}_triggered"] = True
            alpha[f"{leg_key}_status"] = "MANUAL_CLOSED"
            
            # Record in manual_exits list
            strat.setdefault("manual_exits", []).append({
                "leg": leg,
                "strike": strike,
                "option_type": option_type,
                "qty": sell_qty,
                "price": round(sell_price, 2),
                "timestamp": sell_ts,
                "time_ist": _ts_to_ist_str(sell_ts),
                "broker": broker,
                "source": "auto_detected",
            })
            
            strat["updated_ts"] = int(datetime.now(tz=timezone.utc).timestamp())
            active[track] = strat
            uss.save_active_strategies(active, filepath=active_file)
            
            # Sync to MongoDB
            try:
                uss.sync_active_cycles_to_mongodb(active)
            except Exception as e:
                result["mongo_error"] = str(e)
                logger.warning("[%s] Failed to sync to MongoDB: %s", track, e)
            
            result["active_strategies_updated"] = True
            logger.info("[%s] Updated active_strategies.json for %s %s", track, strike, option_type)
    except Exception as e:
        result["active_strategies_error"] = str(e)
        logger.error("[%s] Failed to update active_strategies.json: %s", track, e)

    # 6. Check if we should reset ORDERS_LIVE gate
    if r is not None:
        try:
            # Check if any other legs are still active
            pattern = f"fno:{broker}:enabled:{cycle}:*"
            remaining = [k for k in r.scan_iter(pattern, count=20) if r.get(k)]
            
            if not remaining:
                gate_key = "ORDERS_LIVE_TUE" if cycle == "tue" else "ORDERS_LIVE_THU"
                r.set(gate_key, "0")
                r.set(f"fno:orders_live:{'tuesday' if cycle == 'tue' else 'thursday'}", "0")
                result["gate_reset"] = gate_key
                logger.info("[%s] No active legs remain. Reset %s = 0", track, gate_key)
        except Exception as e:
            result["gate_check_error"] = str(e)
            logger.warning("[%s] Failed to check/reset gate: %s", track, e)

    result["success"] = True
    result["realized_pnl_pts"] = realized_pnl
    result["realized_pnl_pct"] = round((realized_pnl / buy_price) * 100, 2) if buy_price > 0 else 0
    
    logger.info(
        "[%s] Auto-recorded manual exit: %s %s @ ₹%.2f (P&L: %.2f pts / %.1f%%)",
        track, strike, option_type, sell_price, realized_pnl, result["realized_pnl_pct"]
    )
    
    return result


def detect_manual_exit_from_position(
    position: Dict[str, Any],
    track: str,
    broker: str,
) -> Optional[Dict[str, Any]]:
    """
    Analyze a broker position to detect if it was manually closed.
    
    Args:
        position: Broker position dict (Upstox format)
        track: '5day_strategy' or '3day_strategy'
        broker: 'upstox', 'angelone', etc.
    
    Returns:
        Dict with manual exit details if detected, None otherwise
    """
    if not position:
        return None
    
    qty = int(position.get("quantity", 0) or 0)
    day_sell_qty = int(position.get("day_sell_quantity", 0) or 0)
    
    # Manual exit detected when: quantity=0 and day_sell_quantity>0
    # This means the position was sold today via broker app
    if qty > 0:
        return None  # Still holding
    
    if day_sell_qty <= 0:
        return None  # No sale today
    
    # Extract contract details
    tradingsymbol = position.get("tradingsymbol") or position.get("trading_symbol") or ""
    if not tradingsymbol:
        return None
    
    # Parse tradingsymbol: NIFTY26N0924500PE
    import re
    m = re.match(r"NIFTY\d{2}[A-Z]\d{2}(\d+)(CE|PE)$", tradingsymbol.upper())
    if not m:
        # Try alternative format: NIFTY09NOV2624200CE
        m = re.match(r"NIFTY\d{1,2}[A-Z]{3}\d{2}(\d+)(CE|PE)$", tradingsymbol.upper())
    
    if not m:
        logger.warning("Could not parse tradingsymbol: %s", tradingsymbol)
        return None
    
    strike = int(m.group(1))
    option_type = m.group(2)
    
    # Extract sell price and time
    sell_price = float(position.get("sell_price") or position.get("day_sell_price") or 0.0)
    if sell_price <= 0:
        # Calculate from sell_value if available
        sell_value = float(position.get("sell_value") or position.get("day_sell_value") or 0.0)
        if sell_value > 0 and day_sell_qty > 0:
            sell_price = round(sell_value / day_sell_qty, 2)
    
    if sell_price <= 0:
        logger.warning("Could not determine sell price for %s", tradingsymbol)
        return None
    
    # Estimate sell timestamp (we don't have exact time from position API)
    # Use current time as approximation
    sell_ts = int(datetime.now(tz=timezone.utc).timestamp())
    
    # Extract expiry from tradingsymbol
    # This is approximate - we'll normalize it later
    expiry_match = re.search(r"(\d{2})([A-Z]{3})(\d{2})", tradingsymbol)
    if expiry_match:
        day = expiry_match.group(1)
        month_str = expiry_match.group(2)
        year_short = expiry_match.group(3)
        
        MONTH_MAP = {
            "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
            "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
            "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12"
        }
        month = MONTH_MAP.get(month_str, "01")
        year = "20" + year_short
        expiry_iso = f"{year}-{month}-{day.zfill(2)}"
    else:
        # Fallback - will need to be looked up
        expiry_iso = normalize_expiry(None)  # Will error if not found
    
    return {
        "track": track,
        "broker": broker,
        "strike": strike,
        "option_type": option_type,
        "expiry_iso": expiry_iso,
        "sell_price": round(sell_price, 2),
        "sell_qty": day_sell_qty,
        "sell_ts": sell_ts,
        "tradingsymbol": tradingsymbol,
    }


if __name__ == "__main__":
    # Test with sample position data
    import argparse
    
    p = argparse.ArgumentParser(description="Auto-detect and record manual F&O closes")
    p.add_argument("--track", choices=["tuesday", "thursday", "5day_strategy", "3day_strategy"])
    p.add_argument("--broker", default="upstox")
    p.add_argument("--strike", type=int)
    p.add_argument("--option-type", choices=["CE", "PE"])
    p.add_argument("--expiry", help="Expiry in YYYY-MM-DD format")
    p.add_argument("--price", type=float, required=True, help="Sell fill price")
    p.add_argument("--qty", type=int, help="Quantity sold (default: cycle entry qty)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    
    track = args.track
    if track == "tuesday":
        track = "5day_strategy"
    elif track == "thursday":
        track = "3day_strategy"
    
    sell_ts = int(datetime.now(tz=timezone.utc).timestamp())
    expiry_iso = normalize_expiry(args.expiry) if args.expiry else normalize_expiry(None)
    
    result = auto_record_manual_exit(
        track=track,
        broker=args.broker,
        strike=args.strike,
        option_type=args.option_type,
        expiry_iso=expiry_iso,
        sell_price=args.price,
        sell_ts=sell_ts,
        sell_qty=args.qty or 65,  # Default to 1 lot
        dry_run=args.dry_run,
    )
    
    print(json.dumps(result, indent=2, ensure_ascii=False))
