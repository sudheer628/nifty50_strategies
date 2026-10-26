"""
Unit & Simulation Tests for Opening Straddle FSM Engine (Phase 2).
"""
import os
import sqlite3
import time
from datetime import date, datetime, timedelta
import pytest

from strategies.opening_straddle_engine import (
    OpeningStraddleEngine,
    resolve_opening_straddle_expiry,
)
from strategies.opening_straddle_models import (
    BiasClassification,
    ConfidenceLevel,
    ExitReason,
    PreMarketIntelligence,
    SelectedLeg,
    StrategyState,
    StraddleLegQuote,
    TickObservation,
    VolatilityRegime,
)


@pytest.fixture
def mock_pre_market():
    """Generates standard High-Confidence Bullish PreMarketIntelligence."""
    return PreMarketIntelligence(
        cds=0.35,
        coverage=0.90,
        confidence=ConfidenceLevel.HIGH,
        bias=BiasClassification.STRONG_BULLISH,
        components={},
        vix=14.5,
        vix_regime=VolatilityRegime.NORMAL,
        timestamp_utc="2026-10-26T03:44:00Z",
        nifty_prev_close=25000.0,
        nifty_spot=25050.0,
    )


@pytest.fixture
def temp_db(tmp_path):
    """Temporary SQLite database path for isolated session persistence."""
    return str(tmp_path / "test_opening_straddle.db")


def test_dynamic_expiry_selection():
    """
    Verify Dynamic Expiry Selection Rule (Section 3.3):
      - Wednesday/Thursday (> 2 sessions to Tue) -> Current week Tue
      - Friday/Monday/Tuesday (<= 2 sessions to Tue) -> Following week Tue
    """
    # 1. Wednesday (e.g. 2026-10-28) -> Targets coming Tuesday (2026-11-03)
    wed = date(2026, 10, 28)
    exp_wed = resolve_opening_straddle_expiry(wed)
    assert exp_wed == date(2026, 11, 3)

    # 2. Thursday (2026-10-29) -> Targets coming Tuesday (2026-11-03)
    thu = date(2026, 10, 29)
    exp_thu = resolve_opening_straddle_expiry(thu)
    assert exp_thu == date(2026, 11, 3)

    # 3. Friday (2026-10-30) -> 2 sessions remaining -> Rolls to Following Tuesday (2026-11-10)
    # Note: 2026-11-10 is Diwali holiday in NSE calendar, so contract prepones to Monday 2026-11-09!
    fri = date(2026, 10, 30)
    exp_fri = resolve_opening_straddle_expiry(fri)
    assert exp_fri == date(2026, 11, 9)

    # 4. Monday (2026-11-02) -> 1 session remaining -> Rolls to Following week (2026-11-09)
    mon = date(2026, 11, 2)
    exp_mon = resolve_opening_straddle_expiry(mon)
    assert exp_mon == date(2026, 11, 9)

    # 5. Tuesday (2026-11-03, Expiry day) -> 0 sessions remaining -> Rolls to Following week (2026-11-09)
    tue = date(2026, 11, 3)
    exp_tue = resolve_opening_straddle_expiry(tue)
    assert exp_tue == date(2026, 11, 9)


def test_spread_quality_gate_aborts_on_wide_spread(mock_pre_market, temp_db):
    """
    Verify trade is aborted at 09:15:05 if spread > 1.5%.
    """
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    # Normal Call spread (100.0 bid, 100.5 ask -> spread ~ 0.5%)
    good_call = StraddleLegQuote(
        symbol="NIFTY25050CE", token="101", strike=25050, option_type="CE",
        bid=100.0, ask=100.5, ltp=100.0,
    )
    # Wide Put spread (100.0 bid, 103.0 ask -> spread ~ 3.0% > 1.5%)
    wide_put = StraddleLegQuote(
        symbol="NIFTY25050PE", token="102", strike=25050, option_type="PE",
        bid=100.0, ask=103.0, ltp=100.0,
    )

    armed = engine.arm_entry(spot_price=25048.0, call_quote=good_call, put_quote=wide_put)
    assert armed is False
    assert engine.state == StrategyState.ABORTED
    assert engine.exit_reason == ExitReason.EXECUTION_ABORT
    assert engine.status == "ABORTED"

    # Verify record persisted to DB
    with sqlite3.connect(temp_db) as conn:
        cursor = conn.execute("SELECT status, exit_reason FROM opening_straddle_sessions")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "ABORTED"
        assert row[1] == ExitReason.EXECUTION_ABORT.value


