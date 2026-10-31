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
    OrderAction,
    PreMarketIntelligence,
    SelectedLeg,
    StrategyState,
    StraddleLegQuote,
    TickObservation,
    TradeLeg,
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

    mock_pre_market.cds = 0.40
    mock_pre_market.composite_morning_score = 0.40

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
            code = run_opening_straddle(
                db_path=temp_db,
                enable_alerts=False,
                allow_synthetic_spread=False,
                wait_for_market_open=False,
                entry_grace_timeout_sec=0.1,
            )
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
            code = run_opening_straddle(
                db_path=temp_db,
                enable_alerts=False,
                allow_synthetic_spread=False,
                wait_for_market_open=False,
                entry_grace_timeout_sec=0.1,
            )
            assert code == 1


def test_opening_straddle_entry_retry_success(mock_pre_market, temp_db):
    """Verify entry retry loop acquires fresh quotes after an initial stale pre-market quote."""
    from unittest.mock import patch, MagicMock
    import time
    from strategies.opening_straddle_selector import run_opening_straddle

    now_t = time.time()
    stale_spot = {"ltp": 25000.0, "source": "angelone", "feed_time": now_t - 180.0}
    fresh_spot = {"ltp": 25000.0, "source": "angelone", "feed_time": now_t - 1.0}

    stale_chain = {
        "call": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "101", "trading_symbol": "NIFTY25000CE", "feed_time": now_t - 180.0},
        "put": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "102", "trading_symbol": "NIFTY25000PE", "feed_time": now_t - 180.0},
    }
    fresh_chain = {
        "call": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "101", "trading_symbol": "NIFTY25000CE", "feed_time": now_t - 1.0},
        "put": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "102", "trading_symbol": "NIFTY25000PE", "feed_time": now_t - 1.0},
    }

    mock_pre_market.cds = 0.40
    mock_pre_market.composite_morning_score = 0.40

    with patch("strategies.opening_straddle_selector.PreMarketIntelligenceLoader") as mock_loader_cls:
        mock_loader = mock_loader_cls.return_value
        mock_loader.load_intelligence.return_value = mock_pre_market

        # First call stale, second call fresh
        mock_get_spot = MagicMock(side_effect=[stale_spot, fresh_spot, fresh_spot, fresh_spot, fresh_spot])
        mock_get_chain = MagicMock(side_effect=[stale_chain, fresh_chain, fresh_chain, fresh_chain, fresh_chain])

        with patch("strategies.opening_straddle_selector.get_nifty_spot", mock_get_spot), \
             patch("strategies.opening_straddle_selector.get_nifty_option_chain", mock_get_chain), \
             patch("strategies.opening_straddle_selector.OpeningStraddleEngine") as mock_engine_cls:
            mock_engine = mock_engine_cls.return_value
            mock_engine.arm_single_leg_entry.return_value = True
            mock_engine.state = StrategyState.CLOSED

            code = run_opening_straddle(
                db_path=temp_db,
                enable_alerts=False,
                wait_for_market_open=False,
                entry_grace_timeout_sec=5.0,
            )
            assert code == 0
            # Ensure arm_single_leg_entry was called with the fresh quotes
            assert mock_engine.arm_single_leg_entry.called
            call_arg = mock_engine.arm_single_leg_entry.call_args[1]
            assert call_arg["quote"].symbol == "NIFTY25000CE"
            assert call_arg["spot_price"] == 25000.0


def test_wait_for_market_open_timing():
    """Verify compute_market_open_wait_sec accurately computes countdown to 09:15:05 AM IST."""
    from datetime import datetime
    from strategies.opening_straddle_selector import compute_market_open_wait_sec, IST_TZ

    # 1. At 09:14:00 AM IST (65s before 09:15:05)
    t_914 = datetime(2026, 10, 27, 9, 14, 0, tzinfo=IST_TZ)
    assert round(compute_market_open_wait_sec(t_914), 1) == 65.0

    # 2. At 09:14:35 AM IST (30s before 09:15:05)
    t_91435 = datetime(2026, 10, 27, 9, 14, 35, tzinfo=IST_TZ)
    assert round(compute_market_open_wait_sec(t_91435), 1) == 30.0

    # 3. At 09:15:05 AM IST (exact target -> 0s)
    t_91505 = datetime(2026, 10, 27, 9, 15, 5, tzinfo=IST_TZ)
    assert compute_market_open_wait_sec(t_91505) == 0.0

    # 4. Past market open (e.g. 09:15:10 AM IST) -> 0.0s (no wait)
    t_91510 = datetime(2026, 10, 27, 9, 15, 10, tzinfo=IST_TZ)
    assert compute_market_open_wait_sec(t_91510) == 0.0

    # 5. Far before pre-market window (e.g. 08:30:00 AM IST) -> 0.0s
    t_0830 = datetime(2026, 10, 27, 8, 30, 0, tzinfo=IST_TZ)
    assert compute_market_open_wait_sec(t_0830) == 0.0


