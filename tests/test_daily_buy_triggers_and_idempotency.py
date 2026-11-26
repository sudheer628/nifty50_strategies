"""
Unit tests for 4-day daily paired BUY trigger flags, auto-disarm upon fill,
post-market re-arm script, same-day duplicate order prevention, and broker-wide exposure checks.
"""

import json
import os
import tempfile
from datetime import date, datetime, time
from unittest.mock import MagicMock, patch
import pytest
import pytz

import config
from common.entry_gate import EntryGateResult
from scripts.reset_daily_buy_triggers import (
    reset_triggers_for_day,
    reset_triggers_all,
    get_triggers_status,
    is_market_hours_ist,
    DAY_KEY_MAP,
    ALL_BUY_KEYS,
)
from scripts.auto_record_manual_exit import check_prior_cycle_open_positions
import strategies.thursday_option_collector as thursday_collector
import strategies.weekly_option_collector as weekly_collector

config._config_redis_state["unavailable"] = True

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# 1. Tests for scripts/reset_daily_buy_triggers.py
# ---------------------------------------------------------------------------

def test_day_key_mapping():
    assert DAY_KEY_MAP["tuesday"] == "LIVE_BUY_TRIGGER_TUESDAY"
    assert DAY_KEY_MAP["thursday"] == "LIVE_BUY_TRIGGER_THURSDAY"
    assert "wednesday" not in DAY_KEY_MAP
    assert "friday" not in DAY_KEY_MAP


def test_market_hours_check():
    # Pre-market (09:00 IST)
    dt_pre = datetime(2026, 11, 19, 9, 0, tzinfo=IST)
    is_open, reason = is_market_hours_ist(dt_pre)
    assert is_open is False
    assert "Pre-market" in reason

    # Active market (10:02 IST)
    dt_active = datetime(2026, 11, 19, 10, 2, tzinfo=IST)
    is_open, reason = is_market_hours_ist(dt_active)
    assert is_open is True
    assert "Market is open" in reason

    # Post-market (15:45 IST)
    dt_post = datetime(2026, 11, 19, 15, 45, tzinfo=IST)
    is_open, reason = is_market_hours_ist(dt_post)
    assert is_open is False
    assert "Post-market" in reason


def test_reset_triggers_refuses_during_market_hours_without_force():
    dt_active = datetime(2026, 11, 19, 10, 2, tzinfo=IST)
    with patch("scripts.reset_daily_buy_triggers._now_ist", return_value=dt_active), \
         patch("config.set_config_value") as mock_set:
        ok = reset_triggers_for_day("thursday", dry_run=False, force=False)
        assert ok is False
        mock_set.assert_not_called()


def test_reset_triggers_succeeds_post_market():
    dt_post = datetime(2026, 11, 19, 15, 45, tzinfo=IST)
    with patch("scripts.reset_daily_buy_triggers._now_ist", return_value=dt_post), \
         patch("scripts.reset_daily_buy_triggers.set_config_value", return_value=True) as mock_set:
        ok = reset_triggers_for_day("thursday", dry_run=False, force=False)
        assert ok is True
        mock_set.assert_called_once_with("LIVE_BUY_TRIGGER_THURSDAY", "true", redis_client=None)


def test_reset_triggers_force_during_market_hours():
    dt_active = datetime(2026, 11, 19, 10, 2, tzinfo=IST)
    with patch("scripts.reset_daily_buy_triggers._now_ist", return_value=dt_active), \
         patch("scripts.reset_daily_buy_triggers.set_config_value", return_value=True) as mock_set:
        ok = reset_triggers_for_day("tuesday", dry_run=False, force=True)
        assert ok is True
        mock_set.assert_called_once_with("LIVE_BUY_TRIGGER_TUESDAY", "true", redis_client=None)


def test_reset_triggers_dry_run_does_not_call_redis():
    dt_post = datetime(2026, 11, 19, 15, 45, tzinfo=IST)
    with patch("scripts.reset_daily_buy_triggers._now_ist", return_value=dt_post), \
         patch("scripts.reset_daily_buy_triggers.set_config_value") as mock_set:
        ok = reset_triggers_for_day("thursday", dry_run=True, force=False)
        assert ok is True
        mock_set.assert_not_called()


