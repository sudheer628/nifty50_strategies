"""
Weekly option data collection strategy for NIFTY50.

This is the main strategy entry point (Phase 2).  It is designed to be
invoked by cron every hour during market hours.

Simplified weekly cycle model:
    - Tuesday, 9:30 AM:  Previous weekly cycle ends.  A fresh cycle
      begins targeting the *next* weekly expiry.  New CALL/PUT strikes
      are selected from that trigger's NIFTY LTP, and new buy prices are captured.
      The old snapshot is archived; the new one becomes active.
    - Wednesday through Monday:  Collection continues for the same
      active cycle using the Tuesday buy prices.

Usage (cron):
    # Every hour from 9:30 AM to 3:30 PM IST, Monday-Friday
    30 4-10 * * 1-5 cd /path/to/nifty50_strategies && python strategies/weekly_option_collector.py
"""

import argparse
import os
import sys
from datetime import datetime, date, time, timedelta

# Ensure the project root directory is on the Python import path so that
# ``config`` and ``common.*`` can be imported when the script is invoked
# directly (e.g. via cron).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytz

from config import (
    logger,
    STRATEGY_NAME,
    SQLITE_DIR,
)

from common.expiry import (
    get_next_weekly_expiry,
    is_tuesday,
    format_expiry_angelone,
    format_expiry_file,
    strike_selector,
)

from common.calendar_utils import (
    check_nse_holiday,
    is_strategy_start_day,
    is_strategy_closing_day,
    get_strategy_cycle_role,
)

from common.ai_strike_selector import (
    select_strikes,
    compute_static_strikes,
    get_last_composite_score,
)

from common.entry_gate import evaluate_entry_gate

from common.angelone_client import (
    get_nifty_spot,
    get_nifty_option_chain,
)

from common.storage import (
    build_db_path,
    init_db,
    insert_record,
    insert_buy_snapshot,
    save_active_snapshot,
    load_active_snapshot,
    update_active_fsm_state,
    generate_cycle_id,
)

from common.fsm_strategy import (
    init_fsm_state,
    evaluate_fsm_tick,
)

# IST timezone (used for business-logic decisions: market hours,
# Tuesday detection, expiry resolution).
IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.UTC


def _now_ist() -> datetime:
    """Return the current datetime in IST."""
    return datetime.now(IST)


def _now_utc() -> datetime:
    """Return the current datetime in UTC (used for DB timestamps)."""
    return datetime.now(UTC)


def _now_utc_ts() -> int:
    """Return the current UTC timestamp as Unix epoch integer (seconds)."""
    return int(datetime.now(UTC).timestamp())


def _today_ist() -> date:
    """Return today's date in IST."""
    return _now_ist().date()


def _is_market_time() -> bool:
    """
    Return True if the current IST time is between 9:15 AM and 3:35 PM
    on a weekday (Monday-Friday).
    """
    now = _now_ist()
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    market_open = time(9, 15)
    market_close = time(15, 35)
    return market_open <= now.time() <= market_close


def _ensure_db(week_start: date, expiry_date: date) -> str:
    """Create (if needed) and return the DB path for a weekly cycle."""
    start_str = format_expiry_file(week_start)
    expiry_str = format_expiry_file(expiry_date)
    db_path = build_db_path(start_str, expiry_str)
    init_db(db_path)
    return db_path


def _parse_file_date(value: str) -> date:
    """Parse a snapshot date stored in YYYYMMDD format."""
    return datetime.strptime(value, "%Y%m%d").date()


def _cycle_start_for_day(d: date) -> date:
    """Return the start date of the weekly cycle containing ``d``."""
    if is_strategy_start_day(d):
        return d
    days_since_tuesday = (d.weekday() - 1) % 7
    tue = d - timedelta(days=days_since_tuesday)
    mon = tue - timedelta(days=1)
    if check_nse_holiday(mon):
        wed = tue + timedelta(days=1)
        if wed <= d:
            return wed
    return tue


