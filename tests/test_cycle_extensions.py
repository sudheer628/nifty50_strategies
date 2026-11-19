"""
tests/test_cycle_extensions.py
Tests for Wednesday (Tuesday track) and Friday (Thursday track) cycle extensions.
"""

import json
import pytest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import config
from common.calendar_utils import is_tuesday_extension_day, is_thursday_extension_day
import strategies.weekly_option_collector as weekly_collector
import strategies.thursday_option_collector as thursday_collector

config._config_redis_state["unavailable"] = True


def test_calendar_extension_days():
    # Wednesday: 2026-11-04 is Wednesday (weekday 2)
    wed = date(2026, 11, 4)
    assert is_tuesday_extension_day(wed) is True
    assert is_thursday_extension_day(wed) is False

    # Friday: 2026-11-06 is Friday (weekday 4)
    fri = date(2026, 11, 6)
    assert is_thursday_extension_day(fri) is True
    assert is_tuesday_extension_day(fri) is False

    # Tuesday: 2026-11-03
    tue = date(2026, 11, 3)
    assert is_tuesday_extension_day(tue) is False
    assert is_thursday_extension_day(tue) is False


def test_wednesday_extension_skips_when_broker_positions_open(monkeypatch):
    wed = date(2026, 11, 4)
    monkeypatch.setattr(weekly_collector, "_today_ist", lambda: wed)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_WEDNESDAY", "true")
    monkeypatch.setenv("MOCK_BROKER", "true")
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("MOCK_BROKER", "true")
    monkeypatch.setenv("TEST_MODE", "true")

    # Snapshot exists from Tuesday
    snap_data = {
        "strategy_name": "NIFTY50_5DAY_STRATEGY",
        "cycle_id": "CYCLE-TUE-20261103",
        "week_start_date": "20261103",
        "expiry_date": "2026-11-10",
        "call_strike": 24000,
        "put_strike": 24200,
        "call_buy_price": 100.0,
        "put_buy_price": 120.0,
        "entry_status": "FILLED",
        "status": "ongoing",
        "broker": "upstox",
    }
    with open(config.ACTIVE_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap_data, f)

    # Mock broker having open position for the CALL
    mock_adapter = MagicMock()
    mock_adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [
            {
                "tradingsymbol": "NIFTY10NOV2624000CE",
                "quantity": 65,
                "exchange": "NSE_FO",
            }
        ],
    }
    mock_adapter.fetch_order_book.return_value = {"status": "success", "data": []}

    paired_buy_mock = MagicMock()

    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"), \
         patch("common.entry_order_manager.execute_paired_market_buy", paired_buy_mock), \
         patch("strategies.weekly_option_collector.get_nifty_spot", return_value={"ltp": 24100.0, "open": 24050.0, "close": 24000.0}), \
         patch("strategies.weekly_option_collector.get_nifty_option_chain", return_value={"call": {"ltp": 105.0}, "put": {"ltp": 115.0}}):

        ok = weekly_collector.collect_once(dry_run=True)
        assert ok is True

    # Crucial assertion: paired market buy was NOT executed
    paired_buy_mock.assert_not_called()

    # Snapshot remains the Tuesday cycle
    with open(config.ACTIVE_SNAPSHOT_FILE, "r") as f:
        snap = json.load(f)
    assert snap["cycle_id"] == "CYCLE-TUE-20261103"