def test_reset_triggers_all_post_market():
    dt_post = datetime(2026, 11, 19, 15, 45, tzinfo=IST)
    with patch("scripts.reset_daily_buy_triggers._now_ist", return_value=dt_post), \
         patch("scripts.reset_daily_buy_triggers.set_config_value", return_value=True) as mock_set:
        ok = reset_triggers_all(dry_run=False, force=False)
        assert ok is True
        assert mock_set.call_count == 2
        set_keys = [call[0][0] for call in mock_set.call_args_list]
        assert set_keys == ALL_BUY_KEYS


# ---------------------------------------------------------------------------
# 2. Tests for Auto-Disarm on Paired BUY Fill
# ---------------------------------------------------------------------------

def test_thursday_collector_checks_guard_and_fills(monkeypatch):
    thu = date(2026, 11, 19)
    monkeypatch.setattr(thursday_collector, "_today_ist", lambda: thu)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_THURSDAY", "true")
    monkeypatch.setenv("MOCK_BROKER", "true")
    monkeypatch.setenv("TEST_MODE", "true")

    with tempfile.TemporaryDirectory() as tmp_dir:
        snap_file = os.path.join(tmp_dir, "current_thursday_buy.json")

        gate_mock = EntryGateResult(
            should_enter=True,
            status="CLEARED",
            defer_reason="None",
            iv_slope=0.1,
            current_iv=13.5,
            vwap_distance=0.05,
            adx_14=22.0,
            opening_range=25.0,
            evaluated_at_ist="09:32:00",
        )

        with patch("strategies.thursday_option_collector.THURSDAY_SNAPSHOT_FILE", snap_file), \
             patch("strategies.thursday_option_collector._ensure_thursday_db", return_value=":memory:"), \
             patch("strategies.thursday_option_collector.insert_buy_snapshot"), \
             patch("strategies.thursday_option_collector.insert_record"), \
             patch("strategies.thursday_option_collector.update_active_fsm_state"), \
             patch("strategies.thursday_option_collector.retry_pending_order_alerts"), \
             patch("scripts.auto_record_manual_exit.reconcile_broker_positions_with_snapshot"), \
             patch("strategies.thursday_option_collector.load_active_snapshot", return_value={}), \
             patch("strategies.thursday_option_collector.check_nse_holiday", return_value=False), \
             patch("strategies.thursday_option_collector.is_thursday_strategy_active_day", return_value=True), \
             patch("strategies.thursday_option_collector.is_thursday_strategy_start_day", return_value=True), \
             patch("strategies.thursday_option_collector.is_strategy_closing_day", return_value=False), \
             patch("strategies.thursday_option_collector.evaluate_entry_gate", return_value=gate_mock), \
             patch("strategies.thursday_option_collector.get_nifty_spot", return_value={"ltp": 25300.0, "open": 25250.0, "close": 25200.0}), \
             patch("strategies.thursday_option_collector.get_nifty_option_chain", return_value={"call": {"ltp": 150.0}, "put": {"ltp": 160.0}}), \
             patch("common.entry_order_manager.resolve_upstox_option_key", return_value="NSE_FO|KEY"), \
             patch("common.entry_order_manager.check_duplicate_entry_guard", return_value=(False, "Clean")), \
             patch("common.entry_order_manager.execute_paired_market_buy", return_value={
                 "success": True, "status": "FILLED",
                 "call_buy_price": 151.0, "put_buy_price": 161.0,
                 "call_order_id": "THU_C_01", "put_order_id": "THU_P_01",
             }):

            ok = thursday_collector.collect_once(execute_buy=True)
            assert ok is True


