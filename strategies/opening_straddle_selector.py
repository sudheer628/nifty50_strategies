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
from datetime import datetime, timezone
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


def setup_cli_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] [opening_straddle] %(message)s",
    )


def run_opening_straddle(
    dry_run: bool = True,
    db_path: Optional[str] = None,
    poll_interval_sec: float = 1.0,
    max_duration_sec: int = 2700,  # 45 minutes
) -> int:
    """
    Main orchestrator running the daily opening straddle cycle from 09:14 to 10:00 AM.
    """
    logger.info("=" * 65)
    logger.info("Starting NIFTY 50 Opening Straddle Selector [DryRun=%s]", dry_run)
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

    # 3. Step 3: Spot Discovery & ATM Strike Anchor (09:15:05 AM)
    logger.info("Step 3: Sampling 09:15:05 AM spot price anchor...")
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
            logger.info("Using feed cache spot fallback: %.2f", spot_val)
        else:
            logger.error("Unable to obtain valid Nifty spot price. Aborting session.")
            return 1

    atm_strike = int(round(spot_val / 50.0) * 50)
    logger.info("Spot Anchor: %.2f -> ATM Strike: %d", spot_val, atm_strike)

    # 4. Step 4: Resolve Option Chain Quotes & Execution Quality Gate
    logger.info("Step 4: Fetching option chain quotes for %s strike %d...", expiry_angel, atm_strike)
    chain = get_nifty_option_chain(expiry_angel, atm_strike, atm_strike)
    call_info = chain.get("call") or {}
    put_info = chain.get("put") or {}

    ce_ltp = float(call_info.get("ltp") or 0.0)
    pe_ltp = float(put_info.get("ltp") or 0.0)

    if ce_ltp <= 0.0 or pe_ltp <= 0.0:
        logger.error(
            "Failed to fetch option LTPs for strike %d (CE=%.2f, PE=%.2f). Aborting.",
            atm_strike,
            ce_ltp,
            pe_ltp,
        )
        return 1

    ce_token = str(call_info.get("token") or "")
    pe_token = str(put_info.get("token") or "")
    ce_symbol = str(call_info.get("trading_symbol") or f"NIFTY{atm_strike}CE")
    pe_symbol = str(put_info.get("trading_symbol") or f"NIFTY{atm_strike}PE")

    # Bid/Ask from chain (or fallback to tight spread around LTP if depth unavailable)
    ce_bid = float(call_info.get("bid") or (ce_ltp * 0.998))
    ce_ask = float(call_info.get("ask") or (ce_ltp * 1.002))
    pe_bid = float(put_info.get("bid") or (pe_ltp * 0.998))
    pe_ask = float(put_info.get("ask") or (pe_ltp * 1.002))

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

    engine = OpeningStraddleEngine(
        pre_market=intel,
        trade_date=datetime.now().strftime("%Y-%m-%d"),
        expiry_date=target_expiry.strftime("%Y-%m-%d"),
        db_path=db_path,
    )

    if not engine.arm_entry(spot_price=spot_val, call_quote=call_leg, put_quote=put_leg):
        logger.warning("Opening Straddle Entry Gate rejected or aborted.")
        return 1

    # 5. Step 5: Live Observation & Decoupling Loop (1 Hz polling)
    logger.info("Step 5: Entering high-frequency observation loop...")
    start_time = time.time()
    try:
        while engine.state not in (StrategyState.CLOSED, StrategyState.ABORTED):
            now_ts = time.time()
            if (now_ts - start_time) > max_duration_sec:
                logger.info("Session max duration exceeded. Forcing hard time exit.")
                break

            # Fetch fresh quotes
            fresh_spot_res = get_nifty_spot()
            if isinstance(fresh_spot_res, dict):
                curr_spot = float(fresh_spot_res.get("ltp") or spot_val)
            elif fresh_spot_res is not None:
                curr_spot = float(fresh_spot_res)
            else:
                curr_spot = spot_val

            chain_tick = get_nifty_option_chain(expiry_angel, atm_strike, atm_strike)
            c_info = chain_tick.get("call") or {}
            p_info = chain_tick.get("put") or {}

            c_ltp = float(c_info.get("ltp") or ce_ltp)
            p_ltp = float(p_info.get("ltp") or pe_ltp)
            c_bid = float(c_info.get("bid") or c_ltp)
            c_ask = float(c_info.get("ask") or c_ltp)
            p_bid = float(p_info.get("bid") or p_ltp)
            p_ask = float(p_info.get("ask") or p_ltp)

            tick = TickObservation(
                ts=now_ts,
                nifty_spot=curr_spot,
                call_bid=c_bid,
                call_ask=c_ask,
                put_bid=p_bid,
                put_ask=p_ask,
                call_ltp=c_ltp,
                put_ltp=p_ltp,
            )

            engine.process_tick(tick)
            time.sleep(poll_interval_sec)

    except KeyboardInterrupt:
        logger.info("Execution interrupted by user.")
        return 0

    logger.info("Session complete: State=%s, Status=%s", engine.state.value, engine.status)
    return 0


def main():
    parser = argparse.ArgumentParser(description="NIFTY 50 Opening Straddle Strategy (09:15 - 10:00 AM)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Run in simulation/paper mode")
    parser.add_argument("--db-path", type=str, help="Custom SQLite database path")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_cli_logging(args.log_level)
    return run_opening_straddle(dry_run=args.dry_run, db_path=args.db_path)


if __name__ == "__main__":
    sys.exit(main())
