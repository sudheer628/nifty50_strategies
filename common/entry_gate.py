"""
entry_gate.py - Smart Entry Gate for NIFTY50 Weekly Option Strategy.

Eliminates the blind 09:31 AM Tuesday market open IV trap by evaluating:
1. Filter 1 (IV Stability Gate): Defers entry if implied volatility is rapidly
   expanding on open (+1.0 pt spike in 15m), avoiding the immediate IV crush.
2. Filter 2 (ORB & VWAP Gate): Defers entry if market is trapped in a tiny
   opening chop range (< 15 pts) centered on VWAP before 10:00 AM IST.
3. Filter 3 (Regime / ADX Gate): Checks for trend establishment or volatility expansion.
4. Fail-Safe Timeout: If current IST time >= 11:00 AM, forces entry unconditionally
   so the weekly trading cycle is never missed.
"""

import os
import json
import sqlite3
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, date, time
from typing import Optional, Dict, Any, Tuple

try:
    import pytz
    IST_TZ = pytz.timezone("Asia/Kolkata")
except ImportError:
    from zoneinfo import ZoneInfo
    IST_TZ = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger("entry_gate")

DEFAULT_SQLITE_DIR = os.getenv("SQLITE_DIR", "/home/ubuntu/sqlite")
MAX_ENTRY_HOUR_IST = 11  # 11:00 AM IST hard fail-safe cutoff


@dataclass
class EntryGateResult:
    should_enter: bool
    status: str  # "CLEARED" | "DEFERRED" | "FORCED_TIMEOUT" | "BYPASSED"
    defer_reason: str
    iv_slope: Optional[float]
    current_iv: Optional[float]
    vwap_distance: Optional[float]
    adx_14: Optional[float]
    opening_range: Optional[float]
    evaluated_at_ist: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _now_ist() -> datetime:
    return datetime.now(IST_TZ)


def _get_signals_db_path(sqlite_dir: str, dt: datetime) -> str:
    yyyy_mm = dt.strftime("%Y_%m")
    base = sqlite_dir.replace("/strategies", "").rstrip("/\\")
    return os.path.join(base, f"signals_data_{yyyy_mm}.db")


def _get_nsf_db_path(sqlite_dir: str, dt: datetime) -> str:
    yyyy_mm = dt.strftime("%Y_%m")
    base = sqlite_dir.replace("/strategies", "").rstrip("/\\")
    return os.path.join(base, f"nifty_signal_features_{yyyy_mm}.db")


def evaluate_iv_stability(
    sqlite_dir: str,
    as_of_dt: datetime,
    lookback_minutes: int = 45
) -> Tuple[bool, Optional[float], Optional[float], str]:
    """
    Filter 1: Evaluates whether implied volatility has stabilized after market open.
    Returns: (passed: bool, iv_slope: float, current_iv: float, reason: str)
    """
    signals_db = _get_signals_db_path(sqlite_dir, as_of_dt)
    if not os.path.exists(signals_db):
        return True, None, None, "signals_data DB not found; passing by default"

    ts_end = int(as_of_dt.timestamp())
    ts_start = ts_end - (lookback_minutes * 60)

    try:
        conn = sqlite3.connect(signals_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT ts, data FROM market_data WHERE pk = 'INDEX#NIFTY50' AND ts >= ? AND ts <= ? ORDER BY ts ASC",
            (ts_start, ts_end)
        )
        rows = cur.fetchall()
        conn.close()

        iv_series = []
        for r in rows:
            try:
                data = json.loads(r["data"]) if r["data"] else {}
                avg_iv = data.get("avg_iv")
                if avg_iv is not None and float(avg_iv) > 0:
                    iv_series.append((int(r["ts"]), float(avg_iv)))
            except Exception:
                continue

        if len(iv_series) < 2:
            return True, None, (iv_series[-1][1] if iv_series else None), "Insufficient IV history; passing"

        first_ts, first_iv = iv_series[0]
        last_ts, last_iv = iv_series[-1]
        iv_slope = round(last_iv - first_iv, 2)

        # Defer if IV is surging rapidly (> +1.0 point spike) before 10:30 AM
        if iv_slope > 1.0 and (as_of_dt.hour < 10 or (as_of_dt.hour == 10 and as_of_dt.minute < 30)):
            return False, iv_slope, last_iv, f"Opening IV expanding (+{iv_slope:.2f} pts in {lookback_minutes}m); waiting for IV crush to settle"

        return True, iv_slope, last_iv, f"IV stable (slope={iv_slope:+.2f} pts, current={last_iv:.1f})"

    except Exception as e:
        logger.warning("Error reading IV stability from %s: %s", signals_db, e)
        return True, None, None, f"Error checking IV: {e}"


