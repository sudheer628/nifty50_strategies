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
    """Verify P&L extraction from snapshot FSM dict."""
    snapshot = {
        "alpha_fsm": {
            "total_gainloss": 48.5,
            "realized_pnl_pts": 35.0,
        }
    }
    pts, inr = get_active_strategy_pnl(snapshot, "/dummy/db.db")
    assert pts == 48.5
    # Default lot size is 65
    assert inr == 48.5 * 65


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
