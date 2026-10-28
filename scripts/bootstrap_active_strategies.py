#!/usr/bin/env python3
"""
bootstrap_active_strategies.py - Mid-Week Bootstrap Utility.

Enables immediate activation on Wednesday or any mid-cycle day without waiting for Tuesday:
1. Reads `current_week_buy.json` from SNAPSHOT_DIR to seed `5day_strategy`.
2. Checks if `current_thursday_buy.json` exists; if so seeds `3day_strategy`, else sets `pending_entry`.
3. Sets up initial locked prices baseline and simplified exit signal slots.
4. Performs an immediate test LTP poll or records baseline snapshot.
5. Upserts `active_strategies.json` and pushes initial state to MongoDB Atlas (`derivative_active_cycles`).

Usage:
    python scripts/bootstrap_active_strategies.py
    python scripts/bootstrap_active_strategies.py --poll
"""

import os
import sys
import json
import argparse
import logging
from pathlib import Path

# Add project root to sys.path
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_CURRENT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import (
    logger,
    ACTIVE_SNAPSHOT_FILE,
    THURSDAY_SNAPSHOT_FILE,
    ACTIVE_STRATEGIES_JSON,
)
from common.unified_strategy_store import (
    load_active_strategies,
    init_or_rotate_cycle,
    update_locked_prices,
    save_active_strategies,
    sync_active_cycles_to_mongodb,
    to_simple_action,
)


def bootstrap_from_snapshots(poll_now: bool = False, reset_mongo: bool = False) -> bool:
    """Read existing buy snapshots and bootstrap active_strategies.json."""
    logger.info("Starting mid-week bootstrap for active options strategies...")

    if reset_mongo:
        logger.info("Explicit MongoDB live logs purge requested.")
        from common.unified_strategy_store import reset_cycle_live_logs_in_mongodb
        reset_cycle_live_logs_in_mongodb("5day_strategy")
        reset_cycle_live_logs_in_mongodb("3day_strategy")

    active = load_active_strategies()

    # 1. Tuesday 5-Day Strategy
    if os.path.exists(ACTIVE_SNAPSHOT_FILE):
        try:
            with open(ACTIVE_SNAPSHOT_FILE, "r", encoding="utf-8") as fh:
                tue_snap = json.load(fh)
            c_id = tue_snap.get("cycle_id", "CYCLE-TUE-CURRENT")
            start_date = tue_snap.get("week_start_date") or tue_snap.get("start_date")
            expiry_date = tue_snap.get("expiry_date")
            c_strike = tue_snap.get("call_strike")
            p_strike = tue_snap.get("put_strike")
            c_buy = tue_snap.get("call_buy_price")
            p_buy = tue_snap.get("put_buy_price")
            spot = tue_snap.get("entry_spot")
            moneyness = tue_snap.get("moneyness", "ITM")

            logger.info("Found Tuesday snapshot: %s | CE %s (₹%s) | PE %s (₹%s)", c_id, c_strike, c_buy, p_strike, p_buy)

            init_or_rotate_cycle("5day_strategy", {
                "cycle_id": c_id,
                "start_date": start_date,
                "expiry_date": expiry_date,
                "call_strike": c_strike,
                "put_strike": p_strike,
                "moneyness": moneyness,
                "entry_spot": spot,
            })

            if c_buy is not None and p_buy is not None:
                update_locked_prices("5day_strategy", call_buy=c_buy, put_buy=p_buy, spot=spot)

            # Check if peak or fsm state was already active in snapshot
            if tue_snap.get("peak_profit", {}).get("alert_sent"):
                p_data = tue_snap["peak_profit"]
                from common.unified_strategy_store import record_exit_signal_and_sync_mongo
                record_exit_signal_and_sync_mongo(
                    "5day_strategy",
                    "peak_20pct",
                    {
                        "symbol": "20% PEAK PROFIT REACHED",
                        "peak_pnl_pct": p_data.get("peak_pnl_pct"),
                        "peak_pnl_pts": p_data.get("peak_pnl_pts"),
                        "timestamp": p_data.get("last_notified_ts"),
                        "details": f"Recovered from snapshot: Peak profit reached +{p_data.get('peak_pnl_pct', 0):.2f}%",
                    }
                )

        except Exception as e:
            logger.error("Failed to read Tuesday snapshot (%s): %s", ACTIVE_SNAPSHOT_FILE, e)
    else:
        logger.warning("No Tuesday snapshot found at %s. 5day_strategy remains pending.", ACTIVE_SNAPSHOT_FILE)

    # 2. Thursday 3-Day Strategy
    if os.path.exists(THURSDAY_SNAPSHOT_FILE):
        try:
            with open(THURSDAY_SNAPSHOT_FILE, "r", encoding="utf-8") as fh:
                thu_snap = json.load(fh)
            c_id = thu_snap.get("cycle_id", "CYCLE-THU-CURRENT")
            start_date = thu_snap.get("cycle_start_date") or thu_snap.get("start_date")
            expiry_date = thu_snap.get("expiry_date")
            c_strike = thu_snap.get("call_strike")
            p_strike = thu_snap.get("put_strike")
            c_buy = thu_snap.get("call_buy_price")
            p_buy = thu_snap.get("put_buy_price")
            spot = thu_snap.get("entry_spot")

            logger.info("Found Thursday snapshot: %s | CE %s (₹%s) | PE %s (₹%s)", c_id, c_strike, c_buy, p_strike, p_buy)

            init_or_rotate_cycle("3day_strategy", {
                "cycle_id": c_id,
                "start_date": start_date,
                "expiry_date": expiry_date,
                "call_strike": c_strike,
                "put_strike": p_strike,
                "moneyness": "ATM",
                "entry_spot": spot,
            })

            if c_buy is not None and p_buy is not None:
                update_locked_prices("3day_strategy", call_buy=c_buy, put_buy=p_buy, spot=spot)

        except Exception as e:
            logger.error("Failed to read Thursday snapshot (%s): %s", THURSDAY_SNAPSHOT_FILE, e)
    else:
        logger.info("No Thursday snapshot found. 3day_strategy correctly initialized as 'pending_entry'.")

    # Sync to MongoDB Atlas
    final_active = load_active_strategies()
    sync_active_cycles_to_mongodb(final_active)
    logger.info("Bootstrap completed successfully. File saved to: %s", ACTIVE_STRATEGIES_JSON)

    if poll_now:
        logger.info("Executing immediate 5-min poller check...")
        from scripts.monitor_live_options_5min import main as poller_main
        sys.argv = ["monitor_live_options_5min.py", "--force", "--verbose"]
        poller_main()

    return True


def main():
    parser = argparse.ArgumentParser(description="Mid-Week Bootstrap for Active Options Strategies")
    parser.add_argument("--poll", action="store_true", help="Run immediate 5-minute telemetry poll after bootstrap")
    parser.add_argument("--reset-mongo", action="store_true", help="Purge prior cycle live telemetry logs in MongoDB")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [bootstrap] %(message)s")
    bootstrap_from_snapshots(poll_now=args.poll, reset_mongo=args.reset_mongo)


if __name__ == "__main__":
    main()