def evaluate_orb_and_vwap(
    sqlite_dir: str,
    as_of_dt: datetime,
    lookback_minutes: int = 45
) -> Tuple[bool, Optional[float], Optional[float], Optional[float], str]:
    """
    Filter 2 & 3: Evaluates 15m/30m Opening Range Breakout (ORB), VWAP distance, and ADX.
    Returns: (passed: bool, vwap_distance: float, adx_14: float, opening_range: float, reason: str)
    """
    nsf_db = _get_nsf_db_path(sqlite_dir, as_of_dt)
    if not os.path.exists(nsf_db):
        return True, None, None, None, "nifty_signal_features DB not found; passing by default"

    ts_end = int(as_of_dt.timestamp())
    ts_start = ts_end - (lookback_minutes * 60)

    try:
        conn = sqlite3.connect(nsf_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT ts, ltp, vwap, vwap_distance, adx_14, bb_width FROM signals "
            "WHERE symbol = 'NIFTY50' AND ts >= ? AND ts <= ? ORDER BY ts ASC",
            (ts_start, ts_end)
        )
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return True, None, None, None, "No recent NSF rows; passing"

        ltps = [float(r["ltp"]) for r in rows if r["ltp"] is not None]
        opening_range = round(max(ltps) - min(ltps), 1) if ltps else 0.0

        latest = rows[-1]
        vwap_dist = float(latest["vwap_distance"] or 0.0)
        adx = float(latest["adx_14"] or 0.0)
        bb_w = float(latest["bb_width"] or 0.0)

        # Defer if market is deadlocked in < 15-pt range and hugging VWAP before 10:00 AM
        if opening_range < 15.0 and abs(vwap_dist) < 0.05 and as_of_dt.hour < 10:
            return (
                False,
                vwap_dist,
                adx,
                opening_range,
                f"Market in dead opening chop (range {opening_range:.1f} pts < 15, vwap_dist {vwap_dist:+.3f}%); waiting for 30m range"
            )

        # Defer if ADX is extremely dead (< 12.0) with tight BB squeeze before 10:15 AM
        if adx > 0 and adx < 12.0 and bb_w > 0 and bb_w < 0.20 and (as_of_dt.hour < 10 or (as_of_dt.hour == 10 and as_of_dt.minute < 15)):
            return (
                False,
                vwap_dist,
                adx,
                opening_range,
                f"ADX extremely low ({adx:.1f} < 12) with BB squeeze ({bb_w:.3f}%); waiting for volatility expansion"
            )

        return True, vwap_dist, adx, opening_range, f"ORB/VWAP cleared (range={opening_range:.1f} pts, vwap_dist={vwap_dist:+.3f}%, adx={adx:.1f})"

    except Exception as e:
        logger.warning("Error reading NSF features from %s: %s", nsf_db, e)
        return True, None, None, None, f"Error checking ORB/VWAP: {e}"


def evaluate_entry_gate(
    reference_ltp: float,
    sqlite_dir: str = DEFAULT_SQLITE_DIR,
    current_time_ist: Optional[datetime] = None,
    force: bool = False
) -> EntryGateResult:
    """
    Evaluates all Smart Entry Gate filters.

    Args:
        reference_ltp: Current Nifty spot LTP.
        sqlite_dir: Path to sqlite databases directory.
        current_time_ist: Optional datetime in IST (defaults to now).
        force: If True, bypasses all filters.

    Returns:
        EntryGateResult with should_enter flag and metrics.
    """
    now = current_time_ist or _now_ist()
    eval_str = now.strftime("%Y-%m-%d %H:%M:%S IST")

    if force:
        logger.info("[SMART ENTRY GATE] Force flag enabled; gate bypassed.")
        return EntryGateResult(
            should_enter=True,
            status="BYPASSED",
            defer_reason="Explicitly bypassed via force flag",
            iv_slope=None,
            current_iv=None,
            vwap_distance=None,
            adx_14=None,
            opening_range=None,
            evaluated_at_ist=eval_str
        )

    # 1. Hard Fail-Safe Timeout Cutoff (>= 11:00 AM IST forces entry)
    if now.hour > MAX_ENTRY_HOUR_IST or (now.hour == MAX_ENTRY_HOUR_IST and now.minute >= 0):
        logger.info("[SMART ENTRY GATE] Fail-safe cutoff (>= %02d:00 IST) reached; forcing entry.", MAX_ENTRY_HOUR_IST)
        return EntryGateResult(
            should_enter=True,
            status="FORCED_TIMEOUT",
            defer_reason=f"Fail-safe cutoff ({MAX_ENTRY_HOUR_IST}:00 IST) reached; forced entry to prevent missing cycle",
            iv_slope=None,
            current_iv=None,
            vwap_distance=None,
            adx_14=None,
            opening_range=None,
            evaluated_at_ist=eval_str
        )

    # 2. Filter 1: IV Stability Gate
    iv_pass, iv_slope, cur_iv, iv_reason = evaluate_iv_stability(sqlite_dir, now)
    if not iv_pass:
        logger.info("[SMART ENTRY GATE] %s", iv_reason)
        return EntryGateResult(
            should_enter=False,
            status="DEFERRED",
            defer_reason=iv_reason,
            iv_slope=iv_slope,
            current_iv=cur_iv,
            vwap_distance=None,
            adx_14=None,
            opening_range=None,
            evaluated_at_ist=eval_str
        )

    # 3. Filter 2 & 3: ORB, VWAP & ADX Gate
    orb_pass, vwap_dist, adx, op_range, orb_reason = evaluate_orb_and_vwap(sqlite_dir, now)
    if not orb_pass:
        logger.info("[SMART ENTRY GATE] %s", orb_reason)
        return EntryGateResult(
            should_enter=False,
            status="DEFERRED",
            defer_reason=orb_reason,
            iv_slope=iv_slope,
            current_iv=cur_iv,
            vwap_distance=vwap_dist,
            adx_14=adx,
            opening_range=op_range,
            evaluated_at_ist=eval_str
        )

    # All filters cleared
    cleared_msg = f"All filters cleared ({iv_reason} | {orb_reason})"
    logger.info("[SMART ENTRY GATE CLEARED] %s", cleared_msg)
    return EntryGateResult(
        should_enter=True,
        status="CLEARED",
        defer_reason="",
        iv_slope=iv_slope,
        current_iv=cur_iv,
        vwap_distance=vwap_dist,
        adx_14=adx,
        opening_range=op_range,
        evaluated_at_ist=eval_str
    )
