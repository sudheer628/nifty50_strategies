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
    assert engine.status == "PAPER_TRADING"


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
    assert engine.status == "PAPER_TRADING"


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


def test_elevated_vix_tightens_trailing_trigger(temp_db):
    """Elevated VIX (>18.0) must tighten the trailing stop trigger from 2.0% to 1.5%."""
    high_vix_intel = PreMarketIntelligence(
        cds=0.30,
        coverage=0.85,
        confidence=ConfidenceLevel.HIGH,
        bias=BiasClassification.STRONG_BULLISH,
        components={},
        vix=21.5,
        vix_regime=VolatilityRegime.ELEVATED,
        timestamp_utc="2026-10-26T03:44:00Z",
    )
    engine = OpeningStraddleEngine(pre_market=high_vix_intel, db_path=temp_db)
    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q)

    assert engine.trailing_trigger_roi == 1.5


def test_low_iv_crush_vix_early_scratch_at_0920(temp_db):
    """Low IV Crush VIX (<11.5) must trigger immediate Scratch Exit at 09:20:00 if move is inconclusive."""
    low_vix_intel = PreMarketIntelligence(
        cds=0.05,
        coverage=0.90,
        confidence=ConfidenceLevel.HIGH,
        bias=BiasClassification.NEUTRAL,
        components={},
        vix=10.8,
        vix_regime=VolatilityRegime.LOW_IV_CRUSH,
        timestamp_utc="2026-10-26T03:44:00Z",
    )
    start_ts = 1791957300.0
    engine = OpeningStraddleEngine(pre_market=low_vix_intel, db_path=temp_db)
    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)

    # Inconclusive 09:20:00 tick (spot flat, legs slightly decaying)
    tick_920 = TickObservation(
        ts=start_ts + 301.0,
        nifty_spot=25002.0,  # Spot flat (+0.008%)
        call_bid=99.0, call_ask=99.5,
        put_bid=99.0, put_ask=99.5,
    )
    engine.process_tick(tick_920)

    # Must immediately scratch at 09:20:00 without extending to 09:25:00
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.SCRATCH
    assert engine.decision_time_str == "09:20:00"


def test_batched_tick_persistence_in_sqlite(mock_pre_market, temp_db):
    """Verify 1 Hz observations are buffered and persisted to opening_straddle_ticks table."""
    start_ts = 1791957300.0
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)
    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)

    # Process 5 ticks
    for i in range(1, 6):
        engine.process_tick(TickObservation(
            ts=start_ts + i,
            nifty_spot=25000.0 + i,
            call_bid=100.0 + i * 0.5,
            call_ask=100.5 + i * 0.5,
            put_bid=100.0 - i * 0.3,
            put_ask=100.5 - i * 0.3,
        ))

    assert len(engine.tick_buffer) == 5

    # Decouple at 09:20:00
    engine.process_tick(TickObservation(
        ts=start_ts + 301.0,
        nifty_spot=25025.0,
        call_bid=104.0, call_ask=104.5,
        put_bid=98.0, put_ask=98.5,
    ))

    # Conclude session with hard time stop at 09:55:00
    engine.process_tick(TickObservation(
        ts=start_ts + 2401.0,
        nifty_spot=25010.0,
        call_bid=105.0, call_ask=105.5,
        put_bid=95.0, put_ask=95.5,
    ))

    assert engine.state == StrategyState.CLOSED
    # Buffer should be cleared after persistence
    assert len(engine.tick_buffer) == 0

    # Verify rows in opening_straddle_ticks
    with sqlite3.connect(temp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM opening_straddle_ticks").fetchone()[0]
        assert count == 7  # 5 ticks + 1 decouple tick + 1 exit tick
        # Verify combined_net_pnl_pct is recorded dynamically and non-zero
        rois = [r[0] for r in conn.execute("SELECT combined_net_pnl_pct FROM opening_straddle_ticks").fetchall()]
        assert any(r != 0.0 for r in rois)


def test_missing_depth_quote_returns_high_spread_and_aborts(mock_pre_market, temp_db):
    """Missing or non-positive bid/ask quotes must return spread_pct=999.0 and abort entry."""
    # Zero bid
    bad_quote = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=0.0, ask=100.0, ltp=100.0)
    assert bad_quote.spread_pct == 999.0

    # Inverted bid/ask
    inverted_quote = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=105.0, ask=100.0, ltp=100.0)
    assert inverted_quote.spread_pct == 999.0

    # NaN bid/ask quotes
    nan_quote = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=float("nan"), ask=100.0, ltp=100.0)
    assert nan_quote.spread_pct == 999.0

    # Ensure engine arm_entry aborts on missing depth and records ABORTED_NO_DEPTH
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)
    valid_put = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    armed = engine.arm_entry(spot_price=25000.0, call_quote=bad_quote, put_quote=valid_put)
    assert armed is False
    assert engine.state == StrategyState.ABORTED
    assert engine.status == "ABORTED_NO_DEPTH"

    with sqlite3.connect(temp_db) as conn:
        status_db = conn.execute("SELECT status FROM opening_straddle_sessions").fetchone()[0]
        assert status_db == "ABORTED_NO_DEPTH"


