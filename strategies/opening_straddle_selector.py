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
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from common.angelone_client import (
    get_nifty_spot,
    get_nifty_option_chain,
)
from common.expiry import format_expiry_angelone
from strategies.opening_straddle_engine import (
    OpeningStraddleEngine,
    resolve_opening_straddle_expiry,
    DEFAULT_MAX_SPREAD_PCT,
)
from strategies.opening_straddle_models import (
    ExitReason,
    OrderAction,
    PreMarketIntelligence,
    StraddleExecutionMode,
    StrategyState,
    StraddleLegQuote,
    TickObservation,
    TradeLeg,
    derive_bias_from_cms,
)
from strategies.premarket_intelligence import PreMarketIntelligenceLoader

logger = logging.getLogger("nifty50_strategies.opening_straddle_selector")

IST_TZ = timezone(timedelta(hours=5, minutes=30))
MAX_QUOTE_AGE_SEC = 15.0   # Live tick quote must not be older than 15 seconds
MAX_CLOCK_SKEW_SEC = 2.0   # Max acceptable future timestamp clock skew in seconds


def _is_valid_price(p: Any) -> bool:
    """Validates that a numeric price is finite, positive, and non-zero."""
    import math
    if p is None:
        return False
    try:
        val = float(p)
        return math.isfinite(val) and val > 0.0
    except (TypeError, ValueError):
        return False


def compute_market_open_wait_sec(now_dt: Optional[datetime] = None, target_second: int = 5) -> float:
    """
    Computes seconds to wait until 09:15:{target_second:02d} AM IST for market open.

    Returns:
        - float seconds (> 0.0) if currently between 09:00:00 and target IST time.
        - 0.0 if already at or past target IST time, or outside the pre-market window.
    """
    now_dt = now_dt or datetime.now(tz=IST_TZ)
    target_entry = now_dt.replace(hour=9, minute=15, second=target_second, microsecond=0)
    wait_sec = (target_entry - now_dt).total_seconds()
    if 0 < wait_sec <= 900:  # Within 15 minutes before target (09:00 - 09:15:XX)
        return wait_sec
    return 0.0


def compute_one_minute_candle_score(open_spot: Optional[float], close_spot: Optional[float]) -> float:
    """
    Computes 1-minute opening candle momentum score in [-1.0, +1.0] (Plan Section 4.1):
      r_1m = ((close - open) / open) * 100
      score_1m = clip((close - open) / (open * 0.0015), -1.0, 1.0)
    A 0.15% return moves score to full +/- 1.0.
    Returns 0.0 if open or close is missing/invalid.
    """
    if not _is_valid_price(open_spot) or not _is_valid_price(close_spot):
        return 0.0
    r_ratio = (float(close_spot) - float(open_spot)) / (float(open_spot) * 0.0015)
    return max(-1.0, min(1.0, r_ratio))


def compute_final_composite_morning_score(
    pre_market_cms: float,
    pre_market_coverage: float,
    score_1m_candle: float,
    pre_market_weight: float = 0.80,
    candle_weight: float = 0.20,
) -> float:
    """
    Synthesizes final 09:16:05 AM Composite Morning Score (CMS) (Plan Section 4.2):
      - Coverage gate: If premarket coverage < 0.50, CMS is forced to 0.0 (SKIP).
      - Otherwise: CMS = clip(0.80 * pre_market_cms + 0.20 * score_1m_candle, -1.0, 1.0).
    """
    if pre_market_coverage < 0.50:
        return 0.0
    raw_cms = (pre_market_weight * pre_market_cms) + (candle_weight * score_1m_candle)
    return max(-1.0, min(1.0, raw_cms))


DEFAULT_OPENING_CHOP_THRESHOLD = 0.02
DEFAULT_OPENING_STRONG_THRESHOLD = 0.25


