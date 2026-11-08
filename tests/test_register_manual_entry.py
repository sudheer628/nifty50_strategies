"""
tests/test_register_manual_entry.py - Unit tests for manual order registration utility.
"""

import json
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

# Add project root to sys.path
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from scripts.register_manual_entry import (
    auto_detect_orders_from_broker,
    build_trail_redis_keys,
    parse_tradingsymbol,
    register_manual_cycle,
)


def test_parse_tradingsymbol():
    """Verify parsing across various exchange and Upstox option formats."""
    # Format 1: Upstox compact weekly
    res1 = parse_tradingsymbol("NIFTY26N1724300CE")
    assert res1["underlying"] == "NIFTY"
    assert res1["strike"] == "24300"
    assert res1["option_type"] == "CE"

    # Format 2: Upstox compact weekly PE
    res2 = parse_tradingsymbol("NIFTY26N1724100PE")
    assert res2["underlying"] == "NIFTY"
    assert res2["strike"] == "24100"
    assert res2["option_type"] == "PE"

    # Format 3: Upstox standard date
    res3 = parse_tradingsymbol("NIFTY17NOV2624300CE")
    assert res3["underlying"] == "NIFTY"
    assert res3["strike"] == "24300"
    assert res3["option_type"] == "CE"


def test_build_trail_redis_keys():
    """Verify generated Redis keys match Tuesday exit manager specifications."""
    keys = build_trail_redis_keys(
        broker="upstox",
        tradingsymbol="NIFTY26N1724300CE",
        instrument_token="NSE_FO|52341",
        active_expiry="2026-11-17",
        cycle="tue",
    )
    assert "fno:upstox:trail:tue:" in keys["base"]
    assert "2026-11-17:24300:CE" in keys["contract_suffix"]
    assert keys["avg"] == f"{keys['base']}:avg"
    assert keys["sl"] == f"{keys['base']}:sl"
    assert keys["qty"] == f"{keys['base']}:qty"
    assert keys["mode"] == f"{keys['base']}:mode"
    assert keys["entry_order_id"] == f"{keys['base']}:entry_order_id"


def test_auto_detect_orders_from_broker():
    """Verify auto-detect parses broker order book and extracts matching CE and PE orders."""
    mock_adapter = MagicMock()
    mock_adapter.get_access_token.return_value = "mock_token"
    mock_adapter.fetch_order_book.return_value = {
        "status": "success",
        "data": [
            {
                "order_id": "261109000177180",
                "tradingsymbol": "NIFTY26N1724300CE",
                "instrument_token": "NSE_FO|52341",
                "transaction_type": "BUY",
                "status": "complete",
                "quantity": 65,
                "filled_quantity": 65,
                "average_price": 220.50,
                "order_timestamp": "2026-11-09 09:35:00",
            },
            {
                "order_id": "261109000177181",
                "tradingsymbol": "NIFTY26N1724100PE",
                "instrument_token": "NSE_FO|52342",
                "transaction_type": "BUY",
                "status": "complete",
                "quantity": 65,
                "filled_quantity": 65,
                "average_price": 195.00,
                "order_timestamp": "2026-11-09 09:35:05",
            },
            {
                "order_id": "old_order_rejected",
                "tradingsymbol": "NIFTY26N1724300CE",
                "transaction_type": "BUY",
                "status": "rejected",
                "quantity": 65,
                "filled_quantity": 0,
            },
        ],
    }

    with patch("scripts.register_manual_entry.get_broker_adapter", return_value=mock_adapter):
        detected = auto_detect_orders_from_broker(
            broker="upstox",
            target_expiry_iso="2026-11-17",
            redis_client=MagicMock(),
        )

    assert detected is not None
    assert detected["call"]["order_id"] == "261109000177180"
    assert detected["call"]["strike"] == 24300
    assert detected["call"]["price"] == 220.50
    assert detected["call"]["qty"] == 65

    assert detected["put"]["order_id"] == "261109000177181"
    assert detected["put"]["strike"] == 24100
    assert detected["put"]["price"] == 195.00
    assert detected["put"]["qty"] == 65


def test_register_manual_cycle_dry_run(tmp_path):
    """Verify dry-run returns planned actions without writing to disk or Redis."""
    res = register_manual_cycle(
        track="tuesday",
        broker="upstox",
        call_strike=24300,
        put_strike=24100,
        call_price=220.50,
        put_price=195.00,
        call_order_id="261109000177180",
        put_order_id="261109000177181",
        qty=65,
        expiry_iso="2026-11-17",
        start_date_iso="2026-11-11",
        dry_run=True,
    )
    assert res["status"] == "dry_run"
    assert res["call_strike"] == 24300
    assert res["put_strike"] == 24100
    assert res["week_start_date"] == "20261111"
    assert res["expiry_date"] == "20261117"
    assert res["redis_ops_count"] > 10


