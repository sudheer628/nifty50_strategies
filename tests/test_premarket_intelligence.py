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
