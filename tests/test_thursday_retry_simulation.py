"""
Simulation of the Thursday 3-day BUY retry after a SKIPPED_INSUFFICIENT_FUNDS attempt.

Scenario: 09:32 IST attempt was skipped for insufficient funds (snapshot status
SKIPPED_INSUFFICIENT_FUNDS). Once the flag is cleared, the next collector tick
(10:02 IST) re-runs the fresh entry: gate -> ITM1 strikes at the *new* spot -> BUY.

No broker call is possible here: execute_paired_market_buy and the option-key
resolver are mocked, and all snapshot / DB writes go to a temp directory.
"""

import copy
import json
import os
import tempfile
from contextlib import ExitStack
from datetime import date
from unittest.mock import patch

from common.entry_gate import EntryGateResult

SKIPPED_SNAPSHOT = {
    "strategy_name": "nifty50_thursday_3day_collector",
    "cycle_id": "CYCLE-THU-20261112",
    "week_start_date": "20261112",
    "expiry_date": "20261117",
    "entry_spot": 25325.15,
    "call_strike": 25150,
    "put_strike": 25550,
    "call_buy_price": None,
    "put_buy_price": None,
    "call_ref_ltp": 322.0,
    "put_ref_ltp": 326.9,
    "call_order_id": None,
    "put_order_id": None,
    "entry_status": "SKIPPED_INSUFFICIENT_FUNDS",
    "broker": "upstox",
    "alpha_fsm": None,
    "status": "ongoing",
}


def _cleared_snapshot() -> dict:
    """Today's snapshot with the insufficient-funds flag removed (what the operator asked for)."""
    snap = copy.deepcopy(SKIPPED_SNAPSHOT)
    snap.pop("entry_status", None)
    return snap


def _cleared_snapshot_file(tmp_dir: str) -> str:
    path = os.path.join(tmp_dir, "current_thursday_buy.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_cleared_snapshot(), fh)
    return path


def _run_retry(spot_ltp: float, spot_open: float, gate: EntryGateResult, option_ltp=(322.0, 326.9)):
    """Run one collector tick at the given spot with all I/O mocked. Returns (ok, mocks)."""
    from strategies.thursday_option_collector import collect_once

    with tempfile.TemporaryDirectory() as tmp_dir, ExitStack() as st:
        snap_file = _cleared_snapshot_file(tmp_dir)
        st.enter_context(patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", os.path.join(tmp_dir, "active_strategies.json")))
        st.enter_context(patch("common.unified_strategy_store.sync_active_cycles_to_mongodb"))
        st.enter_context(patch("common.unified_strategy_store.append_telemetry_log_to_mongodb"))
        st.enter_context(patch("common.unified_strategy_store.reset_cycle_live_logs_in_mongodb"))
        st.enter_context(patch("strategies.thursday_option_collector._ensure_thursday_db", return_value=os.path.join(tmp_dir, "thursday.db")))
        st.enter_context(patch("strategies.thursday_option_collector.dispatch_order_failure_with_retry"))
        st.enter_context(patch("strategies.thursday_option_collector.THURSDAY_SNAPSHOT_FILE", snap_file))
        st.enter_context(patch("strategies.thursday_option_collector.insert_buy_snapshot"))
        st.enter_context(patch("strategies.thursday_option_collector.insert_gate_deferred_shadow"))
        st.enter_context(patch("strategies.thursday_option_collector.insert_record"))
        st.enter_context(patch("strategies.thursday_option_collector._today_ist", return_value=date(2026, 11, 12)))
        st.enter_context(patch("strategies.thursday_option_collector.check_nse_holiday", return_value=False))
        st.enter_context(patch("strategies.thursday_option_collector.is_thursday_strategy_active_day", return_value=True))
        st.enter_context(patch("strategies.thursday_option_collector.is_thursday_strategy_start_day", return_value=True))
        st.enter_context(patch("strategies.thursday_option_collector.is_strategy_closing_day", return_value=False))
        st.enter_context(patch("strategies.thursday_option_collector.evaluate_entry_gate", return_value=gate))
        mock_spot = st.enter_context(patch("strategies.thursday_option_collector.get_nifty_spot",
                                           return_value={"ltp": spot_ltp, "open": spot_open, "close": 25300.0}))
        mock_chain = st.enter_context(patch("strategies.thursday_option_collector.get_nifty_option_chain", return_value={
            "call": {"ltp": option_ltp[0]}, "put": {"ltp": option_ltp[1]},
        }))
        mock_save = st.enter_context(patch("strategies.thursday_option_collector.save_active_snapshot"))
        st.enter_context(patch("config.get_config_bool", return_value=True))
        st.enter_context(patch("config.get_broker_for_track", return_value="upstox"))
        st.enter_context(patch("config.get_entry_qty_for_track", return_value=65))
        st.enter_context(patch("common.entry_order_manager.resolve_upstox_option_key",
                               side_effect=lambda exp, k, opt: f"NSE_FO|{k}{opt}"))
        mock_buy = st.enter_context(patch("common.entry_order_manager.execute_paired_market_buy", return_value={
            "success": True, "status": "FILLED",
            "call_buy_price": option_ltp[0], "put_buy_price": option_ltp[1],
            "call_order_id": "SIM-CE", "put_order_id": "SIM-PE",
        }))
        st.enter_context(patch("common.entry_order_manager.place_market_order"))  # must never be reached
        st.enter_context(patch("common.entry_order_manager.get_available_funds", return_value=10_000_000.0))

        ok = collect_once(force_static=False, force_entry=False, dry_run=False, execute_buy=True)
        return ok, {"spot": mock_spot, "chain": mock_chain, "save": mock_save, "buy": mock_buy}


def test_retry_gap_up_10_02_picks_itm1_at_new_spot_and_buys():
    """Gap-up: spot 25720 at 10:02 -> ITM1 = CE 25700 / PE 25750 (not the old 25150/25550)."""
    gate = EntryGateResult(should_enter=True, status="CLEARED", defer_reason="All filters passed",
                           iv_slope=0.1, current_iv=13.5, vwap_distance=0.05, adx_14=22.0,
                           opening_range=25.0, evaluated_at_ist="10:02:00")
    ok, m = _run_retry(spot_ltp=25720.0, spot_open=25650.0, gate=gate)

    assert ok is True
    m["buy"].assert_called_once()
    kwargs = m["buy"].call_args.kwargs
    assert kwargs["call_contract"]["strike"] == 25700
    assert kwargs["put_contract"]["strike"] == 25750
    assert kwargs["quantity"] == 65

    saved = m["save"].call_args[0][0]
    assert saved["call_strike"] == 25700
    assert saved["put_strike"] == 25750
    assert saved["entry_status"] == "FILLED"
    assert saved["selection_mode"] == "ITM1_FIXED"


def test_retry_gap_down_10_02_gate_deferred_places_no_order():
    """Gap-down with gate still deferring: no BUY, no active snapshot written, shadow only."""
    gate = EntryGateResult(should_enter=False, status="DEFERRED", defer_reason="Opening chop range < 15 pts",
                           iv_slope=0.1, current_iv=13.5, vwap_distance=0.01, adx_14=12.0,
                           opening_range=8.0, evaluated_at_ist="10:02:00")
    ok, m = _run_retry(spot_ltp=25180.0, spot_open=25200.0, gate=gate)

    assert ok is True
    m["buy"].assert_not_called()
    m["save"].assert_not_called()
