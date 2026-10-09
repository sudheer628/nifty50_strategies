"""
compare_entry_timing_counterfactuals.py - Delayed & Staggered Entry Timing Counterfactual Analysis

Evaluates whether delaying order execution from 09:31 AM to 10:01 AM IST or staggering
legs (Call @ 09:31 + Put @ 10:01 vs Put @ 09:31 + Call @ 10:01) generates a systematic edge
over the baseline 09:31 entry across historical and live weekly cycles.

Variants Evaluated:
  - V0: Baseline (Call @ 09:31, Put @ 09:31)
  - V1: Delayed (Call @ 10:01, Put @ 10:01)
  - V2: Staggered (Call @ 09:31, Put @ 10:01)
  - V3: Staggered (Put @ 09:31, Call @ 10:01)

Exit Models Supported:
  - hold_to_expiry (Primary): Evaluates terminal expiration payoff vs entry cost.
  - fsm_sim (Secondary): Simulates step-by-step FSM stop loss and trailing ladders.

Usage:
    python scripts/compare_entry_timing_counterfactuals.py
    python scripts/compare_entry_timing_counterfactuals.py --exit-model hold_to_expiry
    python scripts/compare_entry_timing_counterfactuals.py --exit-model fsm_sim
    python scripts/compare_entry_timing_counterfactuals.py --db /path/to/nifty50_weekly_data_YYYYMMDD_YYYYMMDD.db
"""

import argparse
import datetime
import glob
import json
import logging
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import pytz
    IST_TZ = pytz.timezone("Asia/Kolkata")
    UTC_TZ = pytz.UTC
except ImportError:
    from zoneinfo import ZoneInfo
    IST_TZ = ZoneInfo("Asia/Kolkata")
    UTC_TZ = ZoneInfo("UTC")

# Ensure project root in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import SQLITE_DIR
from common.storage import get_timing_shadow_entries
from common.fsm_strategy import init_fsm_state, evaluate_fsm_tick, estimate_option_delta

logger = logging.getLogger("timing_counterfactuals")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


WEEKLY_DB_RE = re.compile(r"nifty50_weekly_data_(\d{8})_(\d{8})\.db$")


def get_surface_quote_for_strike(surface: dict, strike: int, leg: str) -> Optional[float]:
    """Extract LTP or close from an option chain surface JSON dictionary for a strike and leg."""
    if not surface or not isinstance(surface, dict):
        return None
    strike_data = surface.get(str(strike)) or surface.get(int(strike))
    if not strike_data or not isinstance(strike_data, dict):
        return None
    leg_data = strike_data.get(leg.upper()) or {}
    ltp = leg_data.get("ltp") or leg_data.get("close")
    return float(ltp) if ltp is not None else None


