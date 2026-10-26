"""
Pre-Market Multi-Source Ingestion & Scoring Engine (09:14:00 AM).

Ingests institutional flows, NLP news sentiment, Upstox Pulse levels/scenarios,
and real-time global feeds to produce:
  1. Composite Directional Score (CDS) in [-1.0, +1.0]
  2. Source Coverage Ratio (0.0 to 1.0) and Confidence Level (HIGH / MODERATE / LOW)
  3. Pre-market Directional Bias Classification
  4. India VIX Volatility Regime (NORMAL / ELEVATED / LOW_IV_CRUSH)
"""
import glob
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from strategies.opening_straddle_models import (
    BiasClassification,
    ConfidenceLevel,
    PreMarketIntelligence,
    SourceComponent,
    VolatilityRegime,
)

logger = logging.getLogger("nifty50_strategies.premarket")

# ---------------------------------------------------------------------------
# Default Version 2 Weights (User-Approved Strategy Hypothesis)
# ---------------------------------------------------------------------------
DEFAULT_WEIGHT_GIFT_NIFTY = 0.30
DEFAULT_WEIGHT_GLOBAL_MACRO = 0.25
DEFAULT_WEIGHT_NEWS_NLP = 0.25
DEFAULT_WEIGHT_FII_FLOW = 0.10
DEFAULT_WEIGHT_UPSTOX_PULSE = 0.10

# Freshness thresholds in seconds
DEFAULT_GIFT_MAX_AGE_SEC = 180      # 3 minutes for opening gap
DEFAULT_MACRO_MAX_AGE_SEC = 3600    # 1 hour for overnight global markets
DEFAULT_NEWS_MAX_AGE_HOURS = 24.0   # 24 hours for daily market news
DEFAULT_FLOW_MAX_AGE_HOURS = 48.0   # 48 hours for institutional F&O flow (handles weekends)
DEFAULT_VIX_MAX_AGE_SEC = 300       # 5 minutes for India VIX


def _clip(value: float, min_val: float = -1.0, max_val: float = 1.0) -> float:
    """Helper to clip numeric floats."""
    return max(min_val, min(value, max_val))