def test_weekly_collector_checks_guard_and_fills(monkeypatch):
    tue = date(2026, 11, 17)
    monkeypatch.setattr(weekly_collector, "_today_ist", lambda: tue)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_TUESDAY", "true")
    monkeypatch.setenv("MOCK_BROKER", "true")
    monkeypatch.setenv("TEST_MODE", "true")

    with tempfile.TemporaryDirectory() as tmp_dir:
        snap_file = os.path.join(tmp_dir, "current_week_buy.json")

        gate_mock = EntryGateResult(
            should_enter=True,
            status="CLEARED",
            defer_reason="None",
            iv_slope=0.1,
            current_iv=13.5,
            vwap_distance=0.05,
            adx_14=22.0,
            opening_range=25.0,
            evaluated_at_ist="09:32:00",
        )

        with patch("config.ACTIVE_SNAPSHOT_FILE", snap_file), \
             patch("strategies.weekly_option_collector._ensure_db", return_value=":memory:"), \
             patch("strategies.weekly_option_collector.insert_buy_snapshot"), \
             patch("strategies.weekly_option_collector.insert_record"), \
             patch("strategies.weekly_option_collector.update_active_fsm_state"), \
             patch("strategies.weekly_option_collector.retry_pending_order_alerts"), \
             patch("scripts.auto_record_manual_exit.reconcile_broker_positions_with_snapshot"), \
             patch("strategies.weekly_option_collector.load_active_snapshot", return_value={}), \
             patch("strategies.weekly_option_collector.check_nse_holiday", return_value=False), \
             patch("strategies.weekly_option_collector.is_tuesday", return_value=True), \
             patch("strategies.weekly_option_collector.is_strategy_start_day", return_value=True), \
             patch("strategies.weekly_option_collector.is_strategy_closing_day", return_value=False), \
             patch("strategies.weekly_option_collector.evaluate_entry_gate", return_value=gate_mock), \
             patch("strategies.weekly_option_collector.get_nifty_spot", return_value={"ltp": 25300.0, "open": 25250.0, "close": 25200.0}), \
             patch("strategies.weekly_option_collector.get_nifty_option_chain", return_value={"call": {"ltp": 150.0}, "put": {"ltp": 160.0}}), \
             patch("common.entry_order_manager.resolve_upstox_option_key", return_value="NSE_FO|KEY"), \
             patch("common.entry_order_manager.check_duplicate_entry_guard", return_value=(False, "Clean")), \
             patch("common.entry_order_manager.execute_paired_market_buy", return_value={
                 "success": True, "status": "FILLED",
                 "call_buy_price": 151.0, "put_buy_price": 161.0,
                 "call_order_id": "TUE_C_01", "put_order_id": "TUE_P_01",
             }):

            ok = weekly_collector.collect_once(execute_buy=True)
            assert ok is True


# ---------------------------------------------------------------------------
# 3. Tests for Hard Same-Day Duplicate Entry Prevention
# ---------------------------------------------------------------------------

def test_thursday_collector_blocks_duplicate_buy_when_already_filled_today(monkeypatch):
    """If current_thursday_buy.json has entry_status=FILLED for today's start date, fresh buy is strictly blocked."""
    thu = date(2026, 11, 19)
    monkeypatch.setattr(thursday_collector, "_today_ist", lambda: thu)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_THURSDAY", "true")

    filled_snapshot = {
        "strategy_name": "nifty50_thursday_3day_collector",
        "cycle_id": "CYCLE-THU-20261119",
        "week_start_date": "20261119",
        "expiry_date": "2026-11-23",
        "call_strike": 25200,
        "put_strike": 25400,
        "call_buy_price": 150.0,
        "put_buy_price": 160.0,
        "entry_status": "FILLED",
        "status": "ongoing",
    }

    paired_buy_mock = MagicMock()
    with patch("scripts.auto_record_manual_exit.reconcile_broker_positions_with_snapshot"), \
         patch("strategies.thursday_option_collector.load_active_snapshot", return_value=filled_snapshot), \
         patch("strategies.thursday_option_collector._active_cycle", return_value={}), \
         patch("strategies.thursday_option_collector.get_nifty_spot", return_value={"ltp": 25300.0, "open": 25250.0, "close": 25200.0}), \
         patch("strategies.thursday_option_collector.get_nifty_option_chain", return_value={"call": {"ltp": 150.0}, "put": {"ltp": 160.0}}), \
         patch("common.entry_order_manager.execute_paired_market_buy", paired_buy_mock):

        ok = thursday_collector.collect_once(execute_buy=True)
        assert ok is True
        paired_buy_mock.assert_not_called()