def test_interrupted_session_flushes_ticks_without_exit_alert(mock_pre_market, temp_db):
    """Interrupted session must flush buffered ticks to DB but NOT send a misleading exit summary alert."""
    start_ts = 1791957300.0
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db, enable_alerts=True)
    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)

    # Process 3 live ticks
    for i in range(1, 4):
        engine.process_tick(TickObservation(
            ts=start_ts + i,
            nifty_spot=25000.0 + i,
            call_bid=100.0 + i * 0.2,
            call_ask=100.5 + i * 0.2,
            put_bid=100.0 - i * 0.2,
            put_ask=100.5 - i * 0.2,
        ))

    assert len(engine.tick_buffer) == 3

    from unittest.mock import patch
    with patch("strategies.opening_straddle_engine.send_straddle_exit_summary") as mock_summary:
        engine.flush_interrupted_session()
        # Must NOT dispatch exit summary alert
        assert not mock_summary.called

    assert engine.status == "INTERRUPTED"
    assert len(engine.tick_buffer) == 0

    with sqlite3.connect(temp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM opening_straddle_ticks").fetchone()[0]
        assert count == 3
        status = conn.execute("SELECT status FROM opening_straddle_sessions").fetchone()[0]
        assert status == "INTERRUPTED"


def test_parse_quote_age_sec_and_tick_persistence(mock_pre_market, temp_db):
    """Verify quote age parsing from timestamps and persistence in opening_straddle_ticks table."""
    from strategies.opening_straddle_selector import parse_quote_age_sec, IST_TZ
    from datetime import datetime

    now_ts = 1791957300.0

    # Numeric seconds & milliseconds
    assert parse_quote_age_sec(now_ts - 2.5, now_ts=now_ts) == 2.5
    assert round(parse_quote_age_sec((now_ts - 1.2) * 1000.0, now_ts=now_ts), 2) == 1.2
    assert parse_quote_age_sec(None, now_ts=now_ts) is None

    # Datetime string parsing
    dt_str = datetime.fromtimestamp(now_ts - 5.0, tz=IST_TZ).strftime("%d-%b-%Y %H:%M:%S")
    assert round(parse_quote_age_sec(dt_str, now_ts=now_ts), 1) == 5.0

    # Clock skew handling: future timestamps
    # Future timestamp within max_skew_sec (2.0s) clamps to 0.0
    assert parse_quote_age_sec(now_ts + 0.8, now_ts=now_ts, max_skew_sec=2.0) == 0.0
    # Future timestamp beyond max_skew_sec returns None (rejected)
    assert parse_quote_age_sec(now_ts + 3.5, now_ts=now_ts, max_skew_sec=2.0) is None

    # Unparseable, garbage or empty string inputs return None
    assert parse_quote_age_sec("", now_ts=now_ts) is None
    assert parse_quote_age_sec("invalid_format", now_ts=now_ts) is None
    assert parse_quote_age_sec("2026-99-99 99:99:99", now_ts=now_ts) is None

    # Engine tick recording with quote_age_sec
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)
    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=now_ts)

    engine.process_tick(TickObservation(
        ts=now_ts + 1.0,
        nifty_spot=25000.0,
        call_bid=100.0,
        call_ask=100.5,
        put_bid=100.0,
        put_ask=100.5,
        quote_age_sec=3.4,
    ))
    assert engine.tick_buffer[0]["quote_age_sec"] == 3.4


def test_opening_straddle_selector_freshness_gating(mock_pre_market, temp_db):
    """Verify run_opening_straddle aborts entry when market quotes lack valid freshness timestamps."""
    from unittest.mock import patch
    import time
    from strategies.opening_straddle_selector import run_opening_straddle

    with patch("strategies.opening_straddle_selector.PreMarketIntelligenceLoader") as mock_loader_cls:
        mock_loader = mock_loader_cls.return_value
        mock_loader.load_intelligence.return_value = mock_pre_market

        # 1. Quotes with missing feed_time -> must abort in live mode
        mock_spot_no_time = {"ltp": 25000.0, "source": "angelone"}
        mock_chain_no_time = {
            "call": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "101", "trading_symbol": "NIFTY25000CE"},
            "put": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "102", "trading_symbol": "NIFTY25000PE"},
        }

        with patch("strategies.opening_straddle_selector.get_nifty_spot", return_value=mock_spot_no_time), \
             patch("strategies.opening_straddle_selector.get_nifty_option_chain", return_value=mock_chain_no_time):
            code = run_opening_straddle(db_path=temp_db, enable_alerts=False, allow_synthetic_spread=False)
            assert code == 1

        # 2. Quotes with stale feed_time (> 15s) -> must abort in live mode
        old_time = time.time() - 30.0
        mock_spot_stale = {"ltp": 25000.0, "source": "angelone", "feed_time": old_time}
        mock_chain_stale = {
            "call": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "101", "trading_symbol": "NIFTY25000CE", "feed_time": old_time},
            "put": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "102", "trading_symbol": "NIFTY25000PE", "feed_time": old_time},
        }

        with patch("strategies.opening_straddle_selector.get_nifty_spot", return_value=mock_spot_stale), \
             patch("strategies.opening_straddle_selector.get_nifty_option_chain", return_value=mock_chain_stale):
            code = run_opening_straddle(db_path=temp_db, enable_alerts=False, allow_synthetic_spread=False)
            assert code == 1
