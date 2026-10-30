"""
Core Finite State Machine & Decoupling Engine for NIFTY 50 Opening Straddle Strategy.

Operates daily between 09:14 and 10:00 AM IST:
  1. Entry Gate: 09:15:05 AM (ATM strike, spread <= 1.5%)
  2. Observation: 09:15:05 - 09:20:00 AM (1 Hz tick tracking)
  3. Checkpoint 1 (09:20:00 AM): Live decoupling evaluation conditioned on pre-market CDS
  4. Checkpoint 2 (09:25:00 AM): Extended evaluation or mandatory scratch flattening
  5. Runner Management: Continuous Total Straddle P&L safeguard (+3% target, -3% stop, trailing)
  6. Hard Time Stop: Mandatory unconditional liquidation by 09:55:00 AM
"""
import logging
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from common.calendar_utils import check_nse_holiday
from strategies.opening_straddle_alerts import (
    send_decoupling_alert,
    send_straddle_entry_alert,
    send_straddle_exit_summary,
)
from strategies.opening_straddle_models import (
    BiasClassification,
    ConfidenceLevel,
    ExitReason,
    PreMarketIntelligence,
    SelectedLeg,
    StrategyState,
    StraddleExecutionMode,
    StraddleLegQuote,
    StraddleSessionRecord,
    TickObservation,
    VolatilityRegime,
)

logger = logging.getLogger("nifty50_strategies.opening_straddle_engine")

# ---------------------------------------------------------------------------
# Default Operational Parameters
# ---------------------------------------------------------------------------
# Combined Basket Fallback Parameters
DEFAULT_PROFIT_TARGET_ROI = 3.0       # +3.0% net combined ROI
DEFAULT_STOP_LOSS_ROI = -3.0          # -3.0% net combined ROI (only during dual-leg holding)
DEFAULT_TRAILING_TRIGGER_ROI = 2.0    # Peak >= +2.0% enables combined trailing stop
DEFAULT_TRAILING_DROP_ROI = 1.0       # Exit if combined ROI drops 1.0 pp below peak
DEFAULT_MAX_SPREAD_PCT = 1.5          # 1.5% maximum opening bid-ask spread
DEFAULT_ESTIMATED_FRICTION_INR = 150.0  # Estimated round-trip friction for 1-lot straddle
DEFAULT_LOT_SIZE = 65                 # Nifty lot size per NSE contract specs

# Independent Runner Management Parameters (Option A)
DEFAULT_RUNNER_PROFIT_TARGET_PCT = 35.0    # +35.0% on runner premium
DEFAULT_RUNNER_STOP_LOSS_PCT = -10.0       # -10.0% on runner premium (individual stop)
DEFAULT_RUNNER_TRAILING_TRIGGER_PCT = 15.0 # Runner Peak >= +15.0% enables trailing stop
DEFAULT_RUNNER_TRAILING_DROP_PCT = 8.0     # Trail 8.0% behind runner peak

# Checkpoint time thresholds (seconds from 09:15:00 AM open)
SEC_920_CHECKPOINT = 300              # 5 minutes in (09:20:00)
SEC_925_CHECKPOINT = 600              # 10 minutes in (09:25:00)
SEC_955_HARD_EXIT = 2400              # 40 minutes in (09:55:00)


def resolve_opening_straddle_expiry(from_date: Optional[date] = None) -> date:
    """
    Selects contract expiry based on the Dynamic Expiry Selection Rule (Section 3.3):
      - Count remaining trading sessions until coming Tuesday expiry (excluding entry day).
      - If sessions remaining <= 2 -> Following weekly expiry.
      - If sessions remaining > 2 -> Current weekly expiry.
      - Holiday preponement is handled automatically per NSE rules.
    """
    ref_date = from_date if from_date else date.today()

    # Step 1: Find next upcoming Tuesday (or today if today is Tuesday)
    days_to_tue = (1 - ref_date.weekday()) % 7
    nearest_tue = ref_date + timedelta(days=days_to_tue)

    # Adjust for holidays (prepone if holiday)
    target_expiry = nearest_tue
    while check_nse_holiday(target_expiry) or target_expiry.weekday() >= 5:
        target_expiry -= timedelta(days=1)

    # If adjusted target_expiry is already in the past or today, must look at next Tuesday
    if target_expiry <= ref_date:
        following_tue = nearest_tue + timedelta(days=7)
        target_expiry = following_tue
        while check_nse_holiday(target_expiry) or target_expiry.weekday() >= 5:
            target_expiry -= timedelta(days=1)
        return target_expiry

    # Step 2: Count remaining active trading sessions AFTER ref_date until target_expiry
    remaining_sessions = 0
    curr = ref_date + timedelta(days=1)
    while curr <= target_expiry:
        if not check_nse_holiday(curr) and curr.weekday() < 5:
            remaining_sessions += 1
        curr += timedelta(days=1)

    # Step 3: If remaining sessions <= 2 (Fri, Mon, or Tue), roll to following week's Tuesday expiry
    if remaining_sessions <= 2:
        following_tue = nearest_tue + timedelta(days=7)
        target_expiry = following_tue
        while check_nse_holiday(target_expiry) or target_expiry.weekday() >= 5:
            target_expiry -= timedelta(days=1)

    return target_expiry


