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

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, date, time, timedelta
from typing import Optional, Dict, Any, List, Tuple


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
    SQLITE_DIR,
    STRIKE_MONEYNESS_MODE,
)

from common.expiry import (
    get_next_weekly_expiry,
    is_tuesday,
    format_expiry_angelone,
    format_expiry_file,
    strike_selector,
    compute_intrinsic_extrinsic,
)


from common.calendar_utils import (
    check_nse_holiday,
    is_strategy_start_day,
    is_strategy_closing_day,
    get_strategy_cycle_role,
)

from common.ai_strike_selector import (
    select_strikes,
    compute_static_strikes,
    get_last_composite_score,
)

from common.entry_gate import evaluate_entry_gate

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
    update_active_fsm_state,
    generate_cycle_id,
    insert_gate_deferred_shadow,
    insert_timing_shadow_entry,
    get_timing_shadow_entries,
)

from common.fsm_strategy import (
    init_fsm_state,
    evaluate_fsm_tick,
    estimate_option_delta,
    fetch_option_surface_iv,
)

from common.profit_monitor import evaluate_peak_profit

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


def _now_utc_ts() -> int:
    """Return the current UTC timestamp as Unix epoch integer (seconds)."""
    return int(datetime.now(UTC).timestamp())


def _today_ist() -> date:
    """Return today's date in IST."""
    return _now_ist().date()