def test_runner_not_aborted_by_closed_leg_loss_runs_till_955(mock_pre_market, temp_db):
    """
    Counterfactual Simulation of 2026-10-29:
      - 09:15:05 AM: Enter Long Straddle (CE 22650 @ 193.40, PE 22650 @ 173.25).
      - 09:20:00 AM: Nifty trends up (+0.10%). PE falls to 139.25 (-19.6%), CE rises to 206.85 (+6.9%).
      - In the old design, combined net ROI was -5.00% <= -3.0%, which prematurely killed the Call runner.
      - In Option A, PE is sold @ 139.25 (loss capped at -1,699.75 INR) and CALL runner is retained!
      - CALL runner continues autonomously until 09:55:00 AM hard time stop.
    """
    start_ts = 1791957300.0  # 09:15:05 AM reference
    engine = OpeningStraddleEngine(
        pre_market=mock_pre_market,
        trade_date="2026-10-29",
        db_path=temp_db,
    )

    call_q = StraddleLegQuote("NIFTY22650CE", "201", 22650, "CE", bid=193.0, ask=193.40, ltp=193.40)
    put_q = StraddleLegQuote("NIFTY22650PE", "202", 22650, "PE", bid=173.0, ask=173.25, ltp=173.25)
    armed = engine.arm_entry(spot_price=22648.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)
    assert armed is True
    assert engine.total_debit_inr == 65 * (193.40 + 173.25)

    # Tick at 09:20:00 (301 seconds elapsed) - Decoupling checkpoint
    tick_920 = TickObservation(
        ts=start_ts + 301.0,
        nifty_spot=22648.0 * 1.0010,  # Spot up +0.10%
        call_bid=206.85,              # CE up +6.95%
        call_ask=207.25,
        put_bid=139.25,               # PE down -19.62%
        put_ask=139.75,
    )
    state = engine.process_tick(tick_920)

    # Verify PUT is sold and CALL runner is retained
    assert state == StrategyState.CALL_RUNNER
    assert engine.selected_runner == SelectedLeg.CALL
    assert engine.closed_leg == SelectedLeg.PUT
    assert engine.put_leg.exit_price == 139.25
    assert round(engine.put_leg.pnl_inr, 2) == round(65 * (139.25 - 173.25), 2)  # -2210.0 INR
    assert engine.runner_peak_roi_pct >= 6.9

    # CRUCIAL CHECK: Runner MUST NOT be stopped out even though basket net ROI is negative!
    assert engine.state == StrategyState.CALL_RUNNER
    assert engine.exit_reason == ExitReason.NONE

    # Tick at 09:35:00 (1200 seconds elapsed): Market continues rally, Call expands to 225.0
    tick_935 = TickObservation(
        ts=start_ts + 1200.0,
        nifty_spot=22690.0,
        call_bid=225.0, call_ask=225.5,
        put_bid=125.0, put_ask=125.5,
    )
    engine.process_tick(tick_935)
    assert engine.state == StrategyState.CALL_RUNNER
    assert engine.runner_peak_roi_pct >= 16.0

    # Tick at 09:55:01 (2401 seconds elapsed): Hard time exit closes runner at full session runway
    tick_955 = TickObservation(
        ts=start_ts + 2401.0,
        nifty_spot=22710.0,
        call_bid=235.0, call_ask=235.5,
        put_bid=115.0, put_ask=115.5,
    )
    engine.process_tick(tick_955)
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.HARD_TIME_STOP
    assert engine.call_leg.exit_price == 235.0
    assert engine.call_leg.pnl_inr == 65 * (235.0 - 193.40)  # +2704.0 INR
    # Final Net P&L = +2704.0 - 2210.0 - 150 = +344.0 INR (Turned a losing trade into profit!)
    assert engine.net_pnl_inr > 0.0


def test_runner_individual_stop_loss(mock_pre_market, temp_db):
    """
    Verify runner exits on STOP_LOSS when its own return drops below runner_stop_loss_pct (-10.0%),
    preventing further loss on a complete intraday reversal.
    """
    start_ts = 1791957300.0
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)

    # 09:20:00 Decouple Call runner
    engine.process_tick(TickObservation(
        ts=start_ts + 301.0,
        nifty_spot=25025.0,
        call_bid=103.5, call_ask=104.0,
        put_bid=98.0, put_ask=98.5,
    ))
    assert engine.state == StrategyState.CALL_RUNNER

    # At 09:30:00, severe adverse reversal: Call drops to 89.0 (-11.4% from entry 100.5)
    tick_reversal = TickObservation(
        ts=start_ts + 900.0,
        nifty_spot=24920.0,
        call_bid=89.0, call_ask=89.5,
        put_bid=115.0, put_ask=115.5,
    )
    engine.process_tick(tick_reversal)
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.STOP_LOSS
    assert engine.call_leg.exit_price == 89.0


def test_runner_individual_profit_target(mock_pre_market, temp_db):
    """
    Verify runner exits on PROFIT_TARGET when its own return hits runner_profit_target_pct (+35.0%).
    """
    start_ts = 1791957300.0
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.5, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.5, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)

    # Decouple Call runner at 09:20:00
    engine.process_tick(TickObservation(
        ts=start_ts + 301.0,
        nifty_spot=25025.0,
        call_bid=104.0, call_ask=104.5,
        put_bid=98.0, put_ask=98.5,
    ))
    assert engine.state == StrategyState.CALL_RUNNER

    # Surge tick at 09:35:00: Call surges to 136.0 (+35.3% on entry 100.5)
    tick_surge = TickObservation(
        ts=start_ts + 1200.0,
        nifty_spot=25150.0,
        call_bid=136.0, call_ask=136.5,
        put_bid=85.0, put_ask=85.5,
    )
    engine.process_tick(tick_surge)
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.PROFIT_TARGET
    assert engine.call_leg.exit_price == 136.0


