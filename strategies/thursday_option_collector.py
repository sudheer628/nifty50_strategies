"""
Thursday-to-Monday 3-day option data collection strategy for NIFTY50.

Parallel strategy track:
    - Runs only on Thursday, Friday, and Monday during market hours (:02 and :32).
    - Exits cleanly as a no-op on Tuesday and Wednesday (reserved for master weekly track).
    - Thursday, ~9:31 AM IST: A fresh 3-day cycle begins targeting the active weekly expiry
      CALL/PUT strikes are selected from Thursday's first-trigger NIFTY LTP, and buy prices are captured.
      The previous 3-day snapshot is archived; the new one becomes active in current_thursday_buy.json.
    - Friday through Monday: Collection continues for this 3-day cycle using the Thursday buy prices.

All strike selection, entry-gate, FSM, and peak-profit logic is reused 1:1 from the tested
master modules (ai_strike_selector, entry_gate, fsm_strategy, profit_monitor) using their
exact published call contracts.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, date, time, timedelta
from typing import Optional, Dict, Any

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytz

from config import (
    logger,
    STRATEGY_NAME_THURSDAY,
    SQLITE_DIR,
    THURSDAY_SNAPSHOT_FILE,
    ACTIVE_SNAPSHOT_FILE,
)

from common.expiry import (
    get_next_weekly_expiry,
    format_expiry_angelone,
    format_expiry_file,
)

from common.calendar_utils import (
    check_nse_holiday,
    is_thursday_strategy_start_day,
    is_thursday_strategy_active_day,
    is_strategy_closing_day,
)

from common.ai_strike_selector import select_strikes

from common.entry_gate import evaluate_entry_gate

from common.angelone_client import (
    get_nifty_spot,
    get_nifty_option_chain,
)

from common.storage import (
    build_thursday_db_path,
    init_db,
    insert_record,
    insert_buy_snapshot,
    save_active_snapshot,
    load_active_snapshot,
    update_active_fsm_state,
    insert_gate_deferred_shadow,
)

from common.fsm_strategy import (
    init_fsm_state,
    evaluate_fsm_tick,
    estimate_option_delta,
)

from common.profit_monitor import evaluate_peak_profit

IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.UTC

PEAK_TRACK_LABEL = "3-Day Thursday Strategy"


def _now_ist() -> datetime:
    """Return the current datetime in IST."""
    return datetime.now(IST)


def _now_utc_ts() -> int:
    """Return the current UTC timestamp as Unix epoch integer (seconds)."""
    return int(datetime.now(UTC).timestamp())


def _today_ist() -> date:
    """Return today's date in IST."""
    return _now_ist().date()


def _is_market_time() -> bool:
    """Return True if the current IST time is between 9:15 AM and 3:35 PM on a weekday."""
    now = _now_ist()
    if now.weekday() >= 5:
        return False
    market_open = time(9, 15)
    market_close = time(15, 35)
    return market_open <= now.time() <= market_close


def _ensure_thursday_db(week_start: date, expiry_date: date) -> str:
    """Create (if needed) and return the DB path for a 3-day Thursday cycle."""
    start_str = format_expiry_file(week_start)
    expiry_str = format_expiry_file(expiry_date)
    db_path = build_thursday_db_path(start_str, expiry_str)
    init_db(db_path)
    return db_path


def _parse_file_date(value: str) -> date:
    """Parse a snapshot date stored in YYYYMMDD format."""
    return datetime.strptime(value, "%Y%m%d").date()


def _thursday_cycle_start_for_day(d: date) -> date:
    """Return the Thursday start date for the cycle containing d."""
    if is_thursday_strategy_start_day(d):
        return d
    # If Friday or Monday, walk back to most recent Thursday
    days_since_thu = (d.weekday() - 3) % 7
    thu = d - timedelta(days=days_since_thu)
    if check_nse_holiday(thu):
        fri = thu + timedelta(days=1)
        if fri <= d:
            return fri
    return thu