def test_wednesday_extension_enters_fresh_cycle_when_broker_positions_zero(monkeypatch):
    wed = date(2026, 11, 4)
    monkeypatch.setattr(weekly_collector, "_today_ist", lambda: wed)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_WEDNESDAY", "true")
    monkeypatch.setenv("MOCK_BROKER", "true")
    monkeypatch.setenv("TEST_MODE", "true")

    # Prior snapshot from Tuesday
    snap_data = {
        "strategy_name": "NIFTY50_5DAY_STRATEGY",
        "cycle_id": "CYCLE-TUE-20261103",
        "week_start_date": "20261103",
        "expiry_date": "2026-11-10",
        "call_strike": 24000,
        "put_strike": 24200,
        "call_buy_price": 100.0,
        "put_buy_price": 120.0,
        "entry_status": "FILLED",
        "status": "ongoing",
        "broker": "upstox",
    }
    with open(config.ACTIVE_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap_data, f)

    # Broker has ZERO open positions
    mock_adapter = MagicMock()
    mock_adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [
            {
                "tradingsymbol": "NIFTY10NOV2624000CE",
                "quantity": 0,
                "exchange": "NSE_FO",
            }
        ],
    }
    mock_adapter.fetch_order_book.return_value = {"status": "success", "data": []}

    mock_buy_result = {
        "success": True,
        "status": "FILLED",
        "call_order_id": "WED_CALL_001",
        "put_order_id": "WED_PUT_001",
        "call_buy_price": 110.0,
        "put_buy_price": 115.0,
        "call_fill_price": 110.0,
        "put_fill_price": 115.0,
        "metrics": {},
    }
    paired_buy_mock = MagicMock(return_value=mock_buy_result)

    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"), \
         patch("common.entry_order_manager.execute_paired_market_buy", paired_buy_mock), \
         patch("strategies.weekly_option_collector.get_nifty_spot", return_value={"ltp": 24100.0, "open": 24050.0, "close": 24000.0}), \
         patch("strategies.weekly_option_collector.get_nifty_option_chain", return_value={"call": {"ltp": 110.0}, "put": {"ltp": 115.0}}):

        ok = weekly_collector.collect_once(force_entry=True, execute_buy=True)
        assert ok is True

    # Paired market buy WAS executed with TUE-BUY tag
    paired_buy_mock.assert_called_once()
    assert paired_buy_mock.call_args[1]["tag"] == "TUE-BUY"

    # New snapshot was written with CYCLE-WED-20261104
    with open(config.ACTIVE_SNAPSHOT_FILE, "r") as f:
        snap = json.load(f)
    assert snap["cycle_id"] == "CYCLE-WED-20261104"
    assert snap["entry_status"] == "FILLED"
    assert snap["call_order_id"] == "WED_CALL_001"
    assert snap["put_order_id"] == "WED_PUT_001"


@pytest.mark.parametrize("entry_status", ["FILLED", "PAPER_ENTRY", None])
def test_closed_wednesday_cycle_does_not_start_another_entry(monkeypatch, entry_status):
    wed = date(2026, 11, 18)
    monkeypatch.setattr(weekly_collector, "_today_ist", lambda: wed)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_WEDNESDAY", "true")
    closed_snapshot = {
        "cycle_id": "CYCLE-WED-20261118",
        "week_start_date": "20261118",
        "expiry_date": "2026-11-23",
        "call_strike": 25300,
        "put_strike": 25400,
        "call_buy_price": 186.05,
        "put_buy_price": 230.40,
        "entry_status": entry_status,
        "status": "closed",
    }

    with patch("scripts.auto_record_manual_exit.reconcile_broker_positions_with_snapshot"), \
         patch("strategies.weekly_option_collector.load_active_snapshot", return_value=closed_snapshot), \
         patch("strategies.weekly_option_collector.get_nifty_spot") as get_spot, \
         patch("strategies.weekly_option_collector.save_active_snapshot") as save_snapshot, \
         patch("common.entry_order_manager.execute_paired_market_buy") as paired_buy:
        assert weekly_collector.collect_once(dry_run=True)

    get_spot.assert_not_called()
    save_snapshot.assert_not_called()
    paired_buy.assert_not_called()


