#!/usr/bin/env python3
"""
register_manual_entry.py - Register Manually Executed Option Orders for Automated Exit Tracking.

Enables manual order placement in the broker (e.g. on Monday before Wednesday start day),
suppresses automated duplicate BUY orders on Wednesday, and registers:
  1. Active snapshot file (current_week_buy.json / current_thursday_buy.json)
  2. Alpha FSM decoupled state with intrinsic/extrinsic decomposition
  3. Unified strategy store (active_strategies.json) & MongoDB Atlas
  4. Redis authorization keys, managed order IDs, and initial trailing stops
  5. Sets LIVE_BUY_TRIGGER_TUESDAY=false and LIVE_EXIT_TRIGGER_TUESDAY=true

Usage:
  # 1. Auto-detect from broker order book (Recommended):
  .venv/bin/python scripts/register_manual_entry.py --auto-detect

  # 2. Explicit CLI arguments:
  .venv/bin/python scripts/register_manual_entry.py \
      --call-strike 24300 --call-price 220.50 --call-order-id 261109000177180 \
      --put-strike 24100 --put-price 195.00 --put-order-id 261109000177181 \
      --qty 65 --expiry 2026-11-17 --start-date 2026-11-11

  # 3. Dry-run preview:
  .venv/bin/python scripts/register_manual_entry.py --auto-detect --dry-run
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# Add repo root to sys.path
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from config import (
    ACTIVE_SNAPSHOT_FILE,
    NIFTY_LOT_SIZE,
    SQLITE_DIR,
    STRIKE_MONEYNESS_MODE,
    THURSDAY_SNAPSHOT_FILE,
    get_broker_for_track,
    get_config_int,
    get_redis_client,
)
from common.broker_adapters import (
    get_broker_adapter,
    order_filled_quantity,
    parse_order_book,
)
from common.calendar_utils import is_strategy_start_day
from common.expiry import (
    compute_intrinsic_extrinsic,
    format_expiry_file,
    get_next_weekly_expiry,
)
from common.fno_keys import (
    build_leg_enabled_key,
    normalize_cycle,
    normalize_expiry,
)
from common.fsm_strategy import init_fsm_state
from common.storage import (
    build_db_path,
    build_thursday_db_path,
    init_db,
    insert_buy_snapshot,
    load_active_snapshot,
    save_active_snapshot,
)
from common.unified_strategy_store import (
    init_or_rotate_cycle,
    update_locked_prices,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [register_manual_entry] %(message)s",
)
logger = logging.getLogger("register_manual_entry")


def parse_tradingsymbol(tradingsymbol: str) -> Dict[str, str]:
    """Parse NSE/Upstox tradingsymbol into underlying, expiry, strike, option_type."""
    raw = str(tradingsymbol or "").upper().strip()
    m1 = re.match(r"^([A-Z]+)(\d{1,2}[A-Z]{3}\d{2})(\d{4,5})(CE|PE)$", raw)
    if m1:
        return {"underlying": m1.group(1), "expiry": m1.group(2), "strike": m1.group(3), "option_type": m1.group(4), "raw": raw}
    m2 = re.match(r"^([A-Z]+)(\d{2}[A-Z]{3})(\d{4,5})(CE|PE)$", raw)
    if m2:
        return {"underlying": m2.group(1), "expiry": m2.group(2), "strike": m2.group(3), "option_type": m2.group(4), "raw": raw}
    m_compact = re.match(r"^([A-Z]+)(\d{2}[1-9OND]\d{2})(\d{4,5})(CE|PE)$", raw)
    if m_compact:
        return {"underlying": m_compact.group(1), "expiry": m_compact.group(2), "strike": m_compact.group(3), "option_type": m_compact.group(4), "raw": raw}
    m_gen = re.match(r"^(.*?)(\d+)(CE|PE)$", raw)
    if m_gen:
        return {"underlying": m_gen.group(1), "expiry": "UNKNOWN", "strike": m_gen.group(2), "option_type": m_gen.group(3), "raw": raw}
    return {"underlying": raw[:10] if raw else "UNKNOWN", "expiry": "UNKNOWN", "strike": "0", "option_type": "UNKNOWN", "raw": raw}


def build_trail_redis_keys(
    broker: str,
    tradingsymbol: str,
    instrument_token: str,
    active_expiry: Optional[str] = None,
    cycle: str = "tue",
) -> Dict[str, str]:
    """Build trailing state Redis keys for the cycle."""
    broker = (broker or "upstox").lower().strip()
    c = normalize_cycle(cycle)
    details = parse_tradingsymbol(tradingsymbol)
    safe_token = str(instrument_token or "NOTOKEN").replace("|", "_").replace(":", "_").upper()
    safe_symbol = str(tradingsymbol or "NOSYMBOL").upper().replace(":", "_").replace("|", "_")
    expiry_iso = normalize_expiry(details.get("expiry"), active_expiry=active_expiry)
    contract_suffix = (
        f"{details['underlying']}:{expiry_iso}:{details['strike']}:"
        f"{details['option_type']}:{safe_symbol}:{safe_token}"
    )
    base = f"fno:{broker}:trail:{c}:{contract_suffix}"
    return {
        "base": base,
        "avg": f"{base}:avg",
        "sl": f"{base}:sl",
        "max": f"{base}:max",
        "qty": f"{base}:qty",
        "mode": f"{base}:mode",
        "order_id": f"{base}:order_id",
        "entry_order_id": f"{base}:entry_order_id",
        "market_order_id": f"{base}:market_order_id",
        "exit_filled_qty": f"{base}:exit_filled_qty",
        "contract_suffix": contract_suffix,
        "expiry_iso": expiry_iso,
        "details": details,
    }


def auto_detect_orders_from_broker(
    broker: str = "upstox",
    target_expiry_iso: str = "2026-11-17",
    redis_client: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Query broker order book for today's executed NIFTY buy orders matching target expiry."""
    try:
        adapter = get_broker_adapter(broker)
    except Exception as exc:
        logger.error("Failed to initialize broker adapter for '%s': %s", broker, exc)
        return None

    try:
        token = adapter.get_access_token(redis_client=redis_client)
    except Exception as exc:
        logger.error("Failed to retrieve access token for '%s': %s", broker, exc)
        return None

    if not token:
        logger.error("Empty access token retrieved for '%s'. Check Redis or .env credentials.", broker)
        return None

    logger.info("Fetching order book from %s...", broker.upper())
    try:
        raw_orders = adapter.fetch_order_book(token)
    except Exception as exc:
        logger.error("Failed to fetch order book from %s: %s", broker, exc)
        return None

    parsed_orders = parse_order_book(raw_orders)
    logger.info("Retrieved %d total orders from %s order book.", len(parsed_orders), broker.upper())

    matching_calls = []
    matching_puts = []

    for order in parsed_orders:
        ttype = str(order.get("transaction_type") or "").upper().strip()
        if ttype != "BUY":
            continue

        filled = order_filled_quantity(order)
        status = str(order.get("status") or "").lower().strip()
        if filled <= 0 and status not in ("complete", "filled"):
            continue

        symbol = str(order.get("tradingsymbol") or "")
        details = parse_tradingsymbol(symbol)
        if details["underlying"] != "NIFTY":
            continue

        exp_iso = normalize_expiry(details["expiry"], active_expiry=target_expiry_iso)
        if exp_iso != target_expiry_iso:
            continue

        opt_type = details["option_type"]
        if opt_type == "CE":
            matching_calls.append(order)
        elif opt_type == "PE":
            matching_puts.append(order)

    if not matching_calls or not matching_puts:
        logger.warning(
            "Auto-detection could not find both matching CE and PE buy orders for %s on expiry %s. "
            "(Found %d CE, %d PE)",
            broker.upper(), target_expiry_iso, len(matching_calls), len(matching_puts)
        )
        return None

    # Pick the most recent matching orders
    call_order = matching_calls[-1]
    put_order = matching_puts[-1]

    c_details = parse_tradingsymbol(call_order.get("tradingsymbol"))
    p_details = parse_tradingsymbol(put_order.get("tradingsymbol"))

    c_avg = float(call_order.get("average_price") or call_order.get("price") or 0.0)
    p_avg = float(put_order.get("average_price") or put_order.get("price") or 0.0)
    c_qty = int(order_filled_quantity(call_order) or call_order.get("quantity") or 0)
    p_qty = int(order_filled_quantity(put_order) or put_order.get("quantity") or 0)

    return {
        "call": {
            "order_id": str(call_order.get("order_id") or ""),
            "strike": int(c_details["strike"]),
            "price": c_avg,
            "qty": c_qty,
            "tradingsymbol": call_order.get("tradingsymbol"),
            "instrument_token": call_order.get("instrument_token"),
        },
        "put": {
            "order_id": str(put_order.get("order_id") or ""),
            "strike": int(p_details["strike"]),
            "price": p_avg,
            "qty": p_qty,
            "tradingsymbol": put_order.get("tradingsymbol"),
            "instrument_token": put_order.get("instrument_token"),
        },
    }


