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
    Return True if the current IST time is between 9:30 AM and 3:15 PM
    on a weekday (Monday-Friday).
    """
    now = _now_ist()
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    market_open = time(9, 30)
    market_close = time(15, 15)
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
    """Return the Tuesday that started the cycle containing ``d``."""
    days_since_tuesday = (d.weekday() - 1) % 7
    return d - timedelta(days=days_since_tuesday)


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

    # A Tuesday always starts a new cycle unless a snapshot has explicitly
    # been prepared for that same Tuesday (manual snapshot scenario).
    if is_tuesday(today) and week_start != today:
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
) -> dict:
    """Build a standard hourly record dictionary."""
    return {
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
        logger.info(
            "Reusing cycle strikes from %s: PUT=%d  CALL=%d",
            active_cycle["week_start_date"], put_strike, call_strike
        )
    else:
        put_strike, call_strike = strike_selector(nifty_ltp)
        logger.info(
            "Selected new strikes from first-trigger LTP %.2f: PUT=%d  CALL=%d",
            nifty_ltp, put_strike, call_strike
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
        # --- Tuesday logic ---
        # Check whether an active snapshot for THIS Tuesday already exists
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
        # --- Wednesday-Monday: reuse the complete active cycle ---
        if not active_cycle:
            logger.warning("No active snapshot found on non-Tuesday; "
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
                "captured_at": _now_utc().isoformat(),
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
    logger.info("  Gain/Loss: %s", gainloss)
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
        today = _today_ist()
        logger.info("DRY RUN mode - would collect at %s", _now_ist().isoformat())
        logger.info("Next calendar expiry: %s", get_next_weekly_expiry(today))
        logger.info("Is Tuesday: %s", is_tuesday(today))
        active_cycle = _active_cycle(load_active_snapshot(), today)
        if active_cycle:
            logger.info(
                "Active cycle: start=%s expiry=%s PUT=%d @ %s CALL=%d @ %s",
                active_cycle["week_start_date"],
                active_cycle["expiry_date"],
                active_cycle["put_strike"],
                active_cycle["put_buy_price"],
                active_cycle["call_strike"],
                active_cycle["call_buy_price"],
            )
        else:
            logger.info("No valid active cycle snapshot for today")
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