def test_register_manual_cycle_live(tmp_path):
    """Verify live registration writes snapshot, updates Alpha FSM, and configures Redis."""
    mock_redis = MagicMock()
    mock_redis.set = MagicMock()
    mock_redis.sadd = MagicMock()

    test_snapshot_file = str(tmp_path / "current_week_buy.json")

    with patch("scripts.register_manual_entry.ACTIVE_SNAPSHOT_FILE", test_snapshot_file), \
         patch("scripts.register_manual_entry.save_active_snapshot") as mock_save, \
         patch("scripts.register_manual_entry.build_db_path", return_value=":memory:"), \
         patch("scripts.register_manual_entry.init_db") as mock_init_db, \
         patch("scripts.register_manual_entry.insert_buy_snapshot") as mock_insert, \
         patch("scripts.register_manual_entry.init_or_rotate_cycle") as mock_init_cycle, \
         patch("scripts.register_manual_entry.update_locked_prices") as mock_update_locked:

        res = register_manual_cycle(
            track="tuesday",
            broker="upstox",
            call_strike=24300,
            put_strike=24100,
            call_price=220.50,
            put_price=195.00,
            call_order_id="261109000177180",
            put_order_id="261109000177181",
            qty=65,
            expiry_iso="2026-11-17",
            start_date_iso="2026-11-11",
            dry_run=False,
            redis_client=mock_redis,
        )

        assert res["status"] == "registered_successfully"

        # Verify save_active_snapshot called with proper payload
        assert mock_save.called
        saved_snap = mock_save.call_args[0][0]
        assert saved_snap["week_start_date"] == "20261111"
        assert saved_snap["expiry_date"] == "20261117"
        assert saved_snap["entry_status"] == "FILLED"
        assert saved_snap["call_strike"] == 24300
        assert saved_snap["put_strike"] == 24100
        assert saved_snap["call_buy_price"] == 220.50
        assert saved_snap["put_buy_price"] == 195.00

        # Verify Alpha FSM initialization
        alpha_fsm = saved_snap["alpha_fsm"]
        assert alpha_fsm["state"] == "DUAL_LONG"
        assert alpha_fsm["call_leg"]["status"] == "ACTIVE"
        assert alpha_fsm["put_leg"]["status"] == "ACTIVE"
        assert alpha_fsm["call_leg"]["buy_price"] == 220.50
        assert alpha_fsm["put_leg"]["buy_price"] == 195.00

        # Verify Redis keys
        set_calls = {call[0][0]: call[0][1] for call in mock_redis.set.call_args_list}
        assert set_calls.get("fno:upstox:enabled:tue:2026-11-17:24300:CE") == "65"
        assert set_calls.get("fno:upstox:enabled:tue:2026-11-17:24100:PE") == "65"
        assert set_calls.get("ORDERS_LIVE_TUE") == "2"
        assert set_calls.get("LIVE_BUY_TRIGGER_TUESDAY") == "false"
        assert set_calls.get("LIVE_EXIT_TRIGGER_TUESDAY") == "true"

        # Verify managed order IDs
        sadd_calls = [call[0] for call in mock_redis.sadd.call_args_list]
        assert ("fno:upstox:managed_order_ids:tue", "261109000177180") in sadd_calls
        assert ("fno:upstox:managed_order_ids:tue", "261109000177181") in sadd_calls


def test_collector_reuses_manual_snapshot_on_wednesday():
    """Verify that weekly_option_collector reuses the snapshot on Wednesday without placing buy orders."""
    from strategies.weekly_option_collector import _active_cycle

    # Simulate registered snapshot for Wednesday 2026-11-11
    registered_snap = {
        "week_start_date": "20261111",
        "expiry_date": "20261117",
        "call_strike": 24300,
        "put_strike": 24100,
        "call_buy_price": 220.50,
        "put_buy_price": 195.00,
        "entry_status": "FILLED",
    }

    wednesday = date(2026, 11, 11)
    cycle = _active_cycle(registered_snap, wednesday)

    assert cycle != {}
    assert cycle["week_start_date"] == "20261111"
    assert cycle["call_strike"] == 24300
    assert cycle["put_strike"] == 24100
    assert cycle["call_buy_price"] == 220.50
    assert cycle["put_buy_price"] == 195.00
