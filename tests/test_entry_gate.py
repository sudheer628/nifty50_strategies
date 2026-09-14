"""
Unit tests for common/entry_gate.py (Smart Entry Gate - Enhancement 3).
"""

import os
import pytest
from datetime import datetime, time
import pytz

from common.entry_gate import (
    evaluate_entry_gate,
    EntryGateResult,
    evaluate_iv_stability,
    evaluate_orb_and_vwap,
    IST_TZ,
)


def test_entry_gate_timeout_cutoff():
    """Verify that >= 11:00 AM IST forces entry regardless of gate conditions."""
    late_dt = datetime(2026, 9, 15, 11, 5, 0, tzinfo=IST_TZ)
    res = evaluate_entry_gate(
        reference_ltp=24500.0,
        sqlite_dir="/tmp/dummy_sqlite",
        current_time_ist=late_dt,
        force=False,
    )
    assert res.should_enter is True
    assert res.status == "FORCED_TIMEOUT"
    assert "cutoff" in res.defer_reason.lower()


def test_entry_gate_force_bypassed():
    """Verify that force=True immediately clears the entry gate."""
    early_dt = datetime(2026, 9, 15, 9, 31, 0, tzinfo=IST_TZ)
    res = evaluate_entry_gate(
        reference_ltp=24500.0,
        sqlite_dir="/tmp/dummy_sqlite",
        current_time_ist=early_dt,
        force=True,
    )
    assert res.should_enter is True
    assert res.status == "BYPASSED"


def test_entry_gate_result_serialization():
    """Verify EntryGateResult serialization to dict."""
    res = EntryGateResult(
        should_enter=True,
        status="CLEARED",
        defer_reason="",
        iv_slope=-0.2,
        current_iv=14.5,
        vwap_distance=0.12,
        adx_14=18.5,
        opening_range=28.0,
        evaluated_at_ist="2026-09-15 10:01:00 IST",
    )
    d = res.to_dict()
    assert d["should_enter"] is True
    assert d["status"] == "CLEARED"
    assert d["iv_slope"] == -0.2
    assert d["adx_14"] == 18.5


def test_entry_gate_missing_db_graceful_pass():
    """Verify that missing DBs do not block strategy entry when within normal hours."""
    normal_dt = datetime(2026, 9, 15, 10, 15, 0, tzinfo=IST_TZ)
    res = evaluate_entry_gate(
        reference_ltp=24500.0,
        sqlite_dir="/tmp/non_existent_dir_xyz",
        current_time_ist=normal_dt,
        force=False,
    )
    # When DB is missing, filters gracefully pass to ensure trading is never blocked by telemetry issues
    assert res.should_enter is True
    assert res.status == "CLEARED"
