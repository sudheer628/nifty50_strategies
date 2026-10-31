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

# Directional Scalp V3 Weights
DEFAULT_WEIGHT_PRIOR_DAY = 0.25
DEFAULT_WEIGHT_GROK_SOCIAL = 0.10

# Freshness thresholds in seconds
DEFAULT_GIFT_MAX_AGE_SEC = 180      # 3 minutes for opening gap
DEFAULT_MACRO_MAX_AGE_SEC = 3600    # 1 hour for overnight global markets
DEFAULT_NEWS_MAX_AGE_HOURS = 24.0   # 24 hours for daily market news
DEFAULT_FLOW_MAX_AGE_HOURS = 48.0   # 48 hours for institutional F&O flow (handles weekends)
DEFAULT_VIX_MAX_AGE_SEC = 300       # 5 minutes for India VIX
DEFAULT_SOCIAL_MAX_AGE_HOURS = 18.0 # 18 hours for X/AI Grok social sentiment
DEFAULT_MAX_VIX_THRESHOLD = 20.0    # India VIX threshold to skip trading


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
        weight_prior_day: float = 0.0,
        weight_grok: float = 0.0,
        feed_cache_path: Optional[str] = None,
        sqlite_base_dir: Optional[str] = None,
        pulse_data_dir: Optional[str] = None,
        social_data_dir: Optional[str] = None,
        max_vix_threshold: Optional[float] = None,
        directional_scalp: bool = False,
    ):
        self.weights = {
            "gift_nifty": weight_gift,
            "global_macro": weight_macro,
            "news_nlp": weight_news,
            "fii_flow": weight_flow,
            "upstox_pulse": weight_pulse,
            "prior_day_structure": weight_prior_day,
            "grok_social": weight_grok,
        }
        if directional_scalp:
            self.apply_directional_scalp_weights()
        self.feed_cache_path = feed_cache_path or os.getenv(
            "UPSTOX_FEED_CACHE_PATH", "/tmp/upstox_live_quotes.json"
        )
        self.sqlite_base_dir = sqlite_base_dir or os.getenv(
            "SQLITE_BASE_DIR", "/home/ubuntu/sqlite"
        )
        self.pulse_data_dir = pulse_data_dir or os.getenv(
            "MARKET_NEWS_DATA_DIR", "/home/ubuntu/data/market_news"
        )
        self.social_data_dir = social_data_dir or os.getenv(
            "SOCIAL_DATA_DIR", "/home/ubuntu/data/social"
        )
        env_vix = os.getenv("OPENING_MAX_VIX")
        self.max_vix_threshold = (
            float(env_vix) if env_vix is not None else (max_vix_threshold or DEFAULT_MAX_VIX_THRESHOLD)
        )

    def apply_directional_scalp_weights(
        self,
        weight_gift: float = 0.25,
        weight_prior_day: float = 0.25,
        weight_pulse: float = 0.15,
        weight_news: float = 0.15,
        weight_grok: float = 0.10,
        weight_flow: float = 0.10,
        weight_macro: float = 0.0,
    ) -> None:
        """Configures weights for Directional Single-Leg Scalp Strategy (09:14-10:00 AM)."""
        self.weights = {
            "gift_nifty": weight_gift,
            "prior_day_structure": weight_prior_day,
            "upstox_pulse": weight_pulse,
            "news_nlp": weight_news,
            "grok_social": weight_grok,
            "fii_flow": weight_flow,
            "global_macro": weight_macro,
        }

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
        comp_prior, prior_ohlc = self._load_prior_day_structure(feed_quotes, now_ts, nifty_prev_close)
        comp_grok, grok_meta = self._load_grok_social(now_ts)

        components = {
            "gift_nifty": comp_gift,
            "global_macro": comp_macro,
            "news_nlp": comp_news,
            "fii_flow": comp_flow,
            "upstox_pulse": comp_pulse,
            "prior_day_structure": comp_prior,
            "grok_social": comp_grok,
        }

        # 3. Calculate Coverage & Normalized CDS
        available_weight_sum = 0.0
        weighted_score_sum = 0.0

        for comp_key, comp in components.items():
            nominal_weight = self.weights.get(comp_key, 0.0)
            if nominal_weight > 0 and comp.is_available and not comp.is_stale:
                available_weight_sum += nominal_weight
                weighted_score_sum += nominal_weight * comp.normalized_score

        coverage = _clip(available_weight_sum, 0.0, 1.0)

        if coverage >= 0.05:
            # Re-normalize across available fresh components
            cds = _clip(weighted_score_sum / coverage, -1.0, 1.0)
        else:
            cds = 0.0

        # Composite Morning Score: requires at least 0.50 coverage per Section 4.2
        cms = cds if coverage >= 0.50 else 0.0

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

        # 6. Load India VIX & Determine Volatility Regime & VIX Skip Gate
        vix_val, vix_regime, vix_fresh = self._load_india_vix(feed_quotes, now_ts)
        vix_skip = False
        vix_skip_reason = None

        if vix_val is None:
            vix_skip = True
            vix_skip_reason = "MISSING_VIX: Quote unavailable in feed and fallback"
        elif not vix_fresh and vix_val > 18.0:
            vix_skip = True
            vix_skip_reason = f"HIGH_VIX_FALLBACK: {vix_val:.2f} > 18.0 fallback threshold"
        elif vix_val > self.max_vix_threshold:
            vix_skip = True
            vix_skip_reason = f"HIGH_VIX: {vix_val:.2f} > {self.max_vix_threshold:.2f} threshold"
        elif not vix_fresh:
            logger.warning("India VIX is from fallback/stale data (%.2f)", vix_val)

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
            vix_fresh=vix_fresh,
            vix_skip=vix_skip,
            vix_skip_reason=vix_skip_reason,
            prior_day_ohlc=prior_ohlc,
            grok_social_sentiment=grok_meta,
            composite_morning_score=cms,
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
    ) -> Tuple[Optional[float], VolatilityRegime, bool]:
        """
        Loads India VIX quote and maps to VolatilityRegime.
        Returns:
            (vix_val, regime, is_fresh)
            vix_val can be None if both feed and fallback are unavailable.
        """
        vix_key = "NSE_INDEX|India VIX"
        vix_quote = quotes.get(vix_key, {})
        vix_ltp = vix_quote.get("ltp")
        updated_epoch = vix_quote.get("updated_at_epoch")
        age_sec = (now_ts - float(updated_epoch)) if updated_epoch is not None else None

        # Freshness requires a verified timestamp within DEFAULT_VIX_MAX_AGE_SEC (300s).
        # A quote with no timestamp cannot be verified and is treated as unverified/stale.
        is_feed_fresh = (
            vix_ltp is not None
            and age_sec is not None
            and 0.0 <= age_sec <= DEFAULT_VIX_MAX_AGE_SEC
        )

        if is_feed_fresh and vix_ltp is not None:
            vix_val = float(vix_ltp)
            regime = self._map_vix_regime(vix_val)
            return vix_val, regime, True

        # Fallback to shared SQLite if feed quote is missing or stale
        fallback_vix = self._query_sqlite_recent_vix(now_ts)
        if fallback_vix is not None:
            logger.warning(
                "Live India VIX missing/stale (age=%s); using SQLite EOD fallback: %.2f",
                age_sec,
                fallback_vix,
            )
            return fallback_vix, self._map_vix_regime(fallback_vix), False

        logger.warning("India VIX unavailable in both feed and SQLite fallback.")
        return None, VolatilityRegime.NORMAL, False

    def _map_vix_regime(self, vix_val: float) -> VolatilityRegime:
        """Maps numeric VIX value to VolatilityRegime enum."""
        if vix_val > 18.0:
            return VolatilityRegime.ELEVATED
        elif vix_val < 11.5:
            return VolatilityRegime.LOW_IV_CRUSH
        return VolatilityRegime.NORMAL

    def _query_sqlite_recent_vix(self, now_ts: float) -> Optional[float]:
        """Queries Shared SQLite for most recent India VIX reading within the last 5 days."""
        db_path = self._resolve_shared_sqlite_path(now_ts)
        if not db_path:
            return None
        min_valid_ts = now_ts - (5.0 * 86400.0)  # Max 5 days back (to cover weekends & holidays)
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                cursor = conn.execute(
                    "SELECT data, ts FROM market_data WHERE (pk LIKE '%VIX%' OR symbol LIKE '%VIX%') AND ts <= ? AND ts >= ? ORDER BY ts DESC LIMIT 1",
                    (now_ts, min_valid_ts),
                )
                row = cursor.fetchone()
                if row:
                    data_raw, entry_ts = row
                    parsed = json.loads(data_raw) if isinstance(data_raw, str) else data_raw
                    val = parsed.get("ltp") or parsed.get("close") or parsed.get("vix")
                    if val is not None:
                        return float(val)
        except Exception as exc:
            logger.debug("Failed to query SQLite recent vix: %s", exc)
        return None

    def _load_prior_day_structure(
        self,
        quotes: Dict[str, Any],
        now_ts: float,
        nifty_prev_close: Optional[float],
    ) -> Tuple[SourceComponent, Optional[Dict[str, Any]]]:
        """
        Ingests and normalizes Prior Day OHLC structure:
          1. Gap % vs Prior Day Close (0.40% gap -> +/- 1.0)
          2. Range Context: Breakout above Prior High (+0.5), breakdown below Prior Low (-0.5), inside range (0.0)
          3. Prior Day Bar Direction: Green day (Close > Open) -> +0.2, Red day (Close < Open) -> -0.2
        """
        nominal_weight = self.weights.get("prior_day_structure", DEFAULT_WEIGHT_PRIOR_DAY)

        # 1. Try to extract OHLC from feed quote first
        nifty_quote = quotes.get("NSE_INDEX|Nifty 50", {})
        curr_spot = nifty_quote.get("ltp")
        p_close = nifty_prev_close or nifty_quote.get("cp") or nifty_quote.get("close")
        p_open = nifty_quote.get("open")
        p_high = nifty_quote.get("high")
        p_low = nifty_quote.get("low")

        # 2. If high/low missing from feed, query SQLite market_data
        if (p_high is None or p_low is None) and self._resolve_shared_sqlite_path(now_ts):
            db_path = self._resolve_shared_sqlite_path(now_ts)
            min_valid_ts = now_ts - (5.0 * 86400.0)
            try:
                with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                    cursor = conn.execute(
                        "SELECT data FROM market_data WHERE pk = 'INDEX#NIFTY 50#DAILY' AND ts <= ? AND ts >= ? ORDER BY ts DESC LIMIT 1",
                        (now_ts, min_valid_ts),
                    )
                    row = cursor.fetchone()
                    if row:
                        parsed = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                        p_open = p_open or parsed.get("open")
                        p_high = p_high or parsed.get("high")
                        p_low = p_low or parsed.get("low")
                        p_close = p_close or parsed.get("close")
            except Exception as exc:
                logger.debug("Failed to read prior day ohlc from sqlite: %s", exc)

        # Invariant: Require actual spot, close, high, and low.
        # Never fabricate +/-0.5% high/low, which could trigger false breakout scores.
        if not p_close or curr_spot is None or p_high is None or p_low is None:
            missing_fields = []
            if not p_close:
                missing_fields.append("p_close")
            if curr_spot is None:
                missing_fields.append("spot")
            if p_high is None:
                missing_fields.append("high")
            if p_low is None:
                missing_fields.append("low")
            return (
                SourceComponent(
                    name="prior_day_structure",
                    raw_value=None,
                    normalized_score=0.0,
                    weight=nominal_weight,
                    is_available=False,
                    metadata={"reason": f"incomplete_prior_ohlc: missing {','.join(missing_fields)}"},
                ),
                None,
            )

        curr_spot = float(curr_spot)
        p_close = float(p_close)
        p_high = float(p_high)
        p_low = float(p_low)
        p_open = float(p_open) if p_open is not None else p_close

        # 1. Opening Gap
        gap_pct = ((curr_spot - p_close) / p_close) * 100.0
        score_gap = _clip(gap_pct / 0.40, -1.0, 1.0) * 0.50

        # 2. Breakout / Range Context
        if curr_spot > p_high:
            score_range = 0.35  # Gap & Go expansion above prior day high
        elif curr_spot < p_low:
            score_range = -0.35 # Breakdown below prior day low
        else:
            score_range = 0.0   # Inside prior day range (mean-reverting)

        # 3. Prior Day Bar Sentiment (Green vs Red day)
        if p_close > p_open:
            score_bar = 0.15
        elif p_close < p_open:
            score_bar = -0.15
        else:
            score_bar = 0.0

        total_structure_score = _clip(score_gap + score_range + score_bar, -1.0, 1.0)
        ohlc_data = {
            "open": p_open,
            "high": p_high,
            "low": p_low,
            "close": p_close,
            "current_spot": curr_spot,
            "gap_pct": round(gap_pct, 3),
            "range_pts": round(p_high - p_low, 2),
            "is_above_prior_high": curr_spot > p_high,
            "is_below_prior_low": curr_spot < p_low,
        }

        return (
            SourceComponent(
                name="prior_day_structure",
                raw_value=ohlc_data,
                normalized_score=total_structure_score,
                weight=nominal_weight,
                is_available=True,
                is_stale=False,
                metadata=ohlc_data,
            ),
            ohlc_data,
        )

    def _load_grok_social(self, now_ts: float) -> Tuple[SourceComponent, Optional[Dict[str, Any]]]:
        """
        Loads X/AI Grok social sentiment from SOCIAL_DATA_DIR (e.g. /home/ubuntu/data/social/*.json).
        Extracts net_bias_score in [-1.0, +1.0] and confidence.
        Freshness limit: 18 hours (DEFAULT_SOCIAL_MAX_AGE_HOURS).
        """
        nominal_weight = self.weights.get("grok_social", DEFAULT_WEIGHT_GROK_SOCIAL)
        if not os.path.isdir(self.social_data_dir):
            return (
                SourceComponent(
                    name="grok_social",
                    raw_value=None,
                    normalized_score=0.0,
                    weight=nominal_weight,
                    is_available=False,
                    metadata={"reason": "social_dir_not_found"},
                ),
                None,
            )

        pattern = os.path.join(self.social_data_dir, "*.json")
        matches = sorted(glob.glob(pattern), reverse=True)
        if not matches:
            return (
                SourceComponent(
                    name="grok_social",
                    raw_value=None,
                    normalized_score=0.0,
                    weight=nominal_weight,
                    is_available=False,
                    metadata={"reason": "no_social_json_files"},
                ),
                None,
            )

        latest_file = matches[0]
        try:
            mtime = os.path.getmtime(latest_file)
            age_sec = now_ts - mtime
            is_stale = age_sec > (DEFAULT_SOCIAL_MAX_AGE_HOURS * 3600)

            with open(latest_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            net_bias = float(data.get("net_bias_score", 0.0))
            confidence = float(data.get("confidence", 0.70))
            overall_sentiment = data.get("overall_sentiment", "neutral")
            key_factors = data.get("key_factors", [])

            # Weighted score by confidence
            normalized_score = _clip(net_bias * confidence, -1.0, 1.0)
            social_meta = {
                "file": os.path.basename(latest_file),
                "age_hours": round(age_sec / 3600.0, 1),
                "net_bias": net_bias,
                "confidence": confidence,
                "overall_sentiment": overall_sentiment,
                "key_factors": key_factors,
            }

            return (
                SourceComponent(
                    name="grok_social",
                    raw_value=net_bias,
                    normalized_score=normalized_score,
                    weight=nominal_weight,
                    age_sec=age_sec,
                    is_available=True,
                    is_stale=is_stale,
                    metadata=social_meta,
                ),
                social_meta,
            )
        except Exception as exc:
            logger.error("Failed to load grok social from %s: %s", latest_file, exc)
            return (
                SourceComponent(
                    name="grok_social",
                    raw_value=None,
                    normalized_score=0.0,
                    weight=nominal_weight,
                    is_available=False,
                    metadata={"error": str(exc)},
                ),
                None,
            )