def init_opening_straddle_db(db_path: str):
    """Initializes dedicated opening_straddle.db SQLite schema."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS opening_straddle_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL UNIQUE,
                expiry_date TEXT NOT NULL,
                atm_strike INTEGER NOT NULL,
                composite_directional_score REAL,
                cds_coverage REAL,
                cds_confidence TEXT,
                bias_classification TEXT,
                gift_nifty_gap_pct REAL,
                gift_nifty_age_sec INTEGER,
                dow_jones_pct REAL,
                brent_crude_pct REAL,
                usdinr_pct REAL,
                news_bias_score REAL,
                fii_futures_5d_cr REAL,
                pulse_sentiment TEXT,
                vix_open REAL,
                call_entry_price REAL,
                put_entry_price REAL,
                total_debit_inr REAL,
                decision_time TEXT,
                selected_leg TEXT,
                closed_leg_exit_price REAL,
                closed_leg_pnl_inr REAL,
                runner_exit_price REAL,
                runner_pnl_inr REAL,
                net_pnl_inr REAL,
                net_roi_pct REAL,
                peak_roi_pct REAL,
                spot_confirmed_ticks INTEGER,
                exit_reason TEXT,
                status TEXT NOT NULL,
                report_sent INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS opening_straddle_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                ts INTEGER NOT NULL,
                nifty_spot REAL NOT NULL,
                call_bid REAL NOT NULL,
                call_ask REAL NOT NULL,
                put_bid REAL NOT NULL,
                put_ask REAL NOT NULL,
                combined_net_pnl_pct REAL NOT NULL,
                quote_age_sec REAL,
                state TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_straddle_ticks_date ON opening_straddle_ticks (trade_date)")
        try:
            conn.execute("ALTER TABLE opening_straddle_sessions ADD COLUMN report_sent INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE opening_straddle_sessions ADD COLUMN execution_mode TEXT DEFAULT 'BUY_STRADDLE'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE opening_straddle_sessions ADD COLUMN runner_peak_roi_pct REAL DEFAULT 0.0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE opening_straddle_ticks ADD COLUMN quote_age_sec REAL")
        except sqlite3.OperationalError:
            pass
        conn.commit()


class OpeningStraddleEngine:
    """
    Finite State Machine driving the daily opening straddle lifecycle.
    """

    def __init__(
        self,
        pre_market: PreMarketIntelligence,
        trade_date: Optional[str] = None,
        expiry_date: Optional[str] = None,
        lot_size: int = DEFAULT_LOT_SIZE,
        friction_inr: float = DEFAULT_ESTIMATED_FRICTION_INR,
        db_path: Optional[str] = None,
        profit_target_roi: float = DEFAULT_PROFIT_TARGET_ROI,
        stop_loss_roi: float = DEFAULT_STOP_LOSS_ROI,
        trailing_trigger_roi: float = DEFAULT_TRAILING_TRIGGER_ROI,
        trailing_drop_roi: float = DEFAULT_TRAILING_DROP_ROI,
        runner_profit_target_pct: float = DEFAULT_RUNNER_PROFIT_TARGET_PCT,
        runner_stop_loss_pct: float = DEFAULT_RUNNER_STOP_LOSS_PCT,
        runner_trailing_trigger_pct: float = DEFAULT_RUNNER_TRAILING_TRIGGER_PCT,
        runner_trailing_drop_pct: float = DEFAULT_RUNNER_TRAILING_DROP_PCT,
        execution_mode: StraddleExecutionMode = StraddleExecutionMode.BUY_STRADDLE,
        enable_alerts: bool = False,
    ):
        self.pre_market = pre_market
        self.trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        self.expiry_date = expiry_date or resolve_opening_straddle_expiry().strftime("%Y-%m-%d")
        self.lot_size = lot_size
        self.friction_inr = friction_inr
        self.profit_target_roi = profit_target_roi
        self.stop_loss_roi = stop_loss_roi
        self.trailing_trigger_roi = trailing_trigger_roi
        self.trailing_drop_roi = trailing_drop_roi
        self.runner_profit_target_pct = runner_profit_target_pct
        self.runner_stop_loss_pct = runner_stop_loss_pct
        self.runner_trailing_trigger_pct = runner_trailing_trigger_pct
        self.runner_trailing_drop_pct = runner_trailing_drop_pct
        self.execution_mode = (
            execution_mode
            if isinstance(execution_mode, StraddleExecutionMode)
            else StraddleExecutionMode(execution_mode)
        )
        self.enable_alerts = enable_alerts

        default_db = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "opening_straddle.db"
        )
        self.db_path = db_path or os.getenv("OPENING_STRADDLE_DB", default_db)

        # FSM State & Order Details
        self.state = StrategyState.PRE_MARKET_ARMED
        self.atm_strike: Optional[int] = None
        self.s0: Optional[float] = None              # Initial spot price
        self.entry_ts: Optional[float] = None
        self.call_leg: Optional[StraddleLegQuote] = None
        self.put_leg: Optional[StraddleLegQuote] = None
        self.total_debit_inr: float = 0.0

        # Decoupling & Runner State
        self.selected_runner: SelectedLeg = SelectedLeg.NONE
        self.closed_leg: SelectedLeg = SelectedLeg.NONE
        self.decision_time_str: Optional[str] = None
        self.peak_roi_pct: float = 0.0
        self.runner_peak_roi_pct: float = 0.0
        self.spot_confirmed_ticks: int = 0
        self.last_spot_direction: int = 0          # +1 for up, -1 for down, 0 for flat

        # Exit & Outcome
        self.exit_reason: ExitReason = ExitReason.NONE
        self.net_pnl_inr: float = 0.0
        self.net_roi_pct: float = 0.0
        self.status: str = "IN_PROGRESS"
        self.tick_buffer: List[Dict[str, Any]] = []

        # Initialize SQLite table
        try:
            init_opening_straddle_db(self.db_path)
        except Exception as exc:
            logger.warning("Could not initialize opening straddle DB at %s: %s", self.db_path, exc)

    def arm_entry(
        self,
        spot_price: float,
        call_quote: StraddleLegQuote,
        put_quote: StraddleLegQuote,
        entry_ts: Optional[float] = None,
    ) -> bool:
        """
        Executes Dual Long Entry gate at 09:15:05 AM.
        Validates ATM strike matching, spread gate (<= 1.5%), and locks entry debit.
        """
        self.s0 = spot_price
        self.entry_ts = entry_ts if entry_ts is not None else time.time()
        self.atm_strike = int(round(spot_price / 50.0) * 50)

        # Volatility Regime Calibration:
        # Elevated VIX (> 18.0) experiences fast gamma moves; tighten trailing triggers.
        if self.pre_market.vix_regime == VolatilityRegime.ELEVATED:
            self.trailing_trigger_roi = 1.5
            self.runner_trailing_trigger_pct = 10.0
            self.runner_trailing_drop_pct = 5.0
            logger.info(
                "Elevated VIX regime (%.2f > 18.0) detected: tightened trailing triggers (basket=%.1f%%, runner=%.1f%%/%.1f%%).",
                self.pre_market.vix,
                self.trailing_trigger_roi,
                self.runner_trailing_trigger_pct,
                self.runner_trailing_drop_pct,
            )
        elif self.pre_market.vix_regime == VolatilityRegime.LOW_IV_CRUSH:
            logger.info(
                "Low IV Crush regime (%.2f < 11.5) detected: early scratch at 09:20:00 will be enforced if inconclusive.",
                self.pre_market.vix,
            )

        # 1. Spread Quality Gate (1.5% maximum spread)
        if call_quote.spread_pct > DEFAULT_MAX_SPREAD_PCT or put_quote.spread_pct > DEFAULT_MAX_SPREAD_PCT:
            is_depth_missing = (call_quote.spread_pct >= 999.0 or put_quote.spread_pct >= 999.0)
            logger.warning(
                "Execution quality gate breached: Call Spread=%.2f%%, Put Spread=%.2f%% (> %.2f%%). Aborting.",
                call_quote.spread_pct,
                put_quote.spread_pct,
                DEFAULT_MAX_SPREAD_PCT,
            )
            self.state = StrategyState.ABORTED
            self.exit_reason = ExitReason.EXECUTION_ABORT
            self.status = "ABORTED_NO_DEPTH" if is_depth_missing else "ABORTED"
            self._persist_session()
            return False

        # 2. Lock Entry Fills
        # Limit buy pegged at ask (or provided entry price)
        c0 = call_quote.entry_price if call_quote.entry_price is not None else call_quote.ask
        p0 = put_quote.entry_price if put_quote.entry_price is not None else put_quote.ask

        call_quote.entry_price = c0
        put_quote.entry_price = p0
        self.call_leg = call_quote
        self.put_leg = put_quote

        self.total_debit_inr = self.lot_size * (c0 + p0)
        self.state = StrategyState.OBSERVATION
        logger.info(
            "Dual Long Straddle Filled: Strike=%d | C0=%.2f, P0=%.2f | Total Debit=INR %.2f",
            self.atm_strike,
            c0,
            p0,
            self.total_debit_inr,
        )

        if self.enable_alerts:
            try:
                send_straddle_entry_alert(
                    atm_strike=self.atm_strike,
                    call_entry=c0,
                    put_entry=p0,
                    total_debit_inr=self.total_debit_inr,
                    intel=self.pre_market,
                    trade_date=self.trade_date,
                )
            except Exception as exc:
                logger.warning("Failed to dispatch entry alert: %s", exc)

        return True

    def process_tick(
        self,
        tick: TickObservation,
        force_eval_920: bool = False,
        force_eval_925: bool = False,
    ) -> StrategyState:
        """
        Processes a single live market tick and advances the FSM state.
        """
        if self.state in (StrategyState.CLOSED, StrategyState.ABORTED):
            return self.state

        if self.state == StrategyState.PRE_MARKET_ARMED:
            return self.state

        elapsed_sec = (tick.ts - self.entry_ts) if self.entry_ts else 0.0

        # Update tick returns
        r_c = ((tick.call_bid - self.call_leg.entry_price) / self.call_leg.entry_price) * 100.0
        r_p = ((tick.put_bid - self.put_leg.entry_price) / self.put_leg.entry_price) * 100.0
        r_n = ((tick.nifty_spot - self.s0) / self.s0) * 100.0

        self.call_leg.bid = tick.call_bid
        self.call_leg.ask = tick.call_ask
        self.put_leg.bid = tick.put_bid
        self.put_leg.ask = tick.put_ask

        # Compute live combined P&L and ROI for current tick
        if self.state in (StrategyState.OBSERVATION, StrategyState.EXTENDED_OBSERVATION):
            c_pnl = self.lot_size * (tick.call_bid - self.call_leg.entry_price)
            p_pnl = self.lot_size * (tick.put_bid - self.put_leg.entry_price)
            current_net_pnl = (c_pnl + p_pnl) - self.friction_inr
        elif self.selected_runner == SelectedLeg.CALL:
            c_pnl = self.lot_size * (tick.call_bid - self.call_leg.entry_price)
            current_net_pnl = (self.put_leg.pnl_inr + c_pnl) - self.friction_inr
        elif self.selected_runner == SelectedLeg.PUT:
            p_pnl = self.lot_size * (tick.put_bid - self.put_leg.entry_price)
            current_net_pnl = (self.call_leg.pnl_inr + p_pnl) - self.friction_inr
        else:
            current_net_pnl = 0.0

        current_net_roi = (current_net_pnl / self.total_debit_inr * 100.0) if self.total_debit_inr > 0 else 0.0
        self.net_pnl_inr = current_net_pnl
        self.net_roi_pct = current_net_roi
        if current_net_roi > self.peak_roi_pct:
            self.peak_roi_pct = current_net_roi

        # Buffer observation for batched database persistence
        self.tick_buffer.append({
            "trade_date": self.trade_date,
            "ts": int(tick.ts),
            "nifty_spot": float(tick.nifty_spot),
            "call_bid": float(tick.call_bid),
            "call_ask": float(tick.call_ask),
            "put_bid": float(tick.put_bid),
            "put_ask": float(tick.put_ask),
            "combined_net_pnl_pct": round(float(self.net_roi_pct), 4),
            "quote_age_sec": round(float(tick.quote_age_sec), 2) if tick.quote_age_sec is not None else None,
            "state": self.state.value,
        })

        # Track consecutive spot confirmation ticks
        current_spot_direction = 1 if r_n >= 0.08 else (-1 if r_n <= -0.08 else 0)
        if current_spot_direction != 0 and current_spot_direction == self.last_spot_direction:
            self.spot_confirmed_ticks += 1
        elif current_spot_direction != 0:
            self.spot_confirmed_ticks = 1
            self.last_spot_direction = current_spot_direction
        else:
            self.spot_confirmed_ticks = 0
            self.last_spot_direction = 0

        # -------------------------------------------------------------------
        # State: OBSERVATION (09:15:05 to 09:20:00)
        # -------------------------------------------------------------------
        if self.state == StrategyState.OBSERVATION:
            if elapsed_sec >= SEC_920_CHECKPOINT or force_eval_920:
                self._evaluate_checkpoint(tick, r_c, r_p, r_n, checkpoint_name="09:20:00")
            return self.state

        # -------------------------------------------------------------------
        # State: EXTENDED_OBSERVATION (09:20:00 to 09:25:00)
        # -------------------------------------------------------------------
        if self.state == StrategyState.EXTENDED_OBSERVATION:
            if elapsed_sec >= SEC_925_CHECKPOINT or force_eval_925:
                self._evaluate_checkpoint(tick, r_c, r_p, r_n, checkpoint_name="09:25:00")
            return self.state

        # -------------------------------------------------------------------
        # State: CALL_RUNNER or PUT_RUNNER
        # -------------------------------------------------------------------
        if self.state in (StrategyState.CALL_RUNNER, StrategyState.PUT_RUNNER):
            self._monitor_runner_pnl(tick, elapsed_sec)

        return self.state

    def _evaluate_checkpoint(
        self,
        tick: TickObservation,
        r_c: float,
        r_p: float,
        r_n: float,
        checkpoint_name: str,
    ):
        """
        Evaluates the dynamic decoupling matrix at 09:20:00 or 09:25:00.
        """
        logger.info(
            "Evaluating %s Checkpoint: Rc=%.2f%%, Rp=%.2f%%, Rn=%.2f%% (SpotTicks=%d)",
            checkpoint_name,
            r_c,
            r_p,
            r_n,
            self.spot_confirmed_ticks,
        )

        call_eligible = self._is_leg_decoupling_eligible(
            leg="CALL", r_win=r_c, r_lose=r_p, r_n=r_n
        )
        put_eligible = self._is_leg_decoupling_eligible(
            leg="PUT", r_win=r_p, r_lose=r_c, r_n=r_n
        )

        # Mutually exclusive decoupling
        if call_eligible and not put_eligible:
            self._decouple_and_retain_runner(
                runner=SelectedLeg.CALL,
                closed_leg=SelectedLeg.PUT,
                exit_price=tick.put_bid,
                checkpoint_name=checkpoint_name,
            )
        elif put_eligible and not call_eligible:
            self._decouple_and_retain_runner(
                runner=SelectedLeg.PUT,
                closed_leg=SelectedLeg.CALL,
                exit_price=tick.call_bid,
                checkpoint_name=checkpoint_name,
            )
        else:
            # Inconclusive or conflicting
            if checkpoint_name == "09:20:00":
                if self.pre_market.vix_regime == VolatilityRegime.LOW_IV_CRUSH:
                    logger.info(
                        "09:20:00 Low IV Crush regime (%.2f < 11.5) & inconclusive move. "
                        "Executing immediate Scratch Exit to avoid theta bleed.",
                        self.pre_market.vix,
                    )
                    self._execute_scratch_exit(tick, checkpoint_name="09:20:00")
                else:
                    logger.info("09:20:00 Inconclusive / Choppy. Holding both legs into Extended Observation.")
                    self.state = StrategyState.EXTENDED_OBSERVATION
            else:
                # 09:25:00 Final Checkpoint -> Scratch Exit
                logger.info("09:25:00 Decoupling failed. Executing Scratch Exit on both legs.")
                self._execute_scratch_exit(tick, checkpoint_name="09:25:00")

    def _is_leg_decoupling_eligible(
        self,
        leg: str,
        r_win: float,
        r_lose: float,
        r_n: float,
    ) -> bool:
        """
        Checks if candidate leg qualifies for retention based on user-approved rules:
          1. Spot Flatness Gate: |Rn| >= 0.05% required.
          2. Contrarian Opposing Move: Requires +4.0% return, >= 3 pp spread, |Rn| >= 0.15% across >= 2 ticks.
          3. Aligned or Neutral Move: Requires +2.5% return, >= 2 pp spread, Rn confirms direction.
        """
        # Rule 1: Spot Flatness Gate
        if abs(r_n) < 0.05:
            logger.debug("%s rejected: Underlying spot is flat (|Rn|=%.3f%% < 0.05%%)", leg, abs(r_n))
            return False

        is_call = (leg == "CALL")
        is_contrarian_candidate = False

        if self.pre_market.is_contrarian_filter_eligible:
            # Bullish CDS opposes PUT runner; Bearish CDS opposes CALL runner
            if is_call and self.pre_market.bias == BiasClassification.STRONG_BEARISH:
                is_contrarian_candidate = True
            elif not is_call and self.pre_market.bias == BiasClassification.STRONG_BULLISH:
                is_contrarian_candidate = True

        if is_contrarian_candidate:
            # Rule 2: Contrarian Move Protection
            min_return = 4.0
            min_spread = 3.0
            min_spot = 0.15
            spot_confirmed = (r_n >= min_spot if is_call else r_n <= -min_spot)
            ticks_confirmed = (self.spot_confirmed_ticks >= 2)

            passed = (
                r_win >= min_return
                and (r_win - r_lose) >= min_spread
                and spot_confirmed
                and ticks_confirmed
            )
            if not passed:
                logger.debug(
                    "Contrarian %s failed criteria: R_win=%.2f (>=4.0), Spread=%.2f (>=3.0), "
                    "Rn=%.3f (>=0.15), Ticks=%d (>=2)",
                    leg,
                    r_win,
                    r_win - r_lose,
                    abs(r_n),
                    self.spot_confirmed_ticks,
                )
            return passed
        else:
            # Rule 3: Aligned or Neutral Prior
            min_return = 2.5
            min_spread = 2.0
            min_spot = 0.08
            spot_confirmed = (r_n >= min_spot if is_call else r_n <= -min_spot)

            return (
                r_win >= min_return
                and (r_win - r_lose) >= min_spread
                and spot_confirmed
            )

    def _decouple_and_retain_runner(
        self,
        runner: SelectedLeg,
        closed_leg: SelectedLeg,
        exit_price: float,
        checkpoint_name: str,
    ):
        """Sells the losing leg and activates the autonomous runner (Option A)."""
        self.selected_runner = runner
        self.closed_leg = closed_leg
        self.decision_time_str = checkpoint_name

        if closed_leg == SelectedLeg.PUT:
            self.put_leg.exit_price = exit_price
            self.put_leg.pnl_inr = self.lot_size * (exit_price - self.put_leg.entry_price)
            self.put_leg.pnl_pct = ((exit_price - self.put_leg.entry_price) / self.put_leg.entry_price) * 100.0
            self.state = StrategyState.CALL_RUNNER
            initial_runner_roi = (
                ((self.call_leg.bid - self.call_leg.entry_price) / self.call_leg.entry_price) * 100.0
                if self.call_leg and self.call_leg.entry_price
                else 0.0
            )
            self.runner_peak_roi_pct = max(0.0, initial_runner_roi)
            logger.info(
                "DECOUPLED: Retaining CALL Runner (Initial ROI: %.2f%%) | PUT Closed at %.2f (P&L: INR %.2f, %.2f%%)",
                initial_runner_roi,
                exit_price,
                self.put_leg.pnl_inr,
                self.put_leg.pnl_pct,
            )
        else:
            self.call_leg.exit_price = exit_price
            self.call_leg.pnl_inr = self.lot_size * (exit_price - self.call_leg.entry_price)
            self.call_leg.pnl_pct = ((exit_price - self.call_leg.entry_price) / self.call_leg.entry_price) * 100.0
            self.state = StrategyState.PUT_RUNNER
            initial_runner_roi = (
                ((self.put_leg.bid - self.put_leg.entry_price) / self.put_leg.entry_price) * 100.0
                if self.put_leg and self.put_leg.entry_price
                else 0.0
            )
            self.runner_peak_roi_pct = max(0.0, initial_runner_roi)
            logger.info(
                "DECOUPLED: Retaining PUT Runner (Initial ROI: %.2f%%) | CALL Closed at %.2f (P&L: INR %.2f, %.2f%%)",
                initial_runner_roi,
                exit_price,
                self.call_leg.pnl_inr,
                self.call_leg.pnl_pct,
            )

        if self.enable_alerts:
            try:
                closed_quote = self.put_leg if closed_leg == SelectedLeg.PUT else self.call_leg
                live_roi = (closed_quote.pnl_inr / self.total_debit_inr) * 100.0 if self.total_debit_inr > 0 else 0.0
                send_decoupling_alert(
                    checkpoint_name=checkpoint_name,
                    selected_runner=runner,
                    closed_leg=closed_leg,
                    closed_exit_price=exit_price,
                    closed_pnl_inr=closed_quote.pnl_inr,
                    closed_pnl_pct=closed_quote.pnl_pct,
                    live_roi_pct=live_roi,
                )
            except Exception as exc:
                logger.warning("Failed to dispatch decoupling alert: %s", exc)

    def _monitor_runner_pnl(self, tick: TickObservation, elapsed_sec: float):
        """
        Monitors runner leg with autonomous profit protection (Option A).
        The runner is independent of the closed leg's past loss and runs until 09:55 AM.
        """
        # Hard Time Stop: 09:55:00 AM (2400 seconds)
        if elapsed_sec >= SEC_955_HARD_EXIT:
            logger.info("09:55:00 Hard Time Stop reached. Liquidating runner.")
            runner_bid = tick.call_bid if self.selected_runner == SelectedLeg.CALL else tick.put_bid
            self._close_runner_and_finish(
                exit_price=runner_bid, exit_reason=ExitReason.HARD_TIME_STOP
            )
            return

        # Runner-specific return & P&L
        if self.selected_runner == SelectedLeg.CALL:
            runner_bid = tick.call_bid
            runner_entry = self.call_leg.entry_price
            realized_pnl = self.put_leg.pnl_inr
            unrealized_pnl = self.lot_size * (tick.call_bid - self.call_leg.entry_price)
        else:
            runner_bid = tick.put_bid
            runner_entry = self.put_leg.entry_price
            realized_pnl = self.call_leg.pnl_inr
            unrealized_pnl = self.lot_size * (tick.put_bid - self.put_leg.entry_price)

        # Runner individual return from entry
        runner_roi_pct = (
            ((runner_bid - runner_entry) / runner_entry) * 100.0
            if runner_entry and runner_entry > 0
            else 0.0
        )
        if runner_roi_pct > self.runner_peak_roi_pct:
            self.runner_peak_roi_pct = runner_roi_pct

        # Combined Session Net P&L & Net ROI (for auditing and total basket profit taking)
        gross_pnl = realized_pnl + unrealized_pnl
        net_pnl = gross_pnl - self.friction_inr
        net_roi = (net_pnl / self.total_debit_inr) * 100.0 if self.total_debit_inr > 0 else 0.0

        if net_roi > self.peak_roi_pct:
            self.peak_roi_pct = net_roi

        # 1. Profit Target Exit:
        # Triggered if Runner reaches individual target (+35.0%) OR total basket reaches combined target (+3.0%)
        if runner_roi_pct >= self.runner_profit_target_pct:
            logger.info(
                "Runner Profit Target Secured: Runner ROI=%.2f%% >= %.2f%% (Net ROI=%.2f%%)",
                runner_roi_pct,
                self.runner_profit_target_pct,
                net_roi,
            )
            self._close_runner_and_finish(
                exit_price=runner_bid, exit_reason=ExitReason.PROFIT_TARGET
            )
            return

        if net_roi >= self.profit_target_roi:
            logger.info(
                "Combined Basket Profit Target Secured: Net ROI=%.2f%% >= %.2f%% (Runner ROI=%.2f%%)",
                net_roi,
                self.profit_target_roi,
                runner_roi_pct,
            )
            self._close_runner_and_finish(
                exit_price=runner_bid, exit_reason=ExitReason.PROFIT_TARGET
            )
            return

        # 2. Runner Stop Loss Exit:
        # Evaluated purely on the runner's own performance to prevent early liquidation from the closed leg's past loss!
        if runner_roi_pct <= self.runner_stop_loss_pct:
            logger.info(
                "Runner Stop Loss Triggered: Runner ROI=%.2f%% <= %.2f%%",
                runner_roi_pct,
                self.runner_stop_loss_pct,
            )
            self._close_runner_and_finish(
                exit_price=runner_bid, exit_reason=ExitReason.STOP_LOSS
            )
            return

        # 3. Trailing Peak Protection:
        # Triggers if runner individual peak triggers trail OR basket peak triggers trail
        runner_trail_triggered = (
            self.runner_peak_roi_pct >= self.runner_trailing_trigger_pct
            and runner_roi_pct <= (self.runner_peak_roi_pct - self.runner_trailing_drop_pct)
        )
        basket_trail_triggered = (
            self.peak_roi_pct >= self.trailing_trigger_roi
            and net_roi <= (self.peak_roi_pct - self.trailing_drop_roi)
        )

        if runner_trail_triggered or basket_trail_triggered:
            trigger_source = "Runner" if runner_trail_triggered else "Combined Basket"
            logger.info(
                "Trailing Stop Triggered (%s): Runner Peak=%.2f%%, Curr=%.2f%% | Basket Peak=%.2f%%, Curr=%.2f%%",
                trigger_source,
                self.runner_peak_roi_pct,
                runner_roi_pct,
                self.peak_roi_pct,
                net_roi,
            )
            self._close_runner_and_finish(
                exit_price=runner_bid, exit_reason=ExitReason.TRAILING_STOP
            )
            return

    def _close_runner_and_finish(self, exit_price: float, exit_reason: ExitReason):
        """Liquidates the runner leg, calculates final P&L, and persists session."""
        if self.selected_runner == SelectedLeg.CALL:
            self.call_leg.exit_price = exit_price
            self.call_leg.pnl_inr = self.lot_size * (exit_price - self.call_leg.entry_price)
            self.call_leg.pnl_pct = ((exit_price - self.call_leg.entry_price) / self.call_leg.entry_price) * 100.0
            gross_pnl = self.put_leg.pnl_inr + self.call_leg.pnl_inr
        else:
            self.put_leg.exit_price = exit_price
            self.put_leg.pnl_inr = self.lot_size * (exit_price - self.put_leg.entry_price)
            self.put_leg.pnl_pct = ((exit_price - self.put_leg.entry_price) / self.put_leg.entry_price) * 100.0
            gross_pnl = self.call_leg.pnl_inr + self.put_leg.pnl_inr

        self.net_pnl_inr = gross_pnl - self.friction_inr
        self.net_roi_pct = (self.net_pnl_inr / self.total_debit_inr) * 100.0 if self.total_debit_inr > 0 else 0.0
        self.exit_reason = exit_reason
        self.state = StrategyState.CLOSED
        self.status = "PAPER_TRADING"

        logger.info(
            "Session Closed [%s]: Net P&L=INR %.2f | Net ROI=%.2f%% | Peak ROI=%.2f%%",
            exit_reason.value,
            self.net_pnl_inr,
            self.net_roi_pct,
            self.peak_roi_pct,
        )
        self._persist_session()

    def _execute_scratch_exit(self, tick: TickObservation, checkpoint_name: str = "09:25:00"):
        """Flattens both legs when decoupling is inconclusive (09:20:00 for Low IV Crush or 09:25:00 final)."""
        self.call_leg.exit_price = tick.call_bid
        self.call_leg.pnl_inr = self.lot_size * (tick.call_bid - self.call_leg.entry_price)
        self.call_leg.pnl_pct = ((tick.call_bid - self.call_leg.entry_price) / self.call_leg.entry_price) * 100.0

        self.put_leg.exit_price = tick.put_bid
        self.put_leg.pnl_inr = self.lot_size * (tick.put_bid - self.put_leg.entry_price)
        self.put_leg.pnl_pct = ((tick.put_bid - self.put_leg.entry_price) / self.put_leg.entry_price) * 100.0

        self.selected_runner = SelectedLeg.SCRATCH
        self.closed_leg = SelectedLeg.SCRATCH
        self.decision_time_str = checkpoint_name
        self.exit_reason = ExitReason.SCRATCH

        gross_pnl = self.call_leg.pnl_inr + self.put_leg.pnl_inr
        self.net_pnl_inr = gross_pnl - self.friction_inr
        self.net_roi_pct = (self.net_pnl_inr / self.total_debit_inr) * 100.0 if self.total_debit_inr > 0 else 0.0
        self.state = StrategyState.CLOSED
        self.status = "PAPER_TRADING"

        logger.info(
            "Scratch Exit Completed [%s]: Net P&L=INR %.2f | Net ROI=%.2f%%",
            checkpoint_name,
            self.net_pnl_inr,
            self.net_roi_pct,
        )

        if self.enable_alerts:
            try:
                send_decoupling_alert(
                    checkpoint_name=checkpoint_name,
                    selected_runner=SelectedLeg.SCRATCH,
                    closed_leg=SelectedLeg.SCRATCH,
                    closed_exit_price=0.0,
                    closed_pnl_inr=gross_pnl,
                    closed_pnl_pct=self.net_roi_pct,
                    live_roi_pct=self.net_roi_pct,
                )
            except Exception as exc:
                logger.warning("Failed to dispatch scratch decoupling alert: %s", exc)

        self._persist_session()

    def to_session_record(self) -> StraddleSessionRecord:
        """Constructs a StraddleSessionRecord dataclass from current engine state."""
        call_entry = self.call_leg.entry_price if self.call_leg and self.call_leg.entry_price is not None else 0.0
        put_entry = self.put_leg.entry_price if self.put_leg and self.put_leg.entry_price is not None else 0.0

        if self.selected_runner == SelectedLeg.CALL:
            closed_exit = self.put_leg.exit_price if self.put_leg else None
            closed_pnl = self.put_leg.pnl_inr if self.put_leg else 0.0
            runner_exit = self.call_leg.exit_price if self.call_leg else None
            runner_pnl = self.call_leg.pnl_inr if self.call_leg else 0.0
        elif self.selected_runner == SelectedLeg.PUT:
            closed_exit = self.call_leg.exit_price if self.call_leg else None
            closed_pnl = self.call_leg.pnl_inr if self.call_leg else 0.0
            runner_exit = self.put_leg.exit_price if self.put_leg else None
            runner_pnl = self.put_leg.pnl_inr if self.put_leg else 0.0
        else:
            closed_exit = None
            closed_pnl = 0.0
            runner_exit = None
            runner_pnl = 0.0

        return StraddleSessionRecord(
            trade_date=self.trade_date,
            expiry_date=self.expiry_date,
            atm_strike=self.atm_strike or 0,
            pre_market=self.pre_market,
            call_entry_price=call_entry,
            put_entry_price=put_entry,
            total_debit_inr=self.total_debit_inr,
            state=self.state,
            selected_leg=self.selected_runner,
            decision_time=self.decision_time_str,
            closed_leg_exit_price=closed_exit,
            closed_leg_pnl_inr=closed_pnl,
            runner_exit_price=runner_exit,
            runner_pnl_inr=runner_pnl,
            net_pnl_inr=self.net_pnl_inr,
            net_roi_pct=self.net_roi_pct,
            peak_roi_pct=self.peak_roi_pct,
            runner_peak_roi_pct=self.runner_peak_roi_pct,
            spot_confirmed_ticks=self.spot_confirmed_ticks,
            exit_reason=self.exit_reason,
            execution_mode=self.execution_mode.value,
            status=self.status,
        )

    def _persist_session(self):
        """Persists the final session audit record to opening_straddle.db."""
        if not self.db_path:
            return

        comp_gift = self.pre_market.get_component("gift_nifty")
        comp_macro = self.pre_market.get_component("global_macro")
        comp_news = self.pre_market.get_component("news_nlp")
        comp_flow = self.pre_market.get_component("fii_flow")
        comp_pulse = self.pre_market.get_component("upstox_pulse")

        record = {
            "trade_date": self.trade_date,
            "expiry_date": self.expiry_date,
            "atm_strike": self.atm_strike or 0,
            "composite_directional_score": self.pre_market.cds,
            "cds_coverage": self.pre_market.coverage,
            "cds_confidence": self.pre_market.confidence.value,
            "bias_classification": self.pre_market.bias.value,
            "gift_nifty_gap_pct": comp_gift.raw_value.get("change_pct") if (comp_gift and comp_gift.raw_value) else None,
            "gift_nifty_age_sec": int(comp_gift.age_sec) if (comp_gift and comp_gift.age_sec is not None) else None,
            "dow_jones_pct": comp_macro.raw_value.get("dow_pct") if (comp_macro and comp_macro.raw_value) else None,
            "brent_crude_pct": comp_macro.raw_value.get("brent_pct") if (comp_macro and comp_macro.raw_value) else None,
            "usdinr_pct": comp_macro.raw_value.get("usdinr_pct") if (comp_macro and comp_macro.raw_value) else None,
            "news_bias_score": comp_news.raw_value if (comp_news and comp_news.raw_value is not None) else None,
            "fii_futures_5d_cr": comp_flow.raw_value if (comp_flow and comp_flow.raw_value is not None) else None,
            "pulse_sentiment": self.pre_market.upstox_pulse_levels.get("overall_sentiment") if self.pre_market.upstox_pulse_levels else None,
            "vix_open": self.pre_market.vix,
            "call_entry_price": self.call_leg.entry_price if self.call_leg else 0.0,
            "put_entry_price": self.put_leg.entry_price if self.put_leg else 0.0,
            "total_debit_inr": self.total_debit_inr,
            "decision_time": self.decision_time_str,
            "selected_leg": self.selected_runner.value,
            "closed_leg_exit_price": self.put_leg.exit_price if self.selected_runner == SelectedLeg.CALL else (self.call_leg.exit_price if self.call_leg else None),
            "closed_leg_pnl_inr": self.put_leg.pnl_inr if self.selected_runner == SelectedLeg.CALL else (self.call_leg.pnl_inr if self.call_leg else 0.0),
            "runner_exit_price": self.call_leg.exit_price if self.selected_runner == SelectedLeg.CALL else (self.put_leg.exit_price if self.selected_runner == SelectedLeg.PUT else None),
            "runner_pnl_inr": self.call_leg.pnl_inr if self.selected_runner == SelectedLeg.CALL else (self.put_leg.pnl_inr if self.selected_runner == SelectedLeg.PUT else 0.0),
            "net_pnl_inr": self.net_pnl_inr,
            "net_roi_pct": self.net_roi_pct,
            "peak_roi_pct": self.peak_roi_pct,
            "runner_peak_roi_pct": self.runner_peak_roi_pct,
            "spot_confirmed_ticks": self.spot_confirmed_ticks,
            "exit_reason": self.exit_reason.value,
            "execution_mode": self.execution_mode.value,
            "status": self.status,
        }

        try:
            with sqlite3.connect(self.db_path) as conn:
                cols = ", ".join(record.keys())
                placeholders = ", ".join("?" for _ in record)
                sql = f"INSERT OR REPLACE INTO opening_straddle_sessions ({cols}) VALUES ({placeholders})"
                conn.execute(sql, list(record.values()))

                if self.tick_buffer:
                    tick_rows = [
                        (
                            t["trade_date"],
                            t["ts"],
                            t["nifty_spot"],
                            t["call_bid"],
                            t["call_ask"],
                            t["put_bid"],
                            t["put_ask"],
                            t["combined_net_pnl_pct"],
                            t["quote_age_sec"],
                            t["state"],
                        )
                        for t in self.tick_buffer
                    ]
                    conn.executemany(
                        """
                        INSERT INTO opening_straddle_ticks (
                            trade_date, ts, nifty_spot, call_bid, call_ask, put_bid, put_ask, combined_net_pnl_pct, quote_age_sec, state
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        tick_rows,
                    )
                    logger.info("Persisted %d ticks to opening_straddle_ticks in %s", len(tick_rows), self.db_path)
                    self.tick_buffer.clear()

                conn.commit()
            logger.info("Persisted session record to %s", self.db_path)
        except Exception as exc:
            logger.error("Failed to persist session record to %s: %s", self.db_path, exc)

        if (
            self.enable_alerts
            and self.state == StrategyState.CLOSED
            and self.exit_reason != ExitReason.NONE
        ):
            try:
                sent = send_straddle_exit_summary(self.to_session_record())
                if sent and self.db_path:
                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute(
                            "UPDATE opening_straddle_sessions SET report_sent = 1 WHERE trade_date = ?",
                            (self.trade_date,),
                        )
                        conn.commit()
            except Exception as exc:
                logger.warning("Failed to dispatch exit summary alert: %s", exc)

    def flush_interrupted_session(self):
        """Orderly flush of buffered ticks and session state upon KeyboardInterrupt without false exit alerts."""
        if self.state not in (StrategyState.CLOSED, StrategyState.ABORTED):
            self.status = "INTERRUPTED"
        self._persist_session()