def test_aligned_call_decoupling_and_profit_target(mock_pre_market, temp_db):
    """
    Simulation Scenario:
      1. Entry at 09:15:05 AM (C0 = 100, P0 = 100, total debit = 13,000 INR).
      2. At 09:20:00 AM, market rallies: Spot +0.12%, Call +4.0%, Put -2.0%.
      3. Call is decoupled (PUT closed at 98, CALL runner retained).
      4. Call continues rising to 112 (+12%), hitting +3.0% Combined Net Profit Target!
    """
    start_ts = 1791957300.0  # 09:15:05 AM
    engine = OpeningStraddleEngine(
        pre_market=mock_pre_market,
        trade_date="2026-10-26",
        db_path=temp_db,
    )

    call_q = StraddleLegQuote("NIFTY25050CE", "101", 25050, "CE", bid=99.8, ask=100.0, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25050PE", "102", 25050, "PE", bid=99.8, ask=100.0, ltp=100.0)

    armed = engine.arm_entry(spot_price=25045.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)
    assert armed is True
    assert engine.atm_strike == 25050
    assert engine.state == StrategyState.OBSERVATION
    assert engine.total_debit_inr == 65 * 200.0  # 13,000 INR

    # Tick at 09:20:00 (300 seconds elapsed)
    tick_920 = TickObservation(
        ts=start_ts + 301.0,
        nifty_spot=25045.0 * 1.0012,  # Spot up +0.12%
        call_bid=104.0,              # Call up +4.0%
        call_ask=104.5,
        put_bid=98.0,                # Put down -2.0%
        put_ask=98.5,
    )

    state = engine.process_tick(tick_920)
    assert state == StrategyState.CALL_RUNNER
    assert engine.selected_runner == SelectedLeg.CALL
    assert engine.closed_leg == SelectedLeg.PUT
    assert engine.put_leg.exit_price == 98.0
    assert engine.put_leg.pnl_inr == 65 * (98.0 - 100.0)  # -130 INR

    # Runner tick 1: Call at 108 -> Net P&L = 65*(108-100) - 130 - 150 = 520 - 280 = 240 INR (+1.8% ROI)
    tick_run1 = TickObservation(
        ts=start_ts + 400.0,
        nifty_spot=25080.0,
        call_bid=108.0, call_ask=108.5,
        put_bid=96.0, put_ask=96.5,
    )
    engine.process_tick(tick_run1)
    assert engine.state == StrategyState.CALL_RUNNER
    assert engine.peak_roi_pct > 1.5

    # Runner tick 2: Call at 112 -> Net P&L = 65*(112-100) - 130 - 150 = 780 - 280 = 500 INR (+3.85% ROI >= +3%)
    tick_target = TickObservation(
        ts=start_ts + 500.0,
        nifty_spot=25100.0,
        call_bid=112.0, call_ask=112.5,
        put_bid=94.0, put_ask=94.5,
    )
    engine.process_tick(tick_target)
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.PROFIT_TARGET
    assert engine.net_roi_pct >= 3.0
    assert engine.status == "COMPLETED"


def test_contrarian_put_decoupling_with_two_tick_confirmation(temp_db):
    """
    Simulation Scenario:
      Pre-market CDS is Bullish (+0.35, High Confidence), but market plunges.
      PUT rises +4.5%, outperforming Call by > 3 pp.
      Contrarian rule: Must confirm |Rn| >= 0.15% across TWO consecutive observations.
    """
    bullish_intel = PreMarketIntelligence(
        cds=0.35, coverage=0.90, confidence=ConfidenceLevel.HIGH,
        bias=BiasClassification.STRONG_BULLISH, components={},
        vix=15.0, vix_regime=VolatilityRegime.NORMAL,
        timestamp_utc="2026-10-26T03:44:00Z",
    )
    start_ts = 1791957300.0
    engine = OpeningStraddleEngine(pre_market=bullish_intel, db_path=temp_db)

    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)

    # First observation at 09:20:00 (Spot drop -0.16% >= 0.15%, but spot_confirmed_ticks is only 1)
    tick_contrarian_1 = TickObservation(
        ts=start_ts + 301.0,
        nifty_spot=24960.0,   # -0.16% drop
        call_bid=97.0, call_ask=97.5,    # Call -3%
        put_bid=104.6, put_ask=105.0,    # Put +4.6% (> 4.0% hurdle)
    )
    engine.process_tick(tick_contrarian_1)
    # Since ticks_confirmed is only 1, contrarian protection delays decoupling!
    assert engine.state == StrategyState.EXTENDED_OBSERVATION

    # Second observation at 09:20:05 (Spot drop -0.18%, consecutive confirmed ticks = 2)
    # Engine is in EXTENDED_OBSERVATION; when force_eval_925 or 09:25 is evaluated:
    tick_contrarian_2 = TickObservation(
        ts=start_ts + 601.0,  # 09:25:00
        nifty_spot=24955.0,   # -0.18% drop
        call_bid=96.0, call_ask=96.5,
        put_bid=105.0, put_ask=105.5,
    )
    engine.process_tick(tick_contrarian_2)
    # Now that 2 consecutive ticks confirmed the move, contrarian PUT is selected!
    assert engine.state == StrategyState.PUT_RUNNER
    assert engine.selected_runner == SelectedLeg.PUT
    assert engine.closed_leg == SelectedLeg.CALL