def _active_cycle(snapshot: dict, today: date) -> dict:
    """Validate and normalize a snapshot that may be active today for the Thursday track."""
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
        logger.warning("[Thursday Track] Active snapshot is missing required cycle fields")
        return {}

    try:
        week_start = _parse_file_date(str(snapshot["week_start_date"]))
        expiry_date = _parse_file_date(str(snapshot["expiry_date"]))
        call_strike = int(snapshot["call_strike"])
        put_strike = int(snapshot["put_strike"])
    except (TypeError, ValueError):
        logger.warning("[Thursday Track] Active snapshot contains invalid dates or strikes")
        return {}

    if snapshot.get("status") == "closed":
        return {}

    # If today is a new Thursday start day, prior cycle is retired
    if is_thursday_strategy_start_day(today) and week_start != today:
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
    """Build a standard record dictionary for Thursday track."""
    record = {
        "strategy_name": STRATEGY_NAME_THURSDAY,
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
    Perform one data collection cycle for the 3-day Thursday track.

    Active only on Thursday, Friday, and Monday (or deferred Tuesday close).
    Clean no-op on Tuesday/Wednesday.
    """
    today = _today_ist()

    # Gate 1: Check market holiday or weekend
    if check_nse_holiday(today):
        logger.info("[Thursday Track] Today (%s) is an NSE market holiday or weekend. Skipping.", today)
        return True

    # Gate 2: Check active operating days for Thursday track (Thu, Fri, Mon, or deferred Tue close)
    if not is_thursday_strategy_active_day(today):
        logger.info("[Thursday Track] Today (%s, %s) is not an active day for the 3-day Thursday track (Thu-Mon only). Skipping.",
                    today, today.strftime("%A"))
        return True

    is_first_run_of_week = is_thursday_strategy_start_day(today)
    today_str = format_expiry_file(today)

    active = load_active_snapshot(filepath=THURSDAY_SNAPSHOT_FILE)
    active_cycle = _active_cycle(active, today)

    # ------------------------------------------------------------------
    # Determine expiry, strikes, and spot data
    # ------------------------------------------------------------------
    if active_cycle:
        expiry_date = active_cycle["expiry"]
        week_start = active_cycle["week_start"]
    else:
        # Check master snapshot for expiry alignment if available
        master_snap = load_active_snapshot(filepath=ACTIVE_SNAPSHOT_FILE)
        expiry_date = get_next_weekly_expiry(today)
        if master_snap and master_snap.get("expiry_date"):
            try:
                master_expiry = _parse_file_date(str(master_snap["expiry_date"]))
                if master_expiry >= today:
                    expiry_date = master_expiry
            except (TypeError, ValueError):
                pass
        week_start = _thursday_cycle_start_for_day(today)

    expiry_angelone = format_expiry_angelone(expiry_date)
    expiry_file = format_expiry_file(expiry_date)
    start_str = format_expiry_file(week_start)

    logger.info("[Thursday Track] Fetching NIFTY50 spot data...")
    spot = get_nifty_spot()
    if not spot:
        logger.error("[Thursday Track] Failed to fetch NIFTY spot data; aborting.")
        return False

    nifty_ltp = float(spot.get("ltp", 0))
    nifty_open = float(spot.get("open", 0))
    nifty_prev_close = float(spot.get("close", 0))

    if nifty_ltp <= 0 or nifty_open <= 0:
        logger.error("[Thursday Track] Invalid NIFTY spot data: LTP=%.2f, Open=%.2f; aborting.",
                     nifty_ltp, nifty_open)
        return False

    logger.info("[Thursday Track] NIFTY LTP=%.2f  Open=%.2f  PrevClose=%.2f",
                nifty_ltp, nifty_open, nifty_prev_close)

    if active_cycle:
        put_strike = active_cycle["put_strike"]
        call_strike = active_cycle["call_strike"]
        selection_mode = active_cycle.get("selection_mode", "REUSED")
        logger.info(
            "[Thursday Track] Reusing cycle strikes from %s: PUT=%d  CALL=%d (Mode: %s)",
            active_cycle["week_start_date"], put_strike, call_strike, selection_mode
        )
    else:
        # Safety: never start a brand-new 3-day cycle on the closing day (Monday, or
        # the deferred Tuesday when Monday was a holiday). A 1-DTE strangle entry has
        # no runway; the cycle is over and run_thursday_close.py owns the wrap-up.
        if is_strategy_closing_day(today):
            logger.warning(
                "[Thursday Track] No active 3-day snapshot on closing day (%s); skipping fresh entry. "
                "Cycle will be closed by run_thursday_close.py.", today
            )
            return True

        # --- Smart Entry Gate on fresh cycle entry (start day only) ---
        gate_decision = None
        if is_first_run_of_week and not force_entry:
            gate_decision = evaluate_entry_gate(
                reference_ltp=nifty_ltp,
                sqlite_dir=SQLITE_DIR,
            )
            logger.info("[Thursday Track] Smart Entry Gate: %s (reason: %s)",
                        gate_decision.status, gate_decision.defer_reason)
            logger.info(
                "  Metrics: IV Slope=%s | VWAP Dist=%s%% | ADX=%s | Range=%s pts",
                f"{gate_decision.iv_slope:+.2f}" if gate_decision.iv_slope is not None else "N/A",
                f"{gate_decision.vwap_distance:+.3f}" if gate_decision.vwap_distance is not None else "N/A",
                f"{gate_decision.adx_14:.1f}" if gate_decision.adx_14 is not None else "N/A",
                f"{gate_decision.opening_range:.1f}" if gate_decision.opening_range is not None else "N/A",
            )

            if not gate_decision.should_enter:
                logger.warning(
                    "[Thursday Track] Smart Entry Gate DEFERRED trade entry: %s. "
                    "Writing shadow record and retrying next interval.",
                    gate_decision.defer_reason
                )
                # Counterfactual Shadow Entry: record hypothetical AI strikes + LTPs
                # (mirrors the master track's gate_deferred_shadows logic).
                try:
                    (
                        hyp_put,
                        hyp_call,
                        _hyp_static_put,
                        _hyp_static_call,
                        hyp_mode,
                        _hyp_rationale,
                        hyp_comp_score,
                        hyp_dir_bias,
                        hyp_c_delta,
                        hyp_p_delta,
                    ) = select_strikes(
                        reference_ltp=nifty_ltp,
                        sqlite_dir=SQLITE_DIR,
                        force_static=force_static,
                    )
                    hyp_chain = get_nifty_option_chain(expiry_angelone, hyp_call, hyp_put)
                    hyp_call_ltp = (hyp_chain.get("call") or {}).get("ltp")
                    hyp_put_ltp = (hyp_chain.get("put") or {}).get("ltp")

                    shadow_record = {
                        "timestamp": _now_utc_ts(),
                        "trade_date": today_str,
                        "nifty_spot": nifty_ltp,
                        "gate_status": gate_decision.status,
                        "defer_reason": gate_decision.defer_reason,
                        "iv_slope": gate_decision.iv_slope,
                        "vwap_distance": gate_decision.vwap_distance,
                        "adx_14": gate_decision.adx_14,
                        "opening_range": gate_decision.opening_range,
                        "hypothetical_call_strike": hyp_call,
                        "hypothetical_put_strike": hyp_put,
                        "hypothetical_call_ltp": hyp_call_ltp,
                        "hypothetical_put_ltp": hyp_put_ltp,
                        "selection_mode": hyp_mode,
                        "composite_direction_score": hyp_comp_score,
                        "directional_bias": hyp_dir_bias,
                        "target_call_delta": hyp_c_delta,
                        "target_put_delta": hyp_p_delta,
                    }
                    insert_gate_deferred_shadow(
                        _ensure_thursday_db(week_start, expiry_date),
                        shadow_record,
                    )
                    logger.info(
                        "  [COUNTERFACTUAL SHADOW LOGGED] Hyp CALL %d @ ₹%s | PUT %d @ ₹%s",
                        hyp_call, f"{hyp_call_ltp:.2f}" if hyp_call_ltp else "N/A",
                        hyp_put, f"{hyp_put_ltp:.2f}" if hyp_put_ltp else "N/A",
                    )
                except Exception as shadow_err:
                    logger.warning(
                        "[Thursday Track] Failed to record counterfactual gate shadow: %s", shadow_err
                    )
                return True

        # --- Dynamic AI strike selection (fail-safe static fallback inside select_strikes) ---
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
        )
        logger.info(
            "[Thursday Track] Selected strikes from LTP %.2f [%s]: PUT=%d (Delta=%.2f)  CALL=%d (Delta=%.2f) "
            "| Bias=%s (Score=%.2f) (Static Benchmark: PUT=%d CALL=%d)",
            nifty_ltp, selection_mode, put_strike, target_put_delta, call_strike, target_call_delta,
            directional_bias, composite_score, static_put_strike, static_call_strike
        )

    # ------------------------------------------------------------------
    # Fetch option LTPs for locked strikes (single batched quote call)
    # ------------------------------------------------------------------
    logger.info("[Thursday Track] Fetching option LTPs for CALL %d and PUT %d (expiry %s)...",
                call_strike, put_strike, expiry_angelone)
    option_data = get_nifty_option_chain(expiry_angelone, call_strike, put_strike)
    call_ltp = (option_data.get("call") or {}).get("ltp")
    put_ltp = (option_data.get("put") or {}).get("ltp")

    if call_ltp is None or put_ltp is None or call_ltp <= 0 or put_ltp <= 0:
        logger.error("[Thursday Track] Failed to fetch valid option LTPs: CALL %d=%s, PUT %d=%s; aborting.",
                     call_strike, call_ltp, put_strike, put_ltp)
        return False

    logger.info("[Thursday Track] PUT %d LTP=%.2f  CALL %d LTP=%.2f",
                put_strike, put_ltp, call_strike, call_ltp)

    # ------------------------------------------------------------------
    # Cycle identity: reuse active cycle or lock a fresh one
    # ------------------------------------------------------------------
    db_path = _ensure_thursday_db(week_start, expiry_date)

    if active_cycle:
        cycle_id = active_cycle.get("cycle_id", f"CYCLE-THU-{start_str}")
        call_buy_price = active_cycle.get("call_buy_price")
        put_buy_price = active_cycle.get("put_buy_price")
        insert_buy_snapshot(db_path, active_cycle)
    else:
        cycle_id = f"CYCLE-THU-{start_str}"
        call_buy_price = call_ltp
        put_buy_price = put_ltp

        snapshot = {
            "strategy_name": STRATEGY_NAME_THURSDAY,
            "cycle_id": cycle_id,
            "week_start_date": start_str,
            "expiry_date": expiry_file,
            "call_strike": call_strike,
            "put_strike": put_strike,
            "call_buy_price": call_buy_price,
            "put_buy_price": put_buy_price,
            "captured_at": _now_utc_ts(),
            "static_call_strike": static_call_strike,
            "static_put_strike": static_put_strike,
            "selection_mode": selection_mode,
            "selection_rationale": selection_rationale,
            "composite_direction_score": composite_score,
            "directional_bias": directional_bias,
            "target_call_delta": target_call_delta,
            "target_put_delta": target_put_delta,
            "smart_gate": (
                gate_decision.to_dict()
                if gate_decision is not None
                else {"status": "MIDWEEK_FALLBACK", "should_enter": True}
            ),
            "alpha_fsm": init_fsm_state(
                call_strike=call_strike,
                put_strike=put_strike,
                call_buy_price=call_buy_price,
                put_buy_price=put_buy_price,
                entry_ts=_now_utc_ts(),
                composite_score=composite_score,
            ),
        }

        # Archives the previous Thursday snapshot (if any) before overwriting
        save_active_snapshot(snapshot, filepath=THURSDAY_SNAPSHOT_FILE)
        insert_buy_snapshot(db_path, snapshot)
        logger.info(
            "[Thursday Track] Cycle %s locked with buy prices: CALL=%d (₹%.2f)  PUT=%d (₹%.2f)",
            cycle_id, call_strike, call_buy_price, put_strike, put_buy_price
        )

    # Fallback: if buy prices are still None, use current LTP
    if call_buy_price is None:
        call_buy_price = call_ltp
    if put_buy_price is None:
        put_buy_price = put_ltp

    prices = (call_ltp, call_buy_price, put_ltp, put_buy_price)
    if all(price is not None for price in prices):
        gainloss = round(
            (float(call_ltp) - float(call_buy_price))
            + (float(put_ltp) - float(put_buy_price)),
            2,
        )
    else:
        gainloss = None

    # ------------------------------------------------------------------
    # Evaluate Decoupled FSM Strategy (Alpha) — mirrors master flow
    # ------------------------------------------------------------------
    curr_active = load_active_snapshot(filepath=THURSDAY_SNAPSHOT_FILE)
    fsm_state = curr_active.get("alpha_fsm")
    if not fsm_state:
        fsm_state = init_fsm_state(
            call_strike=call_strike,
            put_strike=put_strike,
            call_buy_price=call_buy_price,
            put_buy_price=put_buy_price,
            entry_ts=_now_utc_ts(),
        )

    days_to_expiry = max(0.1, (expiry_date - today).days)
    call_delta = estimate_option_delta(nifty_ltp, call_strike, days_to_expiry, is_call=True)
    put_delta = estimate_option_delta(nifty_ltp, put_strike, days_to_expiry, is_call=False)

    updated_fsm, fsm_events = evaluate_fsm_tick(
        fsm_state=fsm_state,
        current_call_ltp=call_ltp,
        current_put_ltp=put_ltp,
        current_ts=_now_utc_ts(),
        days_to_expiry=days_to_expiry,
        call_delta=call_delta,
        put_delta=put_delta,
    )

    if fsm_events:
        for ev in fsm_events:
            logger.info("[Thursday Track] 🔔 FSM Event: %s", ev)
    update_active_fsm_state(updated_fsm, filepath=THURSDAY_SNAPSHOT_FILE)

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
    # Persist hourly record into Thursday SQLite DB
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

    # ------------------------------------------------------------------
    # Evaluate Peak-Profit High-Water Mark & Alert Trigger (labeled)
    # ------------------------------------------------------------------
    try:
        latest_snapshot = load_active_snapshot(filepath=THURSDAY_SNAPSHOT_FILE)
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
            snapshot_filepath=THURSDAY_SNAPSHOT_FILE,
            track_label=PEAK_TRACK_LABEL,
        )
        cost = (call_buy_price or 0.0) + (put_buy_price or 0.0)
        curr_pnl_pts = round(
            (call_ltp - (call_buy_price or call_ltp)) + (put_ltp - (put_buy_price or put_ltp)), 2
        )
        curr_pnl_pct = round((curr_pnl_pts / cost) * 100.0, 2) if cost > 0 else 0.0
        logger.info(
            "[Thursday Track PEAK MONITOR] %s (Current: %+0.2f%% / %+0.2f pts | Peak: %+0.2f%% / %+0.2f pts | Alerts Today: %d)",
            peak_msg,
            curr_pnl_pct,
            curr_pnl_pts,
            peak_state.get("peak_pnl_pct", 0.0),
            peak_state.get("peak_pnl_pts", 0.0),
            peak_state.get("alerts_sent_today", 0),
        )
    except Exception as peak_err:
        logger.warning("[Thursday Track] Failed to evaluate peak profit: %s", peak_err)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    logger.info("=" * 50)
    logger.info("[Thursday Track] Collection complete  %s", _now_ist().strftime("%Y-%m-%d %H:%M IST"))
    logger.info("  NIFTY LTP: %.2f  |  Open: %.2f", nifty_ltp, nifty_open)
    logger.info("  CALL %d  LTP=%s  BuyPrice=%s", call_strike, call_ltp, call_buy_price)
    logger.info("  PUT  %d  LTP=%s  BuyPrice=%s", put_strike, put_ltp, put_buy_price)
    logger.info("  [BASE STRATEGY] Strangle Gain/Loss: %s pts", gainloss)
    logger.info("  [ALPHA STRATEGY] State: %s | P&L: %.2f pts (ROI: %.2f%%)",
                updated_fsm.get("state"),
                updated_fsm.get("total_gainloss", 0.0),
                updated_fsm.get("roi_pct", 0.0))
    logger.info("=" * 50)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect NIFTY50 options data for Thursday 3-day track")
    parser.add_argument("--force-static", action="store_true", help="Bypass AI strike selection; use static rule")
    parser.add_argument("--force-entry", action="store_true", help="Bypass Smart Entry Gate deferral")
    parser.add_argument("--dry-run", action="store_true", help="Do not dispatch external alerts")
    args = parser.parse_args()

    ok = collect_once(force_static=args.force_static, force_entry=args.force_entry, dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
