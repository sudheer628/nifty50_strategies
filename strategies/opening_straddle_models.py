"""
Data models, enums, and dataclasses for the NIFTY 50 Opening Straddle Strategy (09:15 - 10:00 AM).
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class BiasClassification(str, Enum):
    """Pre-market composite directional bias classifications."""
    STRONG_BULLISH = "STRONG_BULLISH"
    MODERATE_BULLISH = "MODERATE_BULLISH"
    NEUTRAL = "NEUTRAL"
    MODERATE_BEARISH = "MODERATE_BEARISH"
    STRONG_BEARISH = "STRONG_BEARISH"


DEFAULT_OPENING_CHOP_THRESHOLD = 0.02
DEFAULT_OPENING_STRONG_THRESHOLD = 0.25


def derive_bias_from_cms(
    cms: Optional[float],
    chop_threshold: Optional[float] = None,
    strong_threshold: Optional[float] = None,
) -> BiasClassification:
    """Derives directional bias from the Composite Morning Score (CMS)."""
    if cms is None:
        return BiasClassification.NEUTRAL
    import os
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
        return BiasClassification.STRONG_BULLISH
    elif cms >= chop_threshold:
        return BiasClassification.MODERATE_BULLISH
    elif cms <= -strong_threshold:
        return BiasClassification.STRONG_BEARISH
    elif cms <= -chop_threshold:
        return BiasClassification.MODERATE_BEARISH
    else:
        return BiasClassification.NEUTRAL


class ConfidenceLevel(str, Enum):
    """Confidence level of the pre-market Composite Directional Score (CDS)."""
    HIGH = "HIGH"          # Coverage >= 80%
    MODERATE = "MODERATE"  # 60% <= Coverage < 80%
    LOW = "LOW"            # Coverage < 60%


class VolatilityRegime(str, Enum):
    """Market volatility regime derived from India VIX."""
    NORMAL = "NORMAL"            # 12.0 <= VIX <= 18.0
    ELEVATED = "ELEVATED"        # VIX > 18.0 (Fast moves, tight trailing stop)
    LOW_IV_CRUSH = "LOW_IV_CRUSH"  # VIX < 11.5 (High premium decay risk)


class StraddleExecutionMode(str, Enum):
    """Execution mode for opening straddle strategy."""
    BUY_STRADDLE = "BUY_STRADDLE"    # Option A: Long Straddle + 09:20 losing leg cut + independent runner
    SELL_STRADDLE = "SELL_STRADDLE"  # Short Straddle: Sell CE + Sell PE with per-leg SL
    SINGLE_LEG_DIRECTIONAL = "SINGLE_LEG_DIRECTIONAL"  # Directional single-leg scalp (BUY/SELL)


class OrderAction(str, Enum):
    """Execution side for the single-leg opening trade."""
    BUY = "BUY"
    SELL = "SELL"


class TradeLeg(str, Enum):
    """Selected contract option type for single-leg trading."""
    CALL = "CALL"
    PUT = "PUT"
    NONE = "NONE"


class StrategyState(str, Enum):
    """Finite State Machine states for the opening straddle trade."""
    PRE_MARKET_INGESTION = "PRE_MARKET_INGESTION"
    PRE_MARKET_ARMED = "PRE_MARKET_ARMED"
    DUAL_LONG_ENTRY = "DUAL_LONG_ENTRY"
    OBSERVATION = "OBSERVATION"
    EVALUATION_920 = "EVALUATION_920"
    EXTENDED_OBSERVATION = "EXTENDED_OBSERVATION"
    EVALUATION_925 = "EVALUATION_925"
    CALL_RUNNER = "CALL_RUNNER"
    PUT_RUNNER = "PUT_RUNNER"
    SINGLE_LEG_ACTIVE = "SINGLE_LEG_ACTIVE"
    PROFIT_TARGET_EXIT = "PROFIT_TARGET_EXIT"
    STOP_LOSS_EXIT = "STOP_LOSS_EXIT"
    TRAILING_STOP_EXIT = "TRAILING_STOP_EXIT"
    HARD_TIME_EXIT = "HARD_TIME_EXIT"
    SCRATCH_EXIT = "SCRATCH_EXIT"
    SKIPPED_HIGH_VIX = "SKIPPED_HIGH_VIX"
    SKIPPED_MISSING_VIX = "SKIPPED_MISSING_VIX"
    SKIPPED_NEUTRAL_CHOP = "SKIPPED_NEUTRAL_CHOP"
    SKIPPED_INSUFFICIENT_COVERAGE = "SKIPPED_INSUFFICIENT_COVERAGE"
    ABORTED = "ABORTED"
    CLOSED = "CLOSED"


class SelectedLeg(str, Enum):
    """The option leg retained as runner."""
    NONE = "NONE"
    CALL = "CALL"
    PUT = "PUT"
    SCRATCH = "SCRATCH"


class ExitReason(str, Enum):
    """Reason for terminating the session."""
    NONE = "NONE"
    PROFIT_TARGET = "PROFIT_TARGET"        # Combined Net ROI >= +3%
    STOP_LOSS = "STOP_LOSS"                # Combined Net ROI <= -3%
    TRAILING_STOP = "TRAILING_STOP"        # Peak >= +2% and dropped >= 1%
    HARD_TIME_STOP = "HARD_TIME_STOP"      # 09:55:00 AM mandatory exit
    SCRATCH = "SCRATCH"                    # Inconclusive at 09:25:00 AM
    EXECUTION_ABORT = "EXECUTION_ABORT"    # Wide spread or partial fill fail-closed
    HIGH_VIX_SKIP = "HIGH_VIX_SKIP"        # VIX > threshold skip
    MISSING_VIX_SKIP = "MISSING_VIX_SKIP"  # VIX missing / stale fail-closed
    NEUTRAL_CHOP_SKIP = "NEUTRAL_CHOP_SKIP" # Dead flat market skip
    INSUFFICIENT_COVERAGE_SKIP = "INSUFFICIENT_COVERAGE_SKIP" # Premarket coverage < 50% skip
    MIN_PROFIT_TARGET = "MIN_PROFIT_TARGET" # Single-leg scalp profit target


@dataclass
class SourceComponent:
    """Represents an individual pre-market intelligence data stream."""
    name: str
    raw_value: Any
    normalized_score: float  # In [-1.0, +1.0]
    weight: float           # Version 2 nominal weight (e.g. 0.30 for GIFT)
    age_sec: Optional[float] = None
    is_available: bool = True
    is_stale: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "raw_value": self.raw_value,
            "normalized_score": round(self.normalized_score, 4),
            "weight": round(self.weight, 4),
            "age_sec": round(self.age_sec, 1) if self.age_sec is not None else None,
            "is_available": self.is_available,
            "is_stale": self.is_stale,
            "metadata": self.metadata,
        }


@dataclass
class PreMarketIntelligence:
    """
    Synthesized pre-market intelligence calculated at 09:14:00 AM.
    Governs directional context, coverage gating, and volatility regime.
    """
    cds: float                                      # Composite Directional Score in [-1.0, +1.0]
    coverage: float                                 # Source weight coverage in [0.0, 1.0]
    confidence: ConfidenceLevel
    bias: BiasClassification
    components: Dict[str, SourceComponent]
    vix: Optional[float]                            # India VIX (None if completely unavailable)
    vix_regime: VolatilityRegime
    timestamp_utc: str
    nifty_prev_close: Optional[float] = None
    nifty_spot: Optional[float] = None
    upstox_pulse_levels: Optional[Dict[str, Any]] = None
    vix_fresh: bool = True                          # True if live VIX quote age <= 300s
    vix_skip: bool = False                          # True if VIX > threshold or missing
    vix_skip_reason: Optional[str] = None           # Explanation for VIX skip if triggered
    prior_day_ohlc: Optional[Dict[str, Any]] = None # Prior Day Open, High, Low, Close, Range
    grok_social_sentiment: Optional[Dict[str, Any]] = None # Grok 4.3 intraday sentiment
    composite_morning_score: Optional[float] = None # CMS score combining gap, 1m candle, etc.
    final_bias: Optional[BiasClassification] = None # Directional bias derived from final synthesized CMS

    @property
    def is_contrarian_filter_eligible(self) -> bool:
        """
        The +4.0% contrarian hurdle requires high confidence (Coverage >= 80%)
        AND a decisive pre-market conviction (|CDS| >= 0.25).
        """
        return (
            self.coverage >= 0.80
            and abs(self.cds) >= 0.25
            and self.bias in (BiasClassification.STRONG_BULLISH, BiasClassification.STRONG_BEARISH)
        )

    def get_component(self, name: str) -> Optional[SourceComponent]:
        return self.components.get(name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cds": round(self.cds, 4),
            "coverage": round(self.coverage, 4),
            "confidence": self.confidence.value,
            "bias": self.bias.value,
            "is_contrarian_filter_eligible": self.is_contrarian_filter_eligible,
            "vix": round(self.vix, 2) if self.vix is not None else None,
            "vix_regime": self.vix_regime.value,
            "vix_fresh": self.vix_fresh,
            "vix_skip": self.vix_skip,
            "vix_skip_reason": self.vix_skip_reason,
            "prior_day_ohlc": self.prior_day_ohlc,
            "grok_social_sentiment": self.grok_social_sentiment,
            "composite_morning_score": round(self.composite_morning_score, 4) if self.composite_morning_score is not None else None,
            "final_bias": self.final_bias.value if self.final_bias is not None else None,
            "timestamp_utc": self.timestamp_utc,
            "nifty_prev_close": self.nifty_prev_close,
            "nifty_spot": self.nifty_spot,
            "upstox_pulse_levels": self.upstox_pulse_levels,
            "components": {k: v.to_dict() for k, v in self.components.items()},
        }

    def summary_str(self) -> str:
        eligible_tag = "ELIGIBLE" if self.is_contrarian_filter_eligible else "INELIGIBLE"
        vix_str = f"{self.vix:.2f}" if self.vix is not None else "N/A"
        fresh_tag = "FRESH" if self.vix_fresh else "STALE/FALLBACK"
        skip_tag = f" [VIX_SKIP: {self.vix_skip_reason}]" if self.vix_skip else ""
        cms_str = f" | CMS={self.composite_morning_score:+.3f}" if self.composite_morning_score is not None else ""
        return (
            f"CDS={self.cds:+.3f}{cms_str} | Bias={self.bias.value} | Coverage={self.coverage*100:.1f}% "
            f"({self.confidence.value}) | ContrarianFilter={eligible_tag} | "
            f"VIX={vix_str} ({self.vix_regime.value}, {fresh_tag}){skip_tag}"
        )


@dataclass
class StraddleLegQuote:
    """Live quote and position tracking for an individual option leg."""
    symbol: str
    token: str
    strike: int
    option_type: str         # "CE" or "PE"
    bid: float
    ask: float
    ltp: float
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    pnl_inr: float = 0.0
    pnl_pct: float = 0.0

    @property
    def spread_pct(self) -> float:
        import math
        if (
            not math.isfinite(self.ask)
            or not math.isfinite(self.bid)
            or self.ask <= 0
            or self.bid <= 0
            or self.ask < self.bid
        ):
            return 999.0
        mid = (self.ask + self.bid) / 2.0
        if not math.isfinite(mid) or mid <= 0:
            return 999.0
        return ((self.ask - self.bid) / mid) * 100.0


@dataclass
class TickObservation:
    """Single tick snapshot of live underlying and option quotes."""
    ts: float                # Epoch seconds
    nifty_spot: float
    call_bid: float
    call_ask: float
    put_bid: float
    put_ask: float
    call_ltp: Optional[float] = None
    put_ltp: Optional[float] = None
    quote_age_sec: Optional[float] = None


@dataclass
class StraddleSessionRecord:
    """Full session audit record persisted to opening_straddle.db."""
    trade_date: str          # YYYY-MM-DD
    expiry_date: str         # YYYY-MM-DD
    atm_strike: int
    pre_market: PreMarketIntelligence
    call_entry_price: float = 0.0
    put_entry_price: float = 0.0
    total_debit_inr: float = 0.0
    state: StrategyState = StrategyState.CLOSED
    selected_leg: SelectedLeg = SelectedLeg.NONE
    decision_time: Optional[str] = None
    closed_leg_exit_price: Optional[float] = None
    closed_leg_pnl_inr: float = 0.0
    runner_exit_price: Optional[float] = None
    runner_pnl_inr: float = 0.0
    net_pnl_inr: float = 0.0
    net_roi_pct: float = 0.0
    peak_roi_pct: float = 0.0
    runner_peak_roi_pct: float = 0.0
    spot_confirmed_ticks: int = 0
    exit_reason: ExitReason = ExitReason.NONE
    execution_mode: str = "BUY_STRADDLE"
    status: str = "COMPLETED"
    # Single-Leg Directional Scalp Fields:
    order_action: OrderAction = OrderAction.BUY
    trade_leg: TradeLeg = TradeLeg.NONE
    executed_strike: Optional[int] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    margin_inr: float = 0.0
    premium_pnl_pct: float = 0.0
    capital_deployed_inr: float = 0.0
    vix_open: Optional[float] = None
    final_bias: Optional[BiasClassification] = None