def register_manual_cycle(
    track: str = "tuesday",
    broker: str = "upstox",
    call_strike: int = 0,
    put_strike: int = 0,
    call_price: float = 0.0,
    put_price: float = 0.0,
    call_order_id: str = "",
    put_order_id: str = "",
    call_tradingsymbol: Optional[str] = None,
    put_tradingsymbol: Optional[str] = None,
    call_token: Optional[str] = None,
    put_token: Optional[str] = None,
    qty: int = 65,
    expiry_iso: str = "2026-11-17",
    start_date_iso: str = "2026-11-11",
    spot: Optional[float] = None,
    dry_run: bool = False,
    redis_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Main registration routine:
    1. Validates strikes and prices.
    2. Decomposes intrinsic / extrinsic value.
    3. Initializes Alpha FSM state.
    4. Writes current_week_buy.json & SQLite DB.
    5. Syncs active_strategies.json & MongoDB Atlas.
    6. Configures Redis authorization, managed order IDs, and trailing keys.
    """
    cycle_shorthand = normalize_cycle(track)
    is_tue = (cycle_shorthand == "tue")
    strategy_key = "5day_strategy" if is_tue else "3day_strategy"
    snapshot_file = ACTIVE_SNAPSHOT_FILE if is_tue else THURSDAY_SNAPSHOT_FILE

    expiry_dt = datetime.strptime(expiry_iso, "%Y-%m-%d").date()
    start_dt = datetime.strptime(start_date_iso, "%Y-%m-%d").date()
    expiry_file = format_expiry_file(expiry_dt)
    start_file = format_expiry_file(start_dt)

    now_ts = int(datetime.now(timezone.utc).timestamp())
    nifty_spot = float(spot) if spot and spot > 0 else (call_strike + put_strike) / 2.0

    logger.info("Computing intrinsic & extrinsic option value decomposition...")
    ie = compute_intrinsic_extrinsic(
        spot=nifty_spot,
        call_strike=call_strike,
        put_strike=put_strike,
        call_ltp=call_price,
        put_ltp=put_price,
    )

    logger.info("Initializing Alpha FSM decoupled state...")
    alpha_fsm = init_fsm_state(
        call_strike=call_strike,
        put_strike=put_strike,
        call_buy_price=call_price,
        put_buy_price=put_price,
        entry_ts=now_ts,
        moneyness="ITM",
        call_intrinsic=ie.get("call_intrinsic", 0.0),
        call_extrinsic=ie.get("call_extrinsic", 0.0),
        put_intrinsic=ie.get("put_intrinsic", 0.0),
        put_extrinsic=ie.get("put_extrinsic", 0.0),
        spread_width=ie.get("spread_width", abs(put_strike - call_strike)),
    )

    cycle_id = f"CYCLE-{cycle_shorthand.upper()}-{start_file}-{expiry_file}"

    snapshot = {
        "strategy_name": "nifty50_options_strategy" if is_tue else "nifty50_thursday_3day_collector",
        "cycle_id": cycle_id,
        "week_start_date": start_file,
        "start_date": start_date_iso,
        "expiry_date": expiry_file,
        "entry_spot": nifty_spot,
        "call_strike": call_strike,
        "put_strike": put_strike,
        "call_buy_price": call_price,
        "put_buy_price": put_price,
        "call_ref_ltp": call_price,
        "put_ref_ltp": put_price,
        "call_order_id": call_order_id,
        "put_order_id": put_order_id,
        "call_tradingsymbol": call_tradingsymbol,
        "put_tradingsymbol": put_tradingsymbol,
        "entry_qty": qty,
        "entry_status": "FILLED",
        "broker": broker,
        "captured_at": now_ts,
        "selection_mode": "MANUAL_ENTRY",
        "selection_rationale": "Manually placed in broker and registered for automated Alpha FSM exit management.",
        "moneyness": "ITM",
        "spread_width": ie.get("spread_width", abs(put_strike - call_strike)),
        "call_intrinsic": ie.get("call_intrinsic", 0.0),
        "call_extrinsic": ie.get("call_extrinsic", 0.0),
        "put_intrinsic": ie.get("put_intrinsic", 0.0),
        "put_extrinsic": ie.get("put_extrinsic", 0.0),
        "total_intrinsic": ie.get("total_intrinsic", 0.0),
        "total_extrinsic": ie.get("total_extrinsic", 0.0),
        "alpha_fsm": alpha_fsm,
    }

    # Redis Keys Setup
    call_sym = call_tradingsymbol or f"NIFTY{expiry_dt.strftime('%y%b').upper()}{call_strike}CE"
    put_sym = put_tradingsymbol or f"NIFTY{expiry_dt.strftime('%y%b').upper()}{put_strike}PE"
    call_tok = call_token or "NOTOKEN"
    put_tok = put_token or "NOTOKEN"

    call_auth_key = build_leg_enabled_key(cycle_shorthand, expiry_iso, call_strike, "CE", broker)
    put_auth_key = build_leg_enabled_key(cycle_shorthand, expiry_iso, put_strike, "PE", broker)

    call_trail = build_trail_redis_keys(broker, call_sym, call_tok, active_expiry=expiry_iso, cycle=cycle_shorthand)
    put_trail = build_trail_redis_keys(broker, put_sym, put_tok, active_expiry=expiry_iso, cycle=cycle_shorthand)

    managed_orders_key = f"fno:{broker}:managed_order_ids:{cycle_shorthand}"
    orders_live_key = "ORDERS_LIVE_TUE" if is_tue else "ORDERS_LIVE_THU"
    live_buy_key = "LIVE_BUY_TRIGGER_TUESDAY" if is_tue else "LIVE_BUY_TRIGGER_THURSDAY"
    live_exit_key = "LIVE_EXIT_TRIGGER_TUESDAY" if is_tue else "LIVE_EXIT_TRIGGER_THURSDAY"

    redis_ops = [
        ("SET", call_auth_key, str(qty)),
        ("SET", put_auth_key, str(qty)),
        ("SADD", managed_orders_key, call_order_id),
        ("SADD", managed_orders_key, put_order_id),
        ("SET", orders_live_key, "2"),
        ("SET", live_buy_key, "false"),
        ("SET", live_exit_key, "true"),
        # Trailing keys for Call
        ("SET", call_trail["avg"], str(call_price)),
        ("SET", call_trail["max"], str(call_price)),
        ("SET", call_trail["sl"], str(round(call_price * 0.75, 2))),
        ("SET", call_trail["qty"], str(qty)),
        ("SET", call_trail["mode"], "slm"),
        ("SET", call_trail["entry_order_id"], str(call_order_id)),
        # Trailing keys for Put
        ("SET", put_trail["avg"], str(put_price)),
        ("SET", put_trail["max"], str(put_price)),
        ("SET", put_trail["sl"], str(round(put_price * 0.75, 2))),
        ("SET", put_trail["qty"], str(qty)),
        ("SET", put_trail["mode"], "slm"),
        ("SET", put_trail["entry_order_id"], str(put_order_id)),
    ]

    result = {
        "status": "ready" if not dry_run else "dry_run",
        "cycle_id": cycle_id,
        "track": track,
        "broker": broker,
        "week_start_date": start_file,
        "expiry_date": expiry_file,
        "call_strike": call_strike,
        "call_price": call_price,
        "call_order_id": call_order_id,
        "put_strike": put_strike,
        "put_price": put_price,
        "put_order_id": put_order_id,
        "qty": qty,
        "snapshot_file": snapshot_file,
        "redis_ops_count": len(redis_ops),
    }

    if dry_run:
        logger.info("[DRY-RUN] Snapshot payload:\n%s", json.dumps(snapshot, indent=2))
        logger.info("[DRY-RUN] Redis operations planned (%d):", len(redis_ops))
        for op, k, v in redis_ops:
            logger.info("  [DRY-RUN] %s %s -> %s", op, k, v)
        return result

    # 1. Save Snapshot
    logger.info("Writing snapshot to %s...", snapshot_file)
    save_active_snapshot(snapshot, filepath=snapshot_file)

    # 2. Insert into SQLite DB
    try:
        db_path = build_db_path(start_file, expiry_file) if is_tue else build_thursday_db_path(start_file, expiry_file)
        init_db(db_path)
        insert_buy_snapshot(db_path, snapshot)
        logger.info("Inserted buy snapshot into SQLite DB (%s)", db_path)
    except Exception as exc:
        logger.warning("Could not insert snapshot into SQLite DB: %s", exc)

    # 3. Update Unified Strategy Store & MongoDB
    try:
        init_or_rotate_cycle(strategy_key, {
            "cycle_id": cycle_id,
            "start_date": start_date_iso,
            "expiry_date": expiry_iso,
            "call_strike": call_strike,
            "put_strike": put_strike,
            "moneyness": "ITM",
            "entry_spot": nifty_spot,
            "entry_qty": qty,
            "broker": broker,
        })
        update_locked_prices(
            strategy_key,
            call_buy=call_price,
            put_buy=put_price,
            spot=nifty_spot,
            timestamp=now_ts,
            call_order_id=call_order_id,
            put_order_id=put_order_id,
            broker=broker,
            entry_status="FILLED",
        )
        logger.info("Updated unified strategy store (%s) and synced to MongoDB Atlas.", strategy_key)
    except Exception as exc:
        logger.warning("Failed to sync unified strategy store: %s", exc)

    # 4. Commit Redis keys
    r = redis_client if redis_client is not None else get_redis_client()
    if r:
        logger.info("Applying Redis authorization, fast gates, and trailing states...")
        for op, k, v in redis_ops:
            try:
                if op == "SET":
                    r.set(k, v)
                elif op == "SADD":
                    r.sadd(k, v)
            except Exception as r_err:
                logger.warning("Redis %s failed for %s: %s", op, k, r_err)
        logger.info("Redis state successfully configured.")
    else:
        logger.warning("Redis client unavailable; Redis authorization keys were not written.")

    result["status"] = "registered_successfully"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register manual option entries for automated exit and Alpha FSM tracking."
    )
    parser.add_argument("--auto-detect", action="store_true", help="Auto-detect executed buy orders from broker order book")
    parser.add_argument("--track", type=str, default="tuesday", choices=["tuesday", "thursday"], help="Strategy track (default: tuesday)")
    parser.add_argument("--broker", type=str, default="upstox", help="Broker name (default: upstox)")
    parser.add_argument("--expiry", type=str, default=None, help="Expiry date YYYY-MM-DD (default: next weekly expiry e.g. 2026-11-17)")
    parser.add_argument("--start-date", type=str, default=None, help="Strategy start date YYYY-MM-DD (default: 2026-11-11 for Wednesday start)")
    parser.add_argument("--call-strike", type=int, default=None, help="Call option strike")
    parser.add_argument("--put-strike", type=int, default=None, help="Put option strike")
    parser.add_argument("--call-price", type=float, default=None, help="Call buy average price")
    parser.add_argument("--put-price", type=float, default=None, help="Put buy average price")
    parser.add_argument("--call-order-id", type=str, default=None, help="Broker order ID for Call leg")
    parser.add_argument("--put-order-id", type=str, default=None, help="Broker order ID for Put leg")
    parser.add_argument("--qty", type=int, default=65, help="Leg quantity (default: 65)")
    parser.add_argument("--spot", type=float, default=None, help="NIFTY spot price at entry")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without modifying disk or Redis")
    parser.add_argument("-y", "--yes", action="store_true", help="Bypass interactive confirmation prompt")

    args = parser.parse_args()

    # Determine default dates
    target_expiry = args.expiry
    if not target_expiry:
        try:
            next_exp = get_next_weekly_expiry(date.today())
            target_expiry = next_exp.strftime("%Y-%m-%d")
        except Exception:
            target_expiry = "2026-11-17"

    start_date = args.start_date
    if not start_date:
        # Default start date for this holiday week is Wednesday 2026-11-11
        start_date = "2026-11-11"

    logger.info("=" * 65)
    logger.info("🛠️  MANUAL ENTRY REGISTRATION FOR AUTOMATED ALPHA FSM EXIT")
    logger.info("   Track: %s | Broker: %s | Start: %s | Expiry: %s", args.track.upper(), args.broker.upper(), start_date, target_expiry)
    logger.info("=" * 65)

    call_strike = args.call_strike
    put_strike = args.put_strike
    call_price = args.call_price
    put_price = args.put_price
    call_oid = args.call_order_id
    put_oid = args.put_order_id
    qty = args.qty
    call_sym = None
    put_sym = None
    call_tok = None
    put_tok = None

    if args.auto_detect:
        logger.info("Running auto-detection against %s order book...", args.broker.upper())
        detected = auto_detect_orders_from_broker(
            broker=args.broker,
            target_expiry_iso=target_expiry,
        )
        if not detected:
            logger.error("Auto-detection failed to find matching orders. Please check broker or provide manual CLI arguments.")
            return 1

        call_data = detected["call"]
        put_data = detected["put"]

        call_strike = call_data["strike"]
        call_price = call_data["price"]
        call_oid = call_data["order_id"]
        call_sym = call_data["tradingsymbol"]
        call_tok = call_data["instrument_token"]

        put_strike = put_data["strike"]
        put_price = put_data["price"]
        put_oid = put_data["order_id"]
        put_sym = put_data["tradingsymbol"]
        put_tok = put_data["instrument_token"]

        qty = call_data["qty"] or put_data["qty"] or args.qty

        print("\n" + "=" * 65)
        print("🔍 AUTO-DETECTED OPTION TRADES:")
        print(f"  • CALL Leg: NIFTY {target_expiry} Strike {call_strike} CE")
        print(f"              Buy Price: ₹{call_price:.2f} | Qty: {qty} | Order ID: {call_oid}")
        print(f"  • PUT Leg : NIFTY {target_expiry} Strike {put_strike} PE")
        print(f"              Buy Price: ₹{put_price:.2f} | Qty: {qty} | Order ID: {put_oid}")
        print("=" * 65 + "\n")

        if not args.yes and not args.dry_run:
            confirm = input("Register these orders for automated Alpha FSM exit management? [Y/n]: ").strip().lower()
            if confirm not in ("", "y", "yes"):
                logger.info("Registration cancelled by user.")
                return 0

    # Validation
    missing = []
    if not call_strike: missing.append("--call-strike")
    if not put_strike: missing.append("--put-strike")
    if call_price is None or call_price <= 0: missing.append("--call-price")
    if put_price is None or put_price <= 0: missing.append("--put-price")
    if not call_oid: missing.append("--call-order-id")
    if not put_oid: missing.append("--put-order-id")

    if missing:
        logger.error("Missing required order details: %s. Use --auto-detect or provide all flags.", ", ".join(missing))
        return 1

    res = register_manual_cycle(
        track=args.track,
        broker=args.broker,
        call_strike=call_strike,
        put_strike=put_strike,
        call_price=call_price,
        put_price=put_price,
        call_order_id=call_oid,
        put_order_id=put_oid,
        call_tradingsymbol=call_sym,
        put_tradingsymbol=put_sym,
        call_token=call_tok,
        put_token=put_tok,
        qty=qty,
        expiry_iso=target_expiry,
        start_date_iso=start_date,
        spot=args.spot,
        dry_run=args.dry_run,
    )

    logger.info("Registration completed with status: %s", res.get("status"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
