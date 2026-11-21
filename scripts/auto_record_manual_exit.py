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
from typing import Any, Dict, List, Optional, Tuple

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


def _snapshot_cycle_contracts(snapshot: Optional[Dict[str, Any]], source: str) -> Optional[List[Dict[str, Any]]]:
    """Return both exact cycle contracts, or None when snapshot identity is incomplete."""
    if not isinstance(snapshot, dict):
        return None

    expiry = normalize_expiry(snapshot.get("expiry_date") or snapshot.get("expiry"))
    if expiry == "UNKNOWN":
        return None

    contracts = []
    for field, option_type, leg in (
        ("call_strike", "CE", "call"),
        ("put_strike", "PE", "put"),
    ):
        try:
            strike_value = float(snapshot.get(field))
            if strike_value <= 0 or not strike_value.is_integer():
                return None
            strike = int(strike_value)
        except (TypeError, ValueError):
            return None

        contracts.append({
            "strike": strike,
            "opt_type": option_type,
            "expiry": expiry,
            "source": f"{source}_{leg}",
        })

    return contracts


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
    has_valid_fill = sell_price is not None and float(sell_price) > 0
    qty_str = f"qty={sell_qty}" if (sell_qty and int(sell_qty) > 0) else "qty=unknown"
    if has_valid_fill and buy_price > 0:
        realized_pnl = round(float(sell_price) - buy_price, 2)
        realized_pnl_pct = round((realized_pnl / buy_price) * 100, 2)
        exit_price_val = round(float(sell_price), 2)
        exit_reason = f"AUTO_DETECTED manual sell at ₹{exit_price_val:.2f} ({qty_str}, broker={broker})"
    elif has_valid_fill:
        realized_pnl = None
        realized_pnl_pct = None
        exit_price_val = round(float(sell_price), 2)
        exit_reason = f"AUTO_DETECTED manual sell at ₹{exit_price_val:.2f} ({qty_str}, broker={broker}, buy price unavailable)"
    else:
        realized_pnl = None
        realized_pnl_pct = None
        exit_price_val = None
        exit_reason = f"AUTO_DETECTED manual sell ({qty_str}, broker={broker}, fill price unavailable)"

    if dry_run:
        result["plan"] = {
            "action": "Would mark MANUAL_CLOSED",
            "realized_pnl_pts": realized_pnl,
            "realized_pnl_pct": realized_pnl_pct,
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
        "exit_price": exit_price_val,
        "exit_ts": sell_ts,
        "exit_reason": exit_reason,
        "realized_pnl": realized_pnl,
        "is_estimated": (not has_valid_fill) or (buy_price <= 0) or (realized_pnl is None),
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
        price_log = f"at ₹{exit_price_val:.2f}" if has_valid_fill else "(fill unavailable)"
        logger.info("[%s] Updated FSM state for %s %s: MANUAL_CLOSED %s", track, strike, option_type, price_log)
    except Exception as e:
        result["error"] = f"Failed to write FSM state: {e}"
        logger.error("[%s] Failed to write FSM state: %s", track, e)
        return result

    # 2. Record exited quantity in Redis (only when verified broker quantity is available)
    if r is not None and sell_qty and int(sell_qty) > 0:
        try:
            exited_key = build_exited_qty_key(cycle, expiry_iso, strike, option_type, broker)
            prev_qty = 0
            try:
                prev_qty = int(float(r.get(exited_key) or 0))
            except (TypeError, ValueError):
                prev_qty = 0

            # Take the max in case of multiple partial fills
            effective_qty = max(prev_qty, int(sell_qty))
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
                "qty": int(sell_qty) if (sell_qty and int(sell_qty) > 0) else None,
                "price": round(sell_price, 2) if sell_price is not None else None,
                "timestamp": sell_ts,
                "time_ist": _ts_to_ist_str(sell_ts),
                "broker": broker,
                "source": "auto_detected",
            })

            # Check if both legs are closed
            c_st = new_fsm.get("call_leg", {}).get("status")
            p_st = new_fsm.get("put_leg", {}).get("status")
            terminal_statuses = ("HARVESTED_CLOSED", "TRAILING_STOP_CLOSED", "SALVAGED_CLOSED", "MANUAL_CLOSED")
            both_legs_closed = (c_st in terminal_statuses and p_st in terminal_statuses)
            if both_legs_closed:
                strat["status"] = "closed"
                try:
                    from common.storage import mark_active_cycle_closed
                    mark_active_cycle_closed(filepath=snapshot_file)
                    result["cycle_closed"] = True
                    logger.info("[%s] All legs closed. Marked active cycle as CLOSED in %s", track, snapshot_file)
                except Exception as e_close:
                    logger.warning("[%s] Failed to mark snapshot closed: %s", track, e_close)

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

            c_st = new_fsm.get("call_leg", {}).get("status")
            p_st = new_fsm.get("put_leg", {}).get("status")
            terminal_statuses = ("HARVESTED_CLOSED", "TRAILING_STOP_CLOSED", "SALVAGED_CLOSED", "MANUAL_CLOSED")
            both_legs_closed = (c_st in terminal_statuses and p_st in terminal_statuses)

            if (not remaining) or both_legs_closed:
                gate_key = "ORDERS_LIVE_TUE" if cycle == "tue" else "ORDERS_LIVE_THU"
                r.set(gate_key, "0")
                r.set(f"fno:orders_live:{'tuesday' if cycle == 'tue' else 'thursday'}", "0")
                result["gate_reset"] = gate_key
                logger.info("[%s] All legs closed or no active legs remain. Reset %s = 0", track, gate_key)
        except Exception as e:
            result["gate_check_error"] = str(e)
            logger.warning("[%s] Failed to check/reset gate: %s", track, e)

    result["success"] = True
    result["realized_pnl_pts"] = realized_pnl
    result["realized_pnl_pct"] = realized_pnl_pct

    price_str = f"₹{sell_price:.2f}" if sell_price is not None else "fill unavailable"
    pnl_str = f"(P&L: {realized_pnl:.2f} pts / {realized_pnl_pct:.1f}%)" if realized_pnl is not None else "(P&L: unrecorded)"
    logger.info(
        "[%s] Auto-recorded manual exit: %s %s @ %s %s",
        track, strike, option_type, price_str, pnl_str
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


def reconcile_broker_positions_with_snapshot(
    track: str,
    broker: Optional[str] = None,
    redis_client=None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Reconcile live broker positions against active strategy snapshot.

    Can run even when ORDERS_LIVE_* is disabled. If a leg is marked ACTIVE
    in the snapshot/Alpha FSM but has 0 net position at the broker, this function
    detects the manual exit, locates the fill price (from closed positions or order book),
    calls auto_record_manual_exit(), and if both legs are closed, marks the cycle closed
    and resets ORDERS_LIVE_* = 0.

    Returns:
        Dict detailing detected exits and reconciliation outcome.
    """
    track = uss.normalize_track(track)
    snapshot_file = ACTIVE_SNAPSHOT_FILE if track == "5day_strategy" else THURSDAY_SNAPSHOT_FILE

    result = {
        "track": track,
        "snapshot_file": snapshot_file,
        "reconciled": False,
        "exits_recorded": [],
        "errors": [],
    }

    try:
        snap = load_active_snapshot(filepath=snapshot_file)
    except Exception as e:
        result["errors"].append(f"Failed to load snapshot: {e}")
        return result

    if not snap:
        result["reason"] = "no_active_snapshot"
        return result

    if snap.get("status") == "closed":
        result["reason"] = "snapshot_already_closed"
        return result

    fsm = snap.get("alpha_fsm") or {}
    call_leg = fsm.get("call_leg") or {}
    put_leg = fsm.get("put_leg") or {}

    # If neither leg is ACTIVE, nothing to reconcile
    active_legs_exist = (call_leg.get("status") == "ACTIVE") or (put_leg.get("status") == "ACTIVE")
    if not active_legs_exist:
        result["reason"] = "no_active_legs"
        return result

    snapshot_contracts = _snapshot_cycle_contracts(snap, track)
    if snapshot_contracts is None:
        result["errors"].append("Active snapshot has incomplete contract identity; reconciliation aborted")
        logger.warning("[%s] Reconciliation aborted: active snapshot has incomplete contract identity", track)
        return result
    snap_expiry = snapshot_contracts[0]["expiry"]

    active_broker = (broker or snap.get("broker") or os.getenv("TUESDAY_BROKER" if track == "5day_strategy" else "THURSDAY_BROKER") or ("upstox" if track == "5day_strategy" else "angelone")).lower().strip()
    result["broker"] = active_broker

    # Retrieve broker token
    try:
        from common.entry_order_manager import get_broker_token
        token = get_broker_token(active_broker, redis_client=redis_client)
    except Exception as e:
        result["errors"].append(f"Failed to get broker token: {e}")
        logger.warning("[%s] Reconciliation aborted: could not obtain broker token for %s: %s", track, active_broker, e)
        return result

    # Fetch live positions & order book
    try:
        from common.broker_adapters import get_broker_adapter, parse_positions, parse_tradingsymbol, _position_quantity, _to_float
        adapter = get_broker_adapter(active_broker)
        raw_pos = adapter.fetch_positions(token)
        long_pos, short_pos, closed_pos = parse_positions(raw_pos, include_closed=True)
    except Exception as e:
        result["errors"].append(f"Failed to fetch/parse positions: {e}")
        logger.warning("[%s] Reconciliation aborted: position fetch failed for %s: %s", track, active_broker, e)
        return result

    orders = []
    try:
        raw_orders = adapter.fetch_order_book(token)
        if isinstance(raw_orders, dict):
            orders = raw_orders.get("data") or []
        elif isinstance(raw_orders, list):
            orders = raw_orders
        if not isinstance(orders, list):
            orders = []
    except Exception as e:
        result["errors"].append(f"Failed to fetch order book: {e}")
        logger.warning("[%s] Reconciliation aborted: order book fetch failed for %s: %s (fail-closed)", track, active_broker, e)
        return result

    snap_call_strike = snap.get("call_strike")
    snap_put_strike = snap.get("put_strike")

    legs_to_check = [
        ("call", snap_call_strike, "CE", call_leg),
        ("put", snap_put_strike, "PE", put_leg),
    ]

    from common.broker_adapters import (
        contract_match_status,
        contract_matches,
        normalize_order_status,
        OPEN_ORDER_STATUSES,
    )

    open_positions = list(long_pos or []) + list(short_pos or [])
    for leg_type, strike, opt_type, fsm_leg in legs_to_check:
        if fsm_leg.get("status") != "ACTIVE":
            continue
        for pos in open_positions:
            if abs(_position_quantity(pos)) <= 0:
                continue
            match_status = contract_match_status(
                pos,
                target_strike=strike,
                target_opt_type=opt_type,
                target_expiry=snap_expiry,
                target_underlying="NIFTY",
            )
            if match_status is None:
                result["errors"].append(
                    f"Could not verify broker position identity for active {leg_type} leg"
                )
                logger.warning(
                    "[%s] Reconciliation aborted: ambiguous broker position identity for active %s leg",
                    track,
                    leg_type,
                )
                return result

        for order in orders:
            order_status = normalize_order_status(order.get("status") or order.get("orderstatus"))
            if order_status not in OPEN_ORDER_STATUSES:
                continue
            match_status = contract_match_status(
                order,
                target_strike=strike,
                target_opt_type=opt_type,
                target_expiry=snap_expiry,
                target_underlying="NIFTY",
            )
            if match_status is None:
                result["errors"].append(
                    f"Could not verify pending broker order identity for active {leg_type} leg"
                )
                logger.warning(
                    "[%s] Reconciliation aborted: ambiguous pending order identity for active %s leg",
                    track,
                    leg_type,
                )
                return result

    for leg_type, strike, opt_type, fsm_leg in legs_to_check:
        if strike is None or fsm_leg.get("status") != "ACTIVE":
            continue

        # Check if matching contract exists in open positions (both long and short: any abs(qty) > 0)
        is_open = False
        for pos in open_positions:
            qty = _position_quantity(pos)
            if abs(qty) > 0 and contract_match_status(
                pos,
                target_strike=strike,
                target_opt_type=opt_type,
                target_expiry=snap_expiry,
                target_underlying="NIFTY",
            ) is True:
                is_open = True
                break

        if is_open:
            # Contract is still actively held at broker
            continue

        # Check if an exit order is currently pending in the broker order book
        has_pending_order = False
        for o in orders:
            st = normalize_order_status(o.get("status") or o.get("orderstatus"))
            if st in OPEN_ORDER_STATUSES:
                if contract_match_status(
                    o,
                    target_strike=strike,
                    target_opt_type=opt_type,
                    target_expiry=snap_expiry,
                    target_underlying="NIFTY",
                ) is True:
                    has_pending_order = True
                    break

        if has_pending_order:
            logger.info(
                "[%s] Skipping manual exit reconciliation for %s %s: pending order exists at broker",
                track, strike, opt_type
            )
            continue

        # Position is NOT open in broker and no pending orders exist -> user closed it manually
        logger.info(
            "[%s] Manual exit detected for %s %s: active in FSM but no open broker position or pending order",
            track, strike, opt_type
        )

        sell_price = None
        sell_qty = None
        sell_ts = int(datetime.now(tz=timezone.utc).timestamp())

        # 1. Search closed positions (only zero-net closed positions, never short positions)
        for cp in (closed_pos or []):
            if contract_matches(cp, target_strike=strike, target_opt_type=opt_type, target_expiry=snap_expiry, target_underlying="NIFTY"):
                sp = float(cp.get("sell_price") or cp.get("day_sell_price") or 0.0)
                if sp <= 0:
                    sv = float(cp.get("sell_value") or cp.get("day_sell_value") or 0.0)
                    sq = float(cp.get("day_sell_quantity") or cp.get("sellqty") or 0)
                    if sv > 0 and sq > 0:
                        sp = round(sv / sq, 2)
                if sp > 0:
                    sell_price = sp
                    day_sq = int(_to_float(cp.get("day_sell_quantity") or cp.get("sellqty") or 0))
                    if day_sq > 0:
                        sell_qty = day_sq
                    break

        # 2. Search order book for completed SELL orders if fill price or quantity still unknown
        if sell_price is None or sell_qty is None:
            for o in orders:
                txn = str(o.get("transaction_type") or "").upper()
                status = str(o.get("status") or "").lower()
                if txn == "SELL" and status in ("complete", "completed", "filled"):
                    if contract_matches(o, target_strike=strike, target_opt_type=opt_type, target_expiry=snap_expiry, target_underlying="NIFTY"):
                        fill_px = float(o.get("average_price") or o.get("price") or 0.0)
                        if fill_px > 0 and sell_price is None:
                            sell_price = fill_px
                        o_qty = int(_to_float(o.get("quantity") or o.get("filled_quantity") or 0))
                        if o_qty > 0 and sell_qty is None:
                            sell_qty = o_qty
                        if sell_price is not None and sell_qty is not None:
                            break

        # Call auto_record_manual_exit with exact fill price and quantity (or None if unavailable)
        rec_res = auto_record_manual_exit(
            track=track,
            broker=active_broker,
            strike=int(strike),
            option_type=opt_type,
            expiry_iso=snap_expiry,
            sell_price=sell_price,
            sell_ts=sell_ts,
            sell_qty=sell_qty,
            redis_client=redis_client,
            dry_run=dry_run,
        )
        result["exits_recorded"].append(rec_res)

    result["reconciled"] = True
    return result


def check_prior_cycle_open_positions(
    track: str,
    active_snapshot: Optional[Dict[str, Any]] = None,
    broker: Optional[str] = None,
    redis_client=None,
    check_cross_track_overlap: bool = False,
    broker_wide: bool = False,
) -> Tuple[bool, bool, str]:
    """
    Check if the broker currently holds any open positions or open orders
    for the exact contracts of the prior cycle (and optionally cross-track overlap).

    Used to ensure zero broker exposure before initiating fresh cycles.

    Returns:
        (has_open_exposure: bool, is_safe_to_enter: bool, reason_str: str)

    Fail-closed policy:
        If token, positions, or order book cannot be verified,
        is_safe_to_enter is False. Fresh cycle entry must ONLY proceed if
        broker data is successfully fetched and verified to have ZERO open positions
        and ZERO open/pending orders for the relevant contracts.
    """
    track = uss.normalize_track(track)
    snapshot_file = ACTIVE_SNAPSHOT_FILE if track == "5day_strategy" else THURSDAY_SNAPSHOT_FILE

    snap = active_snapshot or (
        {} if broker_wide else load_active_snapshot(filepath=snapshot_file)
    )

    contracts_to_check = []
    if not broker_wide:
        contracts_to_check = _snapshot_cycle_contracts(snap, f"{track}_prior")
        if contracts_to_check is None:
            return False, False, (
                f"Prior cycle snapshot at {snapshot_file} is missing or has incomplete contract identity. "
                "Entry blocked (fail-closed)."
            )

        # Contract-scoped cross-track checks also include Tuesday contracts.
        if check_cross_track_overlap or track == "3day_strategy":
            try:
                tue_snap = load_active_snapshot(filepath=ACTIVE_SNAPSHOT_FILE)
            except Exception as e:
                logger.warning("[%s] Could not load Tuesday snapshot for overlap check: %s", track, e)
                return False, False, f"Tuesday overlap snapshot could not be verified: {e} (fail-closed)"

            tue_contracts = _snapshot_cycle_contracts(tue_snap, "tuesday_cycle")
            if tue_contracts is None:
                return False, False, (
                    "Tuesday overlap snapshot is missing or has incomplete contract identity. "
                    "Entry blocked (fail-closed)."
                )
            contracts_to_check.extend(tue_contracts)

    if broker_wide:
        try:
            from config import get_broker_for_track, normalize_broker_name
            active_broker = normalize_broker_name(
                broker or get_broker_for_track(track, redis_client=redis_client)
            )
            if track == "3day_strategy":
                tuesday_broker = get_broker_for_track("5day_strategy", redis_client=redis_client)
                if active_broker != tuesday_broker:
                    return False, False, (
                        "Cross-track broker-wide check requires Tuesday and Thursday tracks to use the same broker; "
                        f"configured brokers differ ({tuesday_broker} vs {active_broker})."
                    )
        except Exception as e:
            logger.warning("[%s] Could not resolve configured broker for broker-wide check: %s", track, e)
            return False, False, f"Could not resolve configured broker: {e} (fail-closed)"
    else:
        active_broker = (
            broker
            or (snap.get("broker") if snap else None)
            or os.getenv("TUESDAY_BROKER" if track == "5day_strategy" else "THURSDAY_BROKER")
            or ("upstox" if track == "5day_strategy" else "angelone")
        ).lower().strip()

    # 1. Fetch broker token (Fail-closed on error)
    try:
        from common.entry_order_manager import get_broker_token
        token = get_broker_token(active_broker, redis_client=redis_client)
    except Exception as e:
        logger.warning("[%s] Could not get broker token for %s: %s", track, active_broker, e)
        return False, False, f"Could not obtain broker token for {active_broker}: {e} (fail-closed)"

    # 2. Fetch broker positions (Fail-closed on error)
    try:
        from common.broker_adapters import (
            get_broker_adapter,
            parse_positions,
            contract_match_status,
            normalize_order_status,
            _position_quantity,
            OPEN_ORDER_STATUSES,
        )
        adapter = get_broker_adapter(active_broker)
        raw_pos = adapter.fetch_positions(token)
        long_pos, short_pos, _ = parse_positions(raw_pos, include_closed=True)
    except Exception as e:
        logger.warning("[%s] Position fetch failed for %s: %s", track, active_broker, e)
        return False, False, f"Position fetch failed: {e} (fail-closed)"

    # 3. Fetch broker order book (Fail-closed on error)
    try:
        raw_orders = adapter.fetch_order_book(token)
        orders = raw_orders.get("data", []) if isinstance(raw_orders, dict) else (raw_orders or [])
        if not isinstance(orders, list):
            orders = []
    except Exception as e:
        logger.warning("[%s] Could not verify order book: %s", track, e)
        return False, False, f"Order book fetch failed: {e} (fail-closed)"

    # Check open positions (both long and short: any abs(qty) > 0 is open exposure)
    all_positions = list(long_pos or []) + list(short_pos or [])
    if broker_wide:
        for pos in all_positions:
            qty = _position_quantity(pos)
            if abs(qty) > 0:
                sym = pos.get("tradingsymbol") or pos.get("trading_symbol") or "position"
                return True, False, f"Broker has open exposure on {sym} (qty={qty})"

        for o in orders:
            st = normalize_order_status(o.get("status") or o.get("orderstatus"))
            if st in OPEN_ORDER_STATUSES:
                sym = o.get("tradingsymbol") or o.get("trading_symbol") or f"order {o.get('order_id')}"
                return True, False, f"Broker has open order {o.get('order_id')} on {sym} (status={st})"

    for pos in all_positions:
        qty = _position_quantity(pos)
        if abs(qty) <= 0:
            continue
        for c in contracts_to_check:
            match_status = contract_match_status(
                pos,
                target_strike=c["strike"],
                target_opt_type=c["opt_type"],
                target_expiry=c["expiry"],
                target_underlying="NIFTY",
            )
            if match_status is None:
                return False, False, (
                    f"Could not verify broker position identity for {c['source']} "
                    f"{c['strike']}{c['opt_type']} (fail-closed)"
                )
            if match_status:
                return True, False, f"Broker has open exposure for {c['source']} {c['strike']}{c['opt_type']} (qty={qty})"

    # Check open orders in order book
    for o in orders:
        st = normalize_order_status(o.get("status") or o.get("orderstatus"))
        if st in OPEN_ORDER_STATUSES:
            for c in contracts_to_check:
                match_status = contract_match_status(
                    o,
                    target_strike=c["strike"],
                    target_opt_type=c["opt_type"],
                    target_expiry=c["expiry"],
                    target_underlying="NIFTY",
                )
                if match_status is None:
                    return False, False, (
                        f"Could not verify open-order identity for {c['source']} "
                        f"{c['strike']}{c['opt_type']} (fail-closed)"
                    )
                if match_status:
                    return True, False, f"Broker has open order {o.get('order_id')} for {c['source']} {c['strike']}{c['opt_type']} (status={st})"

    scope = "broker" if broker_wide else "relevant contracts"
    return False, True, f"0 open positions or open orders found at broker for {scope}"


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
