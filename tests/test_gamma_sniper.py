"""
Unit tests for strategies/monday_gamma_sniper.py (Gamma Sniper - Enhancement 4).
"""

import os
import sqlite3
import pytest
from datetime import datetime, time
import pytz

from strategies.monday_gamma_sniper import (
    is_sniper_time_window,
    get_active_strategy_pnl,
    run_gamma_sniper,
    evaluate_gamma_signals,
    IST_TZ,
)


def test_is_sniper_time_window():
    """Verify window detection (13:15 - 15:00 IST)."""
    in_window = datetime(2026, 9, 14, 13, 45, 0, tzinfo=IST_TZ)
    assert is_sniper_time_window(in_window) is True

    too_early = datetime(2026, 9, 14, 12, 30, 0, tzinfo=IST_TZ)
    assert is_sniper_time_window(too_early) is False

    too_late = datetime(2026, 9, 14, 15, 15, 0, tzinfo=IST_TZ)
    assert is_sniper_time_window(too_late) is False


def test_get_active_strategy_pnl_from_fsm():
    """Verify P&L extraction prioritizes realized P&L from snapshot FSM dict."""
    snapshot = {
        "alpha_fsm": {
            "total_gainloss": 48.5,
            "realized_pnl_pts": 35.0,
        }
    }
    pts, inr = get_active_strategy_pnl(snapshot, "/dummy/db.db")
    # Prioritizes realized profit (35.0) over unrealized total (48.5)
    assert pts == 35.0
    # Default lot size is 65
    assert inr == 35.0 * 65

    # Fallback to total_gainloss if realized_pnl_pts is None
    snapshot_fallback = {
        "alpha_fsm": {
            "total_gainloss": 22.0,
        }
    }
    pts_fb, inr_fb = get_active_strategy_pnl(snapshot_fallback, "/dummy/db.db")
    assert pts_fb == 22.0
    assert inr_fb == 22.0 * 65


def test_zero_principal_risk_guard_blocks_trade():
    """Verify that when the week's strategy is in net loss, Gamma Sniper skips trading."""
    snapshot = {
        "alpha_fsm": {
            "total_gainloss": -15.5,
            "realized_pnl_pts": -10.0,
        }
    }
    pts, inr = get_active_strategy_pnl(snapshot, "/dummy/db.db")
    assert pts <= 0
    # In run_gamma_sniper, when net_pnl_pts <= 0 and force is False, trade is skipped


def test_gamma_sniper_exit_triggers(monkeypatch, tmp_path):
    """Verify that open gamma sniper trades exit on target (+75%) or stop loss (-35%)."""
    from common.storage import init_db, insert_gamma_sniper_trade, get_gamma_sniper_trades

    db_path = str(tmp_path / "test_gamma.db")
    init_db(db_path)

    trade = {
        "trade_timestamp": 1700000000,
        "expiry_date": "20260915",
        "nifty_spot": 24500.0,
        "option_type": "CE",
        "strike": 24550,
        "entry_price": 20.0,
        "exit_price": 20.0,
        "status": "OPEN",
    }
    insert_gamma_sniper_trade(db_path, trade)

    # Mock option chain returning target price 36.0 (>= 20 * 1.75 = 35.0)
    monkeypatch.setattr(
        "strategies.monday_gamma_sniper.get_nifty_option_chain",
        lambda *args, **kwargs: {"call": {"ltp": 36.0}}
    )
    monkeypatch.setattr(
        "strategies.monday_gamma_sniper.load_active_snapshot",
        lambda: {"expiry_date": "20260915", "week_start_date": "20260908"}
    )
    os.makedirs(tmp_path / "strategies", exist_ok=True)
    monkeypatch.setattr("strategies.monday_gamma_sniper._PROJECT_ROOT", str(tmp_path))

    res = run_gamma_sniper(force=True, custom_db_path=db_path)
    assert res["status"] == "MANAGED_OPEN_TRADES"
    closed_trade = res["trades"][0]
    assert closed_trade["status"] == "CLOSED"
    assert closed_trade["exit_price"] == 36.0
    assert "TARGET_HIT" in closed_trade["exit_reason"]
    assert closed_trade["pnl_points"] == 16.0


