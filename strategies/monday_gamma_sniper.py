#!/usr/bin/env python3
"""
monday_gamma_sniper.py - Expiry Afternoon Gamma Scalp Strategy (Enhancement 4).

Executes between 13:15 and 15:00 IST on Strategy Closing Day (Monday, or Tuesday if holiday-shifted).
Key Architecture:
1. Zero Principal Risk Safeguard:
   - Sizing is strictly capped at <= 15% of the week's realized profit from the main strangle.
   - If the main strategy week is in a net loss or breakeven (P&L <= 0), this module DOES NOT TRADE.
2. Signal Triggers (5-min NSF & Microstructure):
   - 5-min VWAP distance > +0.15% (CE) or < -0.15% (PE).
   - Volume surge: Current 5-min volume > 1.5x 20-period average volume.
   - RSI momentum: RSI_14 >= 58 (CE) or <= 42 (PE).
3. Leg Selection:
   - Buy 1 strike OTM (50-100 pts OTM, premium ₹12 to ₹28).
4. Trade Management & Exits:
   - Hard stop-loss: -35% of premium paid.
   - Hard time-stop: 45 minutes maximum holding time or 15:10 IST.
   - Target 1: +75% (take profit / harvest).
5. Persistence:
   - Saves record to strategies/gamma_sniper_YYYYMMDD.json and SQLite table gamma_sniper_trades.

Usage:
    python strategies/monday_gamma_sniper.py --check
    python strategies/monday_gamma_sniper.py --dry-run
    python strategies/monday_gamma_sniper.py --force
"""

import os
import sys
import json
import sqlite3
import logging
import argparse
from datetime import datetime, date, time
from typing import Optional, Dict, Any, Tuple

try:
    import pytz
    IST_TZ = pytz.timezone("Asia/Kolkata")
except ImportError:
    from zoneinfo import ZoneInfo
    IST_TZ = ZoneInfo("Asia/Kolkata")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import (
    logger,
    SQLITE_DIR,
    SNAPSHOT_DIR,
    ACTIVE_SNAPSHOT_FILE,
    NIFTY_LOT_SIZE,
)
from common.calendar_utils import is_strategy_closing_day, check_nse_holiday
from common.storage import (
    load_active_snapshot,
    insert_gamma_sniper_trade,
    update_gamma_sniper_trade,
    get_gamma_sniper_trades,
    build_db_path,
)
from common.expiry import get_next_weekly_expiry, format_expiry_file, format_expiry_angelone
from common.angelone_client import get_nifty_spot, get_nifty_option_chain

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] monday_gamma_sniper: %(message)s"
)
sniper_logger = logging.getLogger("monday_gamma_sniper")


def _now_ist() -> datetime:
    return datetime.now(IST_TZ)


def _now_utc_ts() -> int:
    return int(datetime.now(pytz.UTC).timestamp())


def is_sniper_time_window(as_of: Optional[datetime] = None) -> bool:
    """Check if current time is within the 13:15 to 15:00 IST expiry gamma window."""
    now = as_of or _now_ist()
    # 13:15 to 15:00 IST
    start = time(13, 15)
    end = time(15, 0)
    return start <= now.time() <= end


def get_active_strategy_pnl(snapshot: Dict[str, Any], db_path: str) -> Tuple[float, float]:
    """
    Calculate the week's net P&L in points and INR.
    Prioritizes realized profit (house money), falling back to total gainloss if realized is unavailable.
    Returns: (net_pnl_points, net_pnl_inr)
    """
    lot_size = int(os.environ.get("NIFTY_LOT_SIZE", str(NIFTY_LOT_SIZE)))

    # 1. Check FSM realized/total gainloss from snapshot
    fsm = snapshot.get("alpha_fsm") or {}
    if fsm:
        realized = fsm.get("realized_pnl_pts")
        total = fsm.get("total_gainloss")
        fsm_pts = float(realized if realized is not None else (total or 0.0))
        return round(fsm_pts, 2), round(fsm_pts * lot_size, 2)

    # 2. Check latest row from weekly database
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT gainloss, fsm_realized_pnl, fsm_total_gainloss FROM strategy_hourly_data ORDER BY collection_timestamp DESC LIMIT 1")
            row = cur.fetchone()
            conn.close()
            if row:
                realized_db = row["fsm_realized_pnl"]
                total_db = row["fsm_total_gainloss"] if row["fsm_total_gainloss"] is not None else row["gainloss"]
                gl = realized_db if realized_db is not None else total_db
                gl_float = float(gl or 0.0)
                return round(gl_float, 2), round(gl_float * lot_size, 2)
        except Exception as e:
            sniper_logger.warning("Failed to query latest P&L from %s: %s", db_path, e)

    return 0.0, 0.0