def test_runner_individual_trailing_stop(mock_pre_market, temp_db):
    """
    Verify runner trailing stop: runner gains >= +15.0% (trigger), then drops >= 8.0 pp below its peak.
    """
    start_ts = 1791957300.0
    # Set high basket thresholds so only runner individual trail is tested
    engine = OpeningStraddleEngine(
        pre_market=mock_pre_market,
        profit_target_roi=50.0,
        trailing_trigger_roi=50.0,
        db_path=temp_db,
    )

    call_q = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=100.0, ask=100.0, ltp=100.0)
    put_q = StraddleLegQuote("NIFTY25000PE", "102", 25000, "PE", bid=100.0, ask=100.0, ltp=100.0)
    engine.arm_entry(spot_price=25000.0, call_quote=call_q, put_quote=put_q, entry_ts=start_ts)

    # Decouple Call runner at 09:20:00
    engine.process_tick(TickObservation(
        ts=start_ts + 301.0,
        nifty_spot=25025.0,
        call_bid=104.0, call_ask=104.5,
        put_bid=98.0, put_ask=98.5,
    ))
    assert engine.state == StrategyState.CALL_RUNNER

    # Peak tick: Call reaches 120.0 (+20.0% gain >= 15.0% trigger)
    engine.process_tick(TickObservation(
        ts=start_ts + 600.0,
        nifty_spot=25070.0,
        call_bid=120.0, call_ask=120.5,
        put_bid=92.0, put_ask=92.5,
    ))
    assert engine.state == StrategyState.CALL_RUNNER
    assert engine.runner_peak_roi_pct == 20.0

    # Drop tick: Call drops to 111.0 (+11.0% gain). Drop from peak = 20.0 - 11.0 = 9.0 pp >= 8.0 pp trail!
    engine.process_tick(TickObservation(
        ts=start_ts + 750.0,
        nifty_spot=25040.0,
        call_bid=111.0, call_ask=111.5,
        put_bid=95.0, put_ask=95.5,
    ))
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.TRAILING_STOP
    assert engine.call_leg.exit_price == 111.0