def test_weekly_collector_blocks_duplicate_buy_when_already_filled_today(monkeypatch):
    """If current_week_buy.json has entry_status=FILLED for today's start date, fresh buy is strictly blocked."""
    tue = date(2026, 11, 17)
    monkeypatch.setattr(weekly_collector, "_today_ist", lambda: tue)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_TUESDAY", "true")

    filled_snapshot = {
        "strategy_name": "nifty50_weekly_option_collector",
        "cycle_id": "CYCLE-TUE-20261117",
        "week_start_date": "20261117",
        "expiry_date": "2026-11-24",
        "call_strike": 25200,
        "put_strike": 25400,
        "call_buy_price": 150.0,
        "put_buy_price": 160.0,
        "entry_status": "FILLED",
        "status": "ongoing",
    }

    paired_buy_mock = MagicMock()
    with patch("scripts.auto_record_manual_exit.reconcile_broker_positions_with_snapshot"), \
         patch("strategies.weekly_option_collector.load_active_snapshot", return_value=filled_snapshot), \
         patch("strategies.weekly_option_collector._active_cycle", return_value={}), \
         patch("strategies.weekly_option_collector.get_nifty_spot", return_value={"ltp": 25300.0, "open": 25250.0, "close": 25200.0}), \
         patch("strategies.weekly_option_collector.get_nifty_option_chain", return_value={"call": {"ltp": 150.0}, "put": {"ltp": 160.0}}), \
         patch("common.entry_order_manager.execute_paired_market_buy", paired_buy_mock):

        ok = weekly_collector.collect_once(execute_buy=True)
        assert ok is True
        paired_buy_mock.assert_not_called()


def test_weekly_collector_blocks_duplicate_buy_when_cycle_closed_today(monkeypatch):
    """If current_week_buy.json was started today and closed today, fresh buy is strictly blocked."""
    tue = date(2026, 11, 17)
    monkeypatch.setattr(weekly_collector, "_today_ist", lambda: tue)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_TUESDAY", "true")

    closed_snapshot = {
        "strategy_name": "nifty50_weekly_option_collector",
        "cycle_id": "CYCLE-TUE-20261117",
        "week_start_date": "20261117",
        "expiry_date": "2026-11-24",
        "call_strike": 25200,
        "put_strike": 25400,
        "call_buy_price": 150.0,
        "put_buy_price": 160.0,
        "entry_status": "FILLED",
        "status": "closed",
    }

    paired_buy_mock = MagicMock()
    with patch("scripts.auto_record_manual_exit.reconcile_broker_positions_with_snapshot"), \
         patch("strategies.weekly_option_collector.load_active_snapshot", return_value=closed_snapshot), \
         patch("common.entry_order_manager.execute_paired_market_buy", paired_buy_mock):

        ok = weekly_collector.collect_once(execute_buy=True)
        assert ok is True
        paired_buy_mock.assert_not_called()


def test_thursday_collector_blocks_duplicate_buy_when_cycle_closed_today(monkeypatch):
    """If current_thursday_buy.json was started today and closed today, fresh buy is strictly blocked."""
    thu = date(2026, 11, 19)
    monkeypatch.setattr(thursday_collector, "_today_ist", lambda: thu)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_THURSDAY", "true")

    closed_snapshot = {
        "strategy_name": "nifty50_thursday_3day_collector",
        "cycle_id": "CYCLE-THU-20261119",
        "week_start_date": "20261119",
        "expiry_date": "2026-11-23",
        "call_strike": 25200,
        "put_strike": 25400,
        "call_buy_price": 150.0,
        "put_buy_price": 160.0,
        "entry_status": "FILLED",
        "status": "closed",
    }

    paired_buy_mock = MagicMock()
    with patch("scripts.auto_record_manual_exit.reconcile_broker_positions_with_snapshot"), \
         patch("strategies.thursday_option_collector.load_active_snapshot", return_value=closed_snapshot), \
         patch("common.entry_order_manager.execute_paired_market_buy", paired_buy_mock):

        ok = thursday_collector.collect_once(execute_buy=True)
        assert ok is True
        paired_buy_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Tests for Broker-Wide Exposure Guard on Extensions
# ---------------------------------------------------------------------------