def _active_cycle(snapshot: dict, today: date) -> dict:
    """Validate and normalize a snapshot that may be active today."""
    if not snapshot:
        return {}

    required = (
        "week_start_date",
        "expiry_date",
        "call_strike",
        "put_strike",
        "call_buy_price",
        "put_buy_price",
    )
    if any(snapshot.get(field) is None for field in required):
        logger.warning("Active snapshot is missing required cycle fields")
        return {}

    try:
        week_start = _parse_file_date(str(snapshot["week_start_date"]))
        expiry_date = _parse_file_date(str(snapshot["expiry_date"]))
        call_strike = int(snapshot["call_strike"])
        put_strike = int(snapshot["put_strike"])
    except (TypeError, ValueError):
        logger.warning("Active snapshot contains invalid dates or strikes")
        return {}

    # If the cycle was explicitly marked closed, retire it
    if snapshot.get("status") == "closed":
        return {}

    # When today is a designated START_DAY, any prior cycle is retired
    # unless a snapshot has explicitly been prepared for today.
    if is_strategy_start_day(today) and week_start != today:
        return {}
    if week_start > today or expiry_date < today:
        return {}

    return {
        **snapshot,
        "week_start": week_start,
        "expiry": expiry_date,
        "call_strike": call_strike,
        "put_strike": put_strike,
    }


def _now_utc_ts() -> int:
    """Return current UTC timestamp as Unix integer (seconds since epoch)."""
    return int(datetime.now(UTC).timestamp())


def _build_record(
    cycle_id: str,
    expiry_file: str,
    nifty_open: float,
    nifty_ltp: float,
    nifty_prev_close: float,
    put_strike: int,
    put_ltp,
    call_strike: int,
    call_ltp,
    call_buy_price,
    put_buy_price,
    gainloss,
    fsm_data: Optional[dict] = None,
) -> dict:
    """Build a standard hourly record dictionary with both Base and FSM metrics."""
    record = {
        "strategy_name": STRATEGY_NAME,
        "collection_timestamp": _now_utc_ts(),
        "expiry_date": expiry_file,
        "nifty_open": nifty_open,
        "nifty_ltp": nifty_ltp,
        "nifty_previous_close": nifty_prev_close,
        "put_strike": put_strike,
        "put_ltp": put_ltp,
        "call_strike": call_strike,
        "call_ltp": call_ltp,
        "call_buy_price": call_buy_price,
        "put_buy_price": put_buy_price,
        "gainloss": gainloss,
        "source": "angelone",
        "cycle_id": cycle_id,
    }
    if fsm_data and isinstance(fsm_data, dict):
        record.update(fsm_data)
    return record