def test_single_leg_buy_call_quick_scalp_profit_target(mock_pre_market, temp_db):
    """
    Step 2: Verify Single Leg BUY CALL execution, accounting, and quick scalp target exit.
      - Entry at Ask = 100.0, Capital Deployed = 65 * 100.0 = 6500.0 INR (Margin = 0.0).
      - Price rises to Bid = 110.0 (+10.0% premium gain, gross = +650 INR, net = +575 INR > 8.0 pts).
      - Exit at Bid = 110.0 with MIN_PROFIT_TARGET.
    """
    start_ts = 1793418365.0  # 2026-10-31 09:16:05 IST
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    quote = StraddleLegQuote(
        symbol="NIFTY25000CE", token="101", strike=25000, option_type="CE",
        bid=99.5, ask=100.0, ltp=100.0,
    )

    armed = engine.arm_single_leg_entry(
        leg=TradeLeg.CALL,
        action=OrderAction.BUY,
        strike=25000,
        quote=quote,
        spot_price=25010.0,
        entry_ts=start_ts,
    )
    assert armed is True
    assert engine.state == StrategyState.SINGLE_LEG_ACTIVE
    assert engine.order_action == OrderAction.BUY
    assert engine.trade_leg == TradeLeg.CALL
    assert engine.entry_price == 100.0
    assert engine.margin_inr == 0.0
    assert engine.capital_deployed_inr == 6500.0

    # Advance tick: Call Bid rises to 110.0 (Ask = 110.5) at 09:18:05 IST
    tick = TickObservation(
        ts=start_ts + 120.0,
        nifty_spot=25060.0,
        call_bid=110.0, call_ask=110.5,
        put_bid=80.0, put_ask=80.5,
    )
    engine.process_tick(tick)
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.MIN_PROFIT_TARGET
    assert engine.exit_price == 110.0
    assert engine.net_pnl_inr == pytest.approx(575.0, abs=1e-2)  # 650 gross - 75 friction
    assert engine.premium_pnl_pct == pytest.approx(10.0, abs=1e-2)
    assert engine.net_roi_pct == pytest.approx((575.0 / 6500.0) * 100.0, abs=1e-2)

    # Verify SQLite record
    with sqlite3.connect(temp_db) as conn:
        cursor = conn.execute(
            "SELECT order_action, trade_leg, executed_strike, entry_price, exit_price, net_pnl_inr, exit_reason FROM opening_straddle_sessions"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "BUY"
        assert row[1] == "CALL"
        assert row[2] == 25000
        assert row[3] == 100.0
        assert row[4] == 110.0
        assert row[5] == pytest.approx(575.0, abs=1e-2)
        assert row[6] == "MIN_PROFIT_TARGET"


def test_single_leg_sell_call_accounting_and_margin(mock_pre_market, temp_db):
    """
    Step 2: Verify Single Leg SELL CALL execution:
      - Sells at Bid = 95.0. Capital Deployed = 120,000 INR Margin.
      - Option decays to Ask = 80.0 (+15.0 pts decay = +975 gross - 75 friction = +900 net).
      - Net ROI % is evaluated against Margin base (120,000).
    """
    start_ts = 1793418365.0  # 2026-10-31 09:16:05 IST
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    quote = StraddleLegQuote(
        symbol="NIFTY25050CE", token="103", strike=25050, option_type="CE",
        bid=95.0, ask=95.5, ltp=95.0,
    )

    armed = engine.arm_single_leg_entry(
        leg=TradeLeg.CALL,
        action=OrderAction.SELL,
        strike=25050,
        quote=quote,
        spot_price=25000.0,
        entry_ts=start_ts,
    )
    assert armed is True
    assert engine.state == StrategyState.SINGLE_LEG_ACTIVE
    assert engine.order_action == OrderAction.SELL
    assert engine.entry_price == 95.0
    assert engine.margin_inr == 120000.0
    assert engine.capital_deployed_inr == 120000.0

    # Advance tick: Call decays to Ask = 80.0 (Bid = 79.5) at 09:19:05 IST
    tick = TickObservation(
        ts=start_ts + 180.0,
        nifty_spot=24970.0,
        call_bid=79.5, call_ask=80.0,
        put_bid=110.0, put_ask=110.5,
    )
    engine.process_tick(tick)
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.MIN_PROFIT_TARGET
    assert engine.exit_price == 80.0
    assert engine.net_pnl_inr == pytest.approx(900.0, abs=1e-2)  # (95 - 80)*65 - 75 = 900
    assert engine.premium_pnl_pct == pytest.approx((15.0 / 95.0) * 100.0, abs=1e-2)
    assert engine.net_roi_pct == pytest.approx((900.0 / 120000.0) * 100.0, abs=1e-2)


def test_single_leg_dynamic_trailing_ratchet(mock_pre_market, temp_db):
    """
    Step 2: Verify Single Leg Dynamic Trailing Ratchet:
      - Arms when profit reaches >= +4.0 pts or +6.0%.
      - Trails 2.0 pts behind peak.
      - Locks in partial profit if momentum reverses.
    """
    start_ts = 1793418365.0  # 2026-10-31 09:16:05 IST
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    quote = StraddleLegQuote(
        symbol="NIFTY25000PE", token="104", strike=25000, option_type="PE",
        bid=79.5, ask=80.0, ltp=80.0,
    )
    engine.arm_single_leg_entry(
        leg=TradeLeg.PUT, action=OrderAction.BUY, strike=25000, quote=quote, spot_price=24990.0, entry_ts=start_ts
    )

    # Surge tick: Put Bid surges to 85.5 (+5.5 pts gain >= 4.0 pts arm threshold)
    engine.process_tick(TickObservation(
        ts=start_ts + 60.0,
        nifty_spot=24960.0,
        call_bid=70.0, call_ask=70.5,
        put_bid=85.5, put_ask=86.0,
    ))
    assert engine.state == StrategyState.SINGLE_LEG_ACTIVE
    assert engine.trailing_ratchet_armed is True

    # Peak tick: Put Bid reaches 86.5 (Peak gross points = 6.5, net points ~ 5.35)
    engine.process_tick(TickObservation(
        ts=start_ts + 90.0,
        nifty_spot=24950.0,
        call_bid=68.0, call_ask=68.5,
        put_bid=86.5, put_ask=87.0,
    ))
    assert engine.state == StrategyState.SINGLE_LEG_ACTIVE

    # Drop tick: Put Bid drops to 84.0 (Drop of 2.5 pts >= 2.0 pts trailing drop threshold)
    engine.process_tick(TickObservation(
        ts=start_ts + 120.0,
        nifty_spot=24970.0,
        call_bid=72.0, call_ask=72.5,
        put_bid=84.0, put_ask=84.5,
    ))
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.TRAILING_STOP
    assert engine.exit_price == 84.0
    assert engine.net_pnl_inr > 0.0  # Successfully closed green!


def test_single_leg_stop_loss_limit(mock_pre_market, temp_db):
    """
    Step 2: Verify Single Leg Stop Loss (-8.0 pts or -15.0% on premium).
    """
    start_ts = 1793418365.0  # 2026-10-31 09:16:05 IST
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    quote = StraddleLegQuote(
        symbol="NIFTY25000CE", token="105", strike=25000, option_type="CE",
        bid=99.5, ask=100.0, ltp=100.0,
    )
    engine.arm_single_leg_entry(
        leg=TradeLeg.CALL, action=OrderAction.BUY, strike=25000, quote=quote, spot_price=25000.0, entry_ts=start_ts
    )

    # Adverse move: Call Bid drops to 90.0 (-10.0 pts loss <= -8.0 pts SL threshold) at 09:17:05 IST
    engine.process_tick(TickObservation(
        ts=start_ts + 60.0,
        nifty_spot=24930.0,
        call_bid=90.0, call_ask=90.5,
        put_bid=120.0, put_ask=120.5,
    ))
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.STOP_LOSS
    assert engine.exit_price == 90.0


def test_single_leg_hard_time_stop(mock_pre_market, temp_db):
    """
    Step 2: Verify Hard Time Stop aligns to 09:55:00 AM IST (2,335s from 09:16:05 entry).
    """
    start_ts = 1793418365.0  # 2026-10-31 09:16:05 IST
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)

    quote = StraddleLegQuote(
        symbol="NIFTY25000CE", token="106", strike=25000, option_type="CE",
        bid=99.5, ask=100.0, ltp=100.0,
    )
    engine.arm_single_leg_entry(
        leg=TradeLeg.CALL, action=OrderAction.BUY, strike=25000, quote=quote, spot_price=25000.0, entry_ts=start_ts
    )

    # Tick at exactly 09:55:00 AM IST (1793420700.0, exactly 2335s from 09:16:05)
    engine.process_tick(TickObservation(
        ts=1793420700.0,  # 2026-10-31 09:55:00 IST
        nifty_spot=25005.0,
        call_bid=101.0, call_ask=101.5,
        put_bid=98.0, put_ask=98.5,
    ))
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.HARD_TIME_STOP
    assert engine.exit_price == 101.0

    # Also verify late-started session (e.g. entered at 09:35:00 IST) still unconditionally exits at 09:55:00 AM IST
    engine_late = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)
    engine_late.arm_single_leg_entry(
        leg=TradeLeg.CALL, action=OrderAction.BUY, strike=25000, quote=quote, spot_price=25000.0, entry_ts=1793419500.0  # 09:35:00 IST
    )
    engine_late.process_tick(TickObservation(
        ts=1793420700.0,  # 09:55:00 IST (only 1200s elapsed, but clock cutoff is 09:55:00 IST!)
        nifty_spot=25005.0,
        call_bid=100.5, call_ask=101.0,
        put_bid=98.0, put_ask=98.5,
    ))
    assert engine_late.state == StrategyState.CLOSED
    assert engine_late.exit_reason == ExitReason.HARD_TIME_STOP