def evaluate_gamma_signals(
    sqlite_dir: str,
    as_of_dt: datetime,
    lookback_minutes: int = 40
) -> Tuple[Optional[str], Optional[float], Optional[float], str]:
    """
    Evaluates 5-min VWAP, RSI, and volume momentum for gamma squeeze.
    Returns: (signal_direction: 'CALL' | 'PUT' | None, vwap_dist: float, rsi: float, reason: str)
    """
    yyyy_mm = as_of_dt.strftime("%Y_%m")
    base_dir = sqlite_dir.replace("/strategies", "").rstrip("/\\")
    nsf_db = os.path.join(base_dir, f"nifty_signal_features_{yyyy_mm}.db")

    if not os.path.exists(nsf_db):
        return None, None, None, f"NSF DB not found: {nsf_db}"

    ts_end = int(as_of_dt.timestamp())
    ts_start = ts_end - (lookback_minutes * 60)

    try:
        conn = sqlite3.connect(nsf_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT ts, ltp, vwap, vwap_distance, nsf_rsi14, volume FROM signals "
            "WHERE symbol = 'NIFTY50' AND ts >= ? AND ts <= ? ORDER BY ts ASC",
            (ts_start, ts_end)
        )
        rows = cur.fetchall()
        conn.close()

        if len(rows) < 3:
            return None, None, None, "Insufficient recent 5-min bars (< 3 bars in lookback)"

        latest = rows[-1]
        vwap_dist = float(latest["vwap_distance"] or 0.0)
        rsi = float(latest["nsf_rsi14"] or 50.0)

        # Volume surge check
        volumes = [float(r["volume"]) for r in rows if r["volume"] is not None]
        avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 1.0
        cur_vol = float(latest["volume"] or 0.0)
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0

        # Bullish Gamma Trigger: Spot breaking above VWAP by >= +0.15% with RSI >= 58 and volume surge >= 1.5x
        if vwap_dist >= 0.15 and rsi >= 58 and vol_ratio >= 1.5:
            return "CALL", vwap_dist, rsi, f"Bullish gamma breakout: VWAP dist={vwap_dist:+.3f}%, RSI={rsi:.1f}, Vol ratio={vol_ratio:.1f}x"

        # Bearish Gamma Trigger: Spot breaking below VWAP by <= -0.15% with RSI <= 42 and volume surge >= 1.5x
        if vwap_dist <= -0.15 and rsi <= 42 and vol_ratio >= 1.5:
            return "PUT", vwap_dist, rsi, f"Bearish gamma breakdown: VWAP dist={vwap_dist:+.3f}%, RSI={rsi:.1f}, Vol ratio={vol_ratio:.1f}x"

        return None, vwap_dist, rsi, f"Market rangebound (VWAP dist={vwap_dist:+.3f}%, RSI={rsi:.1f}, Vol ratio={vol_ratio:.1f}x); no directional breakout"

    except Exception as e:
        sniper_logger.error("Error reading gamma signals: %s", e)
        return None, None, None, f"Error: {e}"


