"""
Weekly option data collection strategy for NIFTY50.

This is the main strategy entry point (Phase 2).  It is designed to be
invoked by cron every hour during market hours.

Simplified weekly cycle model:
    - Tuesday, 9:30 AM:  Previous weekly cycle ends.  A fresh cycle
      begins targeting the *next* weekly expiry.  New CALL/PUT strikes
      are selected from the day open, and new buy prices are captured.
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
from datetime import datetime, date, time

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
)

from common.expiry import (
    get_next_weekly_expiry,
    is_tuesday,
    format_expiry_angelone,
    format_expiry_file,
    strike_selector,
)

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
    generate_cycle_id,
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


def _today_ist() -> date:
    """Return today's date in IST."""
    return _now_ist().date()


def _is_market_time() -> bool:
    """
    Return True if the current IST time is between 9:30 AM and 3:30 PM
    on a weekday (Monday-Friday).
    """
    now = _now_ist()
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    market_open = time(9, 30)
    market_close = time(15, 30)
    return market_open <= now.time() <= market_close


def _ensure_db(week_start: date, expiry_date: date) -> str:
    """Create (if needed) and return the DB path for a weekly cycle."""
    start_str = format_expiry_file(week_start)
    expiry_str = format_expiry_file(expiry_date)
    db_path = build_db_path(start_str, expiry_str)
    init_db(db_path)
    return db_path


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
) -> dict:
    """Build a standard hourly record dictionary."""
    return {
        "strategy_name": STRATEGY_NAME,
        "collection_timestamp": _now_utc().isoformat(),
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
        "source": "angelone",
        "cycle_id": cycle_id,
    }


def collect_once() -> bool:
    """
    Perform one hourly data collection cycle.

    - **Tuesday**: ends the previous cycle, starts a fresh one with a
      new expiry, new strikes, and new buy prices.  The old snapshot is
      archived to a dated JSON file; the new one becomes active.
    - **Wednesday-Monday**: reuses the active snapshot's buy prices and
      continues writing to the same weekly DB.

    Returns True on success, False on failure.
    """
    today = _today_ist()
    is_first_run_of_week = is_tuesday(today)

    # ------------------------------------------------------------------
    # Determine expiry, strikes, and spot data
    # ------------------------------------------------------------------
    expiry_date = get_next_weekly_expiry(today)
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

    logger.info("NIFTY LTP=%.2f  Open=%.2f  PrevClose=%.2f",
                nifty_ltp, nifty_open, nifty_prev_close)

    put_strike, call_strike = strike_selector(nifty_open)
    logger.info("Selected strikes: PUT=%d  CALL=%d", put_strike, call_strike)

    option_data = get_nifty_option_chain(
        expiry_angelone, call_strike, put_strike
    )
    call_ltp = (option_data.get("call") or {}).get("ltp")
    put_ltp = (option_data.get("put") or {}).get("ltp")

    # ------------------------------------------------------------------
    # Cycle identity: Tuesday = new, else = reuse active snapshot
    # ------------------------------------------------------------------
    week_start = today
    start_str = format_expiry_file(week_start)

    if is_first_run_of_week:
        # --- Tuesday logic ---
        # Check whether an active snapshot for THIS Tuesday already exists
        # (e.g. manually created with correct 9:30 AM prices). If so,
        # reuse it instead of overwriting with mid-day LTPs.
        active = load_active_snapshot()
        if active and active.get("week_start_date") == start_str:
            logger.info(
                "Active snapshot already exists for today (%s); "
                "reusing those buy prices.", start_str
            )
            cycle_id = active.get("cycle_id", generate_cycle_id(start_str))
            call_buy_price = active.get("call_buy_price")
            put_buy_price = active.get("put_buy_price")
            db_path = _ensure_db(week_start, expiry_date)
        else:
            # --- Genuinely fresh Tuesday: capture new buy prices ---
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
                "captured_at": _now_utc().isoformat(),
            }

            # Archives the previous snapshot (if any) before overwriting
            save_active_snapshot(snapshot)
            insert_buy_snapshot(db_path, snapshot)

            logger.info("Tuesday buy prices captured: CALL=%.2f  PUT=%.2f",
                     call_buy_price, put_buy_price)
    else:
        # --- Wednesday onward: reuse existing buy prices ---
        active = load_active_snapshot()
        if not active:
            logger.warning("No active snapshot found on non-Tuesday; "
                           "capturing fresh buy prices as fallback.")
            cycle_id = generate_cycle_id(start_str)
            call_buy_price = call_ltp
            put_buy_price = put_ltp
        else:
            cycle_id = active.get("cycle_id", generate_cycle_id(start_str))
            call_buy_price = active.get("call_buy_price")
            put_buy_price = active.get("put_buy_price")

        db_path = _ensure_db(week_start, expiry_date)

    # Fallback: if buy prices are still None, use current LTP
    if call_buy_price is None:
        call_buy_price = call_ltp
    if put_buy_price is None:
        put_buy_price = put_ltp

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
    args = parser.parse_args()

    if args.dry_run:
        logger.info("DRY RUN mode - would collect at %s", _now_ist().isoformat())
        logger.info("Next expiry: %s", get_next_weekly_expiry())
        logger.info("Is Tuesday: %s", is_tuesday())
        return

    if not args.force and not _is_market_time():
        logger.info("Outside market hours. Use --force to override.")
        return

    success = collect_once()
    if not success:
        logger.error("Collection cycle failed.")
        sys.exit(1)

    logger.info("Collection cycle completed successfully.")


if __name__ == "__main__":
    main()
