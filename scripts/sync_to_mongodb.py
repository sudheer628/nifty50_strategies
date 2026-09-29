#!/usr/bin/env python3
"""
sync_to_mongodb.py - Push NIFTY50 Weekly Option Strategy Performance to MongoDB Atlas.

Bridge utility allowing direct persistence from the nifty50_strategies repo:
- Reads the weekly strategy SQLite DB (nifty50_weekly_data_YYYYMMDD_YYYYMMDD.db)
- Extracts entry prices, exit prices, hourly progression, and P&L points/INR
- Pushes to MongoDB Atlas collections: `derivative_strategies` and updates `derivative_master`

Usage:
    python scripts/sync_to_mongodb.py
    python scripts/sync_to_mongodb.py --db /path/to/nifty50_weekly_data_YYYYMMDD_YYYYMMDD.db
    python scripts/sync_to_mongodb.py --sample
"""

import os
import sys
import argparse
import logging
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Also add sibling sentinel-hermes to sys.path if available
SENTINEL_DIR = PROJECT_ROOT.parent / "sentinel-hermes"
if SENTINEL_DIR.exists() and str(SENTINEL_DIR) not in sys.path:
    sys.path.insert(0, str(SENTINEL_DIR))

from config import (
    logger, SQLITE_DIR, SNAPSHOT_DIR, ACTIVE_SNAPSHOT_FILE,
    MONGODB_URI, MONGODB_DATABASE, NIFTY_LOT_SIZE
)

def main():
    parser = argparse.ArgumentParser(description="Sync NIFTY50 weekly option strategy to MongoDB Atlas")
    parser.add_argument("--db", help="Path to strategy database (defaults to latest)")
    parser.add_argument("--track", default=None, choices=["weekly", "thursday"], help="Strategy track (weekly or thursday)")
    parser.add_argument("--status", action="store_true", help="Check MongoDB Atlas connection and collection counts")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    try:
        from mongo_derivative_store import MongoDerivativeStore, push_weekly_derivatives_to_mongodb
    except ImportError:
        logger.error("Could not import mongo_derivative_store. Ensure sentinel-hermes is installed or in path.")
        sys.exit(1)

    store = MongoDerivativeStore(lot_size=NIFTY_LOT_SIZE)
    if not store.is_configured:
        logger.error("MONGODB_URI is not configured in .env. Skipping.")
        sys.exit(1)

    if args.status:
        client = store._get_client()
        if not client:
            logger.error("Failed to connect to MongoDB Atlas.")
            sys.exit(1)
        db = store._db
        logger.info(f"Connected to MongoDB Atlas: database={store.db_name}")
        for col_name in (store.strategies_col_name, store.master_col_name):
            cnt = db[col_name].count_documents({})
            logger.info(f"  • Collection '{col_name}': {cnt} documents")
        sys.exit(0)

    # Push from strategy DB
    target_db = args.db
    track = args.track
    if not target_db:
        # Find latest weekly DB
        import glob
        pattern = "nifty50_thursday_data_*.db" if track == "thursday" else "nifty50_weekly_data_*.db"
        db_files = glob.glob(os.path.join(SQLITE_DIR, pattern))
        if not db_files:
            logger.error(f"No strategy databases found in {SQLITE_DIR} with pattern {pattern}")
            sys.exit(1)
        target_db = max(db_files, key=os.path.getmtime)

    if not track:
        track = "thursday" if "thursday" in os.path.basename(target_db).lower() else "weekly"

    logger.info(f"Syncing strategy DB: {target_db} (track={track})")
    success = push_weekly_derivatives_to_mongodb(merged_db_path=target_db, lot_size=NIFTY_LOT_SIZE, track=track)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
