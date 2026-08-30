"""
compare_ai_vs_static_benchmark.py - Side-by-Side Performance Comparison (AI Strikes vs Static Benchmark)

Reconstructs the performance of the classic static anchor +/- 100 strike strategy
using the full option chain surface captured every 5 minutes by market_signal_agent (signals_data_*.db).
Compares it against the active AI-selected weekly strategy in real time.

Usage:
    python scripts/compare_ai_vs_static_benchmark.py
    python scripts/compare_ai_vs_static_benchmark.py --strategy-db /path/to/nifty50_weekly_data_YYYYMMDD_YYYYMMDD.db
"""

import argparse
import datetime
import glob
import json
import logging
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import pytz
    IST_TZ = pytz.timezone("Asia/Kolkata")
except ImportError:
    from zoneinfo import ZoneInfo
    IST_TZ = ZoneInfo("Asia/Kolkata")

# Ensure project root in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import logger, SQLITE_DIR, SNAPSHOT_DIR, ACTIVE_SNAPSHOT_FILE
from common.storage import load_active_snapshot
from common.ai_strike_selector import compute_static_strikes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def load_strategy_records(db_path: str) -> List[Dict[str, Any]]:
    """Load hourly records for the active AI strategy."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM strategy_hourly_data ORDER BY collection_timestamp ASC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_latest_file(pattern: str) -> Optional[str]:
    """Find the most recently modified file matching a glob pattern."""
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def reconstruct_static_benchmark_pnl(
    signals_db_path: str,
    static_call_strike: int,
    static_put_strike: int,
    start_ts: int,
    end_ts: int
) -> List[Dict[str, Any]]:
    """
    Reconstruct static +/- 100 strike option prices from signals_data_YYYY_MM.db option_chain_surface.
    """
    if not os.path.exists(signals_db_path):
        logger.warning(f"Signals DB not found: {signals_db_path}")
        return []

    conn = sqlite3.connect(signals_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT ts, price, data FROM market_data WHERE pk = 'INDEX#NIFTY50' AND ts >= ? AND ts <= ? ORDER BY ts ASC",
        (start_ts - 300, end_ts + 300)
    )
    rows = cursor.fetchall()
    conn.close()

    static_records = []
    c_strike_str = str(int(static_call_strike))
    p_strike_str = str(int(static_put_strike))

    for row in rows:
        ts = row["ts"]
        nifty_price = row["price"]
        try:
            sig_data = json.loads(row["data"]) if row["data"] else {}
        except Exception:
            sig_data = {}

        surface = sig_data.get("option_chain_surface", {})
        if isinstance(surface, str):
            try:
                surface = json.loads(surface)
            except Exception:
                surface = {}

        c_leg = surface.get(c_strike_str, {}).get("CALL", {})
        p_leg = surface.get(p_strike_str, {}).get("PUT", {})

        c_ltp = c_leg.get("ltp") or c_leg.get("close")
        p_ltp = p_leg.get("ltp") or p_leg.get("close")

        if c_ltp is not None and p_ltp is not None:
            static_records.append({
                "ts": ts,
                "nifty_ltp": nifty_price,
                "call_ltp": float(c_ltp),
                "put_ltp": float(p_ltp)
            })

    return static_records


def compare_strategies(
    snapshot: Dict[str, Any],
    strategy_rows: List[Dict[str, Any]],
    signals_db_dir: str = "/home/ubuntu/sqlite"
):
    """Generate and print side-by-side performance comparison between AI Strategy and Static Benchmark."""
    if not snapshot or not strategy_rows:
        logger.error("Insufficient strategy data for comparison.")
        return

    ai_call_strike = int(snapshot.get("call_strike", 0))
    ai_put_strike = int(snapshot.get("put_strike", 0))
    ai_call_buy = float(snapshot.get("call_buy_price") or strategy_rows[0].get("call_buy_price", 0.0))
    ai_put_buy = float(snapshot.get("put_buy_price") or strategy_rows[0].get("put_buy_price", 0.0))
    ai_total_buy = ai_call_buy + ai_put_buy

    # Static strikes
    static_call_strike = int(snapshot.get("static_call_strike") or 0)
    static_put_strike = int(snapshot.get("static_put_strike") or 0)

    if not static_call_strike or not static_put_strike:
        # Compute fallback from entry spot
        nifty_entry = strategy_rows[0].get("nifty_open") or strategy_rows[0].get("nifty_ltp")
        static_put_strike, static_call_strike = compute_static_strikes(nifty_entry)

    # Time range
    start_ts = int(strategy_rows[0].get("collection_timestamp", 0))
    end_ts = int(strategy_rows[-1].get("collection_timestamp", 0))

    # Look up monthly signals db
    dt_start = datetime.datetime.fromtimestamp(start_ts, tz=datetime.timezone.utc)
    yyyy_mm = dt_start.strftime("%Y_%m")
    signals_db = os.path.join(signals_db_dir, f"signals_data_{yyyy_mm}.db")

    static_history = reconstruct_static_benchmark_pnl(
        signals_db, static_call_strike, static_put_strike, start_ts, end_ts
    )

    # Get static entry buy prices (from first matching timestamp)
    static_call_buy = None
    static_put_buy = None
    if static_history:
        static_call_buy = static_history[0]["call_ltp"]
        static_put_buy = static_history[0]["put_ltp"]

    # Latest AI values
    latest_ai = strategy_rows[-1]
    latest_ai_call_ltp = float(latest_ai.get("call_ltp", 0.0))
    latest_ai_put_ltp = float(latest_ai.get("put_ltp", 0.0))
    ai_call_pnl = ((latest_ai_call_ltp - ai_call_buy) / ai_call_buy * 100) if ai_call_buy > 0 else 0.0
    ai_put_pnl = ((latest_ai_put_ltp - ai_put_buy) / ai_put_buy * 100) if ai_put_buy > 0 else 0.0
    ai_total_pnl = ((latest_ai_call_ltp + latest_ai_put_ltp - ai_total_buy) / ai_total_buy * 100) if ai_total_buy > 0 else 0.0

    # Latest Static values
    latest_static_call_ltp = static_history[-1]["call_ltp"] if static_history else None
    latest_static_put_ltp = static_history[-1]["put_ltp"] if static_history else None
    static_total_buy = (static_call_buy + static_put_buy) if (static_call_buy and static_put_buy) else None

    static_call_pnl = ((latest_static_call_ltp - static_call_buy) / static_call_buy * 100) if (static_call_buy and latest_static_call_ltp) else None
    static_put_pnl = ((latest_static_put_ltp - static_put_buy) / static_put_buy * 100) if (static_put_buy and latest_static_put_ltp) else None
    static_total_pnl = ((latest_static_call_ltp + latest_static_put_ltp - static_total_buy) / static_total_buy * 100) if (static_total_buy and latest_static_call_ltp and latest_static_put_ltp) else None

    # Print Table
    cycle_id = snapshot.get("cycle_id", "ACTIVE")
    selection_mode = snapshot.get("selection_mode", "AI")
    rationale = snapshot.get("selection_rationale", "N/A")

    print("\n" + "=" * 78)
    print(f"  WEEKLY OPTION STRATEGY BENCHMARK COMPARISON: {cycle_id}")
    print("=" * 78)
    print(f"Selection Mode:      {selection_mode}")
    print(f"AI Selection Reason: {rationale}")
    print("-" * 78)
    print(f"{'Metric':<25} | {'AI Strategy (Active)':<22} | {'Static Benchmark (±100)':<22}")
    print("-" * 78)
    print(f"{'CALL Strike':<25} | {ai_call_strike:<22} | {static_call_strike:<22}")
    print(f"{'PUT Strike':<25} | {ai_put_strike:<22} | {static_put_strike:<22}")
    print(f"{'Entry CALL Buy Price':<25} | ₹{ai_call_buy:<21.2f} | {'₹' + f'{static_call_buy:.2f}' if static_call_buy else 'N/A':<22}")
    print(f"{'Entry PUT Buy Price':<25} | ₹{ai_put_buy:<21.2f} | {'₹' + f'{static_put_buy:.2f}' if static_put_buy else 'N/A':<22}")
    print(f"{'Current CALL LTP':<25} | ₹{latest_ai_call_ltp:<21.2f} | {'₹' + f'{latest_static_call_ltp:.2f}' if latest_static_call_ltp else 'N/A':<22}")
    print(f"{'Current PUT LTP':<25} | ₹{latest_ai_put_ltp:<21.2f} | {'₹' + f'{latest_static_put_ltp:.2f}' if latest_static_put_ltp else 'N/A':<22}")
    print("-" * 78)
    print(f"{'CALL Leg P&L %':<25} | {ai_call_pnl:+21.2f}% | {f'{static_call_pnl:+.2f}%' if static_call_pnl is not None else 'N/A':<22}")
    print(f"{'PUT Leg P&L %':<25} | {ai_put_pnl:+21.2f}% | {f'{static_put_pnl:+.2f}%' if static_put_pnl is not None else 'N/A':<22}")
    print(f"{'TOTAL STRANGLE P&L %':<25} | {ai_total_pnl:+21.2f}% | {f'{static_total_pnl:+.2f}%' if static_total_pnl is not None else 'N/A':<22}")
    print("-" * 78)

    if static_total_pnl is not None:
        delta_pnl = ai_total_pnl - static_total_pnl
        badge = "🟢 AI OUTPERFORMING" if delta_pnl > 0 else "🔴 STATIC OUTPERFORMING" if delta_pnl < 0 else "⚪ EQUAL"
        print(f"OUTPERFORMANCE DELTA: {delta_pnl:+.2f}% ({badge})")
    print("=" * 78 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Side-by-side performance comparison: AI strikes vs static benchmark")
    parser.add_argument("--strategy-db", default=None, help="Path to weekly strategy DB")
    parser.add_argument("--signals-dir", default="/home/ubuntu/sqlite", help="Directory containing signals_data_*.db")
    args = parser.parse_args()

    snapshot = load_active_snapshot()
    strategy_db = args.strategy_db or get_latest_file(os.path.join(SQLITE_DIR, "nifty50_weekly_data_*.db"))

    if not strategy_db:
        logger.error(f"No strategy DB found in {SQLITE_DIR}")
        sys.exit(1)

    strategy_rows = load_strategy_records(strategy_db)
    compare_strategies(snapshot, strategy_rows, signals_db_dir=args.signals_dir)


if __name__ == "__main__":
    main()