def _is_market_time() -> bool:
    """
    Return True if the current IST time is between 9:15 AM and 3:35 PM
    on a weekday (Monday-Friday).
    """
    now = _now_ist()
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    market_open = time(9, 15)
    market_close = time(15, 35)
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
    """Return the start date of the weekly cycle containing ``d``.

    Mirrors ``is_strategy_start_day``:
    - The fresh cycle starts on Tuesday unless Tuesday is an NSE holiday,
      in which case the start defers to Wednesday (Scenario-2).
    - A Monday holiday only defers the previous cycle's *closing* day to
      Tuesday; the fresh cycle still starts that Tuesday.
    """
    if is_strategy_start_day(d):
        return d
    days_since_tuesday = (d.weekday() - 1) % 7
    tue = d - timedelta(days=days_since_tuesday)
    if check_nse_holiday(tue):
        # Tuesday holiday: the fresh cycle started on Wednesday instead.
        wed = tue + timedelta(days=1)
        if wed <= d:
            return wed
    return tue


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

    # If the cycle was explicitly marked closed, retire it
    if snapshot.get("status") == "closed":
        return {}

    # When today is a designated START_DAY, any prior cycle is retired
    # unless a snapshot has explicitly been prepared for today.
    if is_strategy_start_day(today) and week_start != today:
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
    fsm_data: Optional[dict] = None,
) -> dict:
    """Build a standard hourly record dictionary with both Base and FSM metrics."""
    record = {
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
    if fsm_data and isinstance(fsm_data, dict):
        record.update(fsm_data)
    return record


def collect_once(force_static: bool = False, force_entry: bool = False, dry_run: bool = False) -> bool:
    """
    Perform one hourly data collection cycle.

    - **Tuesday** (or Wednesday when Tuesday was an NSE holiday): ends the
      previous cycle, starts a fresh one with a new expiry, new strikes,
      and new buy prices.  The old snapshot is archived to a dated JSON
      file; the new one becomes active.
    - **Wednesday-Monday**: reuses the active snapshot's buy prices and
      continues writing to the same weekly DB.

    Args:
        force_static: If True, bypass AI strike selector and use static anchor +/- 100 rule.
        force_entry: If True, bypass Smart Entry Gate deferral and force immediate cycle entry.
        dry_run: If True, bypass external alert delivery while keeping tracking active.

    Returns True on success, False on failure.
    """
    today = _today_ist()
    if check_nse_holiday(today):
        logger.info("Today (%s) is an NSE market holiday or weekend. Skipping data collection.", today)
        return True

    is_first_run_of_week = is_strategy_start_day(today)
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
        static_put_strike = active_cycle.get("static_put_strike")
        static_call_strike = active_cycle.get("static_call_strike")
        selection_mode = active_cycle.get("selection_mode", "REUSED")
        selection_rationale = active_cycle.get("selection_rationale", "")
        logger.info(
            "Reusing cycle strikes from %s: PUT=%d  CALL=%d (Mode: %s)",
            active_cycle["week_start_date"], put_strike, call_strike, selection_mode
        )
    else:
        d_to_exp = max(0.1, float((expiry_date - today).days))

        # Smart Entry Gate (Eliminating the blind 09:31 AM IV crush trap)
        if is_first_run_of_week:
            gate_res = evaluate_entry_gate(
                reference_ltp=nifty_ltp,
                sqlite_dir=SQLITE_DIR,
                force=force_entry,
                nifty_open=nifty_open,
                nifty_prev_close=nifty_prev_close,
            )
            if not gate_res.should_enter:
                logger.info("=" * 60)
                logger.info("  [SMART ENTRY GATE DEFERRED] Status: %s", gate_res.status)
                logger.info("  Reason: %s", gate_res.defer_reason)
                logger.info(
                    "  Metrics: IV Slope=%s | VWAP Dist=%s%% | ADX=%s | Range=%s pts",
                    f"{gate_res.iv_slope:+.2f}" if gate_res.iv_slope is not None else "N/A",
                    f"{gate_res.vwap_distance:+.3f}" if gate_res.vwap_distance is not None else "N/A",
                    f"{gate_res.adx_14:.1f}" if gate_res.adx_14 is not None else "N/A",
                    f"{gate_res.opening_range:.1f}" if gate_res.opening_range is not None else "N/A"
                )
                logger.info("  Holding execution until next scheduled collection tick (10:01 / 10:31 IST).")
                logger.info("=" * 60)

                # Counterfactual Shadow Entry: Record hypothetical entry prices for training & benchmarking
                try:
                    (
                        hyp_put,
                        hyp_call,
                        hyp_stat_put,
                        hyp_stat_call,
                        hyp_mode,
                        hyp_rationale,
                        hyp_comp_score,
                        hyp_dir_bias,
                        hyp_c_delta,
                        hyp_p_delta,
                    ) = select_strikes(
                        reference_ltp=nifty_ltp,
                        sqlite_dir=SQLITE_DIR,
                        force_static=force_static,
                        moneyness_mode=STRIKE_MONEYNESS_MODE,
                        days_to_expiry=d_to_exp,
                    )
                    hyp_chain = get_nifty_option_chain(expiry_angelone, hyp_call, hyp_put)
                    hyp_call_ltp = (hyp_chain.get("call") or {}).get("ltp")
                    hyp_put_ltp = (hyp_chain.get("put") or {}).get("ltp")

                    shadow_db = _ensure_db(week_start, expiry_date)
                    shadow_record = {
                        "timestamp": _now_utc_ts(),
                        "trade_date": today_str,
                        "nifty_spot": nifty_ltp,
                        "gate_status": gate_res.status,
                        "defer_reason": gate_res.defer_reason,
                        "iv_slope": gate_res.iv_slope,
                        "vwap_distance": gate_res.vwap_distance,
                        "adx_14": gate_res.adx_14,
                        "opening_range": gate_res.opening_range,
                        "hypothetical_call_strike": hyp_call,
                        "hypothetical_put_strike": hyp_put,
                        "hypothetical_call_ltp": hyp_call_ltp,
                        "hypothetical_put_ltp": hyp_put_ltp,
                        "selection_mode": hyp_mode,
                        "composite_direction_score": hyp_comp_score,
                        "directional_bias": hyp_dir_bias,
                        "target_call_delta": hyp_c_delta,
                        "target_put_delta": hyp_p_delta,
                        "atm_straddle_ratio": gate_res.atm_straddle_ratio,
                        "overnight_gap_pct": gate_res.overnight_gap_pct,
                    }
                    insert_gate_deferred_shadow(shadow_db, shadow_record)
                    logger.info(
                        "  [COUNTERFACTUAL SHADOW LOGGED] Hyp CALL %d @ ₹%s | PUT %d @ ₹%s",
                        hyp_call, f"{hyp_call_ltp:.2f}" if hyp_call_ltp else "N/A",
                        hyp_put, f"{hyp_put_ltp:.2f}" if hyp_put_ltp else "N/A"
                    )
                except Exception as shadow_err:
                    logger.warning("Failed to record counterfactual gate shadow: %s", shadow_err)

                return True

        # Dynamic AI strike selector (with fail-safe static anchor +/- 100 fallback)
        (
            put_strike,
            call_strike,
            static_put_strike,
            static_call_strike,
            selection_mode,
            selection_rationale,
            composite_score,
            directional_bias,
            target_call_delta,
            target_put_delta,
        ) = select_strikes(
            reference_ltp=nifty_ltp,
            sqlite_dir=SQLITE_DIR,
            force_static=force_static,
            moneyness_mode=STRIKE_MONEYNESS_MODE,
            days_to_expiry=d_to_exp,
        )
        logger.info(
            "Selected strikes from LTP %.2f [%s]: PUT=%d (Delta=%.2f)  CALL=%d (Delta=%.2f) | Bias=%s (Score=%.2f) (Static Benchmark: PUT=%d CALL=%d)",
            nifty_ltp, selection_mode, put_strike, target_put_delta, call_strike, target_call_delta,
            directional_bias, composite_score, static_put_strike, static_call_strike
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
        # --- Strategy Start Day (Tuesday or holiday-shifted Wednesday) ---
        # Check whether an active snapshot for THIS start day already exists
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
            # --- Genuinely fresh cycle: capture new buy prices ---
            db_path = _ensure_db(week_start, expiry_date)
            cycle_id = generate_cycle_id(start_str)
            call_buy_price = call_ltp
            put_buy_price = put_ltp

            ie = compute_intrinsic_extrinsic(nifty_ltp, call_strike, put_strike, call_buy_price, put_buy_price)

            snapshot = {
                "strategy_name": STRATEGY_NAME,
                "cycle_id": cycle_id,
                "week_start_date": start_str,
                "expiry_date": expiry_file,
                "entry_spot": nifty_ltp,
                "call_strike": call_strike,
                "put_strike": put_strike,
                "call_buy_price": call_buy_price,
                "put_buy_price": put_buy_price,
                "captured_at": _now_utc_ts(),
                "static_call_strike": static_call_strike,
                "static_put_strike": static_put_strike,
                "selection_mode": selection_mode,
                "selection_rationale": selection_rationale,
                "composite_direction_score": composite_score if 'composite_score' in locals() else get_last_composite_score(),
                "directional_bias": directional_bias if 'directional_bias' in locals() else "NEUTRAL",
                "target_call_delta": target_call_delta if 'target_call_delta' in locals() else 0.68,
                "target_put_delta": target_put_delta if 'target_put_delta' in locals() else -0.68,
                "moneyness": STRIKE_MONEYNESS_MODE,
                "spread_width": ie["spread_width"],
                "call_intrinsic": ie["call_intrinsic"],
                "call_extrinsic": ie["call_extrinsic"],
                "put_intrinsic": ie["put_intrinsic"],
                "put_extrinsic": ie["put_extrinsic"],
                "total_intrinsic": ie["total_intrinsic"],
                "total_extrinsic": ie["total_extrinsic"],
                "smart_gate": gate_res.to_dict() if 'gate_res' in locals() else {},
                "alpha_fsm": init_fsm_state(
                    call_strike=call_strike,
                    put_strike=put_strike,
                    call_buy_price=call_buy_price,
                    put_buy_price=put_buy_price,
                    entry_ts=_now_utc_ts(),
                    composite_score=composite_score if 'composite_score' in locals() else get_last_composite_score(),
                    moneyness=STRIKE_MONEYNESS_MODE,
                    call_intrinsic=ie["call_intrinsic"],
                    call_extrinsic=ie["call_extrinsic"],
                    put_intrinsic=ie["put_intrinsic"],
                    put_extrinsic=ie["put_extrinsic"],
                    spread_width=ie["spread_width"],
                ),
            }

            # Archives the previous snapshot (if any) before overwriting
            save_active_snapshot(snapshot)
            insert_buy_snapshot(db_path, snapshot)

            # Sync to unified active_strategies.json (5day_strategy) & MongoDB
            try:
                from common.unified_strategy_store import init_or_rotate_cycle, update_locked_prices
                init_or_rotate_cycle("5day_strategy", {
                    "cycle_id": cycle_id,
                    "start_date": week_start.strftime("%Y-%m-%d"),
                    "expiry_date": expiry_date.strftime("%Y-%m-%d"),
                    "call_strike": call_strike,
                    "put_strike": put_strike,
                    "moneyness": STRIKE_MONEYNESS_MODE,
                    "entry_spot": nifty_ltp,
                })
                update_locked_prices(
                    "5day_strategy",
                    call_buy=call_buy_price,
                    put_buy=put_buy_price,
                    spot=nifty_ltp,
                    timestamp=_now_utc_ts(),
                )
            except Exception as exc:
                logger.warning("Failed to sync unified 5day_strategy store: %s", exc)

            # Record counterfactual baseline V0 (09:31 lock)
            try:
                shadow_v0 = {
                    "cycle_id": cycle_id,
                    "trade_date": today_str,
                    "variant": "V0_baseline_0931",
                    "call_strike": call_strike,
                    "put_strike": put_strike,
                    "call_entry_time": _now_ist().strftime("%H:%M:%S"),
                    "call_entry_price": call_buy_price,
                    "call_entry_ts": _now_utc_ts(),
                    "put_entry_time": _now_ist().strftime("%H:%M:%S"),
                    "put_entry_price": put_buy_price,
                    "put_entry_ts": _now_utc_ts(),
                    "combined_entry_price": round(call_buy_price + put_buy_price, 2),
                    "spot_at_call_entry": nifty_ltp,
                    "spot_at_put_entry": nifty_ltp,
                    "nifty_spot_entry": nifty_ltp,
                    "moneyness": STRIKE_MONEYNESS_MODE,
                    "created_at": _now_utc_ts(),
                }
                insert_timing_shadow_entry(db_path, shadow_v0)
                logger.info(
                    "  [TIMING SHADOW V0 RECORDED] Baseline 09:31: CALL %d @ ₹%.2f | PUT %d @ ₹%.2f (Combined: ₹%.2f)",
                    call_strike, call_buy_price, put_strike, put_buy_price, shadow_v0["combined_entry_price"]
                )
            except Exception as shadow_err:
                logger.warning("Failed to record timing shadow V0: %s", shadow_err)

            logger.info("Cycle buy prices captured [%s]: CALL=%d (₹%.2f)  PUT=%d (₹%.2f)",
                     selection_mode, call_strike, call_buy_price, put_strike, put_buy_price)
    else:
        # --- Mid-cycle holding/closing day: reuse the complete active cycle ---
        if not active_cycle:
            logger.warning("No active snapshot found on non-start day; "
                           "capturing a midweek fallback snapshot. Its prices "
                           "are current prices, not Tuesday prices.")
            cycle_id = generate_cycle_id(start_str)
            call_buy_price = call_ltp
            put_buy_price = put_ltp
            ie = compute_intrinsic_extrinsic(nifty_ltp, call_strike, put_strike, call_buy_price, put_buy_price)

            snapshot = {
                "strategy_name": STRATEGY_NAME,
                "cycle_id": cycle_id,
                "week_start_date": start_str,
                "expiry_date": expiry_file,
                "entry_spot": nifty_ltp,
                "call_strike": call_strike,
                "put_strike": put_strike,
                "call_buy_price": call_buy_price,
                "put_buy_price": put_buy_price,
                "captured_at": _now_utc_ts(),
                "static_call_strike": static_call_strike,
                "static_put_strike": static_put_strike,
                "selection_mode": selection_mode,
                "selection_rationale": selection_rationale,
                "composite_direction_score": get_last_composite_score(),
                "directional_bias": "NEUTRAL",
                "target_call_delta": 0.68,
                "target_put_delta": -0.68,
                "moneyness": STRIKE_MONEYNESS_MODE,
                "spread_width": ie["spread_width"],
                "call_intrinsic": ie["call_intrinsic"],
                "call_extrinsic": ie["call_extrinsic"],
                "put_intrinsic": ie["put_intrinsic"],
                "put_extrinsic": ie["put_extrinsic"],
                "total_intrinsic": ie["total_intrinsic"],
                "total_extrinsic": ie["total_extrinsic"],
                "smart_gate": {"status": "MIDWEEK_FALLBACK", "should_enter": True},
                "alpha_fsm": init_fsm_state(
                    call_strike=call_strike,
                    put_strike=put_strike,
                    call_buy_price=call_buy_price,
                    put_buy_price=put_buy_price,
                    entry_ts=_now_utc_ts(),
                    composite_score=get_last_composite_score(),
                    moneyness=STRIKE_MONEYNESS_MODE,
                    call_intrinsic=ie["call_intrinsic"],
                    call_extrinsic=ie["call_extrinsic"],
                    put_intrinsic=ie["put_intrinsic"],
                    put_extrinsic=ie["put_extrinsic"],
                    spread_width=ie["spread_width"],
                ),
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
    # Evaluate Decoupled FSM Strategy (Alpha)
    # ------------------------------------------------------------------
    curr_active = load_active_snapshot()
    fsm_state = curr_active.get("alpha_fsm")
    if not fsm_state:
        entry_spot_fb = curr_active.get("entry_spot") or curr_active.get("nifty_open") or nifty_ltp
        ie_fb = compute_intrinsic_extrinsic(entry_spot_fb, call_strike, put_strike, call_buy_price, put_buy_price)
        fsm_state = init_fsm_state(
            call_strike=call_strike,
            put_strike=put_strike,
            call_buy_price=call_buy_price,
            put_buy_price=put_buy_price,
            entry_ts=_now_utc_ts(),
            composite_score=curr_active.get("composite_direction_score", 0.0),
            moneyness=curr_active.get("moneyness") or ("ITM" if put_strike > call_strike else "OTM"),
            call_intrinsic=curr_active.get("call_intrinsic", ie_fb["call_intrinsic"]),
            call_extrinsic=curr_active.get("call_extrinsic", ie_fb["call_extrinsic"]),
            put_intrinsic=curr_active.get("put_intrinsic", ie_fb["put_intrinsic"]),
            put_extrinsic=curr_active.get("put_extrinsic", ie_fb["put_extrinsic"]),
            spread_width=curr_active.get("spread_width", ie_fb["spread_width"]),
        )

    days_to_expiry = max(0.1, (expiry_date - today).days)
    call_delta = estimate_option_delta(nifty_ltp, call_strike, days_to_expiry, is_call=True)
    put_delta = estimate_option_delta(nifty_ltp, put_strike, days_to_expiry, is_call=False)
    call_iv, put_iv = fetch_option_surface_iv(call_strike, put_strike, sqlite_dir=SQLITE_DIR)
    updated_fsm, fsm_events = evaluate_fsm_tick(
        fsm_state=fsm_state,
        current_call_ltp=call_ltp,
        current_put_ltp=put_ltp,
        current_ts=_now_utc_ts(),
        days_to_expiry=days_to_expiry,
        call_delta=call_delta,
        put_delta=put_delta,
        put_iv=put_iv,
        call_iv=call_iv,
    )
    if fsm_events:
        for ev in fsm_events:
            logger.info("  [ALPHA FSM EVENT] %s", ev)
    update_active_fsm_state(updated_fsm)

    fsm_data = {
        "fsm_state": updated_fsm.get("state", "DUAL_LONG"),
        "fsm_call_status": updated_fsm.get("call_leg", {}).get("status", "ACTIVE"),
        "fsm_put_status": updated_fsm.get("put_leg", {}).get("status", "ACTIVE"),
        "fsm_call_exit_price": updated_fsm.get("call_leg", {}).get("exit_price"),
        "fsm_put_exit_price": updated_fsm.get("put_leg", {}).get("exit_price"),
        "fsm_realized_pnl": updated_fsm.get("realized_pnl_pts", 0.0),
        "fsm_unrealized_pnl": updated_fsm.get("unrealized_pnl_pts", 0.0),
        "fsm_total_gainloss": updated_fsm.get("total_gainloss", 0.0),
        "fsm_roi_pct": updated_fsm.get("roi_pct", 0.0),
    }

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
        fsm_data=fsm_data,
    )
    insert_record(db_path, record)

    # Sync latest LTP to unified active_strategies.json & MongoDB
    try:
        from common.unified_strategy_store import update_latest_ltp
        update_latest_ltp("5day_strategy", spot=nifty_ltp, call_ltp=call_ltp, put_ltp=put_ltp)
    except Exception as e_ltp:
        logger.debug("Failed to sync latest LTP to unified store: %s", e_ltp)

    # ------------------------------------------------------------------
    # Evaluate Peak-Profit High-Water Mark & Alert Trigger
    # ------------------------------------------------------------------
    try:
        latest_snapshot = load_active_snapshot()
        is_first_entry_tick = is_first_run_of_week and (
            active_cycle is None or active_cycle.get("week_start_date") != today_str
        )
        peak_state, alert_sent, peak_msg = evaluate_peak_profit(
            snapshot=latest_snapshot,
            nifty_ltp=nifty_ltp,
            call_strike=call_strike,
            call_ltp=call_ltp,
            call_buy=call_buy_price,
            put_strike=put_strike,
            put_ltp=put_ltp,
            put_buy=put_buy_price,
            is_first_tick=is_first_entry_tick,
            dry_run=dry_run,
            fsm_state=updated_fsm.get("state"),
            fsm_total_gainloss=updated_fsm.get("total_gainloss"),
        )
        cost = (call_buy_price or 0.0) + (put_buy_price or 0.0)
        curr_pnl_pts = round((call_ltp - (call_buy_price or call_ltp)) + (put_ltp - (put_buy_price or put_ltp)), 2)
        curr_pnl_pct = round((curr_pnl_pts / cost) * 100.0, 2) if cost > 0 else 0.0
        logger.info(
            "  [PEAK MONITOR] %s (Current: %+0.2f%% / %+0.2f pts | Peak: %+0.2f%% / %+0.2f pts | Alerts Today: %d)",
            peak_msg,
            curr_pnl_pct,
            curr_pnl_pts,
            peak_state.get("peak_pnl_pct", 0.0),
            peak_state.get("peak_pnl_pts", 0.0),
            peak_state.get("alerts_sent_today", 0),
        )
    except Exception as peak_err:
        logger.warning("Failed to evaluate peak profit: %s", peak_err)

    # ------------------------------------------------------------------
    # Counterfactual Timing Shadows (Delayed 10:00 & Staggered Variants)
    # ------------------------------------------------------------------
    try:
        now_dt = _now_ist()
        # On Tuesday (start date) after 09:55 AM IST and before 10:35 AM IST
        if today_str == start_str and (now_dt.hour == 10 or (now_dt.hour == 9 and now_dt.minute >= 55)):
            existing_shadows = {s["variant"] for s in get_timing_shadow_entries(db_path, cycle_id=cycle_id)}
            if "V1_delayed_1000" not in existing_shadows:
                entry_time_str = now_dt.strftime("%H:%M:%S")
                now_ts = _now_utc_ts()

                # V1: Delayed both legs (10:01 CALL + 10:01 PUT)
                v1_record = {
                    "cycle_id": cycle_id,
                    "trade_date": today_str,
                    "variant": "V1_delayed_1000",
                    "call_strike": call_strike,
                    "put_strike": put_strike,
                    "call_entry_time": entry_time_str,
                    "call_entry_price": call_ltp,
                    "call_entry_ts": now_ts,
                    "put_entry_time": entry_time_str,
                    "put_entry_price": put_ltp,
                    "put_entry_ts": now_ts,
                    "combined_entry_price": round(call_ltp + put_ltp, 2),
                    "spot_at_call_entry": nifty_ltp,
                    "spot_at_put_entry": nifty_ltp,
                    "nifty_spot_entry": nifty_ltp,
                    "moneyness": STRIKE_MONEYNESS_MODE,
                    "created_at": now_ts,
                }
                insert_timing_shadow_entry(db_path, v1_record)

                entry_ref = active_cycle or (snapshot if "snapshot" in locals() else None) or curr_active or load_active_snapshot() or {}
                spot_0931 = float(entry_ref.get("entry_spot") or entry_ref.get("nifty_open") or nifty_ltp)

                # V2: Staggered (09:31 CALL + 10:01 PUT)
                v2_record = {
                    "cycle_id": cycle_id,
                    "trade_date": today_str,
                    "variant": "V2_staggered_c0931_p1000",
                    "call_strike": call_strike,
                    "put_strike": put_strike,
                    "call_entry_time": "09:31:00",
                    "call_entry_price": call_buy_price,
                    "call_entry_ts": now_ts,
                    "put_entry_time": entry_time_str,
                    "put_entry_price": put_ltp,
                    "put_entry_ts": now_ts,
                    "combined_entry_price": round(call_buy_price + put_ltp, 2),
                    "spot_at_call_entry": spot_0931,
                    "spot_at_put_entry": nifty_ltp,
                    "nifty_spot_entry": nifty_ltp,
                    "moneyness": STRIKE_MONEYNESS_MODE,
                    "created_at": now_ts,
                }
                insert_timing_shadow_entry(db_path, v2_record)

                # V3: Staggered (09:31 PUT + 10:01 CALL)
                v3_record = {
                    "cycle_id": cycle_id,
                    "trade_date": today_str,
                    "variant": "V3_staggered_p0931_c1000",
                    "call_strike": call_strike,
                    "put_strike": put_strike,
                    "call_entry_time": entry_time_str,
                    "call_entry_price": call_ltp,
                    "call_entry_ts": now_ts,
                    "put_entry_time": "09:31:00",
                    "put_entry_price": put_buy_price,
                    "put_entry_ts": now_ts,
                    "combined_entry_price": round(call_ltp + put_buy_price, 2),
                    "spot_at_call_entry": nifty_ltp,
                    "spot_at_put_entry": spot_0931,
                    "nifty_spot_entry": nifty_ltp,
                    "moneyness": STRIKE_MONEYNESS_MODE,
                    "created_at": now_ts,
                }
                insert_timing_shadow_entry(db_path, v3_record)

                # V4: Trend-guided stagger (selects V2 if spot@09:31 >= prev_close else V3)
                is_bullish_morning = spot_0931 >= (nifty_prev_close or spot_0931)
                chosen_stag = v2_record if is_bullish_morning else v3_record
                v4_record = dict(chosen_stag)
                v4_record["variant"] = "V4_trend_stagger"
                insert_timing_shadow_entry(db_path, v4_record)

                logger.info(
                    "  [TIMING SHADOWS RECORDED] V1 (10:00 Delayed): ₹%.2f | V2 (Call@9:30, Put@10:00): ₹%.2f | V3 (Put@9:30, Call@10:00): ₹%.2f | V4 (Trend Stagger - %s): ₹%.2f",
                    v1_record["combined_entry_price"], v2_record["combined_entry_price"], v3_record["combined_entry_price"],
                    "V2" if is_bullish_morning else "V3", v4_record["combined_entry_price"]
                )
    except Exception as shadow_err:
        logger.warning("Failed to record delayed/staggered timing shadows: %s", shadow_err)

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
    logger.info("  [BASE STRATEGY] Strangle Gain/Loss: %s pts", gainloss)
    logger.info("  [ALPHA STRATEGY] State: %s | Call: %s | Put: %s | P&L: %.2f pts (ROI: %.2f%%)",
                updated_fsm.get("state"),
                updated_fsm.get("call_leg", {}).get("status"),
                updated_fsm.get("put_leg", {}).get("status"),
                updated_fsm.get("total_gainloss", 0.0),
                updated_fsm.get("roi_pct", 0.0))
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
    parser.add_argument(
        "--force-static",
        action="store_true",
        help="Force static anchor +/- 100 pt strike rule instead of AI selector",
    )
    parser.add_argument(
        "--force-entry",
        action="store_true",
        help="Bypass Smart Entry Gate deferral and force immediate cycle entry",
    )
    parser.add_argument(
        "--no-alert",
        "--no-email",
        dest="no_alert",
        action="store_true",
        help="Run collection cycle but skip sending peak Discord alerts (dry_run=True)",
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
                "Active cycle: start=%s expiry=%s PUT=%d @ %s CALL=%d @ %s (Mode: %s)",
                active_cycle["week_start_date"],
                active_cycle["expiry_date"],
                active_cycle["put_strike"],
                active_cycle["put_buy_price"],
                active_cycle["call_strike"],
                active_cycle["call_buy_price"],
                active_cycle.get("selection_mode", "UNKNOWN"),
            )
            peak_info = active_cycle.get("peak_profit")
            if peak_info:
                logger.info(
                    "Peak Profit: High-Water Mark=+%.2f%% (+%.2f pts) | Last Notified=%s | Alerts Today=%d",
                    peak_info.get("peak_pnl_pct", 0.0),
                    peak_info.get("peak_pnl_pts", 0.0),
                    f"+{peak_info.get('last_notified_pct', 0.0):.2f}%" if peak_info.get("last_notified_pct") is not None else "None",
                    peak_info.get("alerts_sent_today", 0),
                )
        else:
            logger.info("No valid active cycle snapshot for today")
        return

    if not args.force and not _is_market_time():
        logger.info("Outside market hours. Use --force to override.")
        return

    success = collect_once(
        force_static=args.force_static,
        force_entry=args.force_entry,
        dry_run=args.no_alert,
    )
    if not success:
        logger.error("Collection cycle failed.")
        sys.exit(1)

    logger.info("Collection cycle completed successfully.")


if __name__ == "__main__":
    main()