def select_single_leg_decision(
    cms: float,
    atm_strike: int,
    prefer_sell_call_on_strong_bearish: bool = True,
    chop_threshold: Optional[float] = None,
    strong_threshold: Optional[float] = None,
) -> Tuple[TradeLeg, Optional[OrderAction], Optional[int], str]:
    """
    Evaluates Composite Morning Score (CMS) against Calibrated Decision Matrix:
      - Strong Bullish (CMS >= +strong_threshold, default 0.25):
          BUY CALL ATM (K_atm)
      - Moderate / Mild Bullish (+chop_threshold <= CMS < +strong_threshold, default 0.02 to 0.25):
          SELL PUT 1-step OTM (K_atm - 50) [Premium decay harvest with upward cushion]
      - Moderate / Mild Bearish (-strong_threshold < CMS <= -chop_threshold, default -0.25 to -0.02):
          SELL CALL 1-step OTM (K_atm + 50) [User Priority Tip: Theta decay + downward cushion]
      - Strong Bearish (CMS <= -strong_threshold):
          SELL CALL 1-step OTM (Primary) or BUY PUT ATM (Secondary)
      - Dead Flat / Neutral Chop (-chop_threshold < CMS < +chop_threshold):
          SKIP TRADE (NEUTRAL_CHOP)
    """
    if chop_threshold is None:
        try:
            chop_threshold = float(os.getenv("OPENING_CHOP_THRESHOLD", str(DEFAULT_OPENING_CHOP_THRESHOLD)))
        except (ValueError, TypeError):
            chop_threshold = DEFAULT_OPENING_CHOP_THRESHOLD

    if strong_threshold is None:
        try:
            strong_threshold = float(os.getenv("OPENING_STRONG_THRESHOLD", str(DEFAULT_OPENING_STRONG_THRESHOLD)))
        except (ValueError, TypeError):
            strong_threshold = DEFAULT_OPENING_STRONG_THRESHOLD

    if cms >= strong_threshold:
        return (TradeLeg.CALL, OrderAction.BUY, atm_strike, f"STRONG_BULLISH (CMS={cms:+.3f}): BUY CALL ATM {atm_strike}")
    elif chop_threshold <= cms < strong_threshold:
        target_strike = atm_strike - 50
        return (TradeLeg.PUT, OrderAction.SELL, target_strike, f"MODERATE_BULLISH (CMS={cms:+.3f}): SELL PUT 1-step OTM {target_strike}")
    elif -strong_threshold < cms <= -chop_threshold:
        target_strike = atm_strike + 50
        return (TradeLeg.CALL, OrderAction.SELL, target_strike, f"MODERATE_BEARISH (CMS={cms:+.3f}): SELL CALL 1-step OTM {target_strike} (User Tip Priority)")
    elif cms <= -strong_threshold:
        if prefer_sell_call_on_strong_bearish:
            target_strike = atm_strike + 50
            return (TradeLeg.CALL, OrderAction.SELL, target_strike, f"STRONG_BEARISH (CMS={cms:+.3f}): SELL CALL 1-step OTM {target_strike} (Primary)")
        else:
            return (TradeLeg.PUT, OrderAction.BUY, atm_strike, f"STRONG_BEARISH (CMS={cms:+.3f}): BUY PUT ATM {atm_strike} (Secondary)")
    else:
        return (TradeLeg.NONE, None, None, f"NEUTRAL_CHOP (CMS={cms:+.3f} in (-{chop_threshold:.3f}, +{chop_threshold:.3f})): SKIP TRADE")


