"""
Tests for Pre-Market Multi-Source Ingestion & Scoring Engine (Phase 1).
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
import pytest

from strategies.opening_straddle_models import (
    BiasClassification,
    ConfidenceLevel,
    VolatilityRegime,
)
from strategies.premarket_intelligence import (
    DEFAULT_WEIGHT_GIFT_NIFTY,
    DEFAULT_WEIGHT_GLOBAL_MACRO,
    DEFAULT_WEIGHT_NEWS_NLP,
    DEFAULT_WEIGHT_FII_FLOW,
    DEFAULT_WEIGHT_UPSTOX_PULSE,
    PreMarketIntelligenceLoader,
)


@pytest.fixture
def test_env(tmp_path):
    """
    Creates isolated temporary files and databases for multi-source ingestion.
    """
    now_epoch = 1791957300.0  # Reference epoch time for tests
    now_dt = datetime.fromtimestamp(now_epoch, tz=timezone.utc)

    # 1. Feed cache JSON (/tmp/upstox_live_quotes.json mock)
    feed_cache_file = tmp_path / "upstox_live_quotes.json"
    feed_payload = {
        "source": "upstox_v3_websocket",
        "timestamp_utc": now_dt.isoformat(),
        "quotes": {
            "GLOBAL_INDEX|SGX NIFTY": {
                "instrument_key": "GLOBAL_INDEX|SGX NIFTY",
                "symbol": "GIFTNIFTY",
                "ltp": 25100.0,
                "cp": 25000.0,
                "change_pct": 0.40,  # +0.40% gap
                "updated_at_epoch": now_epoch - 30.0,  # 30s old (fresh)
            },
            "GLOBAL_INDEX|^DJI": {
                "instrument_key": "GLOBAL_INDEX|^DJI",
                "symbol": "DOWJONES",
                "ltp": 44000.0,
                "change_pct": 0.50,  # +0.50% Dow
                "updated_at_epoch": now_epoch - 1800.0,  # 30m old (fresh for macro)
            },
            "GLOBAL_INDICATOR|BZUSD": {
                "instrument_key": "GLOBAL_INDICATOR|BZUSD",
                "symbol": "BRENTCRUDE",
                "ltp": 75.0,
                "change_pct": -0.75,  # -0.75% oil drop (positive for India)
                "updated_at_epoch": now_epoch - 600.0,
            },
            "GLOBAL_INDICATOR|USDINR": {
                "instrument_key": "GLOBAL_INDICATOR|USDINR",
                "symbol": "USDINR",
                "ltp": 84.50,
                "change_pct": -0.15,  # -0.15% rupee appreciation (positive for India)
                "updated_at_epoch": now_epoch - 600.0,
            },
            "NSE_INDEX|India VIX": {
                "instrument_key": "NSE_INDEX|India VIX",
                "symbol": "INDIAVIX",
                "ltp": 14.50,  # Normal regime
                "updated_at_epoch": now_epoch - 20.0,
            },
            "NSE_INDEX|Nifty 50": {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "symbol": "NIFTY50",
                "ltp": 25080.0,
                "cp": 25000.0,
                "updated_at_epoch": now_epoch - 10.0,
            },
        },
    }
    feed_cache_file.write_text(json.dumps(feed_payload), encoding="utf-8")

    # 2. Shared SQLite DB for News NLP and FII/DII flow
    sqlite_dir = tmp_path / "sqlite"
    sqlite_dir.mkdir()
    db_file = sqlite_dir / f"signals_data_{now_dt.strftime('%Y_%m')}.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("""
            CREATE TABLE market_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pk TEXT NOT NULL,
                ts INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                data JSON NOT NULL
            )
        """)
        # Insert NEWS#MARKET#DAILY
        news_data = {
            "pk": "NEWS#MARKET#DAILY",
            "ts": int(now_epoch - 1800),
            "date": now_dt.strftime("%Y-%m-%d"),
            "net_bias_score": 0.45,
            "sentiment_score": 0.50,
            "contrarian_regime": "NONE",
        }
        conn.execute(
            "INSERT INTO market_data (pk, ts, symbol, data) VALUES (?, ?, ?, ?)",
            ("NEWS#MARKET#DAILY", int(now_epoch - 1800), "GLOBAL", json.dumps(news_data)),
        )
        # Insert FLOW#FII_DII#DAILY
        flow_data = {
            "pk": "FLOW#FII_DII#DAILY",
            "ts": int(now_epoch - 3600),
            "date": now_dt.strftime("%Y-%m-%d"),
            "fno_positioning": {
                "fii_futures_5d_sum": 1500.0,  # +1500 Cr net long
                "bias": "BULLISH",
            },
            "certainty": 0.90,
        }
        conn.execute(
            "INSERT INTO market_data (pk, ts, symbol, data) VALUES (?, ?, ?, ?)",
            ("FLOW#FII_DII#DAILY", int(now_epoch - 3600), "GLOBAL", json.dumps(flow_data)),
        )
        conn.commit()

    # 3. Upstox Daily Pulse JSON file
    pulse_dir = tmp_path / "market_news"
    pulse_sub_dir = pulse_dir / now_dt.strftime("%Y") / now_dt.strftime("%m")
    pulse_sub_dir.mkdir(parents=True)
    pulse_file = pulse_sub_dir / f"{now_dt.strftime('%Y-%m-%d')}.json"
    pulse_payload = {
        "date": now_dt.strftime("%Y-%m-%d"),
        "overall_sentiment": "bullish",
        "scenario_analysis": [
            {"scenario": "bullish", "probability": 0.65},
            {"scenario": "base", "probability": 0.25},
            {"scenario": "bearish", "probability": 0.10},
        ],
        "support_levels": {"nifty": [24950, 24900]},
        "resistance_levels": {"nifty": [25150, 25200]},
        "important_numeric_values": {"pcr_nifty": 1.15},
    }
    pulse_file.write_text(json.dumps(pulse_payload), encoding="utf-8")

    return {
        "now_epoch": now_epoch,
        "feed_cache_path": str(feed_cache_file),
        "sqlite_base_dir": str(sqlite_dir),
        "pulse_data_dir": str(pulse_dir),
    }


def test_full_availability_and_v2_weights(test_env):
    """
    Verify full multi-source ingestion produces 100% coverage, High confidence,
    and applies Version 2 weights correctly.
    """
    loader = PreMarketIntelligenceLoader(
        feed_cache_path=test_env["feed_cache_path"],
        sqlite_base_dir=test_env["sqlite_base_dir"],
        pulse_data_dir=test_env["pulse_data_dir"],
    )

    intel = loader.load_intelligence(ref_time_epoch=test_env["now_epoch"])

    # 1. Coverage & Confidence
    assert intel.coverage == pytest.approx(1.0, abs=1e-3)
    assert intel.confidence == ConfidenceLevel.HIGH

    # 2. Components check
    assert intel.components["gift_nifty"].is_available
    assert not intel.components["gift_nifty"].is_stale
    assert intel.components["gift_nifty"].weight == DEFAULT_WEIGHT_GIFT_NIFTY  # 0.30

    assert intel.components["global_macro"].is_available
    assert intel.components["global_macro"].weight == DEFAULT_WEIGHT_GLOBAL_MACRO  # 0.25

    assert intel.components["news_nlp"].is_available
    assert intel.components["news_nlp"].weight == DEFAULT_WEIGHT_NEWS_NLP  # 0.25

    assert intel.components["fii_flow"].is_available
    assert intel.components["fii_flow"].weight == DEFAULT_WEIGHT_FII_FLOW  # 0.10

    assert intel.components["upstox_pulse"].is_available
    assert intel.components["upstox_pulse"].weight == DEFAULT_WEIGHT_UPSTOX_PULSE  # 0.10

    # 3. Normalized Score Calculations
    # GIFT: +0.40% / 0.50% = +0.80
    assert intel.components["gift_nifty"].normalized_score == pytest.approx(0.80, abs=1e-2)

    # News: +0.45
    assert intel.components["news_nlp"].normalized_score == pytest.approx(0.45, abs=1e-2)

    # FII: (1500 / 2500) * 0.90 = +0.54
    assert intel.components["fii_flow"].normalized_score == pytest.approx(0.54, abs=1e-2)

    # Pulse: 0.65 - 0.10 = +0.55
    assert intel.components["upstox_pulse"].normalized_score == pytest.approx(0.55, abs=1e-2)

    # 4. Composite Directional Score & Bias
    assert intel.cds > 0.25
    assert intel.bias == BiasClassification.STRONG_BULLISH

    # 5. Contrarian Filter Eligibility
    # Coverage >= 80% and |CDS| >= 0.25
    assert intel.is_contrarian_filter_eligible is True

    # 6. Volatility & Levels
    assert intel.vix == pytest.approx(14.50, abs=1e-2)
    assert intel.vix_regime == VolatilityRegime.NORMAL
    assert intel.upstox_pulse_levels["support"] == [24950, 24900]
    assert intel.upstox_pulse_levels["resistance"] == [25150, 25200]


def test_coverage_degradation_and_confidence_levels(test_env, tmp_path):
    """
    Verify coverage calculation and confidence drop when data sources are missing.
    """
    # Create an empty sqlite directory and empty pulse directory
    empty_sqlite = tmp_path / "empty_sqlite"
    empty_sqlite.mkdir()
    empty_pulse = tmp_path / "empty_pulse"
    empty_pulse.mkdir()

    # Only feed cache (GIFT + Macro) is available: 0.30 + 0.25 = 0.55 nominal weight
    loader = PreMarketIntelligenceLoader(
        feed_cache_path=test_env["feed_cache_path"],
        sqlite_base_dir=str(empty_sqlite),
        pulse_data_dir=str(empty_pulse),
    )

    intel = loader.load_intelligence(ref_time_epoch=test_env["now_epoch"])

    # Available weight: 0.30 + 0.25 = 0.55 -> LOW confidence (< 60%)
    assert intel.coverage == pytest.approx(0.55, abs=1e-2)
    assert intel.confidence == ConfidenceLevel.LOW
    assert intel.bias == BiasClassification.NEUTRAL  # Low confidence defaults to NEUTRAL

    # Contrarian filter MUST be disabled under low confidence!
    assert intel.is_contrarian_filter_eligible is False


def test_contrarian_filter_coverage_80_threshold(test_env, tmp_path):
    """
    Verify the user's rule: the +4.0% contrarian filter strictly requires Coverage >= 80%.
    Even if provisional CDS is strong (+0.50), if coverage < 80%, it must be INELIGIBLE.
    """
    # Include News (0.25), GIFT (0.30), and Macro (0.25) -> Total = 0.80 coverage
    # Omit FII and Pulse by setting empty pulse and empty sqlite table for FII
    empty_pulse = tmp_path / "no_pulse"
    empty_pulse.mkdir()

    loader_80 = PreMarketIntelligenceLoader(
        feed_cache_path=test_env["feed_cache_path"],
        sqlite_base_dir=test_env["sqlite_base_dir"],
        pulse_data_dir=str(empty_pulse),
    )

    intel_80 = loader_80.load_intelligence(ref_time_epoch=test_env["now_epoch"])
    # Coverage: 0.30 (GIFT) + 0.25 (Macro) + 0.25 (News) + 0.10 (FII) = 0.90 (>= 0.80)
    assert intel_80.coverage >= 0.80
    assert intel_80.confidence == ConfidenceLevel.HIGH
    assert intel_80.is_contrarian_filter_eligible is True

    # Now simulate GIFT Nifty missing (Drops coverage by 0.30 down to 0.60)
    missing_gift_feed = tmp_path / "no_gift_feed.json"
    missing_gift_payload = {
        "source": "upstox_v3_websocket",
        "quotes": {
            "GLOBAL_INDEX|^DJI": {"change_pct": 1.5, "updated_at_epoch": test_env["now_epoch"]},
            "NSE_INDEX|India VIX": {"ltp": 13.0, "updated_at_epoch": test_env["now_epoch"]},
        },
    }
    missing_gift_feed.write_text(json.dumps(missing_gift_payload), encoding="utf-8")

    loader_moderate = PreMarketIntelligenceLoader(
        feed_cache_path=str(missing_gift_feed),
        sqlite_base_dir=test_env["sqlite_base_dir"],
        pulse_data_dir=str(empty_pulse),
    )

    intel_mod = loader_moderate.load_intelligence(ref_time_epoch=test_env["now_epoch"])
    # Macro (0.25) + News (0.25) + FII (0.10) = 0.60
    assert intel_mod.coverage == pytest.approx(0.60, abs=1e-2)
    assert intel_mod.confidence == ConfidenceLevel.MODERATE
    # Crucial assertion: Contrarian filter is NOT eligible when Coverage < 80%
    assert intel_mod.is_contrarian_filter_eligible is False


def test_stale_gift_nifty_is_excluded(test_env, tmp_path):
    """
    Verify that a GIFT Nifty quote older than 180 seconds is marked stale
    and excluded from coverage calculation.
    """
    stale_feed = tmp_path / "stale_feed.json"
    now_epoch = test_env["now_epoch"]
    stale_payload = {
        "quotes": {
            "GLOBAL_INDEX|SGX NIFTY": {
                "ltp": 25100.0,
                "change_pct": 0.50,
                "updated_at_epoch": now_epoch - 300.0,  # 300 seconds old (> 180s threshold)
            },
            "NSE_INDEX|India VIX": {"ltp": 15.0, "updated_at_epoch": now_epoch - 10.0},
        }
    }
    stale_feed.write_text(json.dumps(stale_payload), encoding="utf-8")

    loader = PreMarketIntelligenceLoader(
        feed_cache_path=str(stale_feed),
        sqlite_base_dir=test_env["sqlite_base_dir"],
        pulse_data_dir=test_env["pulse_data_dir"],
    )

    intel = loader.load_intelligence(ref_time_epoch=now_epoch)
    comp_gift = intel.components["gift_nifty"]

    assert comp_gift.is_available is True
    assert comp_gift.is_stale is True
    assert comp_gift.age_sec == 300.0

    # Since GIFT is stale, nominal 0.30 weight is not in active coverage
    # Macro is also missing from this feed (0.25)
    # Active: News (0.25) + FII (0.10) + Pulse (0.10) = 0.45
    assert intel.coverage == pytest.approx(0.45, abs=1e-2)
    assert intel.confidence == ConfidenceLevel.LOW


def test_vix_volatility_regimes(tmp_path):
    """
    Verify correct classification of India VIX volatility regimes.
    """
    now = time.time()

    def run_with_vix(vix_value):
        feed = tmp_path / f"feed_{vix_value}.json"
        feed.write_text(
            json.dumps({"quotes": {"NSE_INDEX|India VIX": {"ltp": vix_value, "updated_at_epoch": now}}}),
            encoding="utf-8",
        )
        loader = PreMarketIntelligenceLoader(feed_cache_path=str(feed))
        return loader.load_intelligence(ref_time_epoch=now)

    assert run_with_vix(21.5).vix_regime == VolatilityRegime.ELEVATED
    assert run_with_vix(10.2).vix_regime == VolatilityRegime.LOW_IV_CRUSH
    assert run_with_vix(15.0).vix_regime == VolatilityRegime.NORMAL


def test_global_macro_inverse_logic(test_env, tmp_path):
    """
    Verify that Brent crude rise and USDINR depreciation produce negative scores,
    while Dow Jones rise produces a positive score.
    """
    now = test_env["now_epoch"]
    feed = tmp_path / "macro_test.json"
    payload = {
        "quotes": {
            "GLOBAL_INDEX|^DJI": {"change_pct": 1.0, "updated_at_epoch": now},     # Dow +1% -> +1.0 score
            "GLOBAL_INDICATOR|BZUSD": {"change_pct": 1.5, "updated_at_epoch": now}, # Brent +1.5% -> -1.0 score
            "GLOBAL_INDICATOR|USDINR": {"change_pct": 0.3, "updated_at_epoch": now},# USDINR +0.3% -> -1.0 score
        }
    }
    feed.write_text(json.dumps(payload), encoding="utf-8")

    loader = PreMarketIntelligenceLoader(
        feed_cache_path=str(feed),
        sqlite_base_dir=tmp_path,  # empty
        pulse_data_dir=tmp_path,   # empty
    )
    intel = loader.load_intelligence(ref_time_epoch=now)
    macro = intel.components["global_macro"]

    # 50% * (+1.0) + 25% * (-1.0) + 25% * (-1.0) = 0.50 - 0.25 - 0.25 = 0.0
    assert macro.normalized_score == pytest.approx(0.0, abs=1e-3)


def test_serialization_methods(test_env):
    """
    Verify to_dict and summary_str run cleanly and output expected fields.
    """
    loader = PreMarketIntelligenceLoader(
        feed_cache_path=test_env["feed_cache_path"],
        sqlite_base_dir=test_env["sqlite_base_dir"],
        pulse_data_dir=test_env["pulse_data_dir"],
    )
    intel = loader.load_intelligence(ref_time_epoch=test_env["now_epoch"])

    d = intel.to_dict()
    assert "cds" in d
    assert "coverage" in d
    assert "confidence" in d
    assert "components" in d
    assert "is_contrarian_filter_eligible" in d

    s = intel.summary_str()
    assert "CDS=" in s
    assert "Coverage=" in s
    assert "ContrarianFilter=ELIGIBLE" in s


def test_vix_freshness_and_skip_gate(tmp_path):
    """
    Verify VIX freshness checking, fail-safe skip on high VIX, and fail-safe on missing VIX.
    """
    now = 1791957300.0

    # Case 1: High VIX (> 20.0) -> triggers vix_skip
    feed_high = tmp_path / "feed_high_vix.json"
    feed_high.write_text(json.dumps({
        "quotes": {
            "NSE_INDEX|India VIX": {"ltp": 22.5, "updated_at_epoch": now - 30.0},
        }
    }), encoding="utf-8")
    loader_high = PreMarketIntelligenceLoader(feed_cache_path=str(feed_high))
    intel_high = loader_high.load_intelligence(ref_time_epoch=now)
    assert intel_high.vix == pytest.approx(22.5, abs=1e-2)
    assert intel_high.vix_fresh is True
    assert intel_high.vix_skip is True
    assert "HIGH_VIX" in intel_high.vix_skip_reason

    # Case 2: Normal Fresh VIX (15.0 <= 20.0) -> passes gate
    feed_normal = tmp_path / "feed_normal_vix.json"
    feed_normal.write_text(json.dumps({
        "quotes": {
            "NSE_INDEX|India VIX": {"ltp": 15.0, "updated_at_epoch": now - 30.0},
        }
    }), encoding="utf-8")
    loader_normal = PreMarketIntelligenceLoader(feed_cache_path=str(feed_normal))
    intel_normal = loader_normal.load_intelligence(ref_time_epoch=now)
    assert intel_normal.vix == pytest.approx(15.0, abs=1e-2)
    assert intel_normal.vix_fresh is True
    assert intel_normal.vix_skip is False

    # Case 3: Completely missing VIX -> fail closed
    feed_empty = tmp_path / "feed_empty.json"
    feed_empty.write_text(json.dumps({"quotes": {}}), encoding="utf-8")
    loader_empty = PreMarketIntelligenceLoader(feed_cache_path=str(feed_empty), sqlite_base_dir=str(tmp_path))
    intel_empty = loader_empty.load_intelligence(ref_time_epoch=now)
    assert intel_empty.vix_fresh is False
    assert intel_empty.vix_skip is True
    assert "MISSING_VIX" in intel_empty.vix_skip_reason


def test_prior_day_structure_ingestion(tmp_path):
    """
    Verify calculation of gap, range breakout vs high/low, and bar direction.
    """
    now = 1791957300.0
    feed = tmp_path / "feed_structure.json"
    # Spot is 25150, prev close 25000, high 25100 (spot > high -> breakout expansion)
    feed.write_text(json.dumps({
        "quotes": {
            "NSE_INDEX|Nifty 50": {
                "ltp": 25150.0,
                "cp": 25000.0,
                "open": 24900.0,
                "high": 25100.0,
                "low": 24850.0,
                "updated_at_epoch": now - 10.0,
            }
        }
    }), encoding="utf-8")

    loader = PreMarketIntelligenceLoader(feed_cache_path=str(feed))
    loader.apply_directional_scalp_weights(weight_prior_day=0.30)
    intel = loader.load_intelligence(ref_time_epoch=now)

    comp = intel.components["prior_day_structure"]
    assert comp.is_available is True
    assert comp.normalized_score > 0.0  # Bullish expansion
    assert intel.prior_day_ohlc is not None
    assert intel.prior_day_ohlc["is_above_prior_high"] is True
    assert intel.prior_day_ohlc["gap_pct"] == pytest.approx(0.60, abs=1e-2)


def test_grok_social_sentiment_ingestion(tmp_path):
    """
    Verify loading and confidence-weighting of X/AI Grok social sentiment from JSON files.
    """
    now = 1791957300.0
    social_dir = tmp_path / "social"
    social_dir.mkdir()
    social_file = social_dir / "2026-10-31_0900.json"
    social_file.write_text(json.dumps({
        "source": "grok_social",
        "net_bias_score": 0.60,
        "confidence": 0.80,
        "overall_sentiment": "bullish",
        "key_factors": ["fii_buying", "global_tech_rally"],
    }), encoding="utf-8")
    # Set file mtime to 30 mins ago
    os.utime(social_file, (now - 1800, now - 1800))

    loader = PreMarketIntelligenceLoader(social_data_dir=str(social_dir))
    loader.apply_directional_scalp_weights(weight_grok=0.20)
    intel = loader.load_intelligence(ref_time_epoch=now)

    comp = intel.components["grok_social"]
    assert comp.is_available is True
    assert not comp.is_stale
    # 0.60 * 0.80 = 0.48
    assert comp.normalized_score == pytest.approx(0.48, abs=1e-2)
    assert intel.grok_social_sentiment["overall_sentiment"] == "bullish"


def test_single_leg_enums_and_session_record():
    """
    Verify OrderAction, TradeLeg, StrategyState, ExitReason enums and StraddleSessionRecord single-leg fields.
    """
    from strategies.opening_straddle_models import (
        OrderAction,
        TradeLeg,
        StrategyState,
        ExitReason,
        StraddleSessionRecord,
        PreMarketIntelligence,
        VolatilityRegime,
        ConfidenceLevel,
        BiasClassification,
    )

    assert OrderAction.BUY == "BUY"
    assert OrderAction.SELL == "SELL"
    assert TradeLeg.CALL == "CALL"
    assert TradeLeg.PUT == "PUT"
    assert StrategyState.SKIPPED_HIGH_VIX == "SKIPPED_HIGH_VIX"
    assert ExitReason.HIGH_VIX_SKIP == "HIGH_VIX_SKIP"
    assert ExitReason.MIN_PROFIT_TARGET == "MIN_PROFIT_TARGET"

    mock_intel = PreMarketIntelligence(
        cds=0.35,
        coverage=0.90,
        confidence=ConfidenceLevel.HIGH,
        bias=BiasClassification.STRONG_BULLISH,
        components={},
        vix=14.5,
        vix_regime=VolatilityRegime.NORMAL,
        timestamp_utc="2026-10-31T03:44:00Z",
    )

    record = StraddleSessionRecord(
        trade_date="2026-10-31",
        expiry_date="2026-11-03",
        atm_strike=25000,
        pre_market=mock_intel,
        order_action=OrderAction.SELL,
        trade_leg=TradeLeg.CALL,
        executed_strike=25050,
        entry_price=85.0,
        margin_inr=120000.0,
        state=StrategyState.SINGLE_LEG_ACTIVE,
    )
    assert record.order_action == OrderAction.SELL
    assert record.trade_leg == TradeLeg.CALL
    assert record.executed_strike == 25050
    assert record.entry_price == 85.0


def test_cms_low_coverage_forces_zero_score(tmp_path):
    """
    Finding 1: Verify that if coverage < 0.50, composite_morning_score is forced to 0.0
    even if an individual active source has a strong directional score (+1.0).
    """
    now = 1791957300.0
    feed = tmp_path / "feed_low_coverage.json"
    # Only DJI is present (nominal weight 0.25). All other feeds missing.
    feed.write_text(json.dumps({
        "quotes": {
            "GLOBAL_INDEX|^DJI": {"change_pct": 2.0, "updated_at_epoch": now - 30.0},
            "NSE_INDEX|India VIX": {"ltp": 14.0, "updated_at_epoch": now - 30.0},
        }
    }), encoding="utf-8")

    loader = PreMarketIntelligenceLoader(
        feed_cache_path=str(feed),
        sqlite_base_dir=str(tmp_path / "empty_sqlite"),
        pulse_data_dir=str(tmp_path / "empty_pulse"),
        social_data_dir=str(tmp_path / "empty_social"),
    )
    intel = loader.load_intelligence(ref_time_epoch=now)
    assert intel.coverage < 0.50
    assert intel.composite_morning_score == 0.0
    assert intel.bias == BiasClassification.NEUTRAL


def test_directional_scalp_mode_initialization():
    """
    Finding 2: Verify that directional_scalp=True activates the full 5-factor scalp weights.
    """
    loader_default = PreMarketIntelligenceLoader()
    assert loader_default.weights["prior_day_structure"] == 0.0
    assert loader_default.weights["grok_social"] == 0.0

    loader_scalp = PreMarketIntelligenceLoader(directional_scalp=True)
    assert loader_scalp.weights["gift_nifty"] == 0.25
    assert loader_scalp.weights["prior_day_structure"] == 0.25
    assert loader_scalp.weights["upstox_pulse"] == 0.15
    assert loader_scalp.weights["news_nlp"] == 0.15
    assert loader_scalp.weights["grok_social"] == 0.10
    assert loader_scalp.weights["fii_flow"] == 0.10
    assert loader_scalp.weights["global_macro"] == 0.0


def test_vix_missing_timestamp_treated_as_stale(tmp_path):
    """
    Finding 3a: Verify that a VIX quote with no updated_at_epoch is NOT assumed fresh.
    """
    now = 1791957300.0
    feed = tmp_path / "feed_no_vix_ts.json"
    feed.write_text(json.dumps({
        "quotes": {
            "NSE_INDEX|India VIX": {"ltp": 15.0},  # No updated_at_epoch
        }
    }), encoding="utf-8")

    loader = PreMarketIntelligenceLoader(
        feed_cache_path=str(feed),
        sqlite_base_dir=str(tmp_path / "empty_sqlite"),
    )
    intel = loader.load_intelligence(ref_time_epoch=now)
    # Stale/unverified feed quote with no fallback available -> fail closed
    assert intel.vix_fresh is False
    assert intel.vix_skip is True
    assert "MISSING_VIX" in intel.vix_skip_reason


def test_vix_fallback_threshold_and_ts_constraint(tmp_path):
    """
    Finding 3b & 3c:
    - Fallback VIX > 18.0 must trigger HIGH_VIX_FALLBACK skip.
    - SQLite readings older than 5 days must be rejected.
    """
    import sqlite3
    now = 1791957300.0
    db_dir = tmp_path / "sqlite"
    db_dir.mkdir()
    db_path = db_dir / "signals_data_2026_10.db"

    # 1. Setup fallback VIX with 19.5 (> 18.0) recorded 1 hour ago
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE market_data (pk TEXT, symbol TEXT, data TEXT, ts REAL)")
        conn.execute(
            "INSERT INTO market_data VALUES (?, ?, ?, ?)",
            ("INDEX#VIX#DAILY", "INDIAVIX", json.dumps({"ltp": 19.5}), now - 3600.0),
        )

    # Empty feed cache forces SQLite fallback
    empty_feed = tmp_path / "empty_feed.json"
    empty_feed.write_text(json.dumps({"quotes": {}}), encoding="utf-8")

    loader = PreMarketIntelligenceLoader(
        feed_cache_path=str(empty_feed),
        sqlite_base_dir=str(db_dir),
    )
    intel = loader.load_intelligence(ref_time_epoch=now)
    assert intel.vix == pytest.approx(19.5, abs=1e-2)
    assert intel.vix_fresh is False
    assert intel.vix_skip is True
    assert "HIGH_VIX_FALLBACK" in intel.vix_skip_reason

    # 2. Setup SQLite with VIX recorded 10 days ago (stale > 5 days)
    stale_db_dir = tmp_path / "stale_sqlite"
    stale_db_dir.mkdir()
    stale_db_path = stale_db_dir / "signals_data_2026_10.db"
    with sqlite3.connect(stale_db_path) as conn:
        conn.execute("CREATE TABLE market_data (pk TEXT, symbol TEXT, data TEXT, ts REAL)")
        conn.execute(
            "INSERT INTO market_data VALUES (?, ?, ?, ?)",
            ("INDEX#VIX#DAILY", "INDIAVIX", json.dumps({"ltp": 14.0}), now - (10.0 * 86400.0)),
        )

    loader_stale = PreMarketIntelligenceLoader(
        feed_cache_path=str(empty_feed),
        sqlite_base_dir=str(stale_db_dir),
    )
    intel_stale = loader_stale.load_intelligence(ref_time_epoch=now)
    # 10 days old fallback rejected -> missing VIX
    assert intel_stale.vix is None
    assert intel_stale.vix_skip is True
    assert "MISSING_VIX" in intel_stale.vix_skip_reason


def test_missing_vix_reports_none(tmp_path):
    """
    Finding 4: Missing VIX must be reported as None, not substituted with 14.0.
    """
    now = 1791957300.0
    empty_feed = tmp_path / "empty_feed.json"
    empty_feed.write_text(json.dumps({"quotes": {}}), encoding="utf-8")

    loader = PreMarketIntelligenceLoader(
        feed_cache_path=str(empty_feed),
        sqlite_base_dir=str(tmp_path / "empty_sqlite"),
    )
    intel = loader.load_intelligence(ref_time_epoch=now)
    assert intel.vix is None
    assert intel.vix_skip is True
    assert "MISSING_VIX" in intel.vix_skip_reason


def test_incomplete_prior_day_ohlc_not_fabricated(tmp_path):
    """
    Finding 5: Incomplete prior-day OHLC (missing high or low) must be marked unavailable
    rather than fabricating synthetic +/-0.5% levels.
    """
    now = 1791957300.0
    feed = tmp_path / "feed_incomplete_ohlc.json"
    # Spot and close are provided, but high and low are missing
    feed.write_text(json.dumps({
        "quotes": {
            "NSE_INDEX|Nifty 50": {
                "ltp": 25150.0,
                "cp": 25000.0,
                "open": 25000.0,
                "updated_at_epoch": now - 10.0,
            }
        }
    }), encoding="utf-8")

    loader = PreMarketIntelligenceLoader(
        feed_cache_path=str(feed),
        sqlite_base_dir=str(tmp_path / "empty_sqlite"),
        directional_scalp=True,
    )
    intel = loader.load_intelligence(ref_time_epoch=now)
    comp = intel.components["prior_day_structure"]
    assert comp.is_available is False
    assert "incomplete_prior_ohlc" in comp.metadata.get("reason", "")
    assert intel.prior_day_ohlc is None