class PreMarketIntelligenceLoader:
    """
    Ingests and synthesizes pre-market data from multi-repository sources.
    Designed for 09:14:00 AM execution with zero dependencies on external internet calls
    during execution (reads locally persisted SQLite, JSON, and memory/disk feed caches).
    """

    def __init__(
        self,
        weight_gift: float = DEFAULT_WEIGHT_GIFT_NIFTY,
        weight_macro: float = DEFAULT_WEIGHT_GLOBAL_MACRO,
        weight_news: float = DEFAULT_WEIGHT_NEWS_NLP,
        weight_flow: float = DEFAULT_WEIGHT_FII_FLOW,
        weight_pulse: float = DEFAULT_WEIGHT_UPSTOX_PULSE,
        feed_cache_path: Optional[str] = None,
        sqlite_base_dir: Optional[str] = None,
        pulse_data_dir: Optional[str] = None,
    ):
        self.weights = {
            "gift_nifty": weight_gift,
            "global_macro": weight_macro,
            "news_nlp": weight_news,
            "fii_flow": weight_flow,
            "upstox_pulse": weight_pulse,
        }
        self.feed_cache_path = feed_cache_path or os.getenv(
            "UPSTOX_FEED_CACHE_PATH", "/tmp/upstox_live_quotes.json"
        )
        self.sqlite_base_dir = sqlite_base_dir or os.getenv(
            "SQLITE_BASE_DIR", "/home/ubuntu/sqlite"
        )
        self.pulse_data_dir = pulse_data_dir or os.getenv(
            "MARKET_NEWS_DATA_DIR", "/home/ubuntu/data/market_news"
        )

    def load_intelligence(
        self,
        ref_time_epoch: Optional[float] = None,
        nifty_prev_close: Optional[float] = None,
    ) -> PreMarketIntelligence:
        """
        Synthesizes pre-market intelligence into PreMarketIntelligence dataclass.

        Args:
            ref_time_epoch: Optional epoch timestamp for mock / backtesting time anchoring.
            nifty_prev_close: Optional known previous Nifty close (if not provided, deduced from feed).
        """
        now_ts = ref_time_epoch if ref_time_epoch is not None else time.time()
        now_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

        # 1. Ingest feed cache once for all live quotes
        feed_quotes = self._read_feed_cache()

        # Deduce previous close if not explicitly supplied
        if nifty_prev_close is None and feed_quotes:
            nifty_quote = feed_quotes.get("NSE_INDEX|Nifty 50", {})
            nifty_prev_close = nifty_quote.get("cp") or nifty_quote.get("close")

        # 2. Ingest individual components
        comp_gift = self._load_gift_nifty(feed_quotes, now_ts, nifty_prev_close)
        comp_macro = self._load_global_macro(feed_quotes, now_ts)
        comp_news = self._load_news_nlp(now_ts)
        comp_flow = self._load_fii_flow(now_ts)
        comp_pulse, pulse_levels = self._load_upstox_pulse(now_ts)

        components = {
            "gift_nifty": comp_gift,
            "global_macro": comp_macro,
            "news_nlp": comp_news,
            "fii_flow": comp_flow,
            "upstox_pulse": comp_pulse,
        }

        # 3. Calculate Coverage & Normalized CDS
        available_weight_sum = 0.0
        weighted_score_sum = 0.0

        for comp_key, comp in components.items():
            nominal_weight = self.weights.get(comp_key, 0.0)
            if comp.is_available and not comp.is_stale:
                available_weight_sum += nominal_weight
                weighted_score_sum += nominal_weight * comp.normalized_score

        coverage = _clip(available_weight_sum, 0.0, 1.0)

        if coverage >= 0.05:
            # Re-normalize across available fresh components
            cds = _clip(weighted_score_sum / coverage, -1.0, 1.0)
        else:
            cds = 0.0

        # 4. Classify Confidence Level
        if coverage >= 0.80:
            confidence = ConfidenceLevel.HIGH
        elif coverage >= 0.60:
            confidence = ConfidenceLevel.MODERATE
        else:
            confidence = ConfidenceLevel.LOW

        # 5. Classify Directional Bias
        if confidence == ConfidenceLevel.LOW:
            bias = BiasClassification.NEUTRAL
        elif cds >= 0.25:
            bias = BiasClassification.STRONG_BULLISH
        elif cds >= 0.10:
            bias = BiasClassification.MODERATE_BULLISH
        elif cds <= -0.25:
            bias = BiasClassification.STRONG_BEARISH
        elif cds <= -0.10:
            bias = BiasClassification.MODERATE_BEARISH
        else:
            bias = BiasClassification.NEUTRAL

        # 6. Load India VIX & Determine Volatility Regime
        vix_val, vix_regime = self._load_india_vix(feed_quotes, now_ts)

        # 7. Extract spot if available
        nifty_spot = None
        if feed_quotes:
            spot_q = feed_quotes.get("NSE_INDEX|Nifty 50", {})
            nifty_spot = spot_q.get("ltp")

        intelligence = PreMarketIntelligence(
            cds=cds,
            coverage=coverage,
            confidence=confidence,
            bias=bias,
            components=components,
            vix=vix_val,
            vix_regime=vix_regime,
            timestamp_utc=now_iso,
            nifty_prev_close=nifty_prev_close,
            nifty_spot=nifty_spot,
            upstox_pulse_levels=pulse_levels,
        )

        logger.info("Pre-Market Intelligence: %s", intelligence.summary_str())
        return intelligence

    # -----------------------------------------------------------------------
    # Component loaders
    # -----------------------------------------------------------------------

    def _read_feed_cache(self) -> Dict[str, Any]:
        """Safely reads /tmp/upstox_live_quotes.json without raising unhandled exceptions."""
        if not os.path.exists(self.feed_cache_path):
            logger.warning("Feed cache file missing: %s", self.feed_cache_path)
            return {}

        try:
            with open(self.feed_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "quotes" in data:
                return data["quotes"]
        except Exception as exc:
            logger.error("Failed to read feed cache at %s: %s", self.feed_cache_path, exc)
        return {}

    def _load_gift_nifty(
        self,
        quotes: Dict[str, Any],
        now_ts: float,
        nifty_prev_close: Optional[float],
    ) -> SourceComponent:
        """GIFT Nifty quote parsing and gap normalization."""
        nominal_weight = self.weights.get("gift_nifty", DEFAULT_WEIGHT_GIFT_NIFTY)
        gift_key = "GLOBAL_INDEX|SGX NIFTY"
        quote = quotes.get(gift_key)

        if not quote:
            return SourceComponent(
                name="gift_nifty",
                raw_value=None,
                normalized_score=0.0,
                weight=nominal_weight,
                is_available=False,
                metadata={"reason": "instrument_not_in_feed"},
            )

        updated_epoch = quote.get("updated_at_epoch")
        age_sec = (now_ts - float(updated_epoch)) if updated_epoch is not None else None
        is_stale = (age_sec is None or age_sec > DEFAULT_GIFT_MAX_AGE_SEC)

        ltp = quote.get("ltp")
        if ltp is None:
            return SourceComponent(
                name="gift_nifty",
                raw_value=None,
                normalized_score=0.0,
                weight=nominal_weight,
                age_sec=age_sec,
                is_available=False,
                is_stale=is_stale,
                metadata={"reason": "missing_ltp"},
            )

        # Compute percentage change
        if nifty_prev_close and nifty_prev_close > 0:
            change_pct = ((float(ltp) - float(nifty_prev_close)) / float(nifty_prev_close)) * 100.0
        else:
            change_pct = float(quote.get("change_pct", 0.0))

        # Normalized: 0.50% gap maps to 1.0 score
        normalized = _clip(change_pct / 0.50, -1.0, 1.0)

        return SourceComponent(
            name="gift_nifty",
            raw_value={"ltp": ltp, "change_pct": round(change_pct, 3)},
            normalized_score=normalized,
            weight=nominal_weight,
            age_sec=age_sec,
            is_available=True,
            is_stale=is_stale,
            metadata={"ref_close": nifty_prev_close},
        )

    def _load_global_macro(
        self,
        quotes: Dict[str, Any],
        now_ts: float,
    ) -> SourceComponent:
        """Synthesizes Wall Street (DOW), Crude Oil, and USD/INR into a single macro score."""
        nominal_weight = self.weights.get("global_macro", DEFAULT_WEIGHT_GLOBAL_MACRO)
        dow_q = quotes.get("GLOBAL_INDEX|^DJI", {})
        brent_q = quotes.get("GLOBAL_INDICATOR|BZUSD", {})
        usdinr_q = quotes.get("GLOBAL_INDICATOR|USDINR", {})

        if not (dow_q or brent_q or usdinr_q):
            return SourceComponent(
                name="global_macro",
                raw_value=None,
                normalized_score=0.0,
                weight=nominal_weight,
                is_available=False,
                metadata={"reason": "no_macro_quotes_found"},
            )

        # Ages
        ages = []
        for q in (dow_q, brent_q, usdinr_q):
            if q and q.get("updated_at_epoch"):
                ages.append(now_ts - float(q["updated_at_epoch"]))
        max_age = max(ages) if ages else None
        is_stale = (max_age is None or max_age > DEFAULT_MACRO_MAX_AGE_SEC)

        # Dow Jones (+1.0% change = +1.0 score)
        dow_pct = float(dow_q.get("change_pct", 0.0))
        s_dow = _clip(dow_pct / 1.0, -1.0, 1.0)

        # Brent Crude (Inverted: +1.5% oil rise = -1.0 score for Indian equities)
        brent_pct = float(brent_q.get("change_pct", 0.0))
        s_brent = -_clip(brent_pct / 1.5, -1.0, 1.0)

        # USD/INR (Inverted: +0.30% rupee depreciation = -1.0 score for equities)
        usdinr_pct = float(usdinr_q.get("change_pct", 0.0))
        s_usdinr = -_clip(usdinr_pct / 0.30, -1.0, 1.0)

        # Weighted combination: 50% Dow, 25% Brent, 25% USDINR
        macro_score = _clip(0.50 * s_dow + 0.25 * s_brent + 0.25 * s_usdinr, -1.0, 1.0)

        return SourceComponent(
            name="global_macro",
            raw_value={"dow_pct": dow_pct, "brent_pct": brent_pct, "usdinr_pct": usdinr_pct},
            normalized_score=macro_score,
            weight=nominal_weight,
            age_sec=max_age,
            is_available=True,
            is_stale=is_stale,
            metadata={"s_dow": round(s_dow, 3), "s_brent": round(s_brent, 3), "s_usdinr": round(s_usdinr, 3)},
        )

    def _resolve_shared_sqlite_path(self, now_ts: float) -> Optional[str]:
        """Finds the current or most recent monthly SQLite database in sqlite_base_dir."""
        if not os.path.isdir(self.sqlite_base_dir):
            return None

        dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        curr_db = os.path.join(self.sqlite_base_dir, f"signals_data_{dt.strftime('%Y_%m')}.db")
        if os.path.exists(curr_db):
            return curr_db

        # Search for any recent matching database
        matches = sorted(glob.glob(os.path.join(self.sqlite_base_dir, "signals_data_*.db")), reverse=True)
        return matches[0] if matches else None

    def _load_news_nlp(self, now_ts: float) -> SourceComponent:
        """Loads latest NLP market sentiment score from Shared SQLite (NEWS#MARKET#DAILY)."""
        nominal_weight = self.weights.get("news_nlp", DEFAULT_WEIGHT_NEWS_NLP)
        db_path = self._resolve_shared_sqlite_path(now_ts)

        if not db_path:
            return SourceComponent(
                name="news_nlp",
                raw_value=None,
                normalized_score=0.0,
                weight=nominal_weight,
                is_available=False,
                metadata={"reason": "sqlite_db_not_found"},
            )

        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                cursor = conn.execute(
                    "SELECT data, ts FROM market_data WHERE pk = 'NEWS#MARKET#DAILY' ORDER BY ts DESC LIMIT 1"
                )
                row = cursor.fetchone()
                if not row:
                    return SourceComponent(
                        name="news_nlp",
                        raw_value=None,
                        normalized_score=0.0,
                        weight=nominal_weight,
                        is_available=False,
                        metadata={"reason": "pk_not_found"},
                    )

                data_blob, entry_ts = row
                parsed = json.loads(data_blob)
                net_bias = float(parsed.get("net_bias_score", 0.0))
                age_sec = now_ts - float(entry_ts)
                is_stale = age_sec > (DEFAULT_NEWS_MAX_AGE_HOURS * 3600)

                return SourceComponent(
                    name="news_nlp",
                    raw_value=net_bias,
                    normalized_score=_clip(net_bias, -1.0, 1.0),
                    weight=nominal_weight,
                    age_sec=age_sec,
                    is_available=True,
                    is_stale=is_stale,
                    metadata={
                        "sentiment_score": parsed.get("sentiment_score"),
                        "contrarian_regime": parsed.get("contrarian_regime"),
                        "date": parsed.get("date"),
                    },
                )
        except Exception as exc:
            logger.error("Failed to read news nlp from sqlite: %s", exc)
            return SourceComponent(
                name="news_nlp",
                raw_value=None,
                normalized_score=0.0,
                weight=nominal_weight,
                is_available=False,
                metadata={"error": str(exc)},
            )

    def _load_fii_flow(self, now_ts: float) -> SourceComponent:
        """Loads institutional F&O flow data from Shared SQLite (FLOW#FII_DII#DAILY)."""
        nominal_weight = self.weights.get("fii_flow", DEFAULT_WEIGHT_FII_FLOW)
        db_path = self._resolve_shared_sqlite_path(now_ts)

        if not db_path:
            return SourceComponent(
                name="fii_flow",
                raw_value=None,
                normalized_score=0.0,
                weight=nominal_weight,
                is_available=False,
                metadata={"reason": "sqlite_db_not_found"},
            )

        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                cursor = conn.execute(
                    "SELECT data, ts FROM market_data WHERE pk = 'FLOW#FII_DII#DAILY' ORDER BY ts DESC LIMIT 1"
                )
                row = cursor.fetchone()
                if not row:
                    return SourceComponent(
                        name="fii_flow",
                        raw_value=None,
                        normalized_score=0.0,
                        weight=nominal_weight,
                        is_available=False,
                        metadata={"reason": "pk_not_found"},
                    )

                data_blob, entry_ts = row
                parsed = json.loads(data_blob)
                fno = parsed.get("fno_positioning", {})
                futures_sum = float(fno.get("fii_futures_5d_sum", 0.0))
                certainty = float(parsed.get("certainty", 1.0))
                age_sec = now_ts - float(entry_ts)
                is_stale = age_sec > (DEFAULT_FLOW_MAX_AGE_HOURS * 3600)

                # Normalized: +/- 2500 Cr futures flow maps to +/- 1.0 score
                score = _clip((futures_sum / 2500.0), -1.0, 1.0) * certainty

                return SourceComponent(
                    name="fii_flow",
                    raw_value=futures_sum,
                    normalized_score=_clip(score, -1.0, 1.0),
                    weight=nominal_weight,
                    age_sec=age_sec,
                    is_available=True,
                    is_stale=is_stale,
                    metadata={"bias": fno.get("bias"), "certainty": certainty, "date": parsed.get("date")},
                )
        except Exception as exc:
            logger.error("Failed to read fii flow from sqlite: %s", exc)
            return SourceComponent(
                name="fii_flow",
                raw_value=None,
                normalized_score=0.0,
                weight=nominal_weight,
                is_available=False,
                metadata={"error": str(exc)},
            )

    def _load_upstox_pulse(
        self,
        now_ts: float,
    ) -> Tuple[SourceComponent, Optional[Dict[str, Any]]]:
        """Loads Upstox Daily Pulse newsletter scenario probabilities and key levels."""
        nominal_weight = self.weights.get("upstox_pulse", DEFAULT_WEIGHT_UPSTOX_PULSE)
        dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")

        # Potential paths (exact date or most recent file in YYYY/MM/)
        target_path = os.path.join(
            self.pulse_data_dir, dt.strftime("%Y"), dt.strftime("%m"), f"{date_str}.json"
        )

        pulse_file = None
        if os.path.exists(target_path):
            pulse_file = target_path
        else:
            # Fall back to any recent .json in pulse_data_dir
            pattern = os.path.join(self.pulse_data_dir, "*", "*", "*.json")
            matches = sorted(glob.glob(pattern), reverse=True)
            if matches:
                pulse_file = matches[0]

        if not pulse_file:
            return (
                SourceComponent(
                    name="upstox_pulse",
                    raw_value=None,
                    normalized_score=0.0,
                    weight=nominal_weight,
                    is_available=False,
                    metadata={"reason": "pulse_file_not_found"},
                ),
                None,
            )

        try:
            with open(pulse_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            scenarios = data.get("scenario_analysis", [])
            prob_bullish = 0.0
            prob_bearish = 0.0
            for sc in scenarios:
                if isinstance(sc, dict):
                    name = str(sc.get("scenario", "")).lower()
                    prob = float(sc.get("probability", 0.0))
                    if name == "bullish":
                        prob_bullish = prob
                    elif name == "bearish":
                        prob_bearish = prob

            # Directional bias = Bullish probability - Bearish probability
            pulse_score = _clip(prob_bullish - prob_bearish, -1.0, 1.0)
            levels = {
                "support": data.get("support_levels", {}).get("nifty", []),
                "resistance": data.get("resistance_levels", {}).get("nifty", []),
                "pcr_nifty": data.get("important_numeric_values", {}).get("pcr_nifty"),
                "overall_sentiment": data.get("overall_sentiment"),
            }

            mtime = os.path.getmtime(pulse_file)
            age_sec = now_ts - mtime
            is_stale = age_sec > (24.0 * 3600)

            return (
                SourceComponent(
                    name="upstox_pulse",
                    raw_value={"bullish_prob": prob_bullish, "bearish_prob": prob_bearish},
                    normalized_score=pulse_score,
                    weight=nominal_weight,
                    age_sec=age_sec,
                    is_available=True,
                    is_stale=is_stale,
                    metadata=levels,
                ),
                levels,
            )
        except Exception as exc:
            logger.error("Failed to parse upstox pulse file %s: %s", pulse_file, exc)
            return (
                SourceComponent(
                    name="upstox_pulse",
                    raw_value=None,
                    normalized_score=0.0,
                    weight=nominal_weight,
                    is_available=False,
                    metadata={"error": str(exc)},
                ),
                None,
            )

    def _load_india_vix(
        self,
        quotes: Dict[str, Any],
        now_ts: float,
    ) -> Tuple[float, VolatilityRegime]:
        """Loads India VIX quote and maps to VolatilityRegime."""
        vix_key = "NSE_INDEX|India VIX"
        vix_quote = quotes.get(vix_key, {})
        vix_ltp = vix_quote.get("ltp")

        if vix_ltp is None:
            # Baseline default if feed unavailable
            return 14.0, VolatilityRegime.NORMAL

        vix_val = float(vix_ltp)
        if vix_val > 18.0:
            regime = VolatilityRegime.ELEVATED
        elif vix_val < 11.5:
            regime = VolatilityRegime.LOW_IV_CRUSH
        else:
            regime = VolatilityRegime.NORMAL

        return vix_val, regime