def collect_once(force_static: bool = False) -> bool:
    """
    Perform one hourly data collection cycle.

    - **Tuesday**: ends the previous cycle, starts a fresh one with a
      new expiry, new strikes, and new buy prices.  The old snapshot is
      archived to a dated JSON file; the new one becomes active.
    - **Wednesday-Monday**: reuses the active snapshot's buy prices and
      continues writing to the same weekly DB.

    Args:
        force_static: If True, bypass AI strike selector and use static anchor +/- 100 rule.

    Returns True on success, False on failure.
    """
    today = _today_ist()
    if check_nse_holiday(today):
        logger.info("Today (%s) is an NSE market holiday or weekend. Skipping data collection.", today)
        return True

    is_first_run_of_week = is_strategy_start_day(today)
    today_str = format_expiry_file(today)

    active = load_active_snapshot()
    active_cycle = _active_cycle(active, today)

    # ------------------------------------------------------------------
    # Determine expiry, strikes, and spot data
    # ------------------------------------------------------------------
    if active_cycle:
        expiry_date = active_cycle["expiry"]
        week_start = active_cycle["week_start"]
    else:
        expiry_date = get_next_weekly_expiry(today)
        week_start = _cycle_start_for_day(today)

    expiry_angelone = format_expiry_angelone(expiry_date)
    expiry_file = format_expiry_file(expiry_date)

    logger.info("Fetching NIFTY50 spot data...")
    spot = get_nifty_spot()
    if not spot:
        logger.error("Failed to fetch NIFTY spot data; aborting.")
        return False

    nifty_ltp = float(spot.get("ltp", 0))
    nifty_open = float(spot.get("open", 0))
    nifty_prev_close = float(spot.get("close", 0))

    if nifty_ltp <= 0 or nifty_open <= 0:
        logger.error("Invalid NIFTY spot data: LTP=%.2f, Open=%.2f; aborting.", 
                     nifty_ltp, nifty_open)
        return False

    logger.info("NIFTY LTP=%.2f  Open=%.2f  PrevClose=%.2f",
                nifty_ltp, nifty_open, nifty_prev_close)

    if active_cycle:
        put_strike = active_cycle["put_strike"]
        call_strike = active_cycle["call_strike"]
        static_put_strike = active_cycle.get("static_put_strike")
        static_call_strike = active_cycle.get("static_call_strike")
        selection_mode = active_cycle.get("selection_mode", "REUSED")
        selection_rationale = active_cycle.get("selection_rationale", "")
        logger.info(
            "Reusing cycle strikes from %s: PUT=%d  CALL=%d (Mode: %s)",
            active_cycle["week_start_date"], put_strike, call_strike, selection_mode
        )
    else:
        # Smart Entry Gate (Eliminating the blind 09:31 AM IV crush trap)
        if is_first_run_of_week:
            gate_res = evaluate_entry_gate(
                reference_ltp=nifty_ltp,
                sqlite_dir=SQLITE_DIR,
                force=force_static
            )
            if not gate_res.should_enter:
                logger.info("=" * 60)
                logger.info("  [SMART ENTRY GATE DEFERRED] Status: %s", gate_res.status)
                logger.info("  Reason: %s", gate_res.defer_reason)
                logger.info(
                    "  Metrics: IV Slope=%s | VWAP Dist=%s%% | ADX=%s | Range=%s pts",
                    f"{gate_res.iv_slope:+.2f}" if gate_res.iv_slope is not None else "N/A",
                    f"{gate_res.vwap_distance:+.3f}" if gate_res.vwap_distance is not None else "N/A",
                    f"{gate_res.adx_14:.1f}" if gate_res.adx_14 is not None else "N/A",
                    f"{gate_res.opening_range:.1f}" if gate_res.opening_range is not None else "N/A"
                )
                logger.info("  Holding execution until next scheduled collection tick (10:01 / 10:31 IST).")
                logger.info("=" * 60)
                return True

        # Dynamic AI strike selector (with fail-safe static anchor +/- 100 fallback)
        (
            put_strike,
            call_strike,
            static_put_strike,
            static_call_strike,
            selection_mode,
            selection_rationale,
            composite_score,
            directional_bias,
            target_call_delta,
            target_put_delta,
        ) = select_strikes(
            reference_ltp=nifty_ltp,
            sqlite_dir=SQLITE_DIR,
            force_static=force_static
        )
        logger.info(
            "Selected strikes from LTP %.2f [%s]: PUT=%d (Delta=%.2f)  CALL=%d (Delta=%.2f) | Bias=%s (Score=%.2f) (Static Benchmark: PUT=%d CALL=%d)",
            nifty_ltp, selection_mode, put_strike, target_put_delta, call_strike, target_call_delta,
            directional_bias, composite_score, static_put_strike, static_call_strike
        )

    option_data = get_nifty_option_chain(
        expiry_angelone, call_strike, put_strike
    )
    
    # Validate option data before proceeding
    call_ltp = (option_data.get("call") or {}).get("ltp")
    put_ltp = (option_data.get("put") or {}).get("ltp")
    
    if call_ltp is None or put_ltp is None:
        logger.error("Failed to fetch option LTPs: call_ltp=%s, put_ltp=%s; aborting.",
                     call_ltp, put_ltp)
        return False
    
    if call_ltp <= 0 or put_ltp <= 0:
        logger.error("Invalid option LTPs: call=%.2f, put=%.2f; aborting.",
                     call_ltp, put_ltp)
        return False

    # ------------------------------------------------------------------
    # Cycle identity: Tuesday = new, else = reuse active snapshot
    # ------------------------------------------------------------------
    start_str = format_expiry_file(week_start)

    if is_first_run_of_week:
        # --- Strategy Start Day (Tuesday or holiday-shifted Wednesday) ---
        # Check whether an active snapshot for THIS start day already exists
        # (e.g. manually created with correct 9:30 AM prices). If so,
        # reuse it instead of overwriting with mid-day LTPs.
        if active_cycle and active_cycle.get("week_start_date") == today_str:
            logger.info(
                "Active snapshot already exists for today (%s); "
                "reusing its strikes and buy prices.", today_str
            )
            cycle_id = active_cycle.get(
                "cycle_id", generate_cycle_id(start_str)
            )
            call_buy_price = active_cycle.get("call_buy_price")
            put_buy_price = active_cycle.get("put_buy_price")
            db_path = _ensure_db(week_start, expiry_date)
            insert_buy_snapshot(db_path, active_cycle)
        else:
            # --- Genuinely fresh cycle: capture new buy prices ---
            db_path = _ensure_db(week_start, expiry_date)
            cycle_id = generate_cycle_id(start_str)
            call_buy_price = call_ltp
            put_buy_price = put_ltp

            snapshot = {
                "strategy_name": STRATEGY_NAME,
                "cycle_id": cycle_id,
                "week_start_date": start_str,
                "expiry_date": expiry_file,
                "call_strike": call_strike,
                "put_strike": put_strike,
                "call_buy_price": call_buy_price,
                "put_buy_price": put_buy_price,
                "captured_at": _now_utc_ts(),
                "static_call_strike": static_call_strike,
                "static_put_strike": static_put_strike,
                "selection_mode": selection_mode,
                "selection_rationale": selection_rationale,
                "composite_direction_score": composite_score if 'composite_score' in locals() else get_last_composite_score(),
                "directional_bias": directional_bias if 'directional_bias' in locals() else "NEUTRAL",
                "target_call_delta": target_call_delta if 'target_call_delta' in locals() else 0.35,
                "target_put_delta": target_put_delta if 'target_put_delta' in locals() else -0.35,
                "smart_gate": gate_res.to_dict() if 'gate_res' in locals() else {},
                "alpha_fsm": init_fsm_state(
                    call_strike=call_strike,
                    put_strike=put_strike,
                    call_buy_price=call_buy_price,
                    put_buy_price=put_buy_price,
                    entry_ts=_now_utc_ts(),
                    composite_score=composite_score if 'composite_score' in locals() else get_last_composite_score(),
                ),
            }

            # Archives the previous snapshot (if any) before overwriting
            save_active_snapshot(snapshot)
            insert_buy_snapshot(db_path, snapshot)

            logger.info("Cycle buy prices captured [%s]: CALL=%d (₹%.2f)  PUT=%d (₹%.2f)",
                     selection_mode, call_strike, call_buy_price, put_strike, put_buy_price)
    else:
        # --- Mid-cycle holding/closing day: reuse the complete active cycle ---
        if not active_cycle:
            logger.warning("No active snapshot found on non-start day; "
                           "capturing a midweek fallback snapshot. Its prices "
                           "are current prices, not Tuesday prices.")
            cycle_id = generate_cycle_id(start_str)
            call_buy_price = call_ltp
            put_buy_price = put_ltp

            snapshot = {
                "strategy_name": STRATEGY_NAME,
                "cycle_id": cycle_id,
                "week_start_date": start_str,
                "expiry_date": expiry_file,
                "call_strike": call_strike,
                "put_strike": put_strike,
                "call_buy_price": call_buy_price,
                "put_buy_price": put_buy_price,
                "captured_at": _now_utc_ts(),
                "static_call_strike": static_call_strike,
                "static_put_strike": static_put_strike,
                "selection_mode": selection_mode,
                "selection_rationale": selection_rationale,
                "alpha_fsm": init_fsm_state(
                    call_strike=call_strike,
                    put_strike=put_strike,
                    call_buy_price=call_buy_price,
                    put_buy_price=put_buy_price,
                    entry_ts=_now_utc_ts(),
                    composite_score=get_last_composite_score(),
                ),
            }
            save_active_snapshot(snapshot)
        else:
            cycle_id = active_cycle.get(
                "cycle_id", generate_cycle_id(start_str)
            )
            call_buy_price = active_cycle.get("call_buy_price")
            put_buy_price = active_cycle.get("put_buy_price")

        db_path = _ensure_db(week_start, expiry_date)
        if active_cycle:
            insert_buy_snapshot(db_path, active_cycle)
        else:
            insert_buy_snapshot(db_path, snapshot)

    # Fallback: if buy prices are still None, use current LTP
    if call_buy_price is None:
        call_buy_price = call_ltp
    if put_buy_price is None:
        put_buy_price = put_ltp

    gainloss = None
    prices = (call_ltp, call_buy_price, put_ltp, put_buy_price)
    if all(price is not None for price in prices):
        gainloss = round(
            (float(call_ltp) - float(call_buy_price))
            + (float(put_ltp) - float(put_buy_price)),
            2,
        )

    # ------------------------------------------------------------------
    # Evaluate Decoupled FSM Strategy (Alpha)
    # ------------------------------------------------------------------
    curr_active = load_active_snapshot()
    fsm_state = curr_active.get("alpha_fsm")
    if not fsm_state:
        fsm_state = init_fsm_state(
            call_strike=call_strike,
            put_strike=put_strike,
            call_buy_price=call_buy_price,
            put_buy_price=put_buy_price,
            entry_ts=_now_utc_ts(),
        )

    days_to_expiry = max(0.1, (expiry_date - today).days)
    updated_fsm, fsm_events = evaluate_fsm_tick(
        fsm_state=fsm_state,
        current_call_ltp=call_ltp,
        current_put_ltp=put_ltp,
        current_ts=_now_utc_ts(),
        days_to_expiry=days_to_expiry,
    )
    if fsm_events:
        for ev in fsm_events:
            logger.info("  [ALPHA FSM EVENT] %s", ev)
    update_active_fsm_state(updated_fsm)

    fsm_data = {
        "fsm_state": updated_fsm.get("state", "DUAL_LONG"),
        "fsm_call_status": updated_fsm.get("call_leg", {}).get("status", "ACTIVE"),
        "fsm_put_status": updated_fsm.get("put_leg", {}).get("status", "ACTIVE"),
        "fsm_call_exit_price": updated_fsm.get("call_leg", {}).get("exit_price"),
        "fsm_put_exit_price": updated_fsm.get("put_leg", {}).get("exit_price"),
        "fsm_realized_pnl": updated_fsm.get("realized_pnl_pts", 0.0),
        "fsm_unrealized_pnl": updated_fsm.get("unrealized_pnl_pts", 0.0),
        "fsm_total_gainloss": updated_fsm.get("total_gainloss", 0.0),
        "fsm_roi_pct": updated_fsm.get("roi_pct", 0.0),
    }

    # ------------------------------------------------------------------
    # Write the hourly record
    # ------------------------------------------------------------------
    record = _build_record(
        cycle_id=cycle_id,
        expiry_file=expiry_file,
        nifty_open=nifty_open,
        nifty_ltp=nifty_ltp,
        nifty_prev_close=nifty_prev_close,
        put_strike=put_strike,
        put_ltp=put_ltp,
        call_strike=call_strike,
        call_ltp=call_ltp,
        call_buy_price=call_buy_price,
        put_buy_price=put_buy_price,
        gainloss=gainloss,
        fsm_data=fsm_data,
    )
    insert_record(db_path, record)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    logger.info("=" * 50)
    logger.info("Collection complete  %s",
                 _now_ist().strftime("%Y-%m-%d %H:%M IST"))
    logger.info("  NIFTY LTP: %.2f  |  Open: %.2f", nifty_ltp, nifty_open)
    logger.info("  CALL %d  LTP=%s  BuyPrice=%s",
                 call_strike, call_ltp, call_buy_price)
    logger.info("  PUT  %d  LTP=%s  BuyPrice=%s",
                 put_strike, put_ltp, put_buy_price)
    logger.info("  [BASE STRATEGY] Strangle Gain/Loss: %s pts", gainloss)
    logger.info("  [ALPHA STRATEGY] State: %s | Call: %s | Put: %s | P&L: %.2f pts (ROI: %.2f%%)",
                updated_fsm.get("state"),
                updated_fsm.get("call_leg", {}).get("status"),
                updated_fsm.get("put_leg", {}).get("status"),
                updated_fsm.get("total_gainloss", 0.0),
                updated_fsm.get("roi_pct", 0.0))
    logger.info("  Expiry: %s  |  DB: %s", expiry_file, db_path)
    logger.info("=" * 50)

    return True


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="NIFTY50 Weekly Option Data Collector"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be done without writing to DB or calling APIs",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even outside market hours",
    )
    parser.add_argument(
        "--force-static",
        action="store_true",
        help="Force static anchor +/- 100 pt strike rule instead of AI selector",
    )
    args = parser.parse_args()

    if args.dry_run:
        today = _today_ist()
        logger.info("DRY RUN mode - would collect at %s", _now_ist().isoformat())
        logger.info("Next calendar expiry: %s", get_next_weekly_expiry(today))
        logger.info("Is Tuesday: %s", is_tuesday(today))
        active_cycle = _active_cycle(load_active_snapshot(), today)
        if active_cycle:
            logger.info(
                "Active cycle: start=%s expiry=%s PUT=%d @ %s CALL=%d @ %s (Mode: %s)",
                active_cycle["week_start_date"],
                active_cycle["expiry_date"],
                active_cycle["put_strike"],
                active_cycle["put_buy_price"],
                active_cycle["call_strike"],
                active_cycle["call_buy_price"],
                active_cycle.get("selection_mode", "UNKNOWN"),
            )
        else:
            logger.info("No valid active cycle snapshot for today")
        return

    if not args.force and not _is_market_time():
        logger.info("Outside market hours. Use --force to override.")
        return

    success = collect_once(force_static=args.force_static)
    if not success:
        logger.error("Collection cycle failed.")
        sys.exit(1)

    logger.info("Collection cycle completed successfully.")


if __name__ == "__main__":
    main()