def extract_prices_from_signals_db(
    signals_db_path: str,
    target_date_str: str,
    call_strike: int,
    put_strike: int
) -> Tuple[Optional[Dict[str, float]], Optional[Dict[str, float]]]:
    """
    Extract 09:30/09:31 and 10:00/10:01 option chain prices from signals_data_YYYY_MM.db for a given date.
    Returns: (prices_0931, prices_1001) where each is {"call_ltp": float, "put_ltp": float, "spot": float, "ts": int}
    """
    if not os.path.exists(signals_db_path):
        return None, None

    # Parse target date to epoch bounds
    try:
        dt_target = datetime.datetime.strptime(target_date_str, "%Y-%m-%d")
    except ValueError:
        try:
            dt_target = datetime.datetime.strptime(target_date_str, "%Y%m%d")
        except ValueError:
            return None, None

    dt_0930_ist = IST_TZ.localize(datetime.datetime.combine(dt_target.date(), datetime.time(9, 30)))
    dt_1000_ist = IST_TZ.localize(datetime.datetime.combine(dt_target.date(), datetime.time(10, 0)))

    ts_0930 = int(dt_0930_ist.astimezone(UTC_TZ).timestamp())
    ts_1000 = int(dt_1000_ist.astimezone(UTC_TZ).timestamp())

    conn = sqlite3.connect(signals_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Search window: 09:28 to 10:05 IST
    cursor.execute(
        "SELECT ts, price, data FROM market_data WHERE pk = 'INDEX#NIFTY50' AND ts >= ? AND ts <= ? ORDER BY ts ASC",
        (ts_0930 - 180, ts_1000 + 360)
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return None, None

    best_0931 = None
    best_1001 = None
    min_diff_0931 = 999999
    min_diff_1001 = 999999

    for r in rows:
        ts = r["ts"]
        try:
            sig_data = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            sig_data = {}

        surface = sig_data.get("option_chain_surface", {})
        if isinstance(surface, str):
            try:
                surface = json.loads(surface)
            except Exception:
                surface = {}

        c_ltp = get_surface_quote_for_strike(surface, call_strike, "CALL")
        p_ltp = get_surface_quote_for_strike(surface, put_strike, "PUT")

        if c_ltp is not None and p_ltp is not None:
            # Check proximity to 09:30/09:31
            diff_09 = abs(ts - ts_0930)
            if diff_09 < min_diff_0931 and diff_09 <= 600:
                min_diff_0931 = diff_09
                best_0931 = {
                    "call_ltp": c_ltp,
                    "put_ltp": p_ltp,
                    "spot": float(r["price"] or 0.0),
                    "ts": ts,
                }

            # Check proximity to 10:00/10:01
            diff_10 = abs(ts - ts_1000)
            if diff_10 < min_diff_1001 and diff_10 <= 600:
                min_diff_1001 = diff_10
                best_1001 = {
                    "call_ltp": c_ltp,
                    "put_ltp": p_ltp,
                    "spot": float(r["price"] or 0.0),
                    "ts": ts,
                }

    return best_0931, best_1001


def simulate_fsm_run(
    strategy_rows: List[dict],
    call_strike: int,
    put_strike: int,
    call_buy: float,
    put_buy: float,
    expiry_dt: datetime.date,
    sqlite_dir: Optional[str] = None,
    entry_ts: Optional[int] = None
) -> Dict[str, Any]:
    """Simulate FSM execution for an entry variant across hourly ticks starting at or after entry_ts."""
    if entry_ts is None:
        entry_ts = int(strategy_rows[0].get("collection_timestamp") or strategy_rows[0].get("ts") or 0)

    # Filter ticks so delayed variants (e.g. entered at 10:01) do not evaluate pre-entry ticks
    eval_rows = [r for r in strategy_rows if int(r.get("collection_timestamp") or r.get("ts") or 0) >= entry_ts]
    if not eval_rows:
        eval_rows = strategy_rows[-1:]

    fsm = init_fsm_state(
        call_strike=call_strike,
        put_strike=put_strike,
        call_buy_price=call_buy,
        put_buy_price=put_buy,
        entry_ts=entry_ts,
    )

    for row in eval_rows:
        ts = int(row.get("collection_timestamp") or row.get("ts") or 0)
        c_ltp = float(row.get("call_ltp") or 0.0)
        p_ltp = float(row.get("put_ltp") or 0.0)
        spot = float(row.get("nifty_ltp") or 0.0)

        dt_row = datetime.datetime.fromtimestamp(ts, tz=UTC_TZ).astimezone(IST_TZ)
        dt_exp = datetime.datetime.combine(expiry_dt, datetime.time(15, 30))
        days_to_exp = max(0.05, (dt_exp - dt_row.replace(tzinfo=None)).total_seconds() / 86400.0)

        c_delta = estimate_option_delta(spot, call_strike, days_to_exp, is_call=True)
        p_delta = estimate_option_delta(spot, put_strike, days_to_exp, is_call=False)

        fsm, _ = evaluate_fsm_tick(
            fsm_state=fsm,
            current_call_ltp=c_ltp,
            current_put_ltp=p_ltp,
            current_ts=ts,
            days_to_expiry=days_to_exp,
            call_delta=c_delta,
            put_delta=p_delta,
        )

    cost = call_buy + put_buy
    gainloss = fsm.get("total_gainloss", 0.0)
    roi_pct = round((gainloss / cost * 100.0), 2) if cost > 0 else 0.0

    return {
        "final_state": fsm.get("state"),
        "total_gainloss": round(gainloss, 2),
        "roi_pct": roi_pct,
        "call_status": fsm.get("call_leg", {}).get("status"),
        "put_status": fsm.get("put_leg", {}).get("status"),
    }


def analyze_weekly_cycle(
    db_path: str,
    signals_dir: str,
    exit_model: str = "hold_to_expiry"
) -> Optional[Dict[str, Any]]:
    """Analyze all 4 timing variants for a specific weekly strategy database."""
    if not os.path.exists(db_path):
        return None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # Load hourly strategy rows
        cursor.execute("SELECT * FROM strategy_hourly_data ORDER BY collection_timestamp ASC")
        strat_rows = [dict(r) for r in cursor.fetchall()]

        # Load buy snapshot if present
        cursor.execute("SELECT * FROM strategy_buy_snapshots ORDER BY rowid DESC LIMIT 1")
        snap_row = cursor.fetchone()
        snapshot = dict(snap_row) if snap_row else {}

        # Check timing_shadow_entries table
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='timing_shadow_entries'")
        has_timing_table = bool(cursor.fetchone())

        shadow_entries = []
        if has_timing_table:
            cursor.execute("SELECT * FROM timing_shadow_entries ORDER BY id ASC")
            shadow_entries = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

    if not strat_rows and not snapshot:
        return None

    # Derive metadata
    first_row = strat_rows[0] if strat_rows else {}
    last_row = strat_rows[-1] if strat_rows else {}

    cycle_id = snapshot.get("cycle_id") or first_row.get("cycle_id") or os.path.basename(db_path)
    call_strike = int(snapshot.get("call_strike") or first_row.get("call_strike") or 0)
    put_strike = int(snapshot.get("put_strike") or first_row.get("put_strike") or 0)
    expiry_str = str(snapshot.get("expiry_date") or first_row.get("expiry_date") or "")

    if not call_strike or not put_strike:
        return None

    # Date resolution
    start_ts = int(snapshot.get("captured_at") or first_row.get("collection_timestamp") or 0)
    dt_start = datetime.datetime.fromtimestamp(start_ts, tz=UTC_TZ).astimezone(IST_TZ)
    trade_date_str = dt_start.strftime("%Y-%m-%d")

    try:
        expiry_dt = datetime.datetime.strptime(expiry_str, "%Y%m%d").date()
    except Exception:
        expiry_dt = (dt_start + datetime.timedelta(days=7)).date()

    # Determine variant entry prices
    variants: Dict[str, Dict[str, float]] = {}

    # 1. First check if recorded in timing_shadow_entries
    if shadow_entries:
        for s in shadow_entries:
            v_name = s.get("variant")
            if v_name:
                call_ts = s.get("call_entry_ts")
                put_ts = s.get("put_entry_ts")
                valid_ts = [int(t) for t in (call_ts, put_ts) if t is not None]
                variants[v_name] = {
                    "call_buy": float(s.get("call_entry_price") or 0.0),
                    "put_buy": float(s.get("put_entry_price") or 0.0),
                    "combined": float(s.get("combined_entry_price") or 0.0),
                    "entry_ts": min(valid_ts) if valid_ts else None,
                }

    # 2. If missing variants, reconstruct from signals_data_YYYY_MM.db
    if len(variants) < 4:
        yyyy_mm = dt_start.strftime("%Y_%m")
        sig_db = os.path.join(signals_dir, f"signals_data_{yyyy_mm}.db")
        p_0931, p_1001 = extract_prices_from_signals_db(sig_db, trade_date_str, call_strike, put_strike)

        # Baseline V0 fallback to snapshot / first_row if surface query missing 09:31
        v0_call = (p_0931["call_ltp"] if p_0931 else None) or float(snapshot.get("call_buy_price") or first_row.get("call_buy_price") or first_row.get("call_ltp") or 0.0)
        v0_put = (p_0931["put_ltp"] if p_0931 else None) or float(snapshot.get("put_buy_price") or first_row.get("put_buy_price") or first_row.get("put_ltp") or 0.0)

        if "V0_baseline_0931" not in variants and v0_call and v0_put:
            variants["V0_baseline_0931"] = {
                "call_buy": v0_call,
                "put_buy": v0_put,
                "combined": round(v0_call + v0_put, 2),
                "entry_ts": p_0931["ts"] if p_0931 else start_ts,
            }

        if p_1001:
            v1_call, v1_put = p_1001["call_ltp"], p_1001["put_ltp"]
            if "V1_delayed_1000" not in variants:
                variants["V1_delayed_1000"] = {
                    "call_buy": v1_call,
                    "put_buy": v1_put,
                    "combined": round(v1_call + v1_put, 2),
                    "entry_ts": p_1001["ts"],
                }
            if "V2_staggered_c0931_p1000" not in variants and v0_call:
                variants["V2_staggered_c0931_p1000"] = {
                    "call_buy": v0_call,
                    "put_buy": v1_put,
                    "combined": round(v0_call + v1_put, 2),
                    "entry_ts": p_0931["ts"] if p_0931 else start_ts,
                }
            if "V3_staggered_p0931_c1000" not in variants and v0_put:
                variants["V3_staggered_p0931_c1000"] = {
                    "call_buy": v1_call,
                    "put_buy": v0_put,
                    "combined": round(v1_call + v0_put, 2),
                    "entry_ts": p_0931["ts"] if p_0931 else start_ts,
                }

    # 3. Derive V4 (Trend-guided stagger) if not recorded but V2 & V3 exist
    if "V4_trend_stagger" not in variants and "V2_staggered_c0931_p1000" in variants and "V3_staggered_p0931_c1000" in variants:
        spot_0931 = float(snapshot.get("entry_spot") or first_row.get("nifty_open") or first_row.get("nifty_ltp") or 0.0)
        prev_close = float(first_row.get("nifty_prev_close") or spot_0931)
        is_bullish = spot_0931 >= prev_close
        chosen_v = variants["V2_staggered_c0931_p1000"] if is_bullish else variants["V3_staggered_p0931_c1000"]
        variants["V4_trend_stagger"] = dict(chosen_v)

    if "V0_baseline_0931" not in variants or "V1_delayed_1000" not in variants:
        return None

    # Evaluate each variant with exit model
    terminal_call = float(last_row.get("call_ltp") or 0.0)
    terminal_put = float(last_row.get("put_ltp") or 0.0)
    terminal_payoff = terminal_call + terminal_put

    results: Dict[str, Any] = {
        "cycle_id": cycle_id,
        "trade_date": trade_date_str,
        "expiry_date": expiry_str,
        "call_strike": call_strike,
        "put_strike": put_strike,
        "exit_model": exit_model,
        "variants": {},
    }

    sqlite_dir = os.path.dirname(db_path)

    for v_key, v_info in variants.items():
        c_buy = v_info["call_buy"]
        p_buy = v_info["put_buy"]
        cost = v_info["combined"]
        entry_ts = v_info.get("entry_ts")

        if exit_model == "fsm_sim" and len(strat_rows) > 1:
            fsm_res = simulate_fsm_run(
                strat_rows, call_strike, put_strike, c_buy, p_buy, expiry_dt, sqlite_dir, entry_ts=entry_ts
            )
            pnl_pts = fsm_res["total_gainloss"]
            roi_pct = fsm_res["roi_pct"]
            status = fsm_res["final_state"]
        else:
            pnl_pts = round(terminal_payoff - cost, 2)
            roi_pct = round((pnl_pts / cost * 100.0), 2) if cost > 0 else 0.0
            status = "EXPIRED"

        results["variants"][v_key] = {
            "call_buy": c_buy,
            "put_buy": p_buy,
            "entry_cost": cost,
            "pnl_pts": pnl_pts,
            "roi_pct": roi_pct,
            "is_win": pnl_pts > 0,
            "status": status,
        }

    # Relative edge of V1 (delayed) vs V0 (baseline)
    v0_cost = results["variants"]["V0_baseline_0931"]["entry_cost"]
    v1_cost = results["variants"]["V1_delayed_1000"]["entry_cost"]
    cost_diff_pts = round(v0_cost - v1_cost, 2)  # positive = 10:00 was cheaper
    cost_diff_pct = round((cost_diff_pts / v0_cost * 100.0), 2) if v0_cost > 0 else 0.0

    pnl_diff_pts = round(
        results["variants"]["V1_delayed_1000"]["pnl_pts"] - results["variants"]["V0_baseline_0931"]["pnl_pts"], 2
    )

    best_variant = max(results["variants"].items(), key=lambda kv: kv[1]["pnl_pts"])[0]

    results["edge"] = {
        "v1_cheaper_by_pts": cost_diff_pts,
        "v1_discount_pct": cost_diff_pct,
        "v1_pnl_advantage_pts": pnl_diff_pts,
        "best_variant": best_variant,
    }

    return results


def run_timing_comparison(
    strategies_dir: str,
    signals_dir: str,
    target_db: Optional[str] = None,
    exit_model: str = "hold_to_expiry",
    output_format: str = "table"
) -> List[Dict[str, Any]]:
    """Scan strategy DBs and evaluate entry timing counterfactual performance."""
    if target_db:
        db_files = [target_db]
    else:
        db_files = sorted(glob.glob(os.path.join(strategies_dir, "nifty50_weekly_data_*_*.db")))

    if not db_files:
        logger.warning(f"No strategy DBs found in {strategies_dir}")
        return []

    cycle_results = []
    for db_path in db_files:
        res = analyze_weekly_cycle(db_path, signals_dir, exit_model=exit_model)
        if res:
            cycle_results.append(res)

    if not cycle_results:
        logger.warning("No eligible cycles with sufficient option chain surface data found.")
        return []

    if output_format == "json":
        print(json.dumps(cycle_results, indent=2))
        return cycle_results

    # Print Table
    print("\n" + "=" * 140)
    print(f"📊 NIFTY50 ENTRY TIMING COUNTERFACTUAL BACKTEST (Exit Model: {exit_model.upper()})")
    print("=" * 140)
    header = (
        f"{'Cycle Date':<12} | {'Strikes (C/P)':<15} | "
        f"{'V0 Baseline':<12} | {'V1 10:00 Del':<12} | {'V2 C9:30/P10':<12} | {'V3 P9:30/C10':<12} | {'V4 TrendStag':<12} | "
        f"{'Entry Edge':<14} | {'Best Variant':<18}"
    )
    print(header)
    print("-" * 140)

    v0_wins, v1_wins = 0, 0
    total_cost_saved_pts = 0.0
    v1_cheaper_weeks = 0
    total_weeks = len(cycle_results)

    for c in cycle_results:
        t_date = c["trade_date"]
        strikes = f"{c['call_strike']}/{c['put_strike']}"
        v = c["variants"]

        v0_str = f"₹{v['V0_baseline_0931']['entry_cost']:.1f} ({v['V0_baseline_0931']['pnl_pts']:+.1f})"
        v1_str = f"₹{v['V1_delayed_1000']['entry_cost']:.1f} ({v['V1_delayed_1000']['pnl_pts']:+.1f})"
        v2_str = f"₹{v['V2_staggered_c0931_p1000']['entry_cost']:.1f} ({v['V2_staggered_c0931_p1000']['pnl_pts']:+.1f})" if 'V2_staggered_c0931_p1000' in v else "N/A"
        v3_str = f"₹{v['V3_staggered_p0931_c1000']['entry_cost']:.1f} ({v['V3_staggered_p0931_c1000']['pnl_pts']:+.1f})" if 'V3_staggered_p0931_c1000' in v else "N/A"
        v4_str = f"₹{v['V4_trend_stagger']['entry_cost']:.1f} ({v['V4_trend_stagger']['pnl_pts']:+.1f})" if 'V4_trend_stagger' in v else "N/A"

        edge = c["edge"]
        edge_str = f"{edge['v1_discount_pct']:+.1f}% ({edge['v1_cheaper_by_pts']:+.1f}p)"
        best = edge["best_variant"].replace("V0_baseline_0931", "V0 (09:31)").replace("V1_delayed_1000", "V1 (10:00)").replace("V2_staggered_c0931_p1000", "V2 (C9:30/P10)").replace("V3_staggered_p0931_c1000", "V3 (P9:30/C10)").replace("V4_trend_stagger", "V4 (TrendStag)")

        print(f"{t_date:<12} | {strikes:<15} | {v0_str:<12} | {v1_str:<12} | {v2_str:<12} | {v3_str:<12} | {v4_str:<12} | {edge_str:<14} | {best:<18}")

        if edge["v1_cheaper_by_pts"] > 0:
            v1_cheaper_weeks += 1
        total_cost_saved_pts += edge["v1_cheaper_by_pts"]

        if v["V0_baseline_0931"]["is_win"]:
            v0_wins += 1
        if v["V1_delayed_1000"]["is_win"]:
            v1_wins += 1

    print("-" * 125)
    cheaper_pct = (v1_cheaper_weeks / total_weeks * 100.0) if total_weeks > 0 else 0.0
    avg_saved_pts = (total_cost_saved_pts / total_weeks) if total_weeks > 0 else 0.0

    print(f"\n📈 SUMMARY SYNTHESIS ({total_weeks} Trading Weeks Evaluated):")
    print(f"  • 10:00 AM Entry Cheaper Than 09:30 AM: {v1_cheaper_weeks}/{total_weeks} weeks ({cheaper_pct:.1f}%)")
    print(f"  • Average Entry Cost Reduction (V1 vs V0): {avg_saved_pts:+.2f} premium points per cycle")
    print(f"  • Win Rate (V0 Baseline 09:31): {v0_wins}/{total_weeks} ({(v0_wins/total_weeks*100.0 if total_weeks else 0):.1f}%)")
    print(f"  • Win Rate (V1 Delayed 10:00):  {v1_wins}/{total_weeks} ({(v1_wins/total_weeks*100.0 if total_weeks else 0):.1f}%)")
    print("=" * 125 + "\n")

    return cycle_results


def main():
    parser = argparse.ArgumentParser(description="Counterfactual Entry Timing Analysis")
    parser.add_argument("--strategies-dir", default=os.getenv("SQLITE_DIR", "/home/ubuntu/sqlite/strategies"),
                        help="Path to strategy DBs directory")
    parser.add_argument("--signals-dir", default=os.getenv("SIGNALS_DIR", "/home/ubuntu/sqlite"),
                        help="Path to monthly signals DBs directory")
    parser.add_argument("--db", default=None, help="Explicit strategy DB path")
    parser.add_argument("--exit-model", choices=["hold_to_expiry", "fsm_sim"], default="hold_to_expiry",
                        help="Exit model evaluation logic")
    parser.add_argument("--format", choices=["table", "json"], default="table",
                        help="Output format")
    args = parser.parse_args()

    run_timing_comparison(
        strategies_dir=args.strategies_dir,
        signals_dir=args.signals_dir,
        target_db=args.db,
        exit_model=args.exit_model,
        output_format=args.format
    )


if __name__ == "__main__":
    main()