def test_short_margin_configurable(mock_pre_market, temp_db, monkeypatch):
    """
    Step 2: Verify short margin configuration via SHORT_MARGIN_PER_LOT env var and constructor.
    """
    monkeypatch.setenv("SHORT_MARGIN_PER_LOT", "135000.0")
    engine_env = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)
    assert engine_env.short_margin_per_lot == 135000.0

    engine_param = OpeningStraddleEngine(pre_market=mock_pre_market, short_margin_per_lot=140000.0, db_path=temp_db)
    assert engine_param.short_margin_per_lot == 140000.0

    quote = StraddleLegQuote("NIFTY25000CE", "101", 25000, "CE", bid=90.0, ask=90.5, ltp=90.0)
    engine_param.arm_single_leg_entry(
        leg=TradeLeg.CALL, action=OrderAction.SELL, strike=25000, quote=quote, spot_price=25000.0
    )
    assert engine_param.margin_inr == 140000.0
    assert engine_param.capital_deployed_inr == 140000.0


def test_record_skipped_session_high_vix(mock_pre_market, temp_db):
    """
    Step 2: Verify record_skipped_session records SKIPPED_HIGH_VIX and zero P&L.
    """
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)
    engine.record_skipped_session("HIGH_VIX: 22.5 > 20.0", ExitReason.HIGH_VIX_SKIP)

    assert engine.state == StrategyState.SKIPPED_HIGH_VIX
    assert engine.exit_reason == ExitReason.HIGH_VIX_SKIP
    assert engine.status == "SKIPPED"
    assert engine.net_pnl_inr == 0.0

    with sqlite3.connect(temp_db) as conn:
        cursor = conn.execute(
            "SELECT status, exit_reason, net_pnl_inr FROM opening_straddle_sessions"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "SKIPPED"
        assert row[1] == "HIGH_VIX_SKIP"
        assert row[2] == 0.0


def test_single_leg_spread_quality_gate(mock_pre_market, temp_db):
    """
    Step 2: Verify single-leg spread quality gate rejects wide spreads (> 1.5%).
    """
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)
    wide_quote = StraddleLegQuote(
        symbol="NIFTY25000CE", token="107", strike=25000, option_type="CE",
        bid=100.0, ask=103.0, ltp=100.0,  # Spread 3.0% > 1.5%
    )
    armed = engine.arm_single_leg_entry(
        leg=TradeLeg.CALL, action=OrderAction.BUY, strike=25000, quote=wide_quote, spot_price=25000.0
    )
    assert armed is False
    assert engine.state == StrategyState.ABORTED
    assert engine.exit_reason == ExitReason.EXECUTION_ABORT


def test_one_minute_candle_score_calculation():
    """
    Step 3: Verify 1-minute candle momentum normalization:
      - 0.15% return yields +/-1.0
      - Clipped to [-1.0, +1.0]
      - Zero/flat yields 0.0
      - Missing/invalid yields 0.0
    """
    from strategies.opening_straddle_selector import compute_one_minute_candle_score

    # Flat move -> 0.0
    assert compute_one_minute_candle_score(25000.0, 25000.0) == 0.0

    # Exactly +0.15% (37.5 pts on 25000) -> +1.0
    assert compute_one_minute_candle_score(25000.0, 25037.5) == pytest.approx(1.0, abs=1e-4)

    # Exactly -0.15% (-37.5 pts on 25000) -> -1.0
    assert compute_one_minute_candle_score(25000.0, 24962.5) == pytest.approx(-1.0, abs=1e-4)

    # +0.075% move -> +0.50
    assert compute_one_minute_candle_score(25000.0, 25018.75) == pytest.approx(0.50, abs=1e-4)

    # Huge breakout +0.50% -> clipped to +1.0
    assert compute_one_minute_candle_score(25000.0, 25125.0) == 1.0

    # Huge breakdown -0.50% -> clipped to -1.0
    assert compute_one_minute_candle_score(25000.0, 24875.0) == -1.0

    # Missing / None / zero
    assert compute_one_minute_candle_score(None, 25000.0) == 0.0
    assert compute_one_minute_candle_score(25000.0, None) == 0.0
    assert compute_one_minute_candle_score(0.0, 25000.0) == 0.0


def test_final_composite_morning_score_synthesis():
    """
    Step 3: Verify synthesis of Final CMS at 09:16:05:
      - Coverage < 0.50 -> forced to 0.0 (fail-safe)
      - Coverage >= 0.50 -> 0.80 * Premarket + 0.20 * 1m Candle
    """
    from strategies.opening_straddle_selector import compute_final_composite_morning_score

    # Coverage < 0.50 -> forced to 0.0 even with strong signals
    assert compute_final_composite_morning_score(pre_market_cms=0.80, pre_market_coverage=0.45, score_1m_candle=0.90) == 0.0

    # Coverage >= 0.50 -> 0.80 * 0.40 + 0.20 * 0.50 = 0.32 + 0.10 = 0.42
    res = compute_final_composite_morning_score(pre_market_cms=0.40, pre_market_coverage=0.75, score_1m_candle=0.50)
    assert res == pytest.approx(0.42, abs=1e-4)

    # Bearish alignment -> 0.80 * (-0.30) + 0.20 * (-0.40) = -0.24 - 0.08 = -0.32
    res_bear = compute_final_composite_morning_score(pre_market_cms=-0.30, pre_market_coverage=0.80, score_1m_candle=-0.40)
    assert res_bear == pytest.approx(-0.32, abs=1e-4)

    # Clipping to [-1.0, +1.0]
    res_clip = compute_final_composite_morning_score(pre_market_cms=1.0, pre_market_coverage=1.0, score_1m_candle=1.0)
    assert res_clip == 1.0