def sample_opening_candle(
    wait_for_market_open: bool = True,
    poll_interval_sec: float = 1.0,
) -> Dict[str, Optional[float]]:
    """
    Samples the 1-minute opening candle between 09:15:00 and 09:16:00 AM IST.
    Captures: open, high, low, close.
    If wait_for_market_open is False, takes a single snapshot of the current spot.
    """
    now_dt = datetime.now(tz=IST_TZ)

    if not wait_for_market_open:
        spot_res = get_nifty_spot()
        spot_val = None
        if isinstance(spot_res, dict):
            val = spot_res.get("ltp")
            spot_val = float(val) if _is_valid_price(val) else None
        elif _is_valid_price(spot_res):
            spot_val = float(spot_res)
        return {"open": spot_val, "high": spot_val, "low": spot_val, "close": spot_val}

    # Observe 09:15:00 to 09:16:00
    market_open_dt = now_dt.replace(hour=9, minute=15, second=0, microsecond=0)
    candle_end_dt = now_dt.replace(hour=9, minute=16, second=0, microsecond=0)
    target_entry_dt = now_dt.replace(hour=9, minute=16, second=5, microsecond=0)

    # 1. Early launch: If before 09:15:00 IST, wait for opening bell
    if now_dt < market_open_dt:
        wait_sec = (market_open_dt - now_dt).total_seconds()
        logger.info(
            "Current time is %s IST (%.1fs before market open). Waiting until 09:15:00 AM IST for opening candle observation...",
            now_dt.strftime("%H:%M:%S"),
            wait_sec,
        )
        time.sleep(wait_sec)

    # 2. Late launch: If past 09:16:00 IST, observation window has already elapsed
    now_dt = datetime.now(tz=IST_TZ)
    if now_dt >= candle_end_dt:
        logger.warning(
            "Current time %s IST is at or past 09:16:00 AM IST. Opening candle observation window (09:15-09:16) has already closed.",
            now_dt.strftime("%H:%M:%S"),
        )
        return {"open": None, "high": None, "low": None, "close": None}

    open_p: Optional[float] = None
    high_p: Optional[float] = None
    low_p: Optional[float] = None
    close_p: Optional[float] = None

    while datetime.now(tz=IST_TZ) < candle_end_dt:
        spot_res = get_nifty_spot()
        s_val = None
        if isinstance(spot_res, dict):
            val = spot_res.get("ltp")
            if _is_valid_price(val):
                s_val = float(val)
        elif _is_valid_price(spot_res):
            s_val = float(spot_res)

        if s_val is not None and s_val > 0:
            if open_p is None:
                open_p = s_val
            high_p = max(high_p, s_val) if high_p is not None else s_val
            low_p = min(low_p, s_val) if low_p is not None else s_val
            close_p = s_val

        time.sleep(poll_interval_sec)

    # Wait until 09:16:05 AM IST for entry gate
    now_dt = datetime.now(tz=IST_TZ)
    if now_dt < target_entry_dt:
        entry_wait = (target_entry_dt - now_dt).total_seconds()
        if 0 < entry_wait <= 10.0:
            logger.info("Awaiting 09:16:05 entry timestamp (%.1fs buffer)...", entry_wait)
            time.sleep(entry_wait)

    return {"open": open_p, "high": high_p, "low": low_p, "close": close_p}


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
    runner_profit_target_pct: float = 35.0,
    runner_stop_loss_pct: float = -10.0,
    runner_trailing_trigger_pct: float = 15.0,
    runner_trailing_drop_pct: float = 8.0,
    prefer_sell_call_on_strong_bearish: bool = True,
    short_margin_per_lot: Optional[float] = None,
    chop_threshold: Optional[float] = None,
    strong_threshold: Optional[float] = None,
) -> int:
    """
    Main orchestrator running the daily directional single-leg scalp cycle (09:14 to 10:00 AM).
    Operates strictly as an analytical signal, paper-execution, and risk-tracking engine.
    Order placement is completely out of scope.
    """
    logger.info("=" * 70)
    logger.info("Starting NIFTY 50 Directional Single-Leg Scalper (Tracking Mode)")
    logger.info("=" * 70)

    # 1. Step 1: Pre-Market Multi-Source Ingestion & VIX Gate (09:14:00 AM)
    logger.info("Step 1: Running Pre-Market Multi-Source Ingestion Engine (Directional Scalp Mode)...")
    intel_loader = PreMarketIntelligenceLoader(directional_scalp=True)
    intel = intel_loader.load_intelligence()
    logger.info("Pre-Market Summary: %s", intel.summary_str())

    # VIX Fail-Safe Gate: Terminate immediately if VIX is elevated or missing (Rule 2)
    if intel.vix_skip:
        logger.warning(
            "VIX Gate triggered: %s. Skipping morning strategy execution for the day.",
            intel.vix_skip_reason,
        )
        engine = OpeningStraddleEngine(
            pre_market=intel,
            db_path=db_path,
            enable_alerts=enable_alerts,
            execution_mode=StraddleExecutionMode.SINGLE_LEG_DIRECTIONAL,
        )
        skip_exit_reason = (
            ExitReason.HIGH_VIX_SKIP
            if "HIGH_VIX" in (intel.vix_skip_reason or "")
            else ExitReason.MISSING_VIX_SKIP
        )
        engine.record_skipped_session(intel.vix_skip_reason or "VIX_SKIP", skip_exit_reason)
        return 0

    # 2. Step 2: Resolve Target Expiry & Contracts
    target_expiry = resolve_opening_straddle_expiry()
    expiry_angel = format_expiry_angelone(target_expiry)
    logger.info("Target Expiry: %s (Angel Format: %s)", target_expiry.strftime("%Y-%m-%d"), expiry_angel)

    # 3. Step 3: Wait for Market Open & Sample 1-Minute Candle (09:15:00 - 09:16:00 AM IST)
    logger.info("Step 3: Awaiting market open and sampling 1-minute candle momentum (09:15 - 09:16 AM IST)...")
    candle_data = sample_opening_candle(
        wait_for_market_open=wait_for_market_open,
        poll_interval_sec=poll_interval_sec,
    )
    spot_open = candle_data.get("open")
    spot_close = candle_data.get("close")

    # In production live mode (wait_for_market_open=True), missing opening candle means late launch or feed failure
    if wait_for_market_open and (spot_open is None or spot_close is None):
        logger.error(
            "Opening candle (09:15-09:16 IST) data is missing (late launch past 09:16:00 or feed offline). "
            "Cannot synthesize 09:16:05 CMS or execute directional trade. Aborting morning scalp session."
        )
        engine = OpeningStraddleEngine(
            pre_market=intel,
            trade_date=datetime.now().strftime("%Y-%m-%d"),
            expiry_date=target_expiry.strftime("%Y-%m-%d"),
            db_path=db_path,
            enable_alerts=enable_alerts,
            execution_mode=StraddleExecutionMode.SINGLE_LEG_DIRECTIONAL,
        )
        engine.record_skipped_session("OPENING_CANDLE_MISSING (late launch past 09:16:00 or feed offline)", ExitReason.EXECUTION_ABORT)
        return 1

    score_1m = compute_one_minute_candle_score(spot_open, spot_close)
    logger.info(
        "1-Minute Opening Candle (09:15-09:16): Open=%s, High=%s, Low=%s, Close=%s | Score=%.3f",
        f"{spot_open:.2f}" if spot_open else "N/A",
        f"{candle_data.get('high'):.2f}" if candle_data.get('high') else "N/A",
        f"{candle_data.get('low'):.2f}" if candle_data.get('low') else "N/A",
        f"{spot_close:.2f}" if spot_close else "N/A",
        score_1m,
    )

    # Blend Premarket CMS (0.80) and 1m Candle Score (0.20)
    pre_market_cms = (
        intel.composite_morning_score
        if intel.composite_morning_score is not None
        else (intel.cds if intel.coverage >= 0.50 else 0.0)
    )
    final_cms = compute_final_composite_morning_score(
        pre_market_cms=pre_market_cms,
        pre_market_coverage=intel.coverage,
        score_1m_candle=score_1m,
    )
    intel.composite_morning_score = final_cms
    intel.final_bias = derive_bias_from_cms(final_cms)
    logger.info(
        "Final CMS Synthesis at 09:16:05: Premarket CMS=%.3f (cov=%.2f) * 0.80 + 1m Candle=%.3f * 0.20 -> Final CMS=%.3f",
        pre_market_cms,
        intel.coverage,
        score_1m,
        final_cms,
    )

    # 4. Step 4: Decision Matrix & Single Leg Strike Selection (09:16:05 AM IST)
    # Determine reference spot: prefer 09:16 close, then intel.nifty_spot
    ref_spot = spot_close or intel.nifty_spot or 0.0
    if ref_spot <= 0.0:
        spot_res = get_nifty_spot()
        if isinstance(spot_res, dict):
            ref_spot = float(spot_res.get("ltp") or 0.0)
        elif spot_res is not None:
            ref_spot = float(spot_res)

    atm_strike = int(round(ref_spot / 50.0) * 50) if ref_spot > 0 else 0

    trade_leg, order_action, target_strike, decision_reason = select_single_leg_decision(
        cms=final_cms,
        atm_strike=atm_strike,
        prefer_sell_call_on_strong_bearish=prefer_sell_call_on_strong_bearish,
        chop_threshold=chop_threshold,
        strong_threshold=strong_threshold,
    )
    logger.info("Leg Selection Matrix Result: %s", decision_reason)

    # Skip Gate: Insufficient Coverage or Neutral Chop
    if trade_leg == TradeLeg.NONE or order_action is None or target_strike is None:
        is_insufficient_cov = (intel.coverage < 0.50)
        skip_reason_label = (
            f"INSUFFICIENT_COVERAGE (coverage={intel.coverage:.2f} < 0.50): SKIP TRADE"
            if is_insufficient_cov
            else decision_reason
        )
        skip_exit_reason = (
            ExitReason.INSUFFICIENT_COVERAGE_SKIP
            if is_insufficient_cov
            else ExitReason.NEUTRAL_CHOP_SKIP
        )
        logger.warning(
            "Skip Gate triggered: %s. Skipping morning trade.",
            skip_reason_label,
        )
        engine = OpeningStraddleEngine(
            pre_market=intel,
            trade_date=datetime.now().strftime("%Y-%m-%d"),
            expiry_date=target_expiry.strftime("%Y-%m-%d"),
            db_path=db_path,
            enable_alerts=enable_alerts,
            execution_mode=StraddleExecutionMode.SINGLE_LEG_DIRECTIONAL,
        )
        engine.record_skipped_session(skip_reason_label, skip_exit_reason, final_cms=final_cms)
        return 0

    # 5. Step 5: Acquire Fresh Executable Option Quote for Target Strike
    logger.info(
        "Step 5: Sampling 09:16:05 AM executable quote for %s %s Strike=%d...",
        order_action.value,
        trade_leg.value,
        target_strike,
    )

    entry_start_time = time.time()
    spot_val = ref_spot
    leg_quote: Optional[StraddleLegQuote] = None
    last_rejection_reasons: List[str] = []

    while (time.time() - entry_start_time) <= entry_grace_timeout_sec:
        spot_res = get_nifty_spot()
        if isinstance(spot_res, dict):
            s_candidate = spot_res.get("ltp")
            if _is_valid_price(s_candidate):
                spot_val = float(s_candidate)
        elif _is_valid_price(spot_res):
            spot_val = float(spot_res)

        chain = get_nifty_option_chain(expiry_angel, target_strike, target_strike, mode="FULL")
        t_entry_recv = time.time()
        call_info = chain.get("call") or {}
        put_info = chain.get("put") or {}
        leg_info = call_info if trade_leg == TradeLeg.CALL else put_info

        leg_ltp = float(leg_info.get("ltp") or 0.0)
        if leg_ltp <= 0.0:
            last_rejection_reasons = [f"Missing option LTP ({trade_leg.value}={leg_ltp:.2f})"]
            time.sleep(1.0)
            continue

        # Validate quote freshness
        spot_entry_age = parse_quote_age_sec(
            spot_res.get("feed_time") if isinstance(spot_res, dict) else None,
            now_ts=t_entry_recv,
            max_skew_sec=MAX_CLOCK_SKEW_SEC,
        )
        leg_entry_age = parse_quote_age_sec(leg_info.get("feed_time"), now_ts=t_entry_recv, max_skew_sec=MAX_CLOCK_SKEW_SEC)

        entry_stale_reasons = []
        if spot_entry_age is None:
            if not allow_synthetic_spread:
                entry_stale_reasons.append("Spot=missing_or_unverifiable_timestamp")
        elif spot_entry_age > MAX_QUOTE_AGE_SEC:
            entry_stale_reasons.append(f"Spot={spot_entry_age:.1f}s")

        if leg_entry_age is None:
            if not allow_synthetic_spread:
                entry_stale_reasons.append(f"{trade_leg.value}=missing_or_unverifiable_timestamp")
        elif leg_entry_age > MAX_QUOTE_AGE_SEC:
            entry_stale_reasons.append(f"{trade_leg.value}={leg_entry_age:.1f}s")

        if entry_stale_reasons:
            last_rejection_reasons = entry_stale_reasons
            elapsed = time.time() - entry_start_time
            if elapsed < entry_grace_timeout_sec:
                logger.info(
                    "Opening quotes not yet fresh (%s). Awaiting live ticks (elapsed %.1fs/%.0fs)...",
                    ", ".join(entry_stale_reasons),
                    elapsed,
                    entry_grace_timeout_sec,
                )
                time.sleep(1.5)
            continue

        raw_bid = leg_info.get("bid")
        raw_ask = leg_info.get("ask")

        has_depth = (
            _is_valid_price(raw_bid)
            and _is_valid_price(raw_ask)
            and float(raw_ask) >= float(raw_bid)
        )

        if not has_depth:
            if allow_synthetic_spread:
                logger.warning(
                    "Market depth unavailable for %s (bid=%s, ask=%s). Synthesizing 0.4%% spread around LTP.",
                    trade_leg.value, raw_bid, raw_ask,
                )
                leg_bid = leg_ltp * 0.998
                leg_ask = leg_ltp * 1.002
            else:
                last_rejection_reasons = [f"Awaiting depth ({trade_leg.value} bid={raw_bid}, ask={raw_ask})"]
                elapsed = time.time() - entry_start_time
                if elapsed < entry_grace_timeout_sec:
                    logger.info(
                        "Opening depth not yet formed (%s). Retrying (elapsed %.1fs/%.0fs)...",
                        last_rejection_reasons[0],
                        elapsed,
                        entry_grace_timeout_sec,
                    )
                    time.sleep(1.5)
                continue
        else:
            leg_bid = float(raw_bid)
            leg_ask = float(raw_ask)

        leg_token = str(leg_info.get("token") or "")
        opt_suffix = "CE" if trade_leg == TradeLeg.CALL else "PE"
        leg_symbol = str(leg_info.get("trading_symbol") or f"NIFTY{target_strike}{opt_suffix}")

        leg_quote = StraddleLegQuote(
            symbol=leg_symbol,
            token=leg_token,
            strike=target_strike,
            option_type=opt_suffix,
            bid=leg_bid,
            ask=leg_ask,
            ltp=leg_ltp,
        )
        logger.info(
            "Acquired fresh quote and valid depth for %s Strike %d at +%.2fs (Spot=%.2f, LTP=%.2f, Bid=%.2f, Ask=%.2f).",
            trade_leg.value,
            target_strike,
            time.time() - entry_start_time,
            spot_val,
            leg_ltp,
            leg_bid,
            leg_ask,
        )
        break

    if leg_quote is None:
        logger.error(
            "Failed to acquire fresh opening quotes within %.0fs grace window (%s). Aborting entry.",
            entry_grace_timeout_sec,
            ", ".join(last_rejection_reasons or ["Unknown error"]),
        )
        return 1

    # 6. Step 6: Arm Single-Leg Directional Entry
    engine = OpeningStraddleEngine(
        pre_market=intel,
        trade_date=datetime.now().strftime("%Y-%m-%d"),
        expiry_date=target_expiry.strftime("%Y-%m-%d"),
        db_path=db_path,
        runner_profit_target_pct=runner_profit_target_pct,
        runner_stop_loss_pct=runner_stop_loss_pct,
        runner_trailing_trigger_pct=runner_trailing_trigger_pct,
        runner_trailing_drop_pct=runner_trailing_drop_pct,
        short_margin_per_lot=short_margin_per_lot,
        enable_alerts=enable_alerts,
        execution_mode=StraddleExecutionMode.SINGLE_LEG_DIRECTIONAL,
    )

    armed = engine.arm_single_leg_entry(
        leg=trade_leg,
        action=order_action,
        strike=target_strike,
        quote=leg_quote,
        spot_price=spot_val,
        final_cms=final_cms,
    )
    if not armed:
        logger.warning("Single-leg entry gate rejected or aborted (Status=%s).", engine.status)
        return 1

    # 7. Step 7: Live Observation Loop (1 Hz polling until exit or 09:55:00 AM IST)
    logger.info("Step 7: Entering high-frequency single-leg observation loop...")
    start_time = time.time()
    try:
        while engine.state not in (StrategyState.CLOSED, StrategyState.ABORTED):
            loop_now = time.time()
            if (loop_now - start_time) > max_duration_sec:
                logger.info(
                    "Session max duration exceeded (%.1fs > %ds). Forcing hard time exit.",
                    loop_now - start_time,
                    max_duration_sec,
                )
                engine.force_hard_time_exit()
                break

            t_req_start = time.time()
            fresh_spot_res = get_nifty_spot()
            raw_spot = None
            if isinstance(fresh_spot_res, dict):
                raw_spot = fresh_spot_res.get("ltp")
            elif fresh_spot_res is not None:
                raw_spot = fresh_spot_res

            if not _is_valid_price(raw_spot):
                time.sleep(poll_interval_sec)
                continue
            curr_spot = float(raw_spot)

            chain_tick = get_nifty_option_chain(expiry_angel, target_strike, target_strike, mode="FULL")
            t_recv = time.time()
            latency_sec = t_recv - t_req_start
            if latency_sec > 5.0:
                logger.warning("Quote API round-trip latency exceeded (%.2fs > 5.0s). Skipping stale tick.", latency_sec)
                time.sleep(poll_interval_sec)
                continue

            c_info = chain_tick.get("call") or {}
            p_info = chain_tick.get("put") or {}
            leg_info = c_info if trade_leg == TradeLeg.CALL else p_info

            raw_leg_ltp = leg_info.get("ltp")
            if not _is_valid_price(raw_leg_ltp):
                time.sleep(poll_interval_sec)
                continue

            # Check quote freshness
            spot_age = parse_quote_age_sec(
                fresh_spot_res.get("feed_time") if isinstance(fresh_spot_res, dict) else None,
                now_ts=t_recv,
                max_skew_sec=MAX_CLOCK_SKEW_SEC,
            )
            leg_age = parse_quote_age_sec(leg_info.get("feed_time"), now_ts=t_recv, max_skew_sec=MAX_CLOCK_SKEW_SEC)

            stale_reasons = []
            if spot_age is None:
                if not allow_synthetic_spread:
                    stale_reasons.append("Spot=missing_or_unverifiable_timestamp")
            elif spot_age > MAX_QUOTE_AGE_SEC:
                stale_reasons.append(f"Spot={spot_age:.1f}s")

            if leg_age is None:
                if not allow_synthetic_spread:
                    stale_reasons.append(f"{trade_leg.value}=missing_or_unverifiable_timestamp")
            elif leg_age > MAX_QUOTE_AGE_SEC:
                stale_reasons.append(f"{trade_leg.value}={leg_age:.1f}s")

            if stale_reasons:
                logger.warning("Stale market data detected (%s). Skipping tick.", ", ".join(stale_reasons))
                time.sleep(poll_interval_sec)
                continue

            raw_bid = leg_info.get("bid")
            raw_ask = leg_info.get("ask")
            has_depth = (
                _is_valid_price(raw_bid)
                and _is_valid_price(raw_ask)
                and float(raw_ask) >= float(raw_bid)
            )

            if not has_depth:
                if allow_synthetic_spread:
                    curr_leg_ltp = float(raw_leg_ltp)
                    tick_bid = curr_leg_ltp * 0.998
                    tick_ask = curr_leg_ltp * 1.002
                else:
                    logger.warning("Live tick missing depth for active leg %s. Skipping tick.", trade_leg.value)
                    time.sleep(poll_interval_sec)
                    continue
            else:
                tick_bid = float(raw_bid)
                tick_ask = float(raw_ask)

            tick_call_bid = tick_bid if trade_leg == TradeLeg.CALL else 0.0
            tick_call_ask = tick_ask if trade_leg == TradeLeg.CALL else 0.0
            tick_put_bid = tick_bid if trade_leg == TradeLeg.PUT else 0.0
            tick_put_ask = tick_ask if trade_leg == TradeLeg.PUT else 0.0

            tick = TickObservation(
                ts=t_recv,
                nifty_spot=curr_spot,
                call_bid=tick_call_bid,
                call_ask=tick_call_ask,
                put_bid=tick_put_bid,
                put_ask=tick_put_ask,
                call_ltp=float(raw_leg_ltp) if trade_leg == TradeLeg.CALL else 0.0,
                put_ltp=float(raw_leg_ltp) if trade_leg == TradeLeg.PUT else 0.0,
                quote_age_sec=max((a for a in (spot_age, leg_age) if a is not None), default=None),
            )

            engine.process_tick(tick)
            time.sleep(poll_interval_sec)

    except KeyboardInterrupt:
        logger.info("Execution interrupted by user. Flushing buffered ticks and session...")
        engine.flush_interrupted_session()
        return 0

    # Fail-safe liquidation guard: ensure any unclosed position is cleanly liquidated with HARD_TIME_STOP
    if engine.state not in (StrategyState.CLOSED, StrategyState.ABORTED):
        logger.warning(
            "Observation loop ended while strategy was in state %s. Forcing hard time exit.",
            engine.state.value,
        )
        engine.force_hard_time_exit()

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
                   order_action, trade_leg, executed_strike, entry_price, exit_price,
                   net_pnl_inr, net_roi_pct, peak_roi_pct, exit_reason, status
            FROM opening_straddle_sessions
            ORDER BY trade_date DESC LIMIT 30
            """
        ).fetchall()

    if not rows:
        print(f"No sessions found in {target_db}")
        return 0

    print("=" * 96)
    print(" NIFTY 50 DIRECTIONAL SINGLE-LEG / OPENING SCALPER - HISTORICAL SESSIONS")
    print("=" * 96)
    header = f"{'Date':<11} | {'ATM':<6} | {'Prior Bias':<14} | {'Action / Leg':<16} | {'Net P&L':>11} | {'ROI %':>8} | {'Peak %':>8} | {'Exit Reason':<16}"
    print(header)
    print("-" * 96)
    for r in rows:
        pnl_str = f"INR {r['net_pnl_inr']:+,.2f}" if r["net_pnl_inr"] is not None else "N/A"
        roi_str = f"{r['net_roi_pct']:+.2f}%" if r["net_roi_pct"] is not None else "N/A"
        peak_str = f"{r['peak_roi_pct']:+.2f}%" if r["peak_roi_pct"] is not None else "N/A"
        bias = (r["bias_classification"] or "N/A")[:14]

        # Single-leg or legacy display
        if r["order_action"] and r["trade_leg"] and r["trade_leg"] != "NONE":
            act = r["order_action"]
            t_leg = r["trade_leg"]
            stk = r["executed_strike"] or r["atm_strike"] or ""
            leg_display = f"{act} {t_leg} {stk}"[:16]
        else:
            leg_display = (r["selected_leg"] or r["status"] or "N/A")[:16]

        exit_r = (r["exit_reason"] or "N/A")[:16]
        print(f"{r['trade_date']:<11} | {r['atm_strike']:<6} | {bias:<14} | {leg_display:<16} | {pnl_str:>11} | {roi_str:>8} | {peak_str:>8} | {exit_r:<16}")
    print("=" * 96)
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
    parser.add_argument("--runner-stop-loss", type=float, default=-10.0, help="Individual runner stop loss percent (default: -10.0%%)")
    parser.add_argument("--runner-profit-target", type=float, default=35.0, help="Individual runner profit target percent (default: +35.0%%)")
    parser.add_argument("--runner-trailing-trigger", type=float, default=15.0, help="Runner trailing stop activation trigger percent (default: +15.0%%)")
    parser.add_argument("--runner-trailing-drop", type=float, default=8.0, help="Runner trailing stop drop distance from peak (default: 8.0%%)")
    parser.add_argument("--prefer-buy-put-on-strong-bearish", action="store_true", help="BUY PUT ATM instead of SELL CALL 1-step OTM on strong bearish (default: False, prioritizes SELL CALL)")
    parser.add_argument("--short-margin", type=float, default=None, help="Custom short margin INR per lot (default: 120,000 INR)")
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
        runner_profit_target_pct=args.runner_profit_target,
        runner_stop_loss_pct=args.runner_stop_loss,
        runner_trailing_trigger_pct=args.runner_trailing_trigger,
        runner_trailing_drop_pct=args.runner_trailing_drop,
        prefer_sell_call_on_strong_bearish=not args.prefer_buy_put_on_strong_bearish,
        short_margin_per_lot=args.short_margin,
    )


if __name__ == "__main__":
    sys.exit(main())