def test_closed_cycle_does_not_create_midweek_fallback(monkeypatch):
    thu = date(2026, 11, 19)
    monkeypatch.setattr(weekly_collector, "_today_ist", lambda: thu)
    closed_snapshot = {
        "cycle_id": "CYCLE-WED-20261118",
        "week_start_date": "20261118",
        "expiry_date": "2026-11-23",
        "call_strike": 25300,
        "put_strike": 25400,
        "call_buy_price": 186.05,
        "put_buy_price": 230.40,
        "entry_status": "FILLED",
        "status": "closed",
    }

    with patch("scripts.auto_record_manual_exit.reconcile_broker_positions_with_snapshot"), \
         patch("strategies.weekly_option_collector.load_active_snapshot", return_value=closed_snapshot), \
         patch("strategies.weekly_option_collector.get_nifty_spot") as get_spot, \
         patch("strategies.weekly_option_collector.save_active_snapshot") as save_snapshot:
        assert weekly_collector.collect_once(dry_run=True)

    get_spot.assert_not_called()
    save_snapshot.assert_not_called()


@pytest.mark.parametrize("entry_status", ["FILLED", "PAPER_ENTRY", None])
def test_closed_friday_cycle_does_not_start_another_entry(monkeypatch, entry_status):
    fri = date(2026, 11, 6)
    monkeypatch.setattr(thursday_collector, "_today_ist", lambda: fri)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_FRIDAY", "true")
    closed_snapshot = {
        "cycle_id": "CYCLE-FRI-20261106",
        "week_start_date": "20261106",
        "expiry_date": "2026-11-10",
        "call_strike": 25300,
        "put_strike": 25400,
        "call_buy_price": 186.05,
        "put_buy_price": 230.40,
        "entry_status": entry_status,
        "status": "closed",
    }

    with patch("scripts.auto_record_manual_exit.reconcile_broker_positions_with_snapshot"), \
         patch("strategies.thursday_option_collector.load_active_snapshot", return_value=closed_snapshot), \
         patch("strategies.thursday_option_collector.get_nifty_spot") as get_spot, \
         patch("strategies.thursday_option_collector.save_active_snapshot") as save_snapshot, \
         patch("common.entry_order_manager.execute_paired_market_buy") as paired_buy:
        assert thursday_collector.collect_once(dry_run=True)

    get_spot.assert_not_called()
    save_snapshot.assert_not_called()
    paired_buy.assert_not_called()


def test_closed_friday_cycle_does_not_create_later_fallback(monkeypatch):
    mon = date(2026, 11, 9)
    monkeypatch.setattr(thursday_collector, "_today_ist", lambda: mon)
    monkeypatch.setattr(thursday_collector, "check_nse_holiday", lambda _: False)
    monkeypatch.setattr(thursday_collector, "is_thursday_strategy_active_day", lambda _: True)
    monkeypatch.setattr(thursday_collector, "is_thursday_strategy_start_day", lambda _: False)
    closed_snapshot = {
        "cycle_id": "CYCLE-FRI-20261106",
        "week_start_date": "20261106",
        "expiry_date": "2026-11-10",
        "call_strike": 25300,
        "put_strike": 25400,
        "call_buy_price": 186.05,
        "put_buy_price": 230.40,
        "entry_status": "FILLED",
        "status": "closed",
    }

    with patch("scripts.auto_record_manual_exit.reconcile_broker_positions_with_snapshot"), \
         patch("strategies.thursday_option_collector.load_active_snapshot", return_value=closed_snapshot), \
         patch("strategies.thursday_option_collector.get_nifty_spot") as get_spot, \
         patch("strategies.thursday_option_collector.save_active_snapshot") as save_snapshot:
        assert thursday_collector.collect_once(dry_run=True)

    get_spot.assert_not_called()
    save_snapshot.assert_not_called()


