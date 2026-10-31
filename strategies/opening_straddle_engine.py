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

IST_TZ = timezone(timedelta(hours=5, minutes=30))
from strategies.opening_straddle_alerts import (
    send_decoupling_alert,
    send_straddle_entry_alert,
    send_straddle_exit_summary,
)
from strategies.opening_straddle_models import (
    BiasClassification,
    ConfidenceLevel,
    ExitReason,
    OrderAction,
    PreMarketIntelligence,
    SelectedLeg,
    StrategyState,
    StraddleExecutionMode,
    StraddleLegQuote,
    StraddleSessionRecord,
    TickObservation,
    TradeLeg,
    VolatilityRegime,
    derive_bias_from_cms,
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
DEFAULT_ESTIMATED_FRICTION_INR = 150.0  # Estimated round-trip friction for 1-lot dual straddle
DEFAULT_LOT_SIZE = 65                 # Nifty lot size per NSE contract specs

# Directional Single-Leg Scalper Operational Parameters (Rule 1, 2, 3)
DEFAULT_SINGLE_LEG_FRICTION_INR = 75.0      # 1-lot single leg round-trip friction (half of straddle)
DEFAULT_SHORT_MARGIN_PER_LOT = float(
    os.getenv("SHORT_MARGIN_PER_LOT", "120000.0")
)  # Exchange margin capital base for 1 lot short option
DEFAULT_SCALP_TARGET_POINTS = 8.0          # +8.0 option points net profit target (~INR 520)
DEFAULT_SCALP_TARGET_PREMIUM_BUY_PCT = 10.0 # +10% premium gain target on BUY
DEFAULT_SCALP_TARGET_PREMIUM_SELL_PCT = 12.0# +12% premium decay target on SELL
DEFAULT_SCALP_TRAILING_ARM_POINTS = 4.0     # Arm trailing ratchet once +4.0 points net profit reached
DEFAULT_SCALP_TRAILING_ARM_PCT = 6.0        # Arm trailing ratchet once +6.0% premium profit reached
DEFAULT_SCALP_TRAILING_DISTANCE_POINTS = 2.0# Trail 2.0 points behind peak
DEFAULT_SCALP_STOP_LOSS_POINTS = -8.0       # -8.0 points disciplined risk stop
DEFAULT_SCALP_STOP_LOSS_PREMIUM_PCT = -15.0 # -15% stop loss on premium

# Independent Runner Management Parameters (Option A)
DEFAULT_RUNNER_PROFIT_TARGET_PCT = 35.0    # +35.0% on runner premium
DEFAULT_RUNNER_STOP_LOSS_PCT = -10.0       # -10.0% on runner premium (individual stop)
DEFAULT_RUNNER_TRAILING_TRIGGER_PCT = 15.0 # Runner Peak >= +15.0% enables trailing stop
DEFAULT_RUNNER_TRAILING_DROP_PCT = 8.0     # Trail 8.0% behind runner peak

# Checkpoint time thresholds (seconds from 09:15:00 AM open)
SEC_920_CHECKPOINT = 300              # 5 minutes in (09:20:00)
SEC_925_CHECKPOINT = 600              # 10 minutes in (09:25:00)
SEC_955_HARD_EXIT = 2400              # 40 minutes in from 09:15:00 (09:55:00 for dual straddle)
SINGLE_LEG_MAX_HOLDING_SEC = 2335     # Exactly 38m 55s from 09:16:05 entry to 09:55:00 AM IST hard stop


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
                report_sent INTEGER DEFAULT 0,
                order_action TEXT DEFAULT 'BUY',
                trade_leg TEXT DEFAULT 'NONE',
                executed_strike INTEGER,
                entry_price REAL,
                exit_price REAL,
                margin_inr REAL DEFAULT 0.0,
                premium_pnl_pct REAL DEFAULT 0.0,
                capital_deployed_inr REAL DEFAULT 0.0
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
        # Auto-migration for single-leg directional scalper columns
        single_leg_cols = [
            ("order_action", "TEXT DEFAULT 'BUY'"),
            ("trade_leg", "TEXT DEFAULT 'NONE'"),
            ("executed_strike", "INTEGER"),
            ("entry_price", "REAL"),
            ("exit_price", "REAL"),
            ("margin_inr", "REAL DEFAULT 0.0"),
            ("premium_pnl_pct", "REAL DEFAULT 0.0"),
            ("capital_deployed_inr", "REAL DEFAULT 0.0"),
            ("final_bias", "TEXT DEFAULT NULL"),
        ]
        for col_name, col_def in single_leg_cols:
            try:
                conn.execute(f"ALTER TABLE opening_straddle_sessions ADD COLUMN {col_name} {col_def}")
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
        short_margin_per_lot: Optional[float] = None,
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
        if short_margin_per_lot is not None:
            self.short_margin_per_lot = float(short_margin_per_lot)
        elif os.getenv("SHORT_MARGIN_PER_LOT") is not None:
            self.short_margin_per_lot = float(os.getenv("SHORT_MARGIN_PER_LOT"))
        else:
            self.short_margin_per_lot = DEFAULT_SHORT_MARGIN_PER_LOT
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

        # Single-Leg Directional Scalp State (Rule 1, 2, 3)
        self.order_action: OrderAction = OrderAction.BUY
        self.trade_leg: TradeLeg = TradeLeg.NONE
        self.executed_strike: Optional[int] = None
        self.entry_price: Optional[float] = None
        self.exit_price: Optional[float] = None
        self.margin_inr: float = 0.0
        self.capital_deployed_inr: float = 0.0
        self.premium_pnl_pct: float = 0.0
        self.peak_premium_pnl_pct: float = 0.0
        self.peak_net_points: float = 0.0
        self.single_leg_quote: Optional[StraddleLegQuote] = None
        self.trailing_ratchet_armed: bool = False
        self.final_cms: Optional[float] = None
        self.final_bias: Optional[BiasClassification] = None

        # Initialize SQLite table
        try:
            init_opening_straddle_db(self.db_path)
        except Exception as exc:
            logger.warning("Could not initialize opening straddle DB at %s: %s", self.db_path, exc)

    def arm_single_leg_entry(
        self,
        leg: TradeLeg,
        action: OrderAction,
        strike: int,
        quote: StraddleLegQuote,
        spot_price: float,
        entry_ts: Optional[float] = None,
        margin_inr: Optional[float] = None,
        final_cms: Optional[float] = None,
    ) -> bool:
        """
        Executes Directional Single-Leg Entry gate (Rule 1).
        Supports:
          - BUY CALL / BUY PUT: Enters at ask.
          - SELL CALL / SELL PUT: Enters at bid.
        Validates spread gate (<= 1.5%) and locks single-leg position.
        """
        self.execution_mode = StraddleExecutionMode.SINGLE_LEG_DIRECTIONAL
        self.friction_inr = DEFAULT_SINGLE_LEG_FRICTION_INR
        self.s0 = spot_price
        self.entry_ts = entry_ts if entry_ts is not None else time.time()
        self.trade_leg = leg if isinstance(leg, TradeLeg) else TradeLeg(leg)
        self.order_action = action if isinstance(action, OrderAction) else OrderAction(action)
        self.executed_strike = strike
        self.atm_strike = int(round(spot_price / 50.0) * 50)
        resolved_margin = margin_inr if margin_inr is not None else self.short_margin_per_lot
        self.final_cms = final_cms
        self.final_bias = derive_bias_from_cms(final_cms) if final_cms is not None else getattr(self.pre_market, "final_bias", None)
        if final_cms is not None:
            self.pre_market.composite_morning_score = final_cms
            self.pre_market.final_bias = self.final_bias

        # 1. Spread Quality Gate (1.5% maximum spread)
        if quote.spread_pct > DEFAULT_MAX_SPREAD_PCT:
            is_depth_missing = quote.spread_pct >= 999.0
            logger.warning(
                "Single leg execution quality gate breached: %s %s Strike=%d Spread=%.2f%% (> %.2f%%). Aborting.",
                self.order_action.value,
                self.trade_leg.value,
                strike,
                quote.spread_pct,
                DEFAULT_MAX_SPREAD_PCT,
            )
            self.state = StrategyState.ABORTED
            self.exit_reason = ExitReason.EXECUTION_ABORT
            self.status = "ABORTED_NO_DEPTH" if is_depth_missing else "ABORTED"
            self._persist_session()
            return False

        # 2. Lock Executable Entry Price
        # BUY: enter at Ask. SELL: enter at Bid.
        if self.order_action == OrderAction.BUY:
            p0 = quote.entry_price if quote.entry_price is not None else quote.ask
            self.entry_price = p0
            self.margin_inr = 0.0
            self.capital_deployed_inr = self.lot_size * p0
            self.total_debit_inr = self.capital_deployed_inr
        else: # SELL
            p0 = quote.entry_price if quote.entry_price is not None else quote.bid
            self.entry_price = p0
            self.margin_inr = resolved_margin
            self.capital_deployed_inr = resolved_margin
            self.total_debit_inr = resolved_margin

        quote.entry_price = p0
        self.single_leg_quote = quote
        if self.trade_leg == TradeLeg.CALL:
            self.call_leg = quote
        elif self.trade_leg == TradeLeg.PUT:
            self.put_leg = quote

        self.state = StrategyState.SINGLE_LEG_ACTIVE
        logger.info(
            "Single-Leg Directional Position Armed: %s %s Strike=%d | P0=%.2f | Capital=INR %.2f (Margin=INR %.2f)",
            self.order_action.value,
            self.trade_leg.value,
            self.executed_strike,
            p0,
            self.capital_deployed_inr,
            self.margin_inr,
        )

        if self.enable_alerts:
            try:
                from strategies.opening_straddle_alerts import send_single_leg_entry_alert
                send_single_leg_entry_alert(
                    leg=self.trade_leg,
                    action=self.order_action,
                    strike=self.executed_strike,
                    entry_price=p0,
                    capital_deployed_inr=self.capital_deployed_inr,
                    margin_inr=self.margin_inr,
                    intel=self.pre_market,
                    trade_date=self.trade_date,
                    final_cms=self.final_cms,
                )
            except Exception as exc:
                logger.warning("Failed to dispatch single-leg entry alert: %s", exc)

        return True

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

        # Route Single-Leg Directional Scalp
        if self.state == StrategyState.SINGLE_LEG_ACTIVE:
            self._monitor_single_leg_scalp(tick, elapsed_sec)
            return self.state

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

    def _monitor_single_leg_scalp(self, tick: TickObservation, elapsed_sec: float):
        """
        Monitors active single leg position with Quick Scalp Profit Targets & Stops (Rule 3):
          1. Hard Time Stop at 09:55:00 AM IST (elapsed_sec >= 2400)
          2. Target Profit: Net points >= +8.0 pts OR Premium gain >= +10% (BUY) / +12% (SELL)
          3. Dynamic Trailing Ratchet: Arms at +4.0 pts or +6.0%, trails 2.0 pts behind peak
          4. Disciplined Risk Stop: Net points <= -8.0 pts OR Premium loss <= -15.0%
        """
        is_call = (self.trade_leg == TradeLeg.CALL)
        if is_call:
            bid = tick.call_bid
            ask = tick.call_ask
            if self.call_leg:
                self.call_leg.bid = bid
                self.call_leg.ask = ask
        else:
            bid = tick.put_bid
            ask = tick.put_ask
            if self.put_leg:
                self.put_leg.bid = bid
                self.put_leg.ask = ask

        # Executable mark-to-market:
        # Long exits at BID. Short exits at ASK (buy to cover).
        current_exit_price = bid if self.order_action == OrderAction.BUY else ask

        if self.order_action == OrderAction.BUY:
            gross_points = current_exit_price - self.entry_price
        else: # SELL
            gross_points = self.entry_price - current_exit_price

        gross_pnl = self.lot_size * gross_points
        net_pnl = gross_pnl - self.friction_inr
        net_points = net_pnl / self.lot_size if self.lot_size > 0 else 0.0
        premium_pnl_pct = (gross_points / self.entry_price) * 100.0 if self.entry_price > 0 else 0.0
        net_roi = (net_pnl / self.capital_deployed_inr * 100.0) if self.capital_deployed_inr > 0 else 0.0

        self.net_pnl_inr = net_pnl
        self.net_roi_pct = net_roi
        self.premium_pnl_pct = premium_pnl_pct

        if net_points > self.peak_net_points:
            self.peak_net_points = net_points
        if premium_pnl_pct > self.peak_premium_pnl_pct:
            self.peak_premium_pnl_pct = premium_pnl_pct
        if net_roi > self.peak_roi_pct:
            self.peak_roi_pct = net_roi

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

        # 1. Hard Time Stop: Absolute cutoff at 09:55:00 AM IST
        # Every single-leg position unconditionally liquidates at or before 09:55:00 AM IST (or when elapsed duration >= 2,335s).
        dt_ist = datetime.fromtimestamp(tick.ts, tz=timezone.utc).astimezone(IST_TZ)
        is_clock_time_stop = (dt_ist.hour > 9) or (dt_ist.hour == 9 and dt_ist.minute >= 55)
        is_elapsed_time_stop = (elapsed_sec >= SINGLE_LEG_MAX_HOLDING_SEC)

        if is_clock_time_stop or is_elapsed_time_stop:
            logger.info(
                "09:55:00 Hard Time Stop reached (IST=%s, elapsed=%.1fs >= %ds). Liquidating single-leg position.",
                dt_ist.strftime("%H:%M:%S"),
                elapsed_sec,
                SINGLE_LEG_MAX_HOLDING_SEC,
            )
            self._close_single_leg(exit_price=current_exit_price, exit_reason=ExitReason.HARD_TIME_STOP)
            return

        # 2. Minimum Profit Target (Quick Scalp)
        target_pct = (
            DEFAULT_SCALP_TARGET_PREMIUM_BUY_PCT
            if self.order_action == OrderAction.BUY
            else DEFAULT_SCALP_TARGET_PREMIUM_SELL_PCT
        )
        if net_points >= DEFAULT_SCALP_TARGET_POINTS or premium_pnl_pct >= target_pct:
            logger.info(
                "Quick Scalp Profit Target Hit: Points=%.2f (>=%.1f), Premium Gain=%.2f%% (>=%.1f%%)",
                net_points,
                DEFAULT_SCALP_TARGET_POINTS,
                premium_pnl_pct,
                target_pct,
            )
            self._close_single_leg(exit_price=current_exit_price, exit_reason=ExitReason.MIN_PROFIT_TARGET)
            return

        # 3. Dynamic Trailing Profit Ratchet
        if not self.trailing_ratchet_armed:
            if (
                net_points >= DEFAULT_SCALP_TRAILING_ARM_POINTS
                or premium_pnl_pct >= DEFAULT_SCALP_TRAILING_ARM_PCT
            ):
                self.trailing_ratchet_armed = True
                logger.info(
                    "Dynamic Trailing Ratchet ARMED at Points=%.2f, Premium=%.2f%%",
                    net_points,
                    premium_pnl_pct,
                )

        if self.trailing_ratchet_armed:
            if net_points <= (self.peak_net_points - DEFAULT_SCALP_TRAILING_DISTANCE_POINTS):
                logger.info(
                    "Dynamic Trailing Stop Triggered: Peak=%.2f pts, Curr=%.2f pts (Drop >= %.1f pts)",
                    self.peak_net_points,
                    net_points,
                    DEFAULT_SCALP_TRAILING_DISTANCE_POINTS,
                )
                self._close_single_leg(exit_price=current_exit_price, exit_reason=ExitReason.TRAILING_STOP)
                return

        # 4. Disciplined Risk Stop (Stop Loss)
        if (
            net_points <= DEFAULT_SCALP_STOP_LOSS_POINTS
            or premium_pnl_pct <= DEFAULT_SCALP_STOP_LOSS_PREMIUM_PCT
        ):
            logger.info(
                "Stop Loss Triggered: Net Points=%.2f (<=%.1f) or Premium Return=%.2f%% (<=%.1f%%)",
                net_points,
                DEFAULT_SCALP_STOP_LOSS_POINTS,
                premium_pnl_pct,
                DEFAULT_SCALP_STOP_LOSS_PREMIUM_PCT,
            )
            self._close_single_leg(exit_price=current_exit_price, exit_reason=ExitReason.STOP_LOSS)
            return

    def _close_single_leg(self, exit_price: float, exit_reason: ExitReason):
        """Liquidates the single-leg position, computes final P&L metrics, and persists session."""
        self.exit_price = exit_price
        if self.single_leg_quote:
            self.single_leg_quote.exit_price = exit_price

        if self.order_action == OrderAction.BUY:
            gross_points = exit_price - self.entry_price
        else: # SELL
            gross_points = self.entry_price - exit_price

        gross_pnl = self.lot_size * gross_points
        self.net_pnl_inr = gross_pnl - self.friction_inr
        self.premium_pnl_pct = (gross_points / self.entry_price) * 100.0 if self.entry_price > 0 else 0.0
        self.net_roi_pct = (self.net_pnl_inr / self.capital_deployed_inr * 100.0) if self.capital_deployed_inr > 0 else 0.0

        if self.trade_leg == TradeLeg.CALL:
            self.selected_runner = SelectedLeg.CALL
            if self.call_leg:
                self.call_leg.exit_price = exit_price
                self.call_leg.pnl_inr = gross_pnl
                self.call_leg.pnl_pct = self.premium_pnl_pct
        elif self.trade_leg == TradeLeg.PUT:
            self.selected_runner = SelectedLeg.PUT
            if self.put_leg:
                self.put_leg.exit_price = exit_price
                self.put_leg.pnl_inr = gross_pnl
                self.put_leg.pnl_pct = self.premium_pnl_pct

        self.exit_reason = exit_reason
        self.state = StrategyState.CLOSED
        self.status = "PAPER_TRADING"

        logger.info(
            "Single-Leg Session Closed [%s]: %s %s Strike=%d | P0=%.2f -> P_exit=%.2f | Net P&L=INR %.2f (%.2f pts) | Premium=%.2f%% | ROI=%.2f%%",
            exit_reason.value,
            self.order_action.value,
            self.trade_leg.value,
            self.executed_strike,
            self.entry_price,
            self.exit_price,
            self.net_pnl_inr,
            self.net_pnl_inr / self.lot_size if self.lot_size > 0 else 0.0,
            self.premium_pnl_pct,
            self.net_roi_pct,
        )
        self._persist_session()

    def force_hard_time_exit(self, exit_price: Optional[float] = None):
        """
        Unconditionally liquidates an active position on session timeout (max_duration_sec reached)
        or when forced by the orchestrator.
        """
        if self.state in (StrategyState.CLOSED, StrategyState.ABORTED):
            return

        if self.execution_mode == StraddleExecutionMode.SINGLE_LEG_DIRECTIONAL or self.state == StrategyState.SINGLE_LEG_ACTIVE:
            if exit_price is None or exit_price <= 0:
                quote = self.single_leg_quote or (self.call_leg if self.trade_leg == TradeLeg.CALL else self.put_leg)
                if quote:
                    if self.order_action == OrderAction.BUY:
                        exit_price = quote.bid if (quote.bid and quote.bid > 0) else (quote.ltp if (quote.ltp and quote.ltp > 0) else self.entry_price)
                    else:  # SELL
                        exit_price = quote.ask if (quote.ask and quote.ask > 0) else (quote.ltp if (quote.ltp and quote.ltp > 0) else self.entry_price)
                else:
                    exit_price = self.entry_price or 0.0

            logger.info("Forcing Single-Leg Hard Time Stop liquidation @ %.2f", exit_price)
            self._close_single_leg(exit_price=exit_price, exit_reason=ExitReason.HARD_TIME_STOP)
        elif self.state in (StrategyState.CALL_RUNNER, StrategyState.PUT_RUNNER):
            runner_quote = self.call_leg if self.selected_runner == SelectedLeg.CALL else self.put_leg
            if exit_price is None or exit_price <= 0:
                exit_price = (runner_quote.bid if (runner_quote and runner_quote.bid) else (runner_quote.entry_price if runner_quote else 0.0))
            self._close_runner_and_finish(exit_price=exit_price, exit_reason=ExitReason.HARD_TIME_STOP)
        else:
            self.state = StrategyState.CLOSED
            self.exit_reason = ExitReason.HARD_TIME_STOP
            self.status = "PAPER_TRADING"
            self._persist_session()

    def record_skipped_session(
        self,
        skip_reason: str,
        exit_reason: ExitReason,
        final_cms: Optional[float] = None,
    ):
        """Records a session skipped due to High VIX, Missing VIX, Neutral Chop, or Insufficient Coverage."""
        self.exit_reason = exit_reason
        if final_cms is not None:
            self.final_cms = final_cms
            self.final_bias = derive_bias_from_cms(final_cms)
            self.pre_market.composite_morning_score = final_cms
            self.pre_market.final_bias = self.final_bias

        if exit_reason == ExitReason.HIGH_VIX_SKIP:
            self.state = StrategyState.SKIPPED_HIGH_VIX
        elif exit_reason == ExitReason.MISSING_VIX_SKIP:
            self.state = StrategyState.SKIPPED_MISSING_VIX
        elif exit_reason == ExitReason.NEUTRAL_CHOP_SKIP:
            self.state = StrategyState.SKIPPED_NEUTRAL_CHOP
        elif exit_reason == ExitReason.INSUFFICIENT_COVERAGE_SKIP:
            self.state = StrategyState.SKIPPED_INSUFFICIENT_COVERAGE
        else:
            self.state = StrategyState.CLOSED

        self.status = "SKIPPED"
        self.net_pnl_inr = 0.0
        self.net_roi_pct = 0.0
        logger.info("Session Skipped [%s]: %s", exit_reason.value, skip_reason)
        self._persist_session()

        if self.enable_alerts:
            try:
                from strategies.opening_straddle_alerts import send_skipped_session_alert
                send_skipped_session_alert(
                    skip_reason=skip_reason,
                    exit_reason=exit_reason,
                    intel=self.pre_market,
                    trade_date=self.trade_date,
                    final_cms=self.final_cms,
                )
            except Exception as exc:
                logger.warning("Failed to dispatch skipped session alert: %s", exc)

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
        call_entry = (
            self.call_leg.entry_price
            if self.call_leg and self.call_leg.entry_price is not None
            else (self.entry_price if self.trade_leg == TradeLeg.CALL else 0.0)
        )
        put_entry = (
            self.put_leg.entry_price
            if self.put_leg and self.put_leg.entry_price is not None
            else (self.entry_price if self.trade_leg == TradeLeg.PUT else 0.0)
        )

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
            runner_exit = self.exit_price
            runner_pnl = (self.net_pnl_inr + self.friction_inr) if self.execution_mode == StraddleExecutionMode.SINGLE_LEG_DIRECTIONAL else 0.0

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
            order_action=self.order_action,
            trade_leg=self.trade_leg,
            executed_strike=self.executed_strike,
            entry_price=self.entry_price,
            exit_price=self.exit_price,
            margin_inr=self.margin_inr,
            premium_pnl_pct=self.premium_pnl_pct,
            capital_deployed_inr=self.capital_deployed_inr,
            vix_open=self.pre_market.vix,
            final_bias=getattr(self, "final_bias", None) or getattr(self.pre_market, "final_bias", None) or self.pre_market.bias,
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

        call_entry = (
            self.call_leg.entry_price
            if self.call_leg and self.call_leg.entry_price is not None
            else (self.entry_price if self.trade_leg == TradeLeg.CALL else 0.0)
        )
        put_entry = (
            self.put_leg.entry_price
            if self.put_leg and self.put_leg.entry_price is not None
            else (self.entry_price if self.trade_leg == TradeLeg.PUT else 0.0)
        )

        cms_persisted = (
            self.final_cms
            if getattr(self, "final_cms", None) is not None
            else (
                self.pre_market.composite_morning_score
                if self.pre_market.composite_morning_score is not None
                else self.pre_market.cds
            )
        )

        record = {
            "trade_date": self.trade_date,
            "expiry_date": self.expiry_date,
            "atm_strike": self.atm_strike or 0,
            "composite_directional_score": cms_persisted,
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
            "call_entry_price": call_entry,
            "put_entry_price": put_entry,
            "total_debit_inr": self.total_debit_inr,
            "decision_time": self.decision_time_str,
            "selected_leg": self.selected_runner.value,
            "closed_leg_exit_price": self.put_leg.exit_price if (self.put_leg and self.selected_runner == SelectedLeg.CALL) else (self.call_leg.exit_price if (self.call_leg and self.selected_runner == SelectedLeg.PUT) else 0.0),
            "closed_leg_pnl_inr": self.put_leg.pnl_inr if (self.put_leg and self.selected_runner == SelectedLeg.CALL) else (self.call_leg.pnl_inr if (self.call_leg and self.selected_runner == SelectedLeg.PUT) else 0.0),
            "runner_exit_price": self.exit_price if self.execution_mode == StraddleExecutionMode.SINGLE_LEG_DIRECTIONAL else (self.call_leg.exit_price if (self.call_leg and self.selected_runner == SelectedLeg.CALL) else (self.put_leg.exit_price if (self.put_leg and self.selected_runner == SelectedLeg.PUT) else None)),
            "runner_pnl_inr": (self.net_pnl_inr + self.friction_inr) if self.execution_mode == StraddleExecutionMode.SINGLE_LEG_DIRECTIONAL else (self.call_leg.pnl_inr if (self.call_leg and self.selected_runner == SelectedLeg.CALL) else (self.put_leg.pnl_inr if (self.put_leg and self.selected_runner == SelectedLeg.PUT) else 0.0)),
            "net_pnl_inr": self.net_pnl_inr,
            "net_roi_pct": self.net_roi_pct,
            "peak_roi_pct": self.peak_roi_pct,
            "runner_peak_roi_pct": self.runner_peak_roi_pct,
            "spot_confirmed_ticks": self.spot_confirmed_ticks,
            "exit_reason": self.exit_reason.value,
            "execution_mode": self.execution_mode.value,
            "status": self.status,
            "order_action": self.order_action.value if hasattr(self.order_action, "value") else str(self.order_action),
            "trade_leg": self.trade_leg.value if hasattr(self.trade_leg, "value") else str(self.trade_leg),
            "executed_strike": self.executed_strike,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "margin_inr": self.margin_inr,
            "premium_pnl_pct": round(self.premium_pnl_pct, 4),
            "capital_deployed_inr": round(self.capital_deployed_inr, 2),
            "final_bias": getattr(self, "final_bias", None).value if getattr(self, "final_bias", None) is not None else (self.pre_market.final_bias.value if getattr(self.pre_market, "final_bias", None) is not None else self.pre_market.bias.value),
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
