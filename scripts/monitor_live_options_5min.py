#!/usr/bin/env python3
"""
monitor_live_options_5min.py - 5-Minute Options Strategy Telemetry Poller.

Runs on a 5-minute cadence via cron during market hours (09:15 - 15:30 IST, Mon - Fri).
For each active options strategy (Tuesday 5-Day and Thursday 3-Day):
1. Fetches real-time spot and option quotes (CALL & PUT LTP) from Angel One SmartAPI / Upstox.
2. Computes unrealized P&L (points, percentage, and INR with lot size 65).
3. Updates `latest_ltp` in `active_strategies.json`.
4. Appends a clean, single-row telemetry record to MongoDB `derivative_cycle_live_logs`.
5. Syncs the updated living state to MongoDB `derivative_active_cycles`.

Usage:
    python scripts/monitor_live_options_5min.py
    python scripts/monitor_live_options_5min.py --force --verbose
    python scripts/monitor_live_options_5min.py --track 5day_strategy
"""

import os
import sys
import argparse
import logging
from datetime import datetime, time as dtime, timezone
from pathlib import Path

# Add project root to sys.path
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_CURRENT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Also add sibling sentinel-hermes to sys.path if available
SENTINEL_DIR = os.path.join(os.path.dirname(_PROJECT_ROOT), "sentinel-hermes")
if os.path.exists(SENTINEL_DIR) and SENTINEL_DIR not in sys.path:
    sys.path.insert(0, SENTINEL_DIR)

from config import logger, ACTIVE_STRATEGIES_JSON, NIFTY_LOT_SIZE
from common.unified_strategy_store import (
    normalize_track,
    load_active_strategies,
    update_latest_ltp,
)
from common.expiry import format_expiry_angelone

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except ImportError:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

UTC = timezone.utc

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def is_market_open(force: bool = False) -> bool:
    """Check if Indian market is open (09:15 - 15:30 IST, Mon - Fri)."""
    if force:
        return True
    now = datetime.now(IST)
    if now.weekday() > 4:  # Saturday or Sunday
        logger.info("Market closed (Weekend: %s).", now.strftime("%A"))
        return False

    current_time = now.time()
    if not (MARKET_OPEN <= current_time <= MARKET_CLOSE):
        logger.info("Market closed (Outside 09:15 - 15:30 IST: %s).", current_time.strftime("%H:%M:%S"))
        return False

    return True


def fetch_live_quotes(expiry_str: str, call_strike: int, put_strike: int) -> dict:
    """
    Fetch NIFTY spot and active strike option quotes.

    Attempts Angel One SmartAPI first, with graceful fallback to Upstox feed.
    """
    spot_ltp = None
    call_ltp = None
    put_ltp = None

    # Parse expiry date object
    exp_date_obj = None
    clean_exp = str(expiry_str).replace("-", "").strip()
    if len(clean_exp) == 8 and clean_exp.isdigit():
        try:
            exp_date_obj = datetime.strptime(clean_exp, "%Y%m%d").date()
        except ValueError:
            pass

    # 1. Angel One SmartAPI
    try:
        from common.angelone_client import get_nifty_spot, get_nifty_option_chain
        spot_res = get_nifty_spot()
        if spot_res and spot_res.get("ltp"):
            spot_ltp = float(spot_res["ltp"])

        if exp_date_obj:
            exp_angel = format_expiry_angelone(exp_date_obj)
            chain = get_nifty_option_chain(exp_angel, int(call_strike), int(put_strike))
            if chain:
                c_data = chain.get("call") or {}
                p_data = chain.get("put") or {}
                if c_data.get("ltp"):
                    call_ltp = float(c_data["ltp"])
                if p_data.get("ltp"):
                    put_ltp = float(p_data["ltp"])
    except Exception as e:
        logger.debug("Angel One quote fetch exception: %s", e)

    # 2. Upstox fallback if any quotes are missing
    if spot_ltp is None or call_ltp is None or put_ltp is None:
        try:
            from common.upstox_feed_reader import get_latest_quote
            if spot_ltp is None:
                spot_up = get_latest_quote("NSE_INDEX|Nifty 50")
                if spot_up and spot_up.get("last_price"):
                    spot_ltp = float(spot_up["last_price"])
        except Exception as e_up:
            logger.debug("Upstox fallback quote fetch exception: %s", e_up)

    return {
        "spot": spot_ltp,
        "call_ltp": call_ltp,
        "put_ltp": put_ltp,
    }


def poll_cycle_telemetry(track: str, strat_data: dict, force: bool = False, verbose: bool = False) -> bool:
    """
    Execute 5-min telemetry poll for a single active strategy track.
    """
    cycle_id = strat_data.get("cycle_id")
    status = strat_data.get("status")
    call_strike = strat_data.get("call_strike")
    put_strike = strat_data.get("put_strike")
    expiry_date = strat_data.get("expiry_date")

    if status != "active" or not cycle_id or not call_strike or not put_strike:
        if verbose:
            logger.info("[%s] Cycle is not active (%s, status=%s). Skipping.", track, cycle_id, status)
        return False

    quotes = fetch_live_quotes(expiry_date, int(call_strike), int(put_strike))
    spot = quotes.get("spot")
    c_ltp = quotes.get("call_ltp")
    p_ltp = quotes.get("put_ltp")

    if spot is None or c_ltp is None or p_ltp is None:
        logger.warning(
            "[%s] Incomplete quotes fetched (Spot=%s, CALL %s=%s, PUT %s=%s). Skipping tick.",
            track, spot, call_strike, c_ltp, put_strike, p_ltp
        )
        return False

    update_latest_ltp(
        track=track,
        spot=spot,
        call_ltp=c_ltp,
        put_ltp=p_ltp,
    )

    locked = strat_data.get("day1_locked_price") or {}
    comb_buy = locked.get("combined_buy_price")
    comb_ltp = round(c_ltp + p_ltp, 2)
    pnl_pts = round(comb_ltp - comb_buy, 2) if comb_buy else 0.0
    pnl_pct = round((pnl_pts / comb_buy) * 100.0, 2) if comb_buy and comb_buy > 0 else 0.0
    pnl_inr = round(pnl_pts * NIFTY_LOT_SIZE, 2)

    logger.info(
        "[%s | %s] Spot: %.2f | CE %d: ₹%.2f | PE %d: ₹%.2f | Comb: ₹%.2f (P&L: %+.2f pts / %+.2f%% / ₹%+.2f)",
        track, cycle_id, spot, call_strike, c_ltp, put_strike, p_ltp, comb_ltp, pnl_pts, pnl_pct, pnl_inr
    )
    return True


def main():
    parser = argparse.ArgumentParser(description="5-Minute Live Options Strategy Telemetry Poller")
    parser.add_argument("--force", action="store_true", help="Bypass market hours check")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--track", choices=["5day_strategy", "3day_strategy", "both"], default="both", help="Strategy track to poll")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] [options_5min_poller] %(message)s",
    )

    if not is_market_open(force=args.force):
        logger.info("Market is currently closed. Poller exiting gracefully.")
        sys.exit(0)

    active_data = load_active_strategies()
    polled_count = 0

    tracks_to_check = (
        ["5day_strategy", "3day_strategy"] if args.track == "both"
        else [normalize_track(args.track)]
    )

    for track_name in tracks_to_check:
        strat = active_data.get(track_name) or {}
        if poll_cycle_telemetry(track_name, strat, force=args.force, verbose=args.verbose):
            polled_count += 1

    logger.info("Telemetry poller cycle finished. Polled %d active strategy track(s).", polled_count)


if __name__ == "__main__":
    main()
