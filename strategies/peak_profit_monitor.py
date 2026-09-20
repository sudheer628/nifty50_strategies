"""
Standalone Peak-Profit Monitor for NIFTY Weekly Option Strangle.

Runs on a standalone 15-minute cron cadence (alternating at :16 and :46 with the
weekly option collector at :01 and :31) during market hours (09:15 to 15:30 IST).

Fetches fresh quotes directly from Angel One SmartAPI and evaluates the held strangle
against the cycle's profit high-water mark via common.profit_monitor.evaluate_peak_profit.
"""

import argparse
from datetime import datetime, date, time as dtime
import logging
import os
import sys
from typing import Optional
import pytz

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import logger
from common.calendar_utils import check_nse_holiday
from common.expiry import format_expiry_angelone
from common.storage import load_active_snapshot
from common.angelone_client import get_nifty_spot, get_nifty_option_chain
from common.profit_monitor import evaluate_peak_profit

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def _now_ist() -> datetime:
    """Return current datetime in IST."""
    return datetime.now(IST)


def _today_ist() -> date:
    """Return today's date in IST."""
    return _now_ist().date()


def _is_market_time() -> bool:
    """Check if current time is within trading hours (09:15-15:30 IST Mon-Fri)."""
    now = _now_ist()
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def monitor_once(force: bool = False, no_alert: bool = False, no_email: bool = False) -> bool:
    """
    Perform one standalone 15-minute peak profit check.

    Returns:
        bool: True on successful evaluation or clean skip, False on error.
    """
    dry_run = no_alert or no_email
    today = _today_ist()

    # 1. Holiday guard
    if check_nse_holiday(today):
        logger.info("[PEAK MONITOR] Today (%s) is an NSE market holiday or weekend. Skipping.", today)
        return True

    # 2. Market hours guard
    if not force and not _is_market_time():
        logger.info("[PEAK MONITOR] Outside market hours (%s IST). Use --force to override.", _now_ist().strftime("%H:%M"))
        return True

    # 3. Snapshot existence and status guard
    snapshot = load_active_snapshot()
    if not snapshot:
        logger.info("[PEAK MONITOR] No active weekly cycle snapshot found. Skipping.")
        return True

    if snapshot.get("status") == "closed":
        logger.info("[PEAK MONITOR] Active weekly cycle %s is marked closed. Skipping.", snapshot.get("cycle_id"))
        return True

    # 4. Buy prices locked guard
    call_buy = snapshot.get("call_buy_price")
    put_buy = snapshot.get("put_buy_price")
    if call_buy is None or put_buy is None:
        logger.info("[PEAK MONITOR] Order buy prices not yet locked in snapshot. Skipping.")
        return True

    call_strike = snapshot.get("call_strike")
    put_strike = snapshot.get("put_strike")
    expiry_str = snapshot.get("expiry_date")

    if not call_strike or not put_strike or not expiry_str:
        logger.warning("[PEAK MONITOR] Active snapshot missing strike or expiry fields: %s", snapshot)
        return True

    # Parse expiry format for Angel One
    try:
        expiry_date = datetime.strptime(str(expiry_str), "%Y%m%d").date()
        expiry_angelone = format_expiry_angelone(expiry_date)
    except Exception as parse_err:
        logger.error("[PEAK MONITOR] Failed to parse expiry_date '%s': %s", expiry_str, parse_err)
        return False

    # 5. Fetch fresh spot & option quotes from Angel One SmartAPI
    try:
        spot_data = get_nifty_spot()
        nifty_ltp = spot_data.get("ltp")
        if nifty_ltp is None or nifty_ltp <= 0:
            logger.warning("[PEAK MONITOR] Failed to fetch valid NIFTY spot LTP (%s). Aborting tick.", nifty_ltp)
            return False

        option_data = get_nifty_option_chain(expiry_angelone, call_strike, put_strike)
        call_ltp = (option_data.get("call") or {}).get("ltp")
        put_ltp = (option_data.get("put") or {}).get("ltp")

        if call_ltp is None or put_ltp is None or call_ltp <= 0 or put_ltp <= 0:
            logger.warning(
                "[PEAK MONITOR] Failed to fetch valid option LTPs (CALL %s: %s | PUT %s: %s). Aborting tick.",
                call_strike, call_ltp, put_strike, put_ltp
            )
            return False
    except Exception as api_err:
        logger.warning("[PEAK MONITOR] API error fetching live market data: %s", api_err)
        return False

    # 6. Extract FSM context from snapshot
    alpha_fsm = snapshot.get("alpha_fsm") or {}
    fsm_state = alpha_fsm.get("state", "DUAL_LONG")
    fsm_total_gainloss = alpha_fsm.get("total_gainloss", 0.0)

    # 7. Evaluate peak profit
    try:
        peak_state, alert_triggered, peak_msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=nifty_ltp,
            call_strike=call_strike,
            call_ltp=call_ltp,
            call_buy=call_buy,
            put_strike=put_strike,
            put_ltp=put_ltp,
            put_buy=put_buy,
            is_first_tick=False,
            dry_run=dry_run,
            fsm_state=fsm_state,
            fsm_total_gainloss=fsm_total_gainloss,
        )

        total_cost = call_buy + put_buy
        curr_pnl_pts = round((call_ltp - call_buy) + (put_ltp - put_buy), 2)
        curr_pnl_pct = round((curr_pnl_pts / total_cost) * 100.0, 2) if total_cost > 0 else 0.0

        logger.info(
            "[STANDALONE PEAK MONITOR] %s (Current: %+0.2f%% / %+0.2f pts | Peak: %+0.2f%% / %+0.2f pts | Alerts Today: %d)",
            peak_msg,
            curr_pnl_pct,
            curr_pnl_pts,
            peak_state.get("peak_pnl_pct", 0.0),
            peak_state.get("peak_pnl_pts", 0.0),
            peak_state.get("alerts_sent_today", 0),
        )
        return True
    except Exception as eval_err:
        logger.error("[PEAK MONITOR] Error evaluating peak profit: %s", eval_err)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="NIFTY Weekly Option Standalone Peak-Profit Monitor (15-min cadence)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even outside market hours or on holidays",
    )
    parser.add_argument(
        "--no-alert",
        "--no-email",
        dest="no_alert",
        action="store_true",
        help="Evaluate peak profit and update snapshot without sending live Discord alerts",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print active snapshot peak status and exit without calling market APIs",
    )
    args = parser.parse_args()

    if args.check:
        snapshot = load_active_snapshot()
        if not snapshot:
            logger.info("No active snapshot found.")
            return
        peak_info = snapshot.get("peak_profit") or {}
        logger.info("Cycle ID:         %s", snapshot.get("cycle_id"))
        logger.info("Status:           %s", snapshot.get("status"))
        logger.info("Expiry:           %s", snapshot.get("expiry_date"))
        logger.info("CALL %s Buy:    ₹%s", snapshot.get("call_strike"), snapshot.get("call_buy_price"))
        logger.info("PUT  %s Buy:    ₹%s", snapshot.get("put_strike"), snapshot.get("put_buy_price"))
        logger.info("Peak Profit:      +%.2f%% (+%.2f pts / ₹%.2f)",
                    peak_info.get("peak_pnl_pct", 0.0),
                    peak_info.get("peak_pnl_pts", 0.0),
                    peak_info.get("peak_pnl_inr", 0.0))
        logger.info("Last Notified:    %s",
                    f"+{peak_info.get('last_notified_pct'):.2f}%" if peak_info.get("last_notified_pct") is not None else "None")
        logger.info("Alerts Today:     %d", peak_info.get("alerts_sent_today", 0))
        return

    success = monitor_once(force=args.force, no_alert=args.no_alert)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
