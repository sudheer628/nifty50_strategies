"""
Daily CLI & Orchestrator for NIFTY 50 Opening Straddle Strategy (09:14 - 10:00 AM).

Usage:
  # Paper trading mode (default):
  python -m strategies.opening_straddle_selector --dry-run

  # Specify custom DB path:
  python -m strategies.opening_straddle_selector --dry-run --db-path /path/to/opening_straddle.db

  # Historical simulation / backtest replay mode:
  python -m strategies.opening_straddle_selector --simulate-file /path/to/ticks.json
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from common.angelone_client import (
    get_nifty_spot,
    get_nifty_option_chain,
)
from common.expiry import format_expiry_angelone
from strategies.opening_straddle_engine import (
    OpeningStraddleEngine,
    resolve_opening_straddle_expiry,
)
from strategies.opening_straddle_models import (
    PreMarketIntelligence,
    StrategyState,
    StraddleLegQuote,
    TickObservation,
)
from strategies.premarket_intelligence import PreMarketIntelligenceLoader

logger = logging.getLogger("nifty50_strategies.opening_straddle_selector")

IST_TZ = timezone(timedelta(hours=5, minutes=30))
MAX_QUOTE_AGE_SEC = 15.0   # Live tick quote must not be older than 15 seconds
MAX_CLOCK_SKEW_SEC = 2.0   # Max acceptable future timestamp clock skew in seconds


def compute_market_open_wait_sec(now_dt: Optional[datetime] = None) -> float:
    """
    Computes seconds to wait until 09:15:05 AM IST for market open.

    Returns:
        - float seconds (> 0.0) if currently between 09:00:00 and 09:15:05 IST.
        - 0.0 if already at or past 09:15:05 IST, or outside the pre-market window.
    """
    now_dt = now_dt or datetime.now(tz=IST_TZ)
    target_entry = now_dt.replace(hour=9, minute=15, second=5, microsecond=0)
    wait_sec = (target_entry - now_dt).total_seconds()
    if 0 < wait_sec <= 900:  # Within 15 minutes before 09:15:05 AM (09:00 - 09:15:05)
        return wait_sec
    return 0.0


def parse_quote_age_sec(
    feed_time: Any,
    now_ts: Optional[float] = None,
    max_skew_sec: float = MAX_CLOCK_SKEW_SEC,
) -> Optional[float]:
    """
    Computes elapsed age in seconds from broker exchange quote timestamp/epoch.

    Returns:
        - elapsed seconds (>= 0.0) if parsed and within valid time bounds.
        - 0.0 if future timestamp is within max_skew_sec clock-skew allowance.
        - None if feed_time is missing, unparseable, or exceeds clock-skew allowance.
    """
    if feed_time is None:
        return None
    now_ts = now_ts or time.time()
    ts = None
    if isinstance(feed_time, (int, float)):
        ts = feed_time / 1000.0 if feed_time > 1e11 else float(feed_time)
    elif isinstance(feed_time, str):
        feed_str = feed_time.strip()
        if not feed_str:
            return None
        try:
            val = float(feed_str)
            ts = val / 1000.0 if val > 1e11 else val
        except ValueError:
            for fmt in (
                "%d-%b-%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
            ):
                try:
                    dt = datetime.strptime(feed_str.split(".")[0], fmt)
                    dt = dt.replace(tzinfo=IST_TZ)
                    ts = dt.timestamp()
                    break
                except ValueError:
                    pass

    if ts is None:
        return None

    raw_diff = now_ts - ts
    if raw_diff < -max_skew_sec:
        logger.warning(
            "Broker quote timestamp is in the future beyond clock skew allowance (future_by=%.2fs > %.1fs max_skew): %s",
            -raw_diff,
            max_skew_sec,
            feed_time,
        )
        return None
    if -max_skew_sec <= raw_diff < 0.0:
        return 0.0
    return raw_diff


def setup_cli_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] [opening_straddle] %(message)s",
    )


def run_opening_straddle(
    db_path: Optional[str] = None,
    poll_interval_sec: float = 1.0,
    max_duration_sec: int = 2700,  # 45 minutes
    enable_alerts: bool = True,
    allow_synthetic_spread: bool = False,
    wait_for_market_open: bool = True,
    entry_grace_timeout_sec: float = 25.0,
) -> int:
    """
    Main orchestrator running the daily opening straddle cycle from 09:14 to 10:00 AM.
    Operates strictly as an analytical signal, paper-execution, and risk-tracking engine.
    Order placement is completely out of scope.
    """
    logger.info("=" * 65)
    logger.info("Starting NIFTY 50 Opening Straddle Strategy (Tracking Mode)")
    logger.info("=" * 65)

    # 1. Step 1: Pre-Market Multi-Source Ingestion (09:14:00 AM)
    logger.info("Step 1: Running Pre-Market Multi-Source Ingestion Engine...")
    intel_loader = PreMarketIntelligenceLoader()
    intel = intel_loader.load_intelligence()
    logger.info("Pre-Market Summary: %s", intel.summary_str())

    # 2. Step 2: Resolve Target Expiry & Contracts
    target_expiry = resolve_opening_straddle_expiry()
    expiry_angel = format_expiry_angelone(target_expiry)
    logger.info("Target Expiry: %s (Angel Format: %s)", target_expiry.strftime("%Y-%m-%d"), expiry_angel)

    # Wait until 09:15:05 AM IST for market open
    if wait_for_market_open:
        wait_sec = compute_market_open_wait_sec()
        if wait_sec > 0:
            now_ist = datetime.now(tz=IST_TZ)
            logger.info(
                "Pre-market preparation complete at %s IST. Waiting %.1fs until 09:15:05 AM IST for market open...",
                now_ist.strftime("%H:%M:%S"),
                wait_sec,
            )
            time.sleep(wait_sec)

    # 3. Step 3 & 4: Live Spot Discovery, ATM Strike Anchor & Quality Gate (with grace window)
    logger.info("Step 3: Sampling 09:15:05 AM spot price anchor and option quotes...")

    def _is_valid_price(p: Any) -> bool:
        import math
        try:
            val = float(p)
            return math.isfinite(val) and val > 0.0
        except (TypeError, ValueError):
            return False

    entry_start_time = time.time()
    spot_val = 0.0
    atm_strike = 0
    call_leg: Optional[StraddleLegQuote] = None
    put_leg: Optional[StraddleLegQuote] = None
    last_rejection_reasons: List[str] = []

    while (time.time() - entry_start_time) <= entry_grace_timeout_sec:
        spot_res = get_nifty_spot()
        if isinstance(spot_res, dict):
            spot_val = float(spot_res.get("ltp") or 0.0)
        elif spot_res is not None:
            spot_val = float(spot_res)
        else:
            spot_val = 0.0

        if spot_val <= 0:
            if intel.nifty_spot and intel.nifty_spot > 0:
                spot_val = float(intel.nifty_spot)
            else:
                last_rejection_reasons = ["Invalid/missing NIFTY spot price"]
                time.sleep(1.0)
                continue

        atm_strike = int(round(spot_val / 50.0) * 50)
        chain = get_nifty_option_chain(expiry_angel, atm_strike, atm_strike, mode="FULL")
        t_entry_recv = time.time()
        call_info = chain.get("call") or {}
        put_info = chain.get("put") or {}

        ce_ltp = float(call_info.get("ltp") or 0.0)
        pe_ltp = float(put_info.get("ltp") or 0.0)

        if ce_ltp <= 0.0 or pe_ltp <= 0.0:
            last_rejection_reasons = [f"Missing option LTPs (CE={ce_ltp:.2f}, PE={pe_ltp:.2f})"]
            time.sleep(1.0)
            continue

        # Validate opening quote freshness
        spot_entry_age = parse_quote_age_sec(
            spot_res.get("feed_time") if isinstance(spot_res, dict) else None,
            now_ts=t_entry_recv,
            max_skew_sec=MAX_CLOCK_SKEW_SEC,
        )
        ce_entry_age = parse_quote_age_sec(call_info.get("feed_time"), now_ts=t_entry_recv, max_skew_sec=MAX_CLOCK_SKEW_SEC)
        pe_entry_age = parse_quote_age_sec(put_info.get("feed_time"), now_ts=t_entry_recv, max_skew_sec=MAX_CLOCK_SKEW_SEC)

        entry_stale_reasons = []
        if spot_entry_age is None:
            if not allow_synthetic_spread:
                entry_stale_reasons.append("Spot=missing_or_unverifiable_timestamp")
        elif spot_entry_age > MAX_QUOTE_AGE_SEC:
            entry_stale_reasons.append(f"Spot={spot_entry_age:.1f}s")

        if ce_entry_age is None:
            if not allow_synthetic_spread:
                entry_stale_reasons.append("Call=missing_or_unverifiable_timestamp")
        elif ce_entry_age > MAX_QUOTE_AGE_SEC:
            entry_stale_reasons.append(f"Call={ce_entry_age:.1f}s")

        if pe_entry_age is None:
            if not allow_synthetic_spread:
                entry_stale_reasons.append("Put=missing_or_unverifiable_timestamp")
        elif pe_entry_age > MAX_QUOTE_AGE_SEC:
            entry_stale_reasons.append(f"Put={pe_entry_age:.1f}s")

        if entry_stale_reasons:
            last_rejection_reasons = entry_stale_reasons
            elapsed = time.time() - entry_start_time
            if (time.time() - entry_start_time) < entry_grace_timeout_sec:
                logger.info(
                    "Opening quotes not yet fresh (%s). Awaiting live ticks (elapsed %.1fs/%.0fs)...",
                    ", ".join(entry_stale_reasons),
                    elapsed,
                    entry_grace_timeout_sec,
                )
                time.sleep(1.5)
            continue

        raw_ce_bid = call_info.get("bid")
        raw_ce_ask = call_info.get("ask")
        raw_pe_bid = put_info.get("bid")
        raw_pe_ask = put_info.get("ask")

        has_depth = (
            _is_valid_price(raw_ce_bid)
            and _is_valid_price(raw_ce_ask)
            and float(raw_ce_ask) >= float(raw_ce_bid)
            and _is_valid_price(raw_pe_bid)
            and _is_valid_price(raw_pe_ask)
            and float(raw_pe_ask) >= float(raw_pe_bid)
        )

        if not has_depth:
            if allow_synthetic_spread:
                logger.warning(
                    "Opening market depth quotes unavailable from broker (CE bid=%s, ask=%s; PE bid=%s, ask=%s). "
                    "Synthesizing 0.4%% spread around LTP because --allow-synthetic-spread is enabled.",
                    raw_ce_bid, raw_ce_ask, raw_pe_bid, raw_pe_ask
                )
                ce_bid = ce_ltp * 0.998
                ce_ask = ce_ltp * 1.002
                pe_bid = pe_ltp * 0.998
                pe_ask = pe_ltp * 1.002
            else:
                last_rejection_reasons = [f"Awaiting depth (CE bid={raw_ce_bid}, ask={raw_ce_ask}; PE bid={raw_pe_bid}, ask={raw_pe_ask})"]
                elapsed = time.time() - entry_start_time
                if (time.time() - entry_start_time) < entry_grace_timeout_sec:
                    logger.info(
                        "Opening depth not yet formed (%s). Retrying (elapsed %.1fs/%.0fs)...",
                        last_rejection_reasons[0],
                        elapsed,
                        entry_grace_timeout_sec,
                    )
                    time.sleep(1.5)
                continue
        else:
            ce_bid = float(raw_ce_bid)
            ce_ask = float(raw_ce_ask)
            pe_bid = float(raw_pe_bid)
            pe_ask = float(raw_pe_ask)

        ce_token = str(call_info.get("token") or "")
        pe_token = str(put_info.get("token") or "")
        ce_symbol = str(call_info.get("trading_symbol") or f"NIFTY{atm_strike}CE")
        pe_symbol = str(put_info.get("trading_symbol") or f"NIFTY{atm_strike}PE")

        call_leg = StraddleLegQuote(
            symbol=ce_symbol,
            token=ce_token,
            strike=atm_strike,
            option_type="CE",
            bid=ce_bid,
            ask=ce_ask,
            ltp=ce_ltp,
        )
        put_leg = StraddleLegQuote(
            symbol=pe_symbol,
            token=pe_token,
            strike=atm_strike,
            option_type="PE",
            bid=pe_bid,
            ask=pe_ask,
            ltp=pe_ltp,
        )
        logger.info(
            "Acquired fresh opening quotes and valid depth for ATM strike %d at +%.2fs (Spot=%.2f, CE ltp=%.2f, PE ltp=%.2f).",
            atm_strike,
            time.time() - entry_start_time,
            spot_val,
            ce_ltp,
            pe_ltp,
        )
        break

    if call_leg is None or put_leg is None:
        logger.error(
            "Failed to acquire fresh opening quotes within %.0fs grace window (%s). Aborting entry.",
            entry_grace_timeout_sec,
            ", ".join(last_rejection_reasons or ["Unknown error"]),
        )
        return 1

    engine = OpeningStraddleEngine(
        pre_market=intel,
        trade_date=datetime.now().strftime("%Y-%m-%d"),
        expiry_date=target_expiry.strftime("%Y-%m-%d"),
        db_path=db_path,
        enable_alerts=enable_alerts,
    )

    if not engine.arm_entry(spot_price=spot_val, call_quote=call_leg, put_quote=put_leg):
        logger.warning("Opening Straddle Entry Gate rejected or aborted (Status=%s).", engine.status)
        return 1

    # 5. Step 5: Live Observation & Decoupling Loop (1 Hz polling)
    logger.info("Step 5: Entering high-frequency observation loop...")
    start_time = time.time()
    try:
        while engine.state not in (StrategyState.CLOSED, StrategyState.ABORTED):
            loop_now = time.time()
            if (loop_now - start_time) > max_duration_sec:
                logger.info("Session max duration exceeded. Forcing hard time exit.")
                break

            # Fetch fresh quotes with freshness & latency tracking
            t_req_start = time.time()
            fresh_spot_res = get_nifty_spot()
            raw_spot = None
            if isinstance(fresh_spot_res, dict):
                raw_spot = fresh_spot_res.get("ltp")
            elif fresh_spot_res is not None:
                raw_spot = fresh_spot_res

            if not _is_valid_price(raw_spot):
                logger.warning("Invalid or non-finite NIFTY spot quote received (%s). Skipping tick.", raw_spot)
                time.sleep(poll_interval_sec)
                continue
            curr_spot = float(raw_spot)

            chain_tick = get_nifty_option_chain(expiry_angel, atm_strike, atm_strike, mode="FULL")
            t_recv = time.time()
            latency_sec = t_recv - t_req_start
            if latency_sec > 5.0:
                logger.warning("Quote API round-trip latency exceeded threshold (%.2fs > 5.0s). Skipping stale tick.", latency_sec)
                time.sleep(poll_interval_sec)
                continue

            c_info = chain_tick.get("call") or {}
            p_info = chain_tick.get("put") or {}

            raw_c_ltp = c_info.get("ltp")
            raw_p_ltp = p_info.get("ltp")
            if not _is_valid_price(raw_c_ltp) or not _is_valid_price(raw_p_ltp):
                logger.warning("Option LTP missing/invalid on live tick (CE=%s, PE=%s). Skipping tick.", raw_c_ltp, raw_p_ltp)
                time.sleep(poll_interval_sec)
                continue

            c_ltp = float(raw_c_ltp)
            p_ltp = float(raw_p_ltp)

            # Validate underlying market data freshness via broker feed timestamps
            spot_age = parse_quote_age_sec(
                fresh_spot_res.get("feed_time") if isinstance(fresh_spot_res, dict) else None,
                now_ts=t_recv,
                max_skew_sec=MAX_CLOCK_SKEW_SEC,
            )
            c_age = parse_quote_age_sec(c_info.get("feed_time"), now_ts=t_recv, max_skew_sec=MAX_CLOCK_SKEW_SEC)
            p_age = parse_quote_age_sec(p_info.get("feed_time"), now_ts=t_recv, max_skew_sec=MAX_CLOCK_SKEW_SEC)

            stale_reasons = []
            if spot_age is None:
                if not allow_synthetic_spread:
                    stale_reasons.append("Spot=missing_or_unverifiable_timestamp")
            elif spot_age > MAX_QUOTE_AGE_SEC:
                stale_reasons.append(f"Spot={spot_age:.1f}s")

            if c_age is None:
                if not allow_synthetic_spread:
                    stale_reasons.append("Call=missing_or_unverifiable_timestamp")
            elif c_age > MAX_QUOTE_AGE_SEC:
                stale_reasons.append(f"Call={c_age:.1f}s")

            if p_age is None:
                if not allow_synthetic_spread:
                    stale_reasons.append("Put=missing_or_unverifiable_timestamp")
            elif p_age > MAX_QUOTE_AGE_SEC:
                stale_reasons.append(f"Put={p_age:.1f}s")

            if stale_reasons:
                logger.warning(
                    "Stale or unverified market data detected (%s). Skipping tick.",
                    ", ".join(stale_reasons),
                )
                time.sleep(poll_interval_sec)
                continue

            max_observed_age = max(
                (a for a in (spot_age, c_age, p_age) if a is not None),
                default=None,
            )

            # Strict Bid/Ask market depth validation
            raw_c_bid = c_info.get("bid")
            raw_c_ask = c_info.get("ask")
            raw_p_bid = p_info.get("bid")
            raw_p_ask = p_info.get("ask")

            has_live_depth = (
                _is_valid_price(raw_c_bid)
                and _is_valid_price(raw_c_ask)
                and float(raw_c_ask) >= float(raw_c_bid)
                and _is_valid_price(raw_p_bid)
                and _is_valid_price(raw_p_ask)
                and float(raw_p_ask) >= float(raw_p_bid)
            )

            if not has_live_depth:
                if allow_synthetic_spread:
                    c_bid = c_ltp * 0.998
                    c_ask = c_ltp * 1.002
                    p_bid = p_ltp * 0.998
                    p_ask = p_ltp * 1.002
                else:
                    logger.warning(
                        "Live tick missing or invalid bid/ask depth (CE bid=%s, ask=%s; PE bid=%s, ask=%s). Skipping tick.",
                        raw_c_bid, raw_c_ask, raw_p_bid, raw_p_ask,
                    )
                    time.sleep(poll_interval_sec)
                    continue
            else:
                c_bid = float(raw_c_bid)
                c_ask = float(raw_c_ask)
                p_bid = float(raw_p_bid)
                p_ask = float(raw_p_ask)

            tick = TickObservation(
                ts=t_recv,
                nifty_spot=curr_spot,
                call_bid=c_bid,
                call_ask=c_ask,
                put_bid=p_bid,
                put_ask=p_ask,
                call_ltp=c_ltp,
                put_ltp=p_ltp,
                quote_age_sec=max_observed_age,
            )

            engine.process_tick(tick)
            time.sleep(poll_interval_sec)

    except KeyboardInterrupt:
        logger.info("Execution interrupted by user. Flushing buffered ticks and session...")
        engine.flush_interrupted_session()
        return 0

    logger.info("Session complete: State=%s, Status=%s", engine.state.value, engine.status)
    return 0


def print_session_report(db_path: Optional[str] = None) -> int:
    """Prints recent sessions recorded in opening_straddle.db."""
    default_db = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "opening_straddle.db"
    )
    target_db = db_path or os.getenv("OPENING_STRADDLE_DB", default_db)
    if not os.path.isfile(target_db):
        print(f"Opening straddle database not found at: {target_db}")
        return 0

    import sqlite3
    with sqlite3.connect(target_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT trade_date, atm_strike, bias_classification, selected_leg,
                   net_pnl_inr, net_roi_pct, peak_roi_pct, exit_reason, status
            FROM opening_straddle_sessions
            ORDER BY trade_date DESC LIMIT 30
            """
        ).fetchall()

    if not rows:
        print(f"No sessions found in {target_db}")
        return 0

    print("=" * 88)
    print(" NIFTY 50 OPENING STRADDLE - HISTORICAL SESSIONS")
    print("=" * 88)
    header = f"{'Date':<11} | {'ATM':<6} | {'Prior Bias':<14} | {'Leg':<8} | {'Net P&L':>11} | {'ROI %':>8} | {'Peak %':>8} | {'Exit Reason':<14}"
    print(header)
    print("-" * 88)
    for r in rows:
        pnl_str = f"INR {r['net_pnl_inr']:+,.2f}" if r["net_pnl_inr"] is not None else "N/A"
        roi_str = f"{r['net_roi_pct']:+.2f}%" if r["net_roi_pct"] is not None else "N/A"
        peak_str = f"{r['peak_roi_pct']:+.2f}%" if r["peak_roi_pct"] is not None else "N/A"
        bias = (r["bias_classification"] or "N/A")[:14]
        leg = (r["selected_leg"] or "N/A")[:8]
        exit_r = (r["exit_reason"] or "N/A")[:14]
        print(f"{r['trade_date']:<11} | {r['atm_strike']:<6} | {bias:<14} | {leg:<8} | {pnl_str:>11} | {roi_str:>8} | {peak_str:>8} | {exit_r:<14}")
    print("=" * 88)
    return 0


