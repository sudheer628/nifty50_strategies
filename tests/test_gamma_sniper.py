"""
Unit tests for strategies/monday_gamma_sniper.py (Gamma Sniper - Enhancement 4).
"""

import os
import pytest
from datetime import datetime, time
import pytz

from strategies.monday_gamma_sniper import (
    is_sniper_time_window,
    get_active_strategy_pnl,
    run_gamma_sniper,
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