def test_broker_wide_exposure_blocks_on_different_strike_position():
    """When broker_wide=True, an open position on an arbitrary strike blocks extension."""
    adapter = MagicMock()
    # Position exists on strike 26000 (different from snapshot strikes 24000/24200)
    adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [{"tradingsymbol": "NIFTY10NOV2626000CE", "exchange": "NSE_FO", "quantity": 65}],
    }
    adapter.fetch_order_book.return_value = []
    snap = {
        "cycle_id": "c1", "expiry_date": "2026-11-10",
        "call_strike": 24000, "put_strike": 24200, "broker": "upstox",
    }

    with patch("common.broker_adapters.get_broker_adapter", return_value=adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions("5day_strategy", active_snapshot=snap, broker_wide=True)

        assert has_open is True
        assert is_safe is False
        assert "open exposure" in reason.lower()
        assert "26000CE" in reason


def test_broker_wide_exposure_blocks_on_open_order():
    """When broker_wide=True, an open order on the broker blocks extension."""
    adapter = MagicMock()
    adapter.fetch_positions.return_value = {"status": "success", "data": []}
    adapter.fetch_order_book.return_value = {
        "status": "success",
        "data": [{"order_id": "ORD_999", "status": "open", "tradingsymbol": "NIFTY10NOV2624500PE"}],
    }
    snap = {
        "cycle_id": "c1", "expiry_date": "2026-11-10",
        "call_strike": 24000, "put_strike": 24200, "broker": "upstox",
    }

    with patch("common.broker_adapters.get_broker_adapter", return_value=adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions("5day_strategy", active_snapshot=snap, broker_wide=True)

        assert has_open is True
        assert is_safe is False
        assert "open order" in reason.lower()
        assert "ORD_999" in reason


def test_broker_wide_check_uses_broker_data_without_local_snapshot():
    """A missing local snapshot must not block a verified broker-wide extension check."""
    adapter = MagicMock()
    adapter.fetch_positions.return_value = {"status": "success", "data": []}
    adapter.fetch_order_book.return_value = {"status": "success", "data": []}

    with patch("common.broker_adapters.get_broker_adapter", return_value=adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"), \
         patch("scripts.auto_record_manual_exit.load_active_snapshot", side_effect=AssertionError("snapshot lookup")):
        has_open, is_safe, reason = check_prior_cycle_open_positions(
            "5day_strategy",
            broker_wide=True,
        )

    assert has_open is False
    assert is_safe is True
    assert "broker" in reason.lower()


def test_cross_track_broker_wide_check_fails_closed_when_track_brokers_differ():
    with patch("config.get_broker_for_track", side_effect=lambda track, **_: {
        "5day_strategy": "upstox",
        "3day_strategy": "angelone",
    }[track]):
        has_open, is_safe, reason = check_prior_cycle_open_positions(
            "3day_strategy",
            broker_wide=True,
        )

    assert has_open is False
    assert is_safe is False
    assert "same broker" in reason.lower()


def test_check_duplicate_entry_guard_sqlite(tmp_path):
    from common.entry_order_manager import check_duplicate_entry_guard
    from common.storage import init_db, insert_buy_snapshot
    db_file = str(tmp_path / "test.db")
    init_db(db_file)
    insert_buy_snapshot(
        db_file,
        {
            "strategy_name": "nifty50_options_strategy",
            "cycle_id": "CYCLE-TUE-20261125",
            "week_start_date": "20261125",
            "expiry_date": "20261201",
            "call_strike": 24000,
            "put_strike": 24200,
            "call_buy_price": 100.0,
            "put_buy_price": 100.0,
            "entry_spot": 24100.0,
            "captured_at": 1700000000,
            "entry_status": "FILLED",
            "broker": "upstox",
        }
    )

    is_dup, reason = check_duplicate_entry_guard(
        broker="upstox",
        token="SIM_TOKEN",
        tag="TUE-BUY",
        start_date_str="20261125",
        call_contract={"trading_symbol": "CE"},
        put_contract={"trading_symbol": "PE"},
        db_path=db_file,
    )
    assert is_dup is True
    assert "Local SQLite DB" in reason


def test_check_duplicate_entry_guard_single_leg_held():
    from common.entry_order_manager import check_duplicate_entry_guard
    positions = [
        {"tradingsymbol": "NIFTY26NOV24000CE", "instrument_token": "TOK_CE", "quantity": 65}
    ]
    with patch("common.entry_order_manager.fetch_normalized_orders", return_value=[]), \
         patch("common.entry_order_manager.fetch_broker_positions", return_value=positions):
        is_dup, reason = check_duplicate_entry_guard(
            broker="upstox",
            token="VALID_TOKEN",
            tag="TUE-BUY",
            start_date_str="20261125",
            call_contract={"trading_symbol": "NIFTY26NOV24000CE", "instrument_token": "TOK_CE"},
            put_contract={"trading_symbol": "NIFTY26NOV24200PE", "instrument_token": "TOK_PE"},
            db_path=None,
        )
        assert is_dup is True
        assert "Broker portfolio already holds open contract position(s)" in reason


def test_check_duplicate_entry_guard_order_book():
    from common.entry_order_manager import check_duplicate_entry_guard
    with patch("common.entry_order_manager.fetch_normalized_orders", return_value=[
        {"transaction_type": "BUY", "tag": "TUE-BUY", "status": "filled", "order_id": "ORD123"}
    ]):
        is_dup, reason = check_duplicate_entry_guard(
            broker="upstox",
            token="VALID_TOKEN",
            tag="TUE-BUY",
            start_date_str="20261125",
            call_contract={"trading_symbol": "CE"},
            put_contract={"trading_symbol": "PE"},
            db_path=None,
        )
        assert is_dup is True
        assert "Broker order book already has TUE-BUY" in reason


def test_check_duplicate_entry_guard_clean():
    from common.entry_order_manager import check_duplicate_entry_guard
    with patch("common.entry_order_manager.fetch_normalized_orders", return_value=[]), \
         patch("common.entry_order_manager.fetch_broker_positions", return_value=[]):
        is_dup, reason = check_duplicate_entry_guard(
            broker="upstox",
            token="VALID_TOKEN",
            tag="TUE-BUY",
            start_date_str="20261125",
            call_contract={"trading_symbol": "CE"},
            put_contract={"trading_symbol": "PE"},
            db_path=None,
        )
        assert is_dup is False
        assert "Clean" in reason


def test_register_manual_entry_does_not_preauthorize_legs(tmp_path):
    """Verify register_manual_cycle does NOT pre-authorize exit keys in Redis or disarm buy flag."""
    from scripts.register_manual_entry import register_manual_cycle
    import tests.test_tuesday_fno_exit as tfe
    mock_redis = tfe.MockRedis()
    snap_file = str(tmp_path / "current_week_buy.json")
    db_file = str(tmp_path / "weekly.db")

    with patch("scripts.register_manual_entry.ACTIVE_SNAPSHOT_FILE", snap_file), \
         patch("scripts.register_manual_entry.build_db_path", return_value=db_file), \
         patch("scripts.register_manual_entry.init_or_rotate_cycle"), \
         patch("scripts.register_manual_entry.update_locked_prices"):
        res = register_manual_cycle(
            track="5day_strategy",
            call_strike=24000,
            put_strike=24200,
            call_price=150.0,
            put_price=140.0,
            call_order_id="ORD_CALL_01",
            put_order_id="ORD_PUT_01",
            expiry_iso="2026-12-01",
            start_date_iso="2026-11-25",
            broker="upstox",
            redis_client=mock_redis,
        )

    assert res["status"] in ("registered_successfully", "ready")
    # Neither leg should be authorized for exit yet
    call_key = "fno:leg_enabled:tue:2026-12-01:24000:CE"
    put_key = "fno:leg_enabled:tue:2026-12-01:24200:PE"
    assert mock_redis.get(call_key) is None
    assert mock_redis.get(put_key) is None
    assert mock_redis.get("ORDERS_LIVE_TUE") is None
    # Live buy trigger must NOT be set to false
    assert mock_redis.get("LIVE_BUY_TRIGGER_TUESDAY") is None


def test_carryforward_unauthorized_leg_skipped_by_exit_manager():
    """Verify an active carried-forward leg (e.g. Wednesday entry, PE held on Thursday) is skipped by exit manager."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tuesday_exit",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "Tuesday-Fno-Exit-Orders-V1.py")
    )
    tue_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tue_module)
    import tests.test_tuesday_fno_exit as tfe
    mock_redis = tfe.MockRedis()
    # Ensure neither leg is authorized in Redis
    positions = [{
        "tradingsymbol": "NIFTY26DEC25800PE",
        "instrument_token": "NSE_FO_99999",
        "exchange": "NSE_FO",
        "quantity": 65,
        "average_price": 236.75,
        "last_price": 194.10,
    }]
    orders = []

    with patch.object(tue_module, "place_exit_order_market") as mock_mkt, \
         patch.object(tue_module, "place_exit_order_slm") as mock_slm:
        res = tue_module.manage_tuesday_exit_orders(
            token="SIM_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            redis_client=mock_redis,
        )

    # Position must be skipped because Redis authorization key is absent
    assert res.get("authorized_positions") == 0
    mock_mkt.assert_not_called()
    mock_slm.assert_not_called()
    action = res["actions"][0]
    assert action["status"] == "authorization_skipped"
    assert "missing/disabled Redis authorization key" in action["reason"]