def main():
    parser = argparse.ArgumentParser(description="NIFTY 50 Opening Straddle Strategy [Paper Trading / Virtual Tracking] (09:15 - 10:00 AM)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Tracking mode (order placement is out of scope)")
    parser.add_argument("--allow-synthetic-spread", action="store_true", help="Allow synthetic spread around LTP if market depth is unavailable (testing only)")
    parser.add_argument("--no-wait", action="store_true", help="Bypass wait until 09:15:05 AM IST (for manual testing/off-hours run)")
    parser.add_argument("--db-path", type=str, help="Custom SQLite database path")
    parser.add_argument("--no-alerts", action="store_true", help="Disable Discord webhook notifications")
    parser.add_argument("--report", action="store_true", help="Print recent strategy sessions from database and exit")
    parser.add_argument("--send-report", action="store_true", help="Send Discord completion report to DISCORD_OPENING_STRADDLE")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    if args.report:
        return print_session_report(db_path=args.db_path)

    if args.send_report:
        from strategies.opening_straddle_alerts import send_daily_session_report_from_db
        setup_cli_logging(args.log_level)
        success = send_daily_session_report_from_db(db_path=args.db_path)
        if success:
            logger.info("Opening straddle completion report dispatched to DISCORD_OPENING_STRADDLE.")
            return 0
        else:
            logger.warning("Could not dispatch Discord report (check DISCORD_OPENING_STRADDLE in .env).")
            return 1

    setup_cli_logging(args.log_level)
    return run_opening_straddle(
        db_path=args.db_path,
        enable_alerts=not args.no_alerts,
        allow_synthetic_spread=args.allow_synthetic_spread,
        wait_for_market_open=not args.no_wait,
    )


if __name__ == "__main__":
    sys.exit(main())