def test_friday_extension_skips_when_broker_positions_open(monkeypatch):
    fri = date(2026, 11, 6)
    monkeypatch.setattr(thursday_collector, "_today_ist", lambda: fri)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_FRIDAY", "true")
    monkeypatch.setenv("MOCK_BROKER", "true")
    monkeypatch.setenv("TEST_MODE", "true")

    # Snapshot exists from Thursday
    snap_data = {
        "strategy_name": "NIFTY50_3DAY_STRATEGY",
        "cycle_id": "CYCLE-THU-20261105",
        "week_start_date": "20261105",
        "expiry_date": "2026-11-10",
        "call_strike": 24050,
        "put_strike": 24150,
        "call_buy_price": 90.0,
        "put_buy_price": 95.0,
        "entry_status": "FILLED",
        "status": "ongoing",
        "broker": "upstox",
    }
    with open(config.THURSDAY_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap_data, f)

    # Mock broker having open position for the PUT
    mock_adapter = MagicMock()
    mock_adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [
            {
                "tradingsymbol": "NIFTY10NOV2624150PE",
                "quantity": 65,
                "exchange": "NSE_FO",
            }
        ],
    }
    mock_adapter.fetch_order_book.return_value = {"status": "success", "data": []}

    paired_buy_mock = MagicMock()

    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"), \
         patch("common.entry_order_manager.execute_paired_market_buy", paired_buy_mock), \
         patch("strategies.thursday_option_collector.get_nifty_spot", return_value={"ltp": 24100.0, "open": 24050.0, "close": 24000.0}), \
         patch("strategies.thursday_option_collector.get_nifty_option_chain", return_value={"call": {"ltp": 92.0}, "put": {"ltp": 98.0}}):

        ok = thursday_collector.collect_once(dry_run=True)
        assert ok is True

    # Paired market buy was NOT executed
    paired_buy_mock.assert_not_called()

    # Snapshot remains Thursday cycle
    with open(config.THURSDAY_SNAPSHOT_FILE, "r") as f:
        snap = json.load(f)
    assert snap["cycle_id"] == "CYCLE-THU-20261105"


def test_friday_extension_enters_fresh_cycle_when_broker_positions_zero(monkeypatch):
    fri = date(2026, 11, 6)
    monkeypatch.setattr(thursday_collector, "_today_ist", lambda: fri)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_FRIDAY", "true")
    monkeypatch.setenv("MOCK_BROKER", "true")
    monkeypatch.setenv("TEST_MODE", "true")

    # Prior snapshot from Thursday
    snap_data = {
        "strategy_name": "NIFTY50_3DAY_STRATEGY",
        "cycle_id": "CYCLE-THU-20261105",
        "week_start_date": "20261105",
        "expiry_date": "2026-11-10",
        "call_strike": 24050,
        "put_strike": 24150,
        "call_buy_price": 90.0,
        "put_buy_price": 95.0,
        "entry_status": "FILLED",
        "status": "ongoing",
        "broker": "upstox",
    }
    with open(config.THURSDAY_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap_data, f)

    # Broker has ZERO open positions
    mock_adapter = MagicMock()
    mock_adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [
            {
                "tradingsymbol": "NIFTY10NOV2624150PE",
                "quantity": 0,
                "exchange": "NSE_FO",
            }
        ],
    }
    mock_adapter.fetch_order_book.return_value = {"status": "success", "data": []}

    mock_buy_result = {
        "success": True,
        "status": "FILLED",
        "call_order_id": "FRI_CALL_001",
        "put_order_id": "FRI_PUT_001",
        "call_buy_price": 85.0,
        "put_buy_price": 90.0,
        "call_fill_price": 85.0,
        "put_fill_price": 90.0,
        "metrics": {},
    }
    paired_buy_mock = MagicMock(return_value=mock_buy_result)

    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"), \
         patch("common.entry_order_manager.execute_paired_market_buy", paired_buy_mock), \
         patch("strategies.thursday_option_collector.get_nifty_spot", return_value={"ltp": 24100.0, "open": 24050.0, "close": 24000.0}), \
         patch("strategies.thursday_option_collector.get_nifty_option_chain", return_value={"call": {"ltp": 85.0}, "put": {"ltp": 90.0}}):

        ok = thursday_collector.collect_once(force_entry=True, execute_buy=True)
        assert ok is True

    # Paired market buy WAS executed with THU-BUY tag
    paired_buy_mock.assert_called_once()
    assert paired_buy_mock.call_args[1]["tag"] == "THU-BUY"

    # New snapshot was written with CYCLE-FRI-20261106
    with open(config.THURSDAY_SNAPSHOT_FILE, "r") as f:
        snap = json.load(f)
    assert snap["cycle_id"] == "CYCLE-FRI-20261106"
    assert snap["entry_status"] == "FILLED"
    assert snap["call_order_id"] == "FRI_CALL_001"
    assert snap["put_order_id"] == "FRI_PUT_001"