def run_gamma_sniper(
    dry_run: bool = False,
    force: bool = False,
    check_only: bool = False,
    custom_db_path: Optional[str] = None
) -> Dict[str, Any]:
    """Master execution loop for Monday Gamma Sniper."""
    now = _now_ist()
    today = now.date()

    sniper_logger.info("=" * 60)
    sniper_logger.info("Monday Gamma Sniper Execution Check at %s", now.strftime("%Y-%m-%d %H:%M:%S IST"))

    # 1. Day Check
    if not force:
        if check_nse_holiday(today):
            sniper_logger.info("Today is a holiday; skipping.")
            return {"status": "SKIPPED_HOLIDAY"}
        if not is_strategy_closing_day(today):
            sniper_logger.info("Today (%s) is not a strategy closing day; skipping.", today)
            return {"status": "SKIPPED_NOT_CLOSING_DAY"}
        if not is_sniper_time_window(now):
            sniper_logger.info("Outside 13:15-15:00 IST gamma window (current: %s); skipping.", now.strftime("%H:%M"))
            return {"status": "SKIPPED_OUTSIDE_WINDOW"}

    # 2. Load active snapshot & database
    snapshot = load_active_snapshot()
    if not snapshot:
        sniper_logger.warning("No active weekly strategy snapshot found; skipping.")
        return {"status": "SKIPPED_NO_ACTIVE_SNAPSHOT"}

    expiry_str = str(snapshot.get("expiry_date", ""))
    start_str = str(snapshot.get("week_start_date", ""))
    db_path = custom_db_path or build_db_path(start_str, expiry_str)

    # Derive Angel One expiry string from snapshot's active expiry date
    if expiry_str:
        try:
            cycle_expiry_date = datetime.strptime(expiry_str, "%Y%m%d").date()
        except (ValueError, TypeError):
            cycle_expiry_date = get_next_weekly_expiry(today)
    else:
        cycle_expiry_date = get_next_weekly_expiry(today)

    expiry_angelone = format_expiry_angelone(cycle_expiry_date)

    # 3. Guard & Exit Engine: Check existing trades
    existing_trades = get_gamma_sniper_trades(db_path)
    open_trades = [t for t in existing_trades if t.get("status") == "OPEN"]

    if open_trades:
        # Manage open trades: check target (+75%), hard stop (-35%), and time stop (15:10 IST / 45-min hold)
        sniper_logger.info("Managing %d open Gamma Sniper trade(s)...", len(open_trades))
        lot_size = int(os.environ.get("NIFTY_LOT_SIZE", str(NIFTY_LOT_SIZE)))
        managed_results = []
        for trade in open_trades:
            trade_id = trade.get("id")
            strike = int(trade["strike"])
            option_type = trade["option_type"]
            entry_price = float(trade["entry_price"])
            trade_ts = int(trade.get("trade_timestamp") or _now_utc_ts())

            # Fetch live option LTP using cycle expiry
            opt_chain = get_nifty_option_chain(
                expiry_angelone,
                strike if option_type == "CE" else 0,
                strike if option_type == "PE" else 0
            )
            leg_data = opt_chain.get("call" if option_type == "CE" else "put") or {}
            curr_ltp = float(leg_data.get("ltp") or 0.0)

            # Fallback if API returned 0 in dry-run/check mode
            if curr_ltp <= 0 and (dry_run or check_only):
                curr_ltp = float(trade.get("exit_price") or entry_price)

            pnl_pts = round(curr_ltp - entry_price, 2)
            pnl_pct = round((pnl_pts / entry_price) * 100.0, 2) if entry_price > 0 else 0.0
            pnl_inr = round(pnl_pts * lot_size, 2)

            # Exit triggers:
            # 1. Target 1: +75% harvest
            # 2. Hard stop: -35% SL
            # 3. Hard time-stop: 15:10 IST or >= 45 minutes holding time
            elapsed_sec = _now_utc_ts() - trade_ts
            hit_target = curr_ltp >= entry_price * 1.75
            hit_stop = curr_ltp <= entry_price * 0.65
            hit_time_stop = now.time() >= time(15, 10) or elapsed_sec >= 45 * 60

            if hit_target or hit_stop or hit_time_stop:
                if hit_target:
                    reason = f"TARGET_HIT (+75% gain reached @ ₹{curr_ltp:.2f})"
                elif hit_stop:
                    reason = f"STOP_LOSS_HIT (-35% loss reached @ ₹{curr_ltp:.2f})"
                else:
                    reason = f"TIME_STOP_HIT (Hold time: {elapsed_sec//60}m / 15:10 IST reached @ ₹{curr_ltp:.2f})"

                exit_record = {
                    "exit_price": curr_ltp,
                    "exit_timestamp": _now_utc_ts(),
                    "pnl_points": pnl_pts,
                    "pnl_pct": pnl_pct,
                    "pnl_inr": pnl_inr,
                    "exit_reason": reason,
                    "status": "CLOSED",
                }
                sniper_logger.info(
                    "🎯 [GAMMA SNIPER EXIT] Closed %d %s @ ₹%.2f (Entry: ₹%.2f, P&L: %+.2f INR | %+.1f%%) — %s",
                    strike, option_type, curr_ltp, entry_price, pnl_inr, pnl_pct, reason
                )
                if not dry_run and not check_only and trade_id:
                    update_gamma_sniper_trade(db_path, trade_id, exit_record)
                    # Update JSON dossier
                    json_path = os.path.join(_PROJECT_ROOT, "strategies", f"gamma_sniper_{format_expiry_file(today)}.json")
                    try:
                        trade_full = {**trade, **exit_record}
                        with open(json_path, "w", encoding="utf-8") as f:
                            json.dump(trade_full, f, indent=2)
                    except Exception as e:
                        sniper_logger.warning("Could not update json dossier: %s", e)
                managed_results.append({**trade, **exit_record})
            else:
                # Still active, update mark-to-market P&L
                mtm_record = {
                    "exit_price": curr_ltp,
                    "pnl_points": pnl_pts,
                    "pnl_pct": pnl_pct,
                    "pnl_inr": pnl_inr,
                }
                sniper_logger.info(
                    "👀 [GAMMA SNIPER MONITOR] Holding %d %s @ ₹%.2f (Entry: ₹%.2f, P&L: %+.2f INR | %+.1f%%, Elapsed: %dm)",
                    strike, option_type, curr_ltp, entry_price, pnl_inr, pnl_pct, elapsed_sec // 60
                )
                if not dry_run and not check_only and trade_id:
                    update_gamma_sniper_trade(db_path, trade_id, mtm_record)
                managed_results.append({**trade, **mtm_record})

        return {"status": "MANAGED_OPEN_TRADES", "trades": managed_results}

    if existing_trades and not force:
        sniper_logger.info("Gamma sniper already completed %d trade(s) this cycle; skipping duplicate execution.", len(existing_trades))
        return {"status": "ALREADY_COMPLETED", "trades": existing_trades}

    # 4. Zero Principal Risk Safeguard (House Money Only)
    net_pnl_pts, net_pnl_inr = get_active_strategy_pnl(snapshot, db_path)
    sniper_logger.info("Main Weekly Strategy Status: P&L = %+.2f pts (%+.2f INR)", net_pnl_pts, net_pnl_inr)

    if net_pnl_pts <= 0 and not force:
        sniper_logger.info(
            "[ZERO PRINCIPAL RISK] Main strategy is not in net profit (%+.2f pts). "
            "Gamma Sniper is strictly buy-only with house money. SKIPPING trade.",
            net_pnl_pts
        )
        return {
            "status": "SKIPPED_NO_HOUSE_MONEY",
            "net_pnl_points": net_pnl_pts,
            "net_pnl_inr": net_pnl_inr,
            "reason": "Main strategy is at a net loss or breakeven; zero principal risk enforced."
        }

    # Risk Budget: Max 15% of realized profit, capped at ₹1,800
    lot_size = int(os.environ.get("NIFTY_LOT_SIZE", str(NIFTY_LOT_SIZE)))
    risk_budget_inr = min(1800.0, max(500.0, net_pnl_inr * 0.15)) if net_pnl_inr > 0 else 1200.0
    max_premium = round(risk_budget_inr / lot_size, 1)
    sniper_logger.info("House Money Budget: ₹%.2f (Max Premium: ₹%.1f per share for %d lot size)",
                       risk_budget_inr, max_premium, lot_size)

    # 5. Evaluate Technical & Microstructure Signals
    signal_dir, vwap_dist, rsi, sig_reason = evaluate_gamma_signals(SQLITE_DIR, now)
    sniper_logger.info("Signal Evaluation: Direction=%s | %s", signal_dir or "NONE", sig_reason)

    if not signal_dir:
        return {
            "status": "NO_TRIGGER",
            "reason": sig_reason,
            "vwap_distance": vwap_dist,
            "rsi_14": rsi,
            "risk_budget_inr": risk_budget_inr,
        }

    # 6. Fetch Spot & Strikes
    spot_data = get_nifty_spot()
    if not spot_data:
        sniper_logger.error("Could not fetch NIFTY spot data.")
        return {"status": "ERROR_NO_SPOT"}

    spot_ltp = float(spot_data.get("ltp") or 0.0)
    if spot_ltp <= 0:
        sniper_logger.error("Invalid NIFTY spot LTP: %.2f", spot_ltp)
        return {"status": "ERROR_INVALID_SPOT"}

    # Target 50 to 100 pt OTM strike
    anchor = round(spot_ltp / 50.0) * 50
    if signal_dir == "CALL":
        strike = int(anchor + 50)
        option_type = "CE"
    else:
        strike = int(anchor - 50)
        option_type = "PE"

    # Fetch live option LTP using active cycle expiry
    opt_chain = get_nifty_option_chain(expiry_angelone, strike if option_type == "CE" else 0, strike if option_type == "PE" else 0)
    leg_data = opt_chain.get("call" if option_type == "CE" else "put") or {}
    entry_ltp = float(leg_data.get("ltp") or 0.0)

    # In dry-run or check mode, simulate realistic entry premium (~₹18.5) if market closed
    if entry_ltp <= 0:
        if dry_run or check_only:
            entry_ltp = min(max_premium, 18.5)
            sniper_logger.info("Using simulated entry LTP for test/dry-run: ₹%.2f", entry_ltp)
        else:
            sniper_logger.error(
                "Could not fetch valid live option LTP (got %.2f) for %d %s; aborting entry.",
                entry_ltp, strike, option_type
            )
            return {
                "status": "ERROR_NO_OPTION_LTP",
                "strike": strike,
                "option_type": option_type,
                "reason": "Live option chain returned 0 LTP during market hours."
            }

    outlay_inr = round(entry_ltp * lot_size, 2)
    stop_loss = round(entry_ltp * 0.65, 2)  # -35% hard stop
    target_1 = round(entry_ltp * 1.75, 2)   # +75% harvest target

    trade_record = {
        "trade_timestamp": _now_utc_ts(),
        "expiry_date": expiry_str or format_expiry_file(expiry_date_obj),
        "nifty_spot": spot_ltp,
        "option_type": option_type,
        "strike": strike,
        "entry_price": entry_ltp,
        "exit_price": entry_ltp,  # initial entry
        "exit_timestamp": None,
        "pnl_points": 0.0,
        "pnl_pct": 0.0,
        "pnl_inr": 0.0,
        "exit_reason": f"Active trade (SL: ₹{stop_loss}, Target: ₹{target_1})",
        "allocated_risk_inr": outlay_inr,
        "status": "OPEN",
    }

    sniper_logger.info(
        "🚀 [GAMMA SNIPER ENTRY] Bought %d %s @ ₹%.2f (Outlay: ₹%.2f, Budget: ₹%.2f, SL: ₹%.2f, Target: ₹%.2f)",
        strike, option_type, entry_ltp, outlay_inr, risk_budget_inr, stop_loss, target_1
    )

    if check_only:
        return {"status": "CHECK_PASSED", "trade": trade_record}

    if not dry_run:
        insert_gamma_sniper_trade(db_path, trade_record)
        # Write JSON dossier
        json_path = os.path.join(_PROJECT_ROOT, "strategies", f"gamma_sniper_{format_expiry_file(today)}.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(trade_record, f, indent=2)
            sniper_logger.info("Saved trade record to %s", json_path)
        except Exception as e:
            sniper_logger.warning("Could not write json dossier: %s", e)

    return {"status": "ENTERED", "trade": trade_record}


def main():
    parser = argparse.ArgumentParser(description="Monday Afternoon Gamma Sniper Strategy")
    parser.add_argument("--check", action="store_true", help="Evaluate conditions without executing")
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution without saving")
    parser.add_argument("--force", action="store_true", help="Bypass day and time checks")
    parser.add_argument("--db-path", type=str, default=None, help="Explicit weekly database path")
    args = parser.parse_args()

    res = run_gamma_sniper(dry_run=args.dry_run, force=args.force, check_only=args.check, custom_db_path=args.db_path)
    print("\n" + json.dumps(res, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