def test_spot_flatness_gate_prevents_decoupling(mock_pre_market, temp_db):
    """
    Verify that if an option gains +4.0% but spot price is flat (|Rn| < 0.05%),
    decoupling is withheld (gain attributed to spread/IV noise).
    """
    start_ts = 1791957300.0
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)

    # Spot is virtually flat (+0.02% < 0.05%), yet Call bid jumped to 104.5
    tick_flat = TickObservation(
        ts=start_ts + 301.0,
        nifty_spot=25005.0,   # +0.02%
        call_bid=104.5, call_ask=105.0,
        put_bid=99.0, put_ask=99.5,
    )
    engine.process_tick(tick_flat)
    # Flatness gate MUST block decoupling -> extends observation
    assert engine.state == StrategyState.EXTENDED_OBSERVATION


def test_scratch_exit_at_0925(mock_pre_market, temp_db):
    """
    Verify that if neither leg decouples by 09:25:00, both legs are flattened (Scratch Exit).
    """
    start_ts = 1791957300.0
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)

    # 09:20:00 tick (inconclusive chop)
    tick_920 = TickObservation(
        ts=start_ts + 301.0,
        nifty_spot=25002.0,
        call_bid=101.0, call_ask=101.5,
        put_bid=99.5, put_ask=100.0,
    )
    engine.process_tick(tick_920)
    assert engine.state == StrategyState.EXTENDED_OBSERVATION

    # 09:25:00 tick (still inconclusive)
    tick_925 = TickObservation(
        ts=start_ts + 601.0,
        nifty_spot=25003.0,
        call_bid=100.5, call_ask=101.0,
        put_bid=99.0, put_ask=99.5,
    )
    engine.process_tick(tick_925)
    assert engine.state == StrategyState.CLOSED
    assert engine.selected_runner == SelectedLeg.SCRATCH
    assert engine.exit_reason == ExitReason.SCRATCH
    assert engine.status == "COMPLETED"


def test_trailing_stop_protection(mock_pre_market, temp_db):
    """
    Verify Trailing Stop: Peak ROI >= +2.0%, exit if current ROI drops >= 1.0 pp below peak.
    """
    start_ts = 1791957300.0
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)

    # 09:20:00 Decoupling
    tick_920 = TickObservation(
        ts=start_ts + 301.0,
        nifty_spot=25025.0,  # Spot +0.10%
        call_bid=104.0, call_ask=104.5,
        put_bid=98.0, put_ask=98.5,
    )
    engine.process_tick(tick_920)
    assert engine.state == StrategyState.CALL_RUNNER

    # Peak tick: Call at 111.0 -> Net P&L = 65*(111.0-100.5) - 162.5 - 150 = 682.5 - 312.5 = 370 INR (+2.83% ROI)
    tick_peak = TickObservation(
        ts=start_ts + 400.0,
        nifty_spot=25040.0,
        call_bid=111.0, call_ask=111.5,
        put_bid=96.0, put_ask=96.5,
    )
    engine.process_tick(tick_peak)
    assert engine.peak_roi_pct >= 2.5  # Peak >= +2.0% armed trailing stop

    # Drop tick: Call falls back to 106.5 -> Net P&L = 65*(106.5-100.5) - 312.5 = 390 - 312.5 = 77.5 (+0.59% ROI)
    # Peak (2.83%) - Current (0.59%) = 2.24 pp drop >= 1.0 pp!
    tick_drop = TickObservation(
        ts=start_ts + 450.0,
        nifty_spot=25020.0,
        call_bid=106.5, call_ask=107.0,
        put_bid=97.0, put_ask=97.5,
    )
    engine.process_tick(tick_drop)
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.TRAILING_STOP


def test_hard_time_stop_at_0955(mock_pre_market, temp_db):
    """
    Verify mandatory liquidation at 09:55:00 AM (2400 seconds elapsed).
    """
    start_ts = 1791957300.0
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)

    # Decouple at 09:20
    engine.process_tick(TickObservation(
        ts=start_ts + 301.0, nifty_spot=25025.0, call_bid=103.5, call_ask=104.0, put_bid=98.0, put_ask=98.5
    ))

    # Tick at 09:55:01 (2401 seconds elapsed)
    tick_955 = TickObservation(
        ts=start_ts + 2401.0,
        nifty_spot=25030.0,
        call_bid=104.0, call_ask=104.5,
        put_bid=97.0, put_ask=97.5,
    )
    engine.process_tick(tick_955)
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.HARD_TIME_STOP