def test_orders_live_gate_alone_does_not_authorize_buy_entry(monkeypatch):
    """P1 Verification: Setting ORDERS_LIVE_TUE=1 must NOT trigger a live fresh BUY if LIVE_BUY_TRIGGER_WEDNESDAY is false."""
    wed = date(2026, 11, 4)
    monkeypatch.setattr(weekly_collector, "_today_ist", lambda: wed)
    monkeypatch.setenv("ORDERS_LIVE_TUE", "1")
    monkeypatch.delenv("LIVE_BUY_TRIGGER_TUESDAY", raising=False)
    monkeypatch.delenv("LIVE_BUY_TRIGGER_WEDNESDAY", raising=False)
    monkeypatch.setenv("MOCK_BROKER", "true")
    monkeypatch.setenv("TEST_MODE", "true")

    paired_buy_mock = MagicMock()
    with patch("common.entry_order_manager.execute_paired_market_buy", paired_buy_mock), \
         patch("strategies.weekly_option_collector.get_nifty_spot", return_value={"ltp": 24100.0, "open": 24050.0, "close": 24000.0}), \
         patch("strategies.weekly_option_collector.get_nifty_option_chain", return_value={"call": {"ltp": 105.0}, "put": {"ltp": 115.0}}):

        ok = weekly_collector.collect_once(dry_run=True)
        assert ok is True

    # Critical: paired buy was NOT executed because LIVE_BUY_TRIGGER_WEDNESDAY was not enabled
    paired_buy_mock.assert_not_called()


def test_broker_fetch_error_skips_extension_fail_closed(monkeypatch):
    """P1 Verification: When broker position check fails with an error, the extension must fail closed (skip fresh entry)."""
    wed = date(2026, 11, 4)
    monkeypatch.setattr(weekly_collector, "_today_ist", lambda: wed)
    monkeypatch.setenv("LIVE_BUY_TRIGGER_WEDNESDAY", "true")
    monkeypatch.setenv("MOCK_BROKER", "true")
    monkeypatch.setenv("TEST_MODE", "true")

    # Snapshot exists from Tuesday
    snap_data = {
        "strategy_name": "NIFTY50_5DAY_STRATEGY",
        "cycle_id": "CYCLE-TUE-20261103",
        "week_start_date": "20261103",
        "expiry_date": "2026-11-10",
        "call_strike": 24000,
        "put_strike": 24200,
        "call_buy_price": 100.0,
        "put_buy_price": 120.0,
        "entry_status": "FILLED",
        "status": "ongoing",
        "broker": "upstox",
    }
    with open(config.ACTIVE_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap_data, f)

    # Broker fetch raises exception
    mock_adapter = MagicMock()
    mock_adapter.fetch_positions.side_effect = RuntimeError("Broker connection timeout")

    paired_buy_mock = MagicMock()

    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"), \
         patch("common.entry_order_manager.execute_paired_market_buy", paired_buy_mock), \
         patch("strategies.weekly_option_collector.get_nifty_spot", return_value={"ltp": 24100.0, "open": 24050.0, "close": 24000.0}), \
         patch("strategies.weekly_option_collector.get_nifty_option_chain", return_value={"call": {"ltp": 105.0}, "put": {"ltp": 115.0}}):

        ok = weekly_collector.collect_once(dry_run=True)
        assert ok is True

    # Must NOT enter fresh cycle when broker check failed
    paired_buy_mock.assert_not_called()
    with open(config.ACTIVE_SNAPSHOT_FILE, "r") as f:
        snap = json.load(f)
    assert snap["cycle_id"] == "CYCLE-TUE-20261103"