def test_select_single_leg_decision_matrix():
    """
    Step 3: Verify Leg Selection Decision Matrix (Plan Section 5.1):
      - Strong Bullish (>= +0.30): BUY CALL ATM
      - Moderate Bullish (+0.10 to +0.30): SELL PUT 1-step OTM
      - Moderate Bearish (-0.30 to -0.10): SELL CALL 1-step OTM (User Tip Priority)
      - Strong Bearish (<= -0.30): SELL CALL 1-step OTM (Primary) / BUY PUT ATM (Secondary)
      - Neutral Chop (-0.10 to +0.10): SKIP
    """
    from strategies.opening_straddle_selector import select_single_leg_decision

    # 1. Strong Bullish (+0.35) -> BUY CALL ATM 25000
    leg, action, strike, reason = select_single_leg_decision(0.35, 25000)
    assert leg == TradeLeg.CALL
    assert action == OrderAction.BUY
    assert strike == 25000

    # 2. Moderate Bullish (+0.20) -> SELL PUT 1-step OTM 24950
    leg, action, strike, reason = select_single_leg_decision(0.20, 25000)
    assert leg == TradeLeg.PUT
    assert action == OrderAction.SELL
    assert strike == 24950

    # 3. Moderate Bearish (-0.20) -> SELL CALL 1-step OTM 25050 (User Tip Priority)
    leg, action, strike, reason = select_single_leg_decision(-0.20, 25000)
    assert leg == TradeLeg.CALL
    assert action == OrderAction.SELL
    assert strike == 25050

    # 4. Strong Bearish (-0.40, primary default) -> SELL CALL 1-step OTM 25050
    leg, action, strike, reason = select_single_leg_decision(-0.40, 25000, prefer_sell_call_on_strong_bearish=True)
    assert leg == TradeLeg.CALL
    assert action == OrderAction.SELL
    assert strike == 25050

    # 5. Strong Bearish (-0.40, secondary flag) -> BUY PUT ATM 25000
    leg, action, strike, reason = select_single_leg_decision(-0.40, 25000, prefer_sell_call_on_strong_bearish=False)
    assert leg == TradeLeg.PUT
    assert action == OrderAction.BUY
    assert strike == 25000

    # 6. Neutral Chop (+0.05) -> SKIP
    leg, action, strike, reason = select_single_leg_decision(0.05, 25000)
    assert leg == TradeLeg.NONE
    assert action is None
    assert strike is None
    assert "SKIP" in reason

    # 7. Neutral Chop (-0.05) -> SKIP
    leg, action, strike, reason = select_single_leg_decision(-0.05, 25000)
    assert leg == TradeLeg.NONE
    assert action is None
    assert strike is None


def test_run_opening_straddle_neutral_chop_skip(temp_db):
    """
    Step 3: Verify run_opening_straddle terminates cleanly with SKIPPED_NEUTRAL_CHOP when CMS is flat.
    """
    from unittest.mock import patch
    from strategies.opening_straddle_selector import run_opening_straddle

    neutral_intel = PreMarketIntelligence(
        cds=0.02,
        coverage=0.80,
        confidence=ConfidenceLevel.HIGH,
        bias=BiasClassification.NEUTRAL,
        components={},
        vix=15.0,
        vix_regime=VolatilityRegime.NORMAL,
        timestamp_utc="2026-10-31T03:44:00Z",
        nifty_prev_close=25000.0,
        nifty_spot=25000.0,
        composite_morning_score=0.02,
    )

    with patch("strategies.opening_straddle_selector.PreMarketIntelligenceLoader") as mock_loader_cls, \
         patch("strategies.opening_straddle_selector.get_nifty_spot", return_value={"ltp": 25000.0, "feed_time": time.time()}):
        mock_loader = mock_loader_cls.return_value
        mock_loader.load_intelligence.return_value = neutral_intel

        code = run_opening_straddle(
            db_path=temp_db,
            enable_alerts=False,
            wait_for_market_open=False,
        )
        assert code == 0

    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT status, exit_reason, net_pnl_inr FROM opening_straddle_sessions"
        ).fetchone()
        assert row is not None
        assert row[0] == "SKIPPED"
        assert row[1] == "NEUTRAL_CHOP_SKIP"
        assert row[2] == 0.0


def test_force_hard_time_exit_single_leg(mock_pre_market, temp_db):
    """
    Verify engine.force_hard_time_exit() cleanly liquidates active single leg,
    calculates P&L, sets HARD_TIME_STOP, and persists session to SQLite.
    """
    engine = OpeningStraddleEngine(pre_market=mock_pre_market, db_path=temp_db)
    quote = StraddleLegQuote(
        symbol="NIFTY25000CE", token="101", strike=25000, option_type="CE",
        bid=102.0, ask=102.5, ltp=102.0,
    )
    engine.arm_single_leg_entry(
        leg=TradeLeg.CALL, action=OrderAction.BUY, strike=25000, quote=quote, spot_price=25000.0,
    )
    assert engine.state == StrategyState.SINGLE_LEG_ACTIVE

    engine.force_hard_time_exit()
    assert engine.state == StrategyState.CLOSED
    assert engine.exit_reason == ExitReason.HARD_TIME_STOP
    assert engine.exit_price == 102.0
    assert engine.status == "PAPER_TRADING"
    assert engine.net_pnl_inr is not None

    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT status, exit_reason, exit_price, net_pnl_inr FROM opening_straddle_sessions"
        ).fetchone()
        assert row is not None
        assert row[0] == "PAPER_TRADING"
        assert row[1] == "HARD_TIME_STOP"
        assert row[2] == 102.0