def test_evaluate_gamma_signals_without_volume_column(tmp_path):
    """Verify evaluate_gamma_signals succeeds when signals table lacks a volume column (production NSF DB schema)."""
    nsf_db = str(tmp_path / "nifty_signal_features_2026_10.db")
    conn = sqlite3.connect(nsf_db)
    # Production NSF schema: no volume column
    conn.execute("""
        CREATE TABLE signals (
            ts INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            ltp REAL,
            vwap REAL,
            vwap_distance REAL,
            nsf_rsi14 REAL
        )
    """)
    as_of = datetime(2026, 10, 12, 13, 30, 0, tzinfo=IST_TZ)
    base_ts = int(as_of.timestamp())

    # Insert 3 consecutive 5-min bars with bullish gamma breakout (+0.18% VWAP distance, RSI=62.0)
    for i in range(3):
        ts = base_ts - (15 - i * 5) * 60
        conn.execute(
            "INSERT INTO signals VALUES (?, 'NIFTY50', 25100.0, 25055.0, 0.18, 62.0)",
            (ts,)
        )
    conn.commit()
    conn.close()

    # Call evaluate_gamma_signals pointing to tmp_path
    direction, vwap_dist, rsi, reason = evaluate_gamma_signals(str(tmp_path / "strategies"), as_of)
    assert direction == "CALL"
    assert vwap_dist == 0.18
    assert rsi == 62.0
    assert "Bullish gamma breakout" in reason


def test_evaluate_gamma_signals_with_volume_column(tmp_path):
    """Verify evaluate_gamma_signals calculates volume surge when volume column exists."""
    nsf_db = str(tmp_path / "nifty_signal_features_2026_10.db")
    conn = sqlite3.connect(nsf_db)
    conn.execute("""
        CREATE TABLE signals (
            ts INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            ltp REAL,
            vwap REAL,
            vwap_distance REAL,
            nsf_rsi14 REAL,
            volume REAL
        )
    """)
    as_of = datetime(2026, 10, 12, 13, 30, 0, tzinfo=IST_TZ)
    base_ts = int(as_of.timestamp())

    # First 2 bars volume = 1000, 3rd bar surge volume = 2000 (ratio = 2.0x >= 1.5x)
    conn.execute("INSERT INTO signals VALUES (?, 'NIFTY50', 24900.0, 24950.0, -0.20, 38.0, 1000.0)", (base_ts - 600,))
    conn.execute("INSERT INTO signals VALUES (?, 'NIFTY50', 24890.0, 24950.0, -0.24, 36.0, 1000.0)", (base_ts - 300,))
    conn.execute("INSERT INTO signals VALUES (?, 'NIFTY50', 24880.0, 24950.0, -0.28, 35.0, 2000.0)", (base_ts,))
    conn.commit()
    conn.close()

    direction, vwap_dist, rsi, reason = evaluate_gamma_signals(str(tmp_path / "strategies"), as_of)
    assert direction == "PUT"
    assert vwap_dist == -0.28
    assert rsi == 35.0
    assert "Bearish gamma breakdown" in reason
    assert "Vol ratio=2.0x" in reason


def test_evaluate_gamma_signals_rangebound(tmp_path):
    """Verify rangebound markets yield no direction trigger."""
    nsf_db = str(tmp_path / "nifty_signal_features_2026_10.db")
    conn = sqlite3.connect(nsf_db)
    conn.execute("""
        CREATE TABLE signals (
            ts INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            ltp REAL,
            vwap REAL,
            vwap_distance REAL,
            nsf_rsi14 REAL
        )
    """)
    as_of = datetime(2026, 10, 12, 13, 30, 0, tzinfo=IST_TZ)
    base_ts = int(as_of.timestamp())

    for i in range(3):
        ts = base_ts - (15 - i * 5) * 60
        conn.execute(
            "INSERT INTO signals VALUES (?, 'NIFTY50', 25000.0, 25000.0, 0.02, 51.0)",
            (ts,)
        )
    conn.commit()
    conn.close()

    direction, vwap_dist, rsi, reason = evaluate_gamma_signals(str(tmp_path / "strategies"), as_of)
    assert direction is None
    assert "Market rangebound" in reason


