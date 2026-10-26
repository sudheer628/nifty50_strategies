"""
Tests for Opening Straddle 15-Session Backtest Engine (Phase 3).
"""
import pytest
from strategies.opening_straddle_backtest import (
    build_15_market_scenarios,
    evaluate_model_configuration,
    run_full_backtest_suite,
)


def test_15_market_scenarios_completeness():
    """Verify all 15 scenarios are generated with valid data."""
    scenarios = build_15_market_scenarios()
    assert len(scenarios) == 15

    # Check distinct scenario categories
    types = {s.scenario_type for s in scenarios}
    assert "TREND_BULL" in types
    assert "TREND_BEAR" in types
    assert "CONTRARIAN_TRUE" in types
    assert "CONTRARIAN_TRAP" in types
    assert "CHOP" in types
    assert "TRAIL_PEAK" in types
    assert "STOP_LOSS" in types
    assert "WIDE_SPREAD" in types


def test_backtest_suite_execution():
    """Verify full backtest suite runs cleanly across all 3 model configurations."""
    results = run_full_backtest_suite()

    assert "v2_flagship" in results
    assert "v1_baseline" in results
    assert "price_only" in results

    v2 = results["v2_flagship"]
    assert v2.total_sessions == 15
    assert v2.trades_executed == 14
    assert v2.trades_aborted == 1
    assert v2.win_rate_pct > 50.0
    assert v2.profit_factor > 1.0

    # Verify zero unhandled/NONE exits
    assert "NONE" not in v2.exit_breakdown