def test_run_opening_straddle_max_duration_forced_exit(mock_pre_market, temp_db):
    """
    Verify run_opening_straddle forces hard time liquidation when max_duration_sec is exceeded,
    ensuring no session is left hanging in SINGLE_LEG_ACTIVE.
    """
    from unittest.mock import patch
    import time
    from strategies.opening_straddle_selector import run_opening_straddle

    mock_pre_market.cds = 0.40
    mock_pre_market.composite_morning_score = 0.40

    now_t = time.time()
    spot_quote = {"ltp": 25000.0, "source": "angelone", "feed_time": now_t - 1.0}
    chain_quote = {
        "call": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "101", "trading_symbol": "NIFTY25000CE", "feed_time": now_t - 1.0},
        "put": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "102", "trading_symbol": "NIFTY25000PE", "feed_time": now_t - 1.0},
    }

    with patch("strategies.opening_straddle_selector.PreMarketIntelligenceLoader") as mock_loader_cls, \
         patch("strategies.opening_straddle_selector.get_nifty_spot", return_value=spot_quote), \
         patch("strategies.opening_straddle_selector.get_nifty_option_chain", return_value=chain_quote):
        mock_loader = mock_loader_cls.return_value
        mock_loader.load_intelligence.return_value = mock_pre_market

        # Setting max_duration_sec = 0 forces immediate timeout exit in observation loop
        code = run_opening_straddle(
            db_path=temp_db,
            enable_alerts=False,
            wait_for_market_open=False,
            max_duration_sec=0,
        )
        assert code == 0

    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT status, exit_reason, exit_price, net_pnl_inr FROM opening_straddle_sessions"
        ).fetchone()
        assert row is not None
        assert row[0] == "PAPER_TRADING"
        assert row[1] == "HARD_TIME_STOP"
        assert row[2] is not None
        assert row[3] is not None


def test_insufficient_coverage_skip_persisted(temp_db):
    """
    Verify that when premarket coverage < 0.50, the selector records
    ExitReason.INSUFFICIENT_COVERAGE_SKIP and StrategyState.SKIPPED_INSUFFICIENT_COVERAGE
    in SQLite, cleanly distinguishing missing data from neutral chop.
    """
    from unittest.mock import patch
    from strategies.opening_straddle_models import PreMarketIntelligence, ConfidenceLevel, VolatilityRegime, BiasClassification
    from strategies.opening_straddle_selector import run_opening_straddle

    low_cov_intel = PreMarketIntelligence(
        cds=0.25,
        coverage=0.30,  # Below 0.50 threshold
        confidence=ConfidenceLevel.LOW,
        bias=BiasClassification.NEUTRAL,
        components={},
        vix=14.5,
        vix_regime=VolatilityRegime.NORMAL,
        timestamp_utc="2026-10-31T03:44:00Z",
        nifty_prev_close=25000.0,
        nifty_spot=25000.0,
        composite_morning_score=0.0,
    )

    with patch("strategies.opening_straddle_selector.PreMarketIntelligenceLoader") as mock_loader_cls, \
         patch("strategies.opening_straddle_selector.get_nifty_spot", return_value={"ltp": 25000.0, "feed_time": time.time()}):
        mock_loader = mock_loader_cls.return_value
        mock_loader.load_intelligence.return_value = low_cov_intel

        code = run_opening_straddle(
            db_path=temp_db,
            enable_alerts=False,
            wait_for_market_open=False,
        )
        assert code == 0

    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT status, exit_reason, selected_leg FROM opening_straddle_sessions"
        ).fetchone()
        assert row is not None
        assert row[0] == "SKIPPED"
        assert row[1] == "INSUFFICIENT_COVERAGE_SKIP"


def test_final_cms_persisted_in_db(mock_pre_market, temp_db):
    """
    Verify that the final blended CMS (incorporating 0.80 premarket + 0.20 candle)
    is persisted into SQLite composite_directional_score column instead of raw premarket cds.
    """
    from unittest.mock import patch
    from strategies.opening_straddle_selector import run_opening_straddle

    mock_pre_market.cds = 0.50
    mock_pre_market.composite_morning_score = 0.50
    mock_pre_market.coverage = 0.90

    now_t = time.time()
    spot_quote = {"ltp": 25000.0, "source": "angelone", "feed_time": now_t - 1.0}
    chain_quote = {
        "call": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "101", "trading_symbol": "NIFTY25000CE", "feed_time": now_t - 1.0},
        "put": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "102", "trading_symbol": "NIFTY25000PE", "feed_time": now_t - 1.0},
    }

    with patch("strategies.opening_straddle_selector.PreMarketIntelligenceLoader") as mock_loader_cls, \
         patch("strategies.opening_straddle_selector.get_nifty_spot", return_value=spot_quote), \
         patch("strategies.opening_straddle_selector.get_nifty_option_chain", return_value=chain_quote):
        mock_loader = mock_loader_cls.return_value
        mock_loader.load_intelligence.return_value = mock_pre_market

        code = run_opening_straddle(
            db_path=temp_db,
            enable_alerts=False,
            wait_for_market_open=False,
            max_duration_sec=0,
        )
        assert code == 0

    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT composite_directional_score FROM opening_straddle_sessions"
        ).fetchone()
        assert row is not None
        # With premarket CMS = 0.50 and flat candle score = 0.0:
        # final_cms = 0.80 * 0.50 + 0.20 * 0.0 = 0.400
        assert round(row[0], 3) == 0.400


def test_sample_opening_candle_early_launch():
    """
    Verify sample_opening_candle sleeps until 09:15:00 if launched early (e.g. at 08:50 AM).
    """
    from unittest.mock import patch
    from strategies.opening_straddle_selector import sample_opening_candle, IST_TZ
    from datetime import datetime

    early_dt = datetime(2026, 10, 27, 8, 50, 0, tzinfo=IST_TZ)
    # Target 09:15:00 is 1500 seconds away
    mock_now_times = [
        early_dt,  # Initial check
        datetime(2026, 10, 27, 9, 15, 0, tzinfo=IST_TZ),  # After sleeping
        datetime(2026, 10, 27, 9, 16, 1, tzinfo=IST_TZ),  # Loop end
        datetime(2026, 10, 27, 9, 16, 6, tzinfo=IST_TZ),  # Buffer check
    ]

    sleep_calls = []
    with patch("strategies.opening_straddle_selector.datetime") as mock_dt, \
         patch("strategies.opening_straddle_selector.time.sleep", side_effect=lambda s: sleep_calls.append(s)), \
         patch("strategies.opening_straddle_selector.get_nifty_spot", return_value={"ltp": 25000.0}):
        mock_dt.now.side_effect = mock_now_times
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

        candle = sample_opening_candle(wait_for_market_open=True)
        # Should have slept for 1500s until 09:15:00
        assert len(sleep_calls) >= 1
        assert round(sleep_calls[0]) == 1500


