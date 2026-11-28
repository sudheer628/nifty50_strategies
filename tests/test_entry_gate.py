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


def test_entry_gate_overnight_gap_and_straddle_telemetry():
    """Verify that overnight gap and ATM straddle ratio are computed and attached as shadow telemetry."""
    normal_dt = datetime(2026, 9, 15, 10, 15, 0, tzinfo=IST_TZ)
    res = evaluate_entry_gate(
        reference_ltp=24500.0,
        sqlite_dir="/tmp/dummy_sqlite",
        current_time_ist=normal_dt,
        force=False,
        nifty_open=24700.0,
        nifty_prev_close=24500.0,
        atm_straddle_price=350.0,
    )
    assert res.should_enter is True
    assert res.overnight_gap_pct == pytest.approx(0.816, rel=1e-3)
    assert res.atm_straddle_ratio == pytest.approx(1.429, rel=1e-3)

    d = res.to_dict()
    assert "overnight_gap_pct" in d
    assert "atm_straddle_ratio" in d
    assert d["overnight_gap_pct"] == pytest.approx(0.816, rel=1e-3)
    assert d["atm_straddle_ratio"] == pytest.approx(1.429, rel=1e-3)


def test_evaluate_orb_and_vwap_falls_back_to_unified_signals_db(tmp_path):
    """Verify entry gate reads VIEW signals from signals_data_YYYY_MM.db when NSF DB is decommissioned."""
    import sqlite3

    signals_db = str(tmp_path / "signals_data_2026_12.db")
    conn = sqlite3.connect(signals_db)
    conn.execute("""
        CREATE TABLE market_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pk TEXT NOT NULL,
            ts INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            price REAL,
            data TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE VIEW signals AS
        SELECT
            id,
            ts,
            symbol,
            price AS ltp,
            CAST(json_extract(data, '$.vwap') AS REAL) AS vwap,
            CAST(json_extract(data, '$.vwap_distance') AS REAL) AS vwap_distance,
            CAST(json_extract(data, '$.adx_14') AS REAL) AS adx_14,
            CAST(json_extract(data, '$.bb_width') AS REAL) AS bb_width
        FROM market_data
        WHERE pk LIKE 'INDEX#%'
    """)
    as_of = datetime(2026, 12, 1, 9, 45, 0, tzinfo=IST_TZ)
    base_ts = int(as_of.timestamp())

    # Narrow range (< 15 pts) and tiny VWAP distance before 10:00 AM -> should DEFER
    import json
    for i, price in enumerate([25000.0, 25004.0, 25002.0]):
        ts = base_ts - (2 - i) * 60
        conn.execute(
            "INSERT INTO market_data (pk, ts, symbol, price, data) VALUES (?, ?, ?, ?, ?)",
            (
                "INDEX#NIFTY50",
                ts,
                "NIFTY50",
                price,
                json.dumps({"vwap": 25000.0, "vwap_distance": 0.01, "adx_14": 18.0, "bb_width": 0.30}),
            ),
        )
    conn.commit()
    conn.close()

    passed, vwap_dist, adx, opening_range, reason = evaluate_orb_and_vwap(str(tmp_path / "strategies"), as_of)
    assert passed is False
    assert opening_range == 4.0
    assert vwap_dist == 0.01
    assert adx == 18.0
    assert "dead opening chop" in reason.lower()