def test_sample_opening_candle_late_launch_returns_none_and_aborts(mock_pre_market, temp_db):
    """
    Verify sample_opening_candle returns None when launched past 09:16:00,
    and run_opening_straddle aborts session cleanly with ExitReason.EXECUTION_ABORT.
    """
    from unittest.mock import patch
    from strategies.opening_straddle_selector import run_opening_straddle, sample_opening_candle, IST_TZ
    from datetime import datetime

    late_dt = datetime(2026, 10, 27, 9, 20, 0, tzinfo=IST_TZ)

    with patch("strategies.opening_straddle_selector.datetime") as mock_dt:
        mock_dt.now.return_value = late_dt
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
        candle = sample_opening_candle(wait_for_market_open=True)
        assert candle == {"open": None, "high": None, "low": None, "close": None}

    # Verify run_opening_straddle aborts
    with patch("strategies.opening_straddle_selector.PreMarketIntelligenceLoader") as mock_loader_cls, \
         patch("strategies.opening_straddle_selector.sample_opening_candle", return_value={"open": None, "high": None, "low": None, "close": None}):
        mock_loader = mock_loader_cls.return_value
        mock_loader.load_intelligence.return_value = mock_pre_market

        code = run_opening_straddle(
            db_path=temp_db,
            enable_alerts=False,
            wait_for_market_open=True,
        )
        assert code == 1

    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT status, exit_reason FROM opening_straddle_sessions"
        ).fetchone()
        assert row is not None
        assert row[0] == "SKIPPED"
        assert row[1] == "EXECUTION_ABORT"


def test_premarket_bias_preserved_and_alert_shift_annotation(mock_pre_market, temp_db, monkeypatch):
    """
    Integration test verifying:
    1. Premarket bias (STRONG_BULLISH) is preserved and persisted to SQLite bias_classification.
    2. Final execution bias (MODERATE_BULLISH) is derived from final CMS and persisted to final_bias.
    3. The Discord entry alert receives both and displays:
       `MODERATE_BULLISH` *(Premarket: STRONG_BULLISH)*.
    """
    from unittest.mock import MagicMock, patch
    import time
    from strategies.opening_straddle_models import BiasClassification
    from strategies.opening_straddle_selector import run_opening_straddle

    monkeypatch.setenv("DISCORD_OPENING_STRADDLE", "https://discord.com/api/webhooks/test")

    # Premarket CDS = 0.40 -> STRONG_BULLISH
    mock_pre_market.cds = 0.40
    mock_pre_market.composite_morning_score = 0.40
    mock_pre_market.bias = BiasClassification.STRONG_BULLISH

    now_t = time.time()
    spot_quote = {"ltp": 25000.0, "source": "angelone", "feed_time": now_t - 1.0}
    # Target strike for MODERATE_BULLISH (SELL PUT 1-step OTM) is 25000 - 50 = 24950
    chain_quote = {
        "call": {"ltp": 100.0, "bid": 99.8, "ask": 100.2, "token": "101", "trading_symbol": "NIFTY25000CE", "feed_time": now_t - 1.0},
        "put": {"ltp": 80.0, "bid": 79.8, "ask": 80.2, "token": "102", "trading_symbol": "NIFTY24950PE", "feed_time": now_t - 1.0},
    }

    # Opening candle score:
    # r_ratio = (25000.0 - 25032.0) / (25032.0 * 0.0015) = -32.0 / 37.548 = -0.8522
    # final_cms = 0.80 * 0.40 + 0.20 * (-0.8522) = 0.320 - 0.1704 = 0.150 (MODERATE_BULLISH)
    candle_mock = {"open": 25032.0, "high": 25032.0, "low": 24995.0, "close": 25000.0}

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("strategies.opening_straddle_selector.PreMarketIntelligenceLoader") as mock_loader_cls, \
         patch("strategies.opening_straddle_selector.sample_opening_candle", return_value=candle_mock), \
         patch("strategies.opening_straddle_selector.get_nifty_spot", return_value=spot_quote), \
         patch("strategies.opening_straddle_selector.get_nifty_option_chain", return_value=chain_quote), \
         patch("requests.post", return_value=mock_resp) as mock_post:
        mock_loader = mock_loader_cls.return_value
        mock_loader.load_intelligence.return_value = mock_pre_market

        code = run_opening_straddle(
            db_path=temp_db,
            enable_alerts=True,
            wait_for_market_open=False,
            max_duration_sec=0,
        )
        assert code == 0

    # 1. Verify intel.bias was NOT mutated
    assert mock_pre_market.bias == BiasClassification.STRONG_BULLISH

    # 2. Verify SQLite columns
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT bias_classification, final_bias, composite_directional_score, order_action, trade_leg FROM opening_straddle_sessions"
        ).fetchone()
        assert row is not None
        assert row[0] == "STRONG_BULLISH"   # bias_classification preserves premarket prior bias
        assert row[1] == "MODERATE_BULLISH" # final_bias stores final execution bias
        assert round(row[2], 3) == 0.150    # final_cms
        assert row[3] == "SELL"
        assert row[4] == "PUT"

    # 3. Verify entry alert embed contained the shift annotation
    assert mock_post.called
    entry_embed = mock_post.call_args_list[0][1]["json"]["embeds"][0]
    cms_field = next(f for f in entry_embed["fields"] if "Composite Score" in f["name"])
    assert "+0.150" in cms_field["value"]
    assert "MODERATE_BULLISH" in cms_field["value"]
    assert "Premarket: STRONG_BULLISH" in cms_field["value"]


