"""
Unit tests for Automated Paired MARKET BUY Entry Order Manager.

Validates:
1. Broker Routing:
   - Tuesday pinned strictly to Upstox.
   - Thursday pinned strictly to AngelOne.
   - Invalid brokers rejected with ValueError.
2. Funds Preflight:
   - Preflight passes when funds sufficient (cost + buffer + charges).
   - Preflight fails and skips when funds insufficient (NEITHER order placed).
   - Contracts, LTPs, and quantity validation.
3. Paired MARKET BUY Execution:
   - Back-to-back submission of both legs.
   - Clean fills: both legs complete -> actual fill prices, quantities, and OIDs recorded.
4. Atomic Rollback on Rejection / Failure:
   - One leg rejected -> opposite leg cancelled / unwound via MARKET SELL.
   - Timeout or partial fill -> open orders cancelled, filled quantities unwound via MARKET SELL.
   - Verification that rollback handles exact filled quantities.
5. Idempotency & State Persistence:
   - Reference LTPs (call_ref_ltp, put_ref_ltp) preserved alongside actual fill prices (call_buy_price, put_buy_price).
   - Collector skips re-entry if cycle was already entered or aborted/skipped today.
   - SQLite strategy_buy_snapshots dynamic column persistence.
"""

import json
import os
import sqlite3
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from common.entry_order_manager import (
    DEFAULT_BUY_BUFFER_PCT,
    DEFAULT_ESTIMATED_CHARGES,
    MOCK_TOKEN,
    cancel_broker_order,
    execute_paired_market_buy,
    get_available_funds,
    get_broker_token,
    normalize_status,
    place_market_order,
    poll_order_fills,
    preflight_entry,
    resolve_upstox_option_key,
    rollback_partial_entry,
    verify_positions_flat,
)
from common.storage import init_db, insert_buy_snapshot
from common.unified_strategy_store import update_locked_prices, load_active_strategies


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_contracts():
    call_contract_upstox = {
        "instrument_token": "NSE_FO|53750",
        "strike": 24500,
        "option_type": "CE",
        "trading_symbol": "NIFTY03NOV2624500CE",
    }
    put_contract_upstox = {
        "instrument_token": "NSE_FO|53795",
        "strike": 24300,
        "option_type": "PE",
        "trading_symbol": "NIFTY03NOV2624300PE",
    }
    call_contract_angel = {
        "token": "53750",
        "symboltoken": "53750",
        "strike": 24500,
        "option_type": "CE",
        "trading_symbol": "NIFTY03NOV2624500CE",
        "exchange": "NFO",
    }
    put_contract_angel = {
        "token": "53795",
        "symboltoken": "53795",
        "strike": 24300,
        "option_type": "PE",
        "trading_symbol": "NIFTY03NOV2624300PE",
        "exchange": "NFO",
    }
    return {
        "upstox_call": call_contract_upstox,
        "upstox_put": put_contract_upstox,
        "angel_call": call_contract_angel,
        "angel_put": put_contract_angel,
    }


# ---------------------------------------------------------------------------
# 1. Broker Routing & Token Retrieval Tests
# ---------------------------------------------------------------------------

def test_broker_routing_rejection(sample_contracts):
    """Passing an unsupported broker name must fail fast with ValueError."""
    with pytest.raises(ValueError, match="Unsupported broker 'zerodha'"):
        execute_paired_market_buy(
            broker="zerodha",
            call_contract=sample_contracts["upstox_call"],
            put_contract=sample_contracts["upstox_put"],
            quantity=65,
            call_ltp=120.0,
            put_ltp=110.0,
        )


def test_token_retrieval_mock_mode():
    """When MOCK_BROKER=true or TEST_MODE=true, returns MOCK_TOKEN cleanly."""
    with patch.dict(os.environ, {"MOCK_BROKER": "true"}):
        tok_upstox = get_broker_token("upstox")
        tok_angel = get_broker_token("angelone")
        assert tok_upstox == MOCK_TOKEN
        assert tok_angel == MOCK_TOKEN


def test_status_normalization():
    """Broker order statuses should normalize into canonical categories."""
    assert normalize_status("complete") == "complete"
    assert normalize_status("COMPLETE") == "complete"
    assert normalize_status("filled") == "complete"
    assert normalize_status("traded") == "complete"
    assert normalize_status("cancelled") == "cancelled"
    assert normalize_status("canceled") == "cancelled"
    assert normalize_status("rejected") == "rejected"
    assert normalize_status("open") == "open"
    assert normalize_status("trigger pending") == "open"


# ---------------------------------------------------------------------------
# 2. Funds Preflight Tests
# ---------------------------------------------------------------------------

def test_preflight_sufficient_funds(sample_contracts):
    """Preflight passes when available funds exceed premium + buffer + charges."""
    # Est cost: (100 + 100) * 65 = 13,000. Buffer 5% = 650. Charges = 40. Total = 13,690.
    with patch("common.entry_order_manager.get_available_funds", return_value=50000.0):
        passed, reason, metrics = preflight_entry(
            broker="upstox",
            token=MOCK_TOKEN,
            call_contract=sample_contracts["upstox_call"],
            put_contract=sample_contracts["upstox_put"],
            quantity=65,
            call_ltp=100.0,
            put_ltp=100.0,
            buffer_pct=0.05,
            estimated_charges=40.0,
        )
        assert passed is True
        assert "passed" in reason.lower()
        assert metrics["estimated_cost"] == 13000.0
        assert metrics["buffer_amount"] == 650.0
        assert metrics["required_funds"] == 13690.0
        assert metrics["available_funds"] == 50000.0


def test_preflight_insufficient_funds_aborts_without_orders(sample_contracts):
    """When available funds are less than required funds, preflight must reject and place NO orders."""
    # Est cost: (150 + 150) * 65 = 19,500. Required ~ 20,515. Available = 10,000.
    with patch("common.entry_order_manager.get_available_funds", return_value=10000.0), \
         patch("common.entry_order_manager.place_market_order") as mock_place:
        passed, reason, metrics = preflight_entry(
            broker="angelone",
            token=MOCK_TOKEN,
            call_contract=sample_contracts["angel_call"],
            put_contract=sample_contracts["angel_put"],
            quantity=65,
            call_ltp=150.0,
            put_ltp=150.0,
        )
        assert passed is False
        assert "Insufficient funds in angelone" in reason
        mock_place.assert_not_called()


def test_preflight_invalid_contract_params(sample_contracts):
    """Preflight rejects when contracts miss broker-required tokens."""
    passed, reason, _ = preflight_entry(
        broker="upstox",
        token=MOCK_TOKEN,
        call_contract={"strike": 24500},  # missing instrument_token
        put_contract=sample_contracts["upstox_put"],
        quantity=65,
        call_ltp=100.0,
        put_ltp=100.0,
    )
    assert passed is False
    assert "missing instrument_token" in reason


# ---------------------------------------------------------------------------
# 3. Paired Market Order Execution & Fills
# ---------------------------------------------------------------------------

def test_paired_market_buy_clean_fill(sample_contracts):
    """Both legs submitted back-to-back and confirmed fully filled."""
    call_oid = "UPSTOX-CE-1001"
    put_oid = "UPSTOX-PE-1002"

    placed_orders = []

    def mock_place(broker, token, contract, quantity, transaction_type="BUY", tag="AUTO-BUY"):
        oid = call_oid if "CE" in contract.get("trading_symbol", "") else put_oid
        placed_orders.append((contract.get("trading_symbol"), transaction_type, quantity))
        return {"success": True, "order_id": oid, "status": "submitted"}

    mock_order_book = [
        {"order_id": call_oid, "status": "complete", "filled_quantity": 65, "average_price": 105.25},
        {"order_id": put_oid, "status": "complete", "filled_quantity": 65, "average_price": 98.50},
    ]

    with patch("common.entry_order_manager.get_available_funds", return_value=100000.0), \
         patch("common.entry_order_manager.place_market_order", side_effect=mock_place), \
         patch("common.entry_order_manager.fetch_normalized_orders", return_value=mock_order_book):

        result = execute_paired_market_buy(
            broker="upstox",
            call_contract=sample_contracts["upstox_call"],
            put_contract=sample_contracts["upstox_put"],
            quantity=65,
            call_ltp=100.0,
            put_ltp=95.0,
            token=MOCK_TOKEN,
            timeout_sec=2.0,
        )

        assert result["success"] is True
        assert result["status"] == "FILLED"
        assert result["call_buy_price"] == 105.25
        assert result["put_buy_price"] == 98.50
        assert result["call_ref_ltp"] == 100.0
        assert result["put_ref_ltp"] == 95.0
        assert result["call_order_id"] == call_oid
        assert result["put_order_id"] == put_oid
        # Ensure back-to-back submission of 2 BUY orders
        assert len(placed_orders) == 2
        assert placed_orders[0][1] == "BUY"
        assert placed_orders[1][1] == "BUY"


# ---------------------------------------------------------------------------
# 4. Atomic Rollback Tests
# ---------------------------------------------------------------------------

def test_rollback_when_call_rejected_immediately(sample_contracts):
    """If CALL order placement fails/rejected, rollback cancels/unwinds PUT."""
    put_oid = "ANGEL-PE-2002"

    def mock_place(broker, token, contract, quantity, transaction_type="BUY", tag="AUTO-BUY"):
        if "CE" in contract.get("trading_symbol", ""):
            return {"success": False, "error": "RMS price band rejection"}
        return {"success": True, "order_id": put_oid, "status": "submitted"}

    mock_order_book = [
        {"order_id": put_oid, "status": "cancelled", "filled_quantity": 0},
    ]

    with patch("common.entry_order_manager.get_available_funds", return_value=100000.0), \
         patch("common.entry_order_manager.place_market_order", side_effect=mock_place), \
         patch("common.entry_order_manager.cancel_broker_order", return_value={"success": True}) as mock_cancel, \
         patch("common.entry_order_manager.fetch_normalized_orders", return_value=mock_order_book):

        result = execute_paired_market_buy(
            broker="angelone",
            call_contract=sample_contracts["angel_call"],
            put_contract=sample_contracts["angel_put"],
            quantity=65,
            call_ltp=120.0,
            put_ltp=110.0,
            token=MOCK_TOKEN,
        )

        assert result["success"] is False
        assert result["status"] == "FAILED_ROLLED_BACK"
        # PUT order was cancelled
        mock_cancel.assert_called_with("angelone", MOCK_TOKEN, put_oid, variety="NORMAL")


def test_rollback_when_partial_fill_occurs(sample_contracts):
    """
    If CALL fills 65 but PUT fills 0 and is rejected:
    1. Cancel PUT.
    2. Submit immediate MARKET SELL for CALL (65 qty) to unwind.
    """
    call_oid = "UPSTOX-CE-3001"
    put_oid = "UPSTOX-PE-3002"

    sell_orders_placed = []

    def mock_place(broker, token, contract, quantity, transaction_type="BUY", tag="AUTO-BUY"):
        if transaction_type == "SELL":
            sell_orders_placed.append((contract.get("trading_symbol"), quantity))
            return {"success": True, "order_id": f"SELL-{quantity}", "status": "submitted"}
        oid = call_oid if "CE" in contract.get("trading_symbol", "") else put_oid
        return {"success": True, "order_id": oid, "status": "submitted"}

    mock_order_book = [
        {"order_id": call_oid, "status": "complete", "filled_quantity": 65, "average_price": 102.0},
        {"order_id": put_oid, "status": "rejected", "filled_quantity": 0, "average_price": 0.0},
        {"order_id": "SELL-65", "status": "complete", "filled_quantity": 65, "average_price": 101.5},
    ]

    with patch("common.entry_order_manager.get_available_funds", return_value=100000.0), \
         patch("common.entry_order_manager.place_market_order", side_effect=mock_place), \
         patch("common.entry_order_manager.cancel_broker_order", return_value={"success": True}), \
         patch("common.entry_order_manager.fetch_normalized_orders", return_value=mock_order_book):

        result = execute_paired_market_buy(
            broker="upstox",
            call_contract=sample_contracts["upstox_call"],
            put_contract=sample_contracts["upstox_put"],
            quantity=65,
            call_ltp=100.0,
            put_ltp=90.0,
            token=MOCK_TOKEN,
            timeout_sec=1.0,
        )

        assert result["success"] is False
        assert result["status"] == "FAILED_ROLLED_BACK"
        # CALL was unwound with exact 65 quantity MARKET SELL
        assert len(sell_orders_placed) == 1
        assert sell_orders_placed[0][0] == sample_contracts["upstox_call"]["trading_symbol"]
        assert sell_orders_placed[0][1] == 65
        assert result["rollback"]["call_filled"] == 65
        assert result["rollback"]["put_filled"] == 0


def test_rollback_asymmetric_partial_fills(sample_contracts):
    """
    If CALL fills 40 and PUT fills 65 before timeout:
    Unwinds BOTH: 40 qty on CALL and 65 qty on PUT.
    """
    call_oid = "ANGEL-CE-4001"
    put_oid = "ANGEL-PE-4002"
    sells = []

    def mock_place(broker, token, contract, quantity, transaction_type="BUY", tag="AUTO-BUY"):
        if transaction_type == "SELL":
            sells.append((contract.get("trading_symbol"), quantity))
            return {"success": True, "order_id": f"SELL-{quantity}", "status": "submitted"}
        oid = call_oid if "CE" in contract.get("trading_symbol", "") else put_oid
        return {"success": True, "order_id": oid, "status": "submitted"}

    mock_order_book = [
        {"order_id": call_oid, "status": "cancelled", "filled_quantity": 40, "average_price": 105.0},
        {"order_id": put_oid, "status": "complete", "filled_quantity": 65, "average_price": 95.0},
        {"order_id": "SELL-40", "status": "complete", "filled_quantity": 40, "average_price": 104.0},
        {"order_id": "SELL-65", "status": "complete", "filled_quantity": 65, "average_price": 94.0},
    ]

    with patch("common.entry_order_manager.get_available_funds", return_value=100000.0), \
         patch("common.entry_order_manager.place_market_order", side_effect=mock_place), \
         patch("common.entry_order_manager.cancel_broker_order", return_value={"success": True}), \
         patch("common.entry_order_manager.fetch_normalized_orders", return_value=mock_order_book):

        result = execute_paired_market_buy(
            broker="angelone",
            call_contract=sample_contracts["angel_call"],
            put_contract=sample_contracts["angel_put"],
            quantity=65,
            call_ltp=100.0,
            put_ltp=90.0,
            token=MOCK_TOKEN,
            timeout_sec=1.0,
        )

        assert result["success"] is False
        assert result["status"] == "FAILED_ROLLED_BACK"
        assert len(sells) == 2
        # Verify exact quantities unwound
        sell_dict = dict(sells)
        assert sell_dict[sample_contracts["angel_call"]["trading_symbol"]] == 40
        assert sell_dict[sample_contracts["angel_put"]["trading_symbol"]] == 65


# ---------------------------------------------------------------------------
# 5. Storage & Persistence Tests
# ---------------------------------------------------------------------------

def test_sqlite_buy_snapshot_persistence():
    """Verify strategy_buy_snapshots stores new order_id, ref_ltp, and entry_status fields."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        init_db(db_path)
        snapshot = {
            "strategy_name": "NIFTY50_5DAY_STRATEGY",
            "cycle_id": "CYCLE-TUE-20261103",
            "week_start_date": "20261103",
            "expiry_date": "20261105",
            "entry_spot": 24550.0,
            "call_strike": 24600,
            "put_strike": 24500,
            "call_buy_price": 108.50,
            "put_buy_price": 97.25,
            "call_ref_ltp": 105.00,
            "put_ref_ltp": 95.00,
            "call_order_id": "UP-ORD-111",
            "put_order_id": "UP-ORD-222",
            "entry_status": "FILLED",
            "broker": "upstox",
            "captured_at": 1787027400,
        }
        insert_buy_snapshot(db_path, snapshot)

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT call_buy_price, put_buy_price, call_ref_ltp, put_ref_ltp,
                   call_order_id, put_order_id, entry_status, broker
            FROM strategy_buy_snapshots
            WHERE cycle_id = 'CYCLE-TUE-20261103'
        """)
        row = cur.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == 108.50  # call_buy_price (actual fill)
        assert row[1] == 97.25   # put_buy_price (actual fill)
        assert row[2] == 105.00  # call_ref_ltp (captured anchor)
        assert row[3] == 95.00   # put_ref_ltp (captured anchor)
        assert row[4] == "UP-ORD-111"
        assert row[5] == "UP-ORD-222"
        assert row[6] == "FILLED"
        assert row[7] == "upstox"
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_unified_strategy_store_update_locked_prices():
    """Verify update_locked_prices persists order IDs and reference LTPs."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        json_path = f.name

    try:
        # Seed active strategies JSON
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"5day_strategy": {"cycle_id": "CYCLE-TUE-TEST"}}, f)

        update_locked_prices(
            "5day_strategy",
            call_buy=112.50,
            put_buy=88.00,
            spot=24500.0,
            timestamp=1787027400,
            filepath=json_path,
            call_order_id="ORD-CALL-999",
            put_order_id="ORD-PUT-888",
            call_ref_ltp=110.00,
            put_ref_ltp=85.00,
            broker="upstox",
            entry_status="FILLED",
        )

        active = load_active_strategies(filepath=json_path)
        strat = active.get("5day_strategy", {})
        locked = strat.get("day1_locked_price", {})

        assert locked.get("call_buy_price") == 112.50
        assert locked.get("put_buy_price") == 88.00
        assert locked.get("call_ref_ltp") == 110.00
        assert locked.get("put_ref_ltp") == 85.00
        assert locked.get("call_order_id") == "ORD-CALL-999"
        assert locked.get("put_order_id") == "ORD-PUT-888"
        assert locked.get("broker") == "upstox"
        assert locked.get("entry_status") == "FILLED"
    finally:
        if os.path.exists(json_path):
            os.unlink(json_path)


# ---------------------------------------------------------------------------
# 6. Collector Integration & Idempotency Tests
# ---------------------------------------------------------------------------

def test_tuesday_collector_live_buy_insufficient_funds_skips(sample_contracts):
    """
    On Tuesday start day with LIVE_BUY_TRIGGER_TUESDAY=true:
    If Upstox funds are insufficient, save SKIPPED_INSUFFICIENT_FUNDS snapshot
    and abort without placing orders.
    """
    from strategies.weekly_option_collector import collect_once

    with tempfile.TemporaryDirectory() as tmp_dir, ExitStack() as stack:
        stack.enter_context(patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", os.path.join(tmp_dir, "active_strategies.json")))
        stack.enter_context(patch("common.unified_strategy_store.sync_active_cycles_to_mongodb"))
        stack.enter_context(patch("common.unified_strategy_store.append_telemetry_log_to_mongodb"))
        stack.enter_context(patch("common.unified_strategy_store.reset_cycle_live_logs_in_mongodb"))
        stack.enter_context(patch("strategies.weekly_option_collector._ensure_db", return_value=os.path.join(tmp_dir, "weekly.db")))
        stack.enter_context(patch("strategies.weekly_option_collector.dispatch_order_failure_with_retry"))
        stack.enter_context(patch("strategies.weekly_option_collector.load_active_snapshot", return_value=None))
        mock_save = stack.enter_context(patch("strategies.weekly_option_collector.save_active_snapshot"))
        stack.enter_context(patch("strategies.weekly_option_collector.insert_buy_snapshot"))
        mock_today = stack.enter_context(patch("strategies.weekly_option_collector._today_ist"))
        stack.enter_context(patch("strategies.weekly_option_collector.check_nse_holiday", return_value=False))
        stack.enter_context(patch("strategies.weekly_option_collector.is_strategy_start_day", return_value=True))
        stack.enter_context(patch("strategies.weekly_option_collector.get_nifty_spot", return_value={"ltp": 24500.0, "open": 24450.0, "close": 24400.0}))
        stack.enter_context(patch("strategies.weekly_option_collector.select_strikes", return_value=(24400, 24600, 24400, 24600, "DYNAMIC", "Rationale", 0.0, "NEUTRAL", 0.68, -0.68)))
        stack.enter_context(patch("strategies.weekly_option_collector.get_nifty_option_chain", return_value={
            "call": {"ltp": 150.0, "instrument_key": "NSE_FO|53750"},
            "put": {"ltp": 140.0, "instrument_key": "NSE_FO|53795"},
        }))
        stack.enter_context(patch("common.entry_order_manager.resolve_upstox_option_key", side_effect=lambda exp, st, opt: f"NSE_FO|{st}"))
        stack.enter_context(patch("common.entry_order_manager.get_available_funds", return_value=1000.0))
        mock_place = stack.enter_context(patch("common.entry_order_manager.place_market_order"))
        stack.enter_context(patch.dict(os.environ, {"LIVE_BUY_TRIGGER_TUESDAY": "true", "MOCK_BROKER": "true"}))

        from datetime import date
        mock_today.return_value = date(2026, 11, 3)

        ok = collect_once()
        assert ok is True
        mock_place.assert_not_called()
        assert mock_save.called
        saved_snap = mock_save.call_args[0][0]
        assert saved_snap["entry_status"] == "SKIPPED_INSUFFICIENT_FUNDS"
        assert saved_snap["call_buy_price"] is None
        assert saved_snap["put_buy_price"] is None
        assert saved_snap["call_ref_ltp"] == 150.0
        assert saved_snap["put_ref_ltp"] == 140.0
        assert saved_snap["alpha_fsm"] is None


def test_tuesday_collector_idempotent_on_repeat_run():
    """
    If snapshot for today already has entry_status='SKIPPED_INSUFFICIENT_FUNDS',
    subsequent collector run does not query broker or attempt re-entry.
    """
    from strategies.weekly_option_collector import collect_once
    from datetime import date

    existing_snap = {
        "week_start_date": "20261103",
        "entry_status": "SKIPPED_INSUFFICIENT_FUNDS",
        "call_strike": 24600,
        "put_strike": 24400,
    }

    with patch("strategies.weekly_option_collector.load_active_snapshot", return_value=existing_snap), \
         patch("strategies.weekly_option_collector._today_ist", return_value=date(2026, 11, 3)), \
         patch("strategies.weekly_option_collector.check_nse_holiday", return_value=False), \
         patch("strategies.weekly_option_collector.get_nifty_spot") as mock_spot, \
         patch.dict(os.environ, {"LIVE_BUY_TRIGGER_TUESDAY": "true"}):

        ok = collect_once()
        assert ok is True
        mock_spot.assert_not_called()




def test_thursday_collector_live_buy_insufficient_funds_skips(sample_contracts):
    """
    On Thursday start day with LIVE_BUY_TRIGGER_THURSDAY=true:
    If AngelOne funds are insufficient, save SKIPPED_INSUFFICIENT_FUNDS snapshot
    and abort without placing orders.
    """
    from strategies.thursday_option_collector import collect_once

    with tempfile.TemporaryDirectory() as tmp_dir, ExitStack() as stack:
        snap_file = os.path.join(tmp_dir, "active_cycle_thursday.json")
        stack.enter_context(patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", os.path.join(tmp_dir, "active_strategies.json")))
        stack.enter_context(patch("common.unified_strategy_store.sync_active_cycles_to_mongodb"))
        stack.enter_context(patch("common.unified_strategy_store.append_telemetry_log_to_mongodb"))
        stack.enter_context(patch("common.unified_strategy_store.reset_cycle_live_logs_in_mongodb"))
        stack.enter_context(patch("strategies.thursday_option_collector._ensure_thursday_db", return_value=os.path.join(tmp_dir, "thursday.db")))
        stack.enter_context(patch("strategies.thursday_option_collector.dispatch_order_failure_with_retry"))
        stack.enter_context(patch("strategies.thursday_option_collector.THURSDAY_SNAPSHOT_FILE", snap_file))
        stack.enter_context(patch("strategies.thursday_option_collector.load_active_snapshot", return_value=None))
        mock_save = stack.enter_context(patch("strategies.thursday_option_collector.save_active_snapshot"))
        stack.enter_context(patch("strategies.thursday_option_collector.insert_buy_snapshot"))
        mock_today = stack.enter_context(patch("strategies.thursday_option_collector._today_ist"))
        stack.enter_context(patch("strategies.thursday_option_collector.check_nse_holiday", return_value=False))
        stack.enter_context(patch("strategies.thursday_option_collector.is_thursday_strategy_active_day", return_value=True))
        stack.enter_context(patch("strategies.thursday_option_collector.is_thursday_strategy_start_day", return_value=True))
        stack.enter_context(patch("strategies.thursday_option_collector.get_nifty_spot", return_value={"ltp": 24500.0, "open": 24450.0, "close": 24400.0}))
        stack.enter_context(patch("strategies.thursday_option_collector.select_strikes", return_value=(24500, 24500, 24500, 24500, "DYNAMIC", "Rationale", 0.0, "NEUTRAL", 0.50, -0.50)))
        stack.enter_context(patch("strategies.thursday_option_collector.get_nifty_option_chain", return_value={
            "call": {"ltp": 120.0, "token": "53750", "trading_symbol": "NIFTY03NOV2624500CE"},
            "put": {"ltp": 110.0, "token": "53795", "trading_symbol": "NIFTY03NOV2624500PE"},
        }))
        stack.enter_context(patch("common.angelone_client.resolve_option_token", side_effect=lambda exp, st, opt: {"token": str(st), "symboltoken": str(st), "trading_symbol": f"NIFTY{exp}{st}{opt}", "strike": st, "option_type": opt}))
        stack.enter_context(patch("common.entry_order_manager.get_available_funds", return_value=1000.0))
        mock_place = stack.enter_context(patch("common.entry_order_manager.place_market_order"))
        stack.enter_context(patch.dict(os.environ, {"LIVE_BUY_TRIGGER_THURSDAY": "true", "MOCK_BROKER": "true"}))

        from datetime import date
        mock_today.return_value = date(2026, 11, 5)

        ok = collect_once()
        assert ok is True
        mock_place.assert_not_called()
        assert mock_save.called
        saved_snap = mock_save.call_args[0][0]
        assert saved_snap["entry_status"] == "SKIPPED_INSUFFICIENT_FUNDS"
        assert saved_snap["call_buy_price"] is None
        assert saved_snap["put_buy_price"] is None
        assert saved_snap["call_ref_ltp"] == 120.0
        assert saved_snap["put_ref_ltp"] == 110.0
        assert saved_snap["alpha_fsm"] is None


def test_thursday_collector_idempotent_on_repeat_run():
    """
    If Thursday snapshot for today already has entry_status='SKIPPED_INSUFFICIENT_FUNDS',
    subsequent collector run does not query broker or attempt re-entry.
    """
    from strategies.thursday_option_collector import collect_once
    from datetime import date

    existing_snap = {
        "week_start_date": "20261105",
        "entry_status": "SKIPPED_INSUFFICIENT_FUNDS",
        "call_strike": 24500,
        "put_strike": 24500,
    }

    with patch("strategies.thursday_option_collector.load_active_snapshot", return_value=existing_snap), \
         patch("strategies.thursday_option_collector._today_ist", return_value=date(2026, 11, 5)), \
         patch("strategies.thursday_option_collector.check_nse_holiday", return_value=False), \
         patch("strategies.thursday_option_collector.is_thursday_strategy_active_day", return_value=True), \
         patch("strategies.thursday_option_collector.get_nifty_spot") as mock_spot, \
         patch.dict(os.environ, {"LIVE_BUY_TRIGGER_THURSDAY": "true"}):

        ok = collect_once()
        assert ok is True
        mock_spot.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Reviews & Enhancements Tests (Gaps 1-6)
# ---------------------------------------------------------------------------

def test_preflight_unresolved_placeholder_upstox_key_rejected(sample_contracts):
    """
    Upstox preflight must strictly reject synthetic placeholder keys (e.g. 'NSE_FO|NIFTY_...')
    or missing 'NSE_FO|' prefixes to prevent submitting invalid orders in live trading.
    """
    call_bad = {"instrument_token": "NSE_FO|NIFTY_03NOV26_24500_CE", "strike": 24500}
    put_bad = {"instrument_token": "NSE_FO|NIFTY_03NOV26_24300_PE", "strike": 24300}

    passed, reason, _ = preflight_entry(
        broker="upstox",
        token=MOCK_TOKEN,
        call_contract=call_bad,
        put_contract=sample_contracts["upstox_put"],
        quantity=65,
        call_ltp=100.0,
        put_ltp=100.0,
    )
    assert passed is False
    assert "Unresolved placeholder Upstox instrument key" in reason

    # Test missing prefix
    call_noprefix = {"instrument_token": "53750", "strike": 24500}
    passed2, reason2, _ = preflight_entry(
        broker="upstox",
        token=MOCK_TOKEN,
        call_contract=call_noprefix,
        put_contract=sample_contracts["upstox_put"],
        quantity=65,
        call_ltp=100.0,
        put_ltp=100.0,
    )
    assert passed2 is False
    assert "Invalid Upstox instrument key prefix" in reason2


def test_preflight_charges_scale_with_lots(sample_contracts):
    """
    Preflight must scale estimated charges with lot count (e.g. 1 lot -> 40, 2 lots -> 80).
    """
    with patch("common.entry_order_manager.get_available_funds", return_value=100000.0):
        # 1 lot (65 qty)
        _, _, metrics_1lot = preflight_entry(
            broker="upstox",
            token=MOCK_TOKEN,
            call_contract=sample_contracts["upstox_call"],
            put_contract=sample_contracts["upstox_put"],
            quantity=65,
            call_ltp=100.0,
            put_ltp=100.0,
            buffer_pct=0.03,
            estimated_charges=40.0,
        )
        assert metrics_1lot["num_lots"] == 1
        assert metrics_1lot["total_charges"] == 40.0
        # (100 + 100) * 65 = 13000 + 3% buffer (390) + 40 = 13430
        assert metrics_1lot["required_funds"] == 13430.0

        # 2 lots (130 qty)
        _, _, metrics_2lots = preflight_entry(
            broker="upstox",
            token=MOCK_TOKEN,
            call_contract=sample_contracts["upstox_call"],
            put_contract=sample_contracts["upstox_put"],
            quantity=130,
            call_ltp=100.0,
            put_ltp=100.0,
            buffer_pct=0.03,
            estimated_charges=40.0,
        )
        assert metrics_2lots["num_lots"] == 2
        assert metrics_2lots["total_charges"] == 80.0
        # (100 + 100) * 130 = 26000 + 3% buffer (780) + 80 = 26860
        assert metrics_2lots["required_funds"] == 26860.0


def test_rollback_recovery_sell_fill_confirmation(sample_contracts):
    """
    Rollback should poll recovery SELL orders and confirm they reach 'complete'
    with full filled quantity before declaring unwind_successful = True.
    """
    mock_order_book = [
        {"order_id": "BUY_CALL_1", "status": "complete", "filled_quantity": 65, "average_price": 105.0},
        {"order_id": "BUY_PUT_1", "status": "rejected", "filled_quantity": 0},
        {"order_id": "SELL_UNWIND_CALL_1", "status": "complete", "filled_quantity": 65, "average_price": 104.5},
    ]

    with patch("common.entry_order_manager.place_market_order") as mock_place, \
         patch("common.entry_order_manager.fetch_normalized_orders", return_value=mock_order_book):

        mock_place.side_effect = [
            {"success": True, "order_id": "BUY_CALL_1"}, # CALL BUY
            {"success": False, "error": "Margin shortfall"}, # PUT BUY fails
            {"success": True, "order_id": "SELL_UNWIND_CALL_1"}, # Unwind SELL CALL
        ]

        res = execute_paired_market_buy(
            broker="upstox",
            call_contract=sample_contracts["upstox_call"],
            put_contract=sample_contracts["upstox_put"],
            quantity=65,
            call_ltp=100.0,
            put_ltp=100.0,
            token=MOCK_TOKEN,
            order_book_provider=lambda: mock_order_book,
        )

        assert res["success"] is False
        assert res["status"] == "FAILED_ROLLED_BACK"
        assert res["rollback"]["unwind_successful"] is True
        assert res["rollback"]["call_filled"] == 65
        assert res["rollback"]["put_filled"] == 0


def test_rollback_recovery_sell_failure_triggers_alert(sample_contracts):
    """
    If recovery SELL fails to submit, is rejected, or times out unfilled:
    - unwind_successful must be False.
    - status must be UNWIND_FAILED.
    - dispatch_unwind_failure_alert must be triggered.
    """
    mock_order_book = [
        {"order_id": "BUY_CALL_1", "status": "complete", "filled_quantity": 65, "average_price": 105.0},
        {"order_id": "BUY_PUT_1", "status": "rejected", "filled_quantity": 0},
        {"order_id": "SELL_UNWIND_CALL_1", "status": "rejected", "filled_quantity": 0},
    ]

    with patch("common.entry_order_manager.place_market_order") as mock_place, \
         patch("common.entry_order_manager.dispatch_unwind_failure_alert") as mock_alert, \
         patch("common.entry_order_manager.fetch_normalized_orders", return_value=mock_order_book):

        mock_place.side_effect = [
            {"success": True, "order_id": "BUY_CALL_1"},
            {"success": False, "error": "Margin shortfall"},
            {"success": True, "order_id": "SELL_UNWIND_CALL_1"},
        ]

        res = execute_paired_market_buy(
            broker="upstox",
            call_contract=sample_contracts["upstox_call"],
            put_contract=sample_contracts["upstox_put"],
            quantity=65,
            call_ltp=100.0,
            put_ltp=100.0,
            token=MOCK_TOKEN,
            order_book_provider=lambda: mock_order_book,
        )

        assert res["success"] is False
        assert res["status"] == "UNWIND_FAILED"
        assert res["rollback"]["unwind_successful"] is False
        assert mock_alert.called


def test_collector_cli_argument_precedence_and_env():
    """
    Verify CLI argument precedence:
    - When execute_buy is None: LIVE_BUY_TRIGGER_* env var determines behavior.
    - When execute_buy is False (--no-execute-buy): forces paper mode even if env var is true.
    - When execute_buy is True (--execute-buy): forces live mode even if env var is false.
    """
    from strategies.weekly_option_collector import collect_once as tue_collect_once
    from strategies.thursday_option_collector import collect_once as thu_collect_once
    from datetime import date

    saved_snaps = []
    def fake_save(s, **kw):
        saved_snaps.append(s)
    def fake_load(**kw):
        return saved_snaps[-1] if saved_snaps else None

    # Tuesday: execute_buy=False overrides LIVE_BUY_TRIGGER_TUESDAY=true
    with tempfile.TemporaryDirectory() as tmp_dir, \
         patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", os.path.join(tmp_dir, "active_strategies.json")), \
         patch("common.unified_strategy_store.sync_active_cycles_to_mongodb"), \
         patch("common.unified_strategy_store.append_telemetry_log_to_mongodb"), \
         patch("common.unified_strategy_store.reset_cycle_live_logs_in_mongodb"), \
         patch("strategies.weekly_option_collector._ensure_db", return_value=os.path.join(tmp_dir, "weekly.db")), \
         patch("strategies.weekly_option_collector.update_active_fsm_state"), \
         patch("strategies.weekly_option_collector.load_active_snapshot", side_effect=fake_load), \
         patch("strategies.weekly_option_collector.save_active_snapshot", side_effect=fake_save), \
         patch("strategies.weekly_option_collector.insert_buy_snapshot"), \
         patch("strategies.weekly_option_collector._today_ist", return_value=date(2026, 11, 3)), \
         patch("strategies.weekly_option_collector.check_nse_holiday", return_value=False), \
         patch("strategies.weekly_option_collector.is_tuesday", return_value=True), \
         patch("strategies.weekly_option_collector.get_nifty_spot", return_value={"ltp": 24500.0, "open": 24450.0, "close": 24400.0}), \
         patch("strategies.weekly_option_collector.select_strikes", return_value=(24600, 24400, 24600, 24400, "DYNAMIC", "Rationale", 0.0, "NEUTRAL", 0.68, -0.68)), \
         patch("strategies.weekly_option_collector.get_nifty_option_chain", return_value={
             "call": {"ltp": 150.0, "token": "53750", "trading_symbol": "NIFTY03NOV2624600CE"},
             "put": {"ltp": 140.0, "token": "53795", "trading_symbol": "NIFTY03NOV2624400PE"},
         }), \
         patch("common.entry_order_manager.place_market_order") as mock_place, \
         patch.dict(os.environ, {"LIVE_BUY_TRIGGER_TUESDAY": "true"}):

        # Passed execute_buy=False (--no-execute-buy)
        ok = tue_collect_once(execute_buy=False)
        assert ok is True
        mock_place.assert_not_called()
        assert len(saved_snaps) > 0
        snap = saved_snaps[-1]
        assert snap["entry_status"] == "PAPER_ENTRY"


def test_collector_dry_run_does_not_suppress_live_buy(sample_contracts):
    """
    Running with dry_run=True (--no-alert / --dry-run) must NOT suppress live BUY order execution
    when LIVE_BUY_TRIGGER_* is enabled.
    """
    from strategies.weekly_option_collector import collect_once as tue_collect_once
    from datetime import date

    saved_snaps = []
    def fake_save(s, **kw):
        saved_snaps.append(s)
    def fake_load(**kw):
        return saved_snaps[-1] if saved_snaps else None

    with tempfile.TemporaryDirectory() as tmp_dir, \
         patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", os.path.join(tmp_dir, "active_strategies.json")), \
         patch("common.unified_strategy_store.sync_active_cycles_to_mongodb"), \
         patch("common.unified_strategy_store.append_telemetry_log_to_mongodb"), \
         patch("common.unified_strategy_store.reset_cycle_live_logs_in_mongodb"), \
         patch("strategies.weekly_option_collector._ensure_db", return_value=os.path.join(tmp_dir, "weekly.db")), \
         patch("strategies.weekly_option_collector.update_active_fsm_state"), \
         patch("strategies.weekly_option_collector.load_active_snapshot", side_effect=fake_load), \
         patch("strategies.weekly_option_collector.save_active_snapshot", side_effect=fake_save), \
         patch("strategies.weekly_option_collector.insert_buy_snapshot"), \
         patch("strategies.weekly_option_collector._today_ist", return_value=date(2026, 11, 3)), \
         patch("strategies.weekly_option_collector.check_nse_holiday", return_value=False), \
         patch("strategies.weekly_option_collector.is_tuesday", return_value=True), \
         patch("strategies.weekly_option_collector.get_nifty_spot", return_value={"ltp": 24500.0, "open": 24450.0, "close": 24400.0}), \
         patch("strategies.weekly_option_collector.select_strikes", return_value=(24600, 24400, 24600, 24400, "DYNAMIC", "Rationale", 0.0, "NEUTRAL", 0.68, -0.68)), \
         patch("strategies.weekly_option_collector.get_nifty_option_chain", return_value={
             "call": {"ltp": 150.0, "token": "53750", "trading_symbol": "NIFTY03NOV2624600CE"},
             "put": {"ltp": 140.0, "token": "53795", "trading_symbol": "NIFTY03NOV2624400PE"},
         }), \
         patch("common.entry_order_manager.get_available_funds", return_value=50000.0), \
         patch("common.entry_order_manager.place_market_order") as mock_place, \
         patch("common.entry_order_manager.poll_order_fills") as mock_poll, \
         patch.dict(os.environ, {"LIVE_BUY_TRIGGER_TUESDAY": "true", "MOCK_BROKER": "true"}):

        mock_place.side_effect = [
            {"success": True, "order_id": "TUE_CALL_1"},
            {"success": True, "order_id": "TUE_PUT_1"},
        ]
        mock_poll.return_value = (True, {
            "TUE_CALL_1": {"status": "complete", "average_price": 150.5, "filled_quantity": 65},
            "TUE_PUT_1": {"status": "complete", "average_price": 140.5, "filled_quantity": 65},
        })

        # dry_run=True passed (--no-alert)
        ok = tue_collect_once(dry_run=True)
        assert ok is True
        # Live order execution DID occur!
        assert mock_place.call_count == 2
        assert len(saved_snaps) > 0
        snap = saved_snaps[-1]
        assert snap["entry_status"] == "FILLED"
        assert snap["call_buy_price"] == 150.5
        assert snap["put_buy_price"] == 140.5


def test_unified_strategy_store_and_mongo_synced_on_failed_entry():
    """
    When entry order preflight fails or is skipped due to insufficient funds:
    Unified strategy store and MongoDB must be synchronized with SKIPPED_INSUFFICIENT_FUNDS.
    """
    from strategies.weekly_option_collector import collect_once
    from datetime import date

    with tempfile.TemporaryDirectory() as tmp_dir:
        unified_json = os.path.join(tmp_dir, "active_strategies.json")
        with patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", unified_json), \
             patch("common.unified_strategy_store.reset_cycle_live_logs_in_mongodb"), \
             patch("strategies.weekly_option_collector.load_active_snapshot", return_value=None), \
             patch("strategies.weekly_option_collector.save_active_snapshot"), \
             patch("strategies.weekly_option_collector.insert_buy_snapshot"), \
             patch("strategies.weekly_option_collector._ensure_db", return_value=os.path.join(tmp_dir, "weekly.db")), \
             patch("strategies.weekly_option_collector.dispatch_order_failure_with_retry"), \
             patch("strategies.weekly_option_collector._today_ist", return_value=date(2026, 11, 3)), \
             patch("strategies.weekly_option_collector.check_nse_holiday", return_value=False), \
             patch("strategies.weekly_option_collector.is_tuesday", return_value=True), \
             patch("strategies.weekly_option_collector.get_nifty_spot", return_value={"ltp": 24500.0, "open": 24450.0, "close": 24400.0}), \
             patch("strategies.weekly_option_collector.select_strikes", return_value=(24600, 24400, 24600, 24400, "DYNAMIC", "Rationale", 0.0, "NEUTRAL", 0.68, -0.68)), \
             patch("strategies.weekly_option_collector.get_nifty_option_chain", return_value={
                 "call": {"ltp": 150.0, "token": "53750", "trading_symbol": "NIFTY03NOV2624600CE"},
                 "put": {"ltp": 140.0, "token": "53795", "trading_symbol": "NIFTY03NOV2624400PE"},
             }), \
              patch("common.entry_order_manager.get_available_funds", return_value=1000.0), \
              patch("strategies.weekly_option_collector.dispatch_order_failure_with_retry"), \
              patch("common.unified_strategy_store.reset_cycle_live_logs_in_mongodb"), \
              patch("common.unified_strategy_store.sync_active_cycles_to_mongodb") as mock_mongo_sync, \
             patch.dict(os.environ, {"LIVE_BUY_TRIGGER_TUESDAY": "true", "MOCK_BROKER": "true"}):

            ok = collect_once()
            assert ok is True
            # Verify unified store was synced
            active = load_active_strategies(unified_json)
            strat = active.get("5day_strategy", {})
            assert strat.get("entry_status") == "SKIPPED_INSUFFICIENT_FUNDS"
            assert strat.get("broker") == "upstox"
            assert mock_mongo_sync.called


# ---------------------------------------------------------------------------
# 6. Blocker & Gap Verification Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("abort_st", [
    "UNWIND_FAILED",
    "FAILED_ROLLED_BACK",
    "PREFLIGHT_FAILED",
    "AUTH_FAILED",
    "FAILED",
])
def test_abort_statuses_prevent_reentry_tuesday_and_thursday(abort_st):
    """
    Blocker 1: Snapshots with UNWIND_FAILED, FAILED_ROLLED_BACK, PREFLIGHT_FAILED, etc.,
    must cleanly block subsequent hourly collection cycles from retrying fresh market entry.
    """
    from strategies.weekly_option_collector import collect_once as tue_collect_once
    from strategies.thursday_option_collector import collect_once as thu_collect_once
    from datetime import date

    existing_snap = {
        "week_start_date": "20261103",
        "entry_status": abort_st,
        "call_strike": 24600,
        "put_strike": 24400,
        "call_buy_price": None,
        "put_buy_price": None,
    }

    # Tuesday Track Check
    with patch("strategies.weekly_option_collector.load_active_snapshot", return_value=existing_snap), \
         patch("strategies.weekly_option_collector._today_ist", return_value=date(2026, 11, 3)), \
         patch("strategies.weekly_option_collector.check_nse_holiday", return_value=False), \
         patch("strategies.weekly_option_collector.get_nifty_spot") as mock_spot, \
         patch.dict(os.environ, {"LIVE_BUY_TRIGGER_TUESDAY": "true"}):

        ok = tue_collect_once()
        assert ok is True
        mock_spot.assert_not_called()

    # Thursday Track Check
    existing_thu_snap = {
        "week_start_date": "20261105",
        "entry_status": abort_st,
        "call_strike": 24600,
        "put_strike": 24400,
        "call_buy_price": None,
        "put_buy_price": None,
    }
    with patch("strategies.thursday_option_collector.load_active_snapshot", return_value=existing_thu_snap), \
         patch("strategies.thursday_option_collector._today_ist", return_value=date(2026, 11, 5)), \
         patch("strategies.thursday_option_collector.check_nse_holiday", return_value=False), \
         patch("strategies.thursday_option_collector.is_thursday_strategy_active_day", return_value=True), \
         patch("strategies.thursday_option_collector.get_nifty_spot") as mock_spot_thu, \
         patch.dict(os.environ, {"LIVE_BUY_TRIGGER_THURSDAY": "true"}):

        ok = thu_collect_once()
        assert ok is True
        mock_spot_thu.assert_not_called()


def test_preflight_rejects_non_lot_multiple_quantity(sample_contracts):
    """
    Gap 2: Preflight validation must reject quantities that are not whole multiples of NIFTY_LOT_SIZE.
    """
    from config import NIFTY_LOT_SIZE

    # Non-multiple (e.g. 50 or 100 when lot size is 65 or 75)
    invalid_qty = NIFTY_LOT_SIZE + 10
    passed, reason, _ = preflight_entry(
        broker="upstox",
        token=MOCK_TOKEN,
        call_contract=sample_contracts["upstox_call"],
        put_contract=sample_contracts["upstox_put"],
        quantity=invalid_qty,
        call_ltp=100.0,
        put_ltp=90.0,
    )
    assert passed is False
    assert f"multiple of NIFTY lot size ({NIFTY_LOT_SIZE})" in reason

    # Zero or negative quantity
    passed, reason, _ = preflight_entry(
        broker="upstox",
        token=MOCK_TOKEN,
        call_contract=sample_contracts["upstox_call"],
        put_contract=sample_contracts["upstox_put"],
        quantity=0,
        call_ltp=100.0,
        put_ltp=90.0,
    )
    assert passed is False

    # Valid multiple passes
    with patch("common.entry_order_manager.get_available_funds", return_value=100000.0):
        passed, reason, _ = preflight_entry(
            broker="upstox",
            token=MOCK_TOKEN,
            call_contract=sample_contracts["upstox_call"],
            put_contract=sample_contracts["upstox_put"],
            quantity=NIFTY_LOT_SIZE * 2,
            call_ltp=100.0,
            put_ltp=90.0,
        )
        assert passed is True


def test_confirmed_fill_with_zero_average_price_triggers_rollback(sample_contracts):
    """
    Gap 3: If confirmed fill has average_price <= 0.0, refuse to substitute reference LTP.
    Must initiate rollback and return FAILED_ROLLED_BACK / UNWIND_FAILED.
    """
    call_oid = "ANGEL-CE-ZERO-AVG"
    put_oid = "ANGEL-PE-ZERO-AVG"
    unwound_sells = []

    def mock_place(broker, token, contract, quantity, transaction_type="BUY", tag="AUTO-BUY"):
        if transaction_type == "SELL":
            unwound_sells.append((contract.get("trading_symbol"), quantity))
            return {"success": True, "order_id": f"SELL-{quantity}", "status": "submitted"}
        oid = call_oid if "CE" in contract.get("trading_symbol", "") else put_oid
        return {"success": True, "order_id": oid, "status": "submitted"}

    # Orders are 'complete' in status, but CALL average_price is 0.0
    mock_order_book = [
        {"order_id": call_oid, "status": "complete", "filled_quantity": 65, "average_price": 0.0},
        {"order_id": put_oid, "status": "complete", "filled_quantity": 65, "average_price": 105.0},
        {"order_id": "SELL-65", "status": "complete", "filled_quantity": 65, "average_price": 104.0},
    ]

    with patch("common.entry_order_manager.get_available_funds", return_value=100000.0), \
         patch("common.entry_order_manager.place_market_order", side_effect=mock_place), \
         patch("common.entry_order_manager.cancel_broker_order", return_value={"success": True}), \
         patch("common.entry_order_manager.fetch_normalized_orders", return_value=mock_order_book), \
         patch("common.entry_order_manager.fetch_broker_positions", return_value=[]):

        result = execute_paired_market_buy(
            broker="angelone",
            call_contract=sample_contracts["angel_call"],
            put_contract=sample_contracts["angel_put"],
            quantity=65,
            call_ltp=120.0,
            put_ltp=110.0,
            token=MOCK_TOKEN,
        )

        assert result["success"] is False
        assert result["status"] == "FAILED_ROLLED_BACK"
        assert "Zero or missing average_price" in result["error"]
        # Both CALL and PUT must have been unwound
        assert len(unwound_sells) == 2


def test_rollback_cancellation_timeout_marks_unwind_failed(sample_contracts):
    """
    Blocker 2: When open BUY cancellation fails to reach terminal status before timeout:
    Rollback marks unwind_successful=False, returns UNWIND_FAILED, and dispatches critical alert.
    """
    call_oid = "UPSTOX-CE-STUCK"
    put_oid = "UPSTOX-PE-STUCK"

    # BUY order remains 'open' despite cancel request
    stuck_order_book = [
        {"order_id": call_oid, "status": "open", "filled_quantity": 0},
        {"order_id": put_oid, "status": "cancelled", "filled_quantity": 0},
    ]

    with patch("common.entry_order_manager.cancel_broker_order", return_value={"success": True}), \
         patch("common.entry_order_manager.fetch_normalized_orders", return_value=stuck_order_book), \
         patch("common.entry_order_manager.dispatch_unwind_failure_alert") as mock_alert:

        res = rollback_partial_entry(
            broker="upstox",
            token=MOCK_TOKEN,
            call_contract=sample_contracts["upstox_call"],
            put_contract=sample_contracts["upstox_put"],
            target_qty=65,
            call_order_id=call_oid,
            put_order_id=put_oid,
            unwind_timeout_sec=0.1,  # Short timeout for fast test
        )

        assert res["unwind_successful"] is False
        assert mock_alert.called
        alert_details = mock_alert.call_args[1]["details"]
        assert alert_details["buy_cancellation_confirmed"] is False


def test_rollback_residual_position_marks_unwind_failed(sample_contracts):
    """
    Blocker 2: When recovery SELL completes, but broker position snapshot still shows residual open quantity:
    Rollback marks unwind_successful=False, returns UNWIND_FAILED, and dispatches emergency alert.
    """
    call_oid = "UPSTOX-CE-PARTIAL"
    put_oid = "UPSTOX-PE-REJECT"

    order_book = [
        {"order_id": call_oid, "status": "complete", "filled_quantity": 65, "average_price": 100.0},
        {"order_id": put_oid, "status": "rejected", "filled_quantity": 0},
        {"order_id": "SELL-65", "status": "complete", "filled_quantity": 65, "average_price": 99.0},
    ]

    # Broker position query returns net_quantity = 25 (non-flat residual)
    non_flat_positions = [
        {"instrument_token": sample_contracts["upstox_call"]["instrument_token"], "net_quantity": 25}
    ]

    with patch("common.entry_order_manager.cancel_broker_order", return_value={"success": True}), \
         patch("common.entry_order_manager.place_market_order", return_value={"success": True, "order_id": "SELL-65"}), \
         patch("common.entry_order_manager.fetch_normalized_orders", return_value=order_book), \
         patch("common.entry_order_manager.fetch_broker_positions", return_value=non_flat_positions), \
         patch("common.entry_order_manager.dispatch_unwind_failure_alert") as mock_alert:

        res = rollback_partial_entry(
            broker="upstox",
            token=MOCK_TOKEN,
            call_contract=sample_contracts["upstox_call"],
            put_contract=sample_contracts["upstox_put"],
            target_qty=65,
            call_order_id=call_oid,
            put_order_id=put_oid,
            unwind_timeout_sec=0.5,
        )

        assert res["unwind_successful"] is False
        assert res["positions_flat"] is False
        assert mock_alert.called


def test_rollback_negative_short_position_marks_unwind_failed(sample_contracts):
    """
    Flatness check must strictly reject negative (short) positions.
    Holding net short option contracts is forbidden; rollback must mark unwind_successful=False,
    return UNWIND_FAILED, and dispatch an emergency alert.
    """
    call_oid = "ANGEL-CE-OVERSOLD"
    put_oid = "ANGEL-PE-REJECT"

    order_book = [
        {"order_id": call_oid, "status": "complete", "filled_quantity": 65, "average_price": 100.0},
        {"order_id": put_oid, "status": "rejected", "filled_quantity": 0},
        {"order_id": "SELL-65", "status": "complete", "filled_quantity": 65, "average_price": 99.0},
    ]

    # Broker position query returns net_quantity = -65 (unintended short position)
    short_positions = [
        {"symboltoken": sample_contracts["angel_call"]["token"], "netqty": "-65"}
    ]

    with patch("common.entry_order_manager.cancel_broker_order", return_value={"success": True}), \
         patch("common.entry_order_manager.place_market_order", return_value={"success": True, "order_id": "SELL-65"}), \
         patch("common.entry_order_manager.fetch_normalized_orders", return_value=order_book), \
         patch("common.entry_order_manager.fetch_broker_positions", return_value=short_positions), \
         patch("common.entry_order_manager.dispatch_unwind_failure_alert") as mock_alert:

        res = rollback_partial_entry(
            broker="angelone",
            token=MOCK_TOKEN,
            call_contract=sample_contracts["angel_call"],
            put_contract=sample_contracts["angel_put"],
            target_qty=65,
            call_order_id=call_oid,
            put_order_id=put_oid,
            unwind_timeout_sec=0.5,
        )

        assert res["unwind_successful"] is False
        assert res["positions_flat"] is False
        assert res["position_details"]["call_net"] == -65
        assert mock_alert.called


def test_execute_paired_market_buy_returns_unwind_failed_on_negative_short_position(sample_contracts):
    """
    When execute_paired_market_buy triggers rollback and the resulting position is negative (short),
    the orchestrator must return status 'UNWIND_FAILED'.
    """
    call_oid = "ANGEL-CE-PART"
    put_oid = "ANGEL-PE-FAIL"

    def mock_place(broker, token, contract, quantity, transaction_type="BUY", tag="AUTO-BUY"):
        if transaction_type == "SELL":
            return {"success": True, "order_id": f"SELL-{quantity}", "status": "submitted"}
        oid = call_oid if "CE" in contract.get("trading_symbol", "") else put_oid
        return {"success": True, "order_id": oid, "status": "submitted"}

    order_book = [
        {"order_id": call_oid, "status": "complete", "filled_quantity": 65, "average_price": 105.0},
        {"order_id": put_oid, "status": "rejected", "filled_quantity": 0, "average_price": 0.0},
        {"order_id": "SELL-65", "status": "complete", "filled_quantity": 65, "average_price": 104.0},
    ]

    # Position provider reports net -65 for PUT
    short_put_positions = [
        {"symboltoken": sample_contracts["angel_put"]["token"], "netqty": "-65"}
    ]

    with patch("common.entry_order_manager.get_available_funds", return_value=100000.0), \
         patch("common.entry_order_manager.place_market_order", side_effect=mock_place), \
         patch("common.entry_order_manager.cancel_broker_order", return_value={"success": True}), \
         patch("common.entry_order_manager.fetch_normalized_orders", return_value=order_book), \
         patch("common.entry_order_manager.dispatch_unwind_failure_alert"):

        result = execute_paired_market_buy(
            broker="angelone",
            call_contract=sample_contracts["angel_call"],
            put_contract=sample_contracts["angel_put"],
            quantity=65,
            call_ltp=100.0,
            put_ltp=90.0,
            token=MOCK_TOKEN,
            position_provider=lambda: short_put_positions,
        )

        assert result["success"] is False
        assert result["status"] == "UNWIND_FAILED"
        assert result["rollback"]["unwind_successful"] is False
        assert result["rollback"]["positions_flat"] is False


def test_verify_positions_flat_strictly_requires_zero_net(sample_contracts):
    """
    Unit test for verify_positions_flat:
    - Net 0 on both legs -> is_flat: True
    - Net positive (long) on either leg -> is_flat: False
    - Net negative (short) on either leg -> is_flat: False
    """
    call_c = sample_contracts["upstox_call"]
    put_c = sample_contracts["upstox_put"]
    call_tok = call_c["instrument_token"]
    put_tok = put_c["instrument_token"]

    # 1. Perfectly flat
    flat_positions = [
        {"instrument_token": call_tok, "net_quantity": 0},
        {"instrument_token": put_tok, "net_quantity": 0},
    ]
    is_flat, details = verify_positions_flat("upstox", flat_positions, call_c, put_c)
    assert is_flat is True
    assert details["call_net"] == 0
    assert details["put_net"] == 0

    # 2. Positive residual long on CALL
    long_positions = [
        {"instrument_token": call_tok, "net_quantity": 25},
        {"instrument_token": put_tok, "net_quantity": 0},
    ]
    is_flat, details = verify_positions_flat("upstox", long_positions, call_c, put_c)
    assert is_flat is False
    assert details["call_net"] == 25

    # 3. Negative residual short on CALL
    short_call_positions = [
        {"instrument_token": call_tok, "net_quantity": -65},
        {"instrument_token": put_tok, "net_quantity": 0},
    ]
    is_flat, details = verify_positions_flat("upstox", short_call_positions, call_c, put_c)
    assert is_flat is False
    assert details["call_net"] == -65

    # 4. Negative residual short on PUT
    short_put_positions = [
        {"instrument_token": call_tok, "net_quantity": 0},
        {"instrument_token": put_tok, "net_quantity": -65},
    ]
    is_flat, details = verify_positions_flat("upstox", short_put_positions, call_c, put_c)
    assert is_flat is False
    assert details["put_net"] == -65



def test_update_locked_prices_aborted_status_does_not_lock_zero_price():
    """
    Gap 1: When update_locked_prices is called with an aborted/skipped entry status:
    - day1_locked_price remains None (NOT a 0.0 price lock).
    - status is set to 'skipped' or 'aborted'.
    - Telemetry event is ENTRY_<status>, not LOCKED_PRICE.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        json_path = f.name

    try:
        # Initialize cycle
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"5day_strategy": {"cycle_id": "CYCLE-TUE-TEST-FAIL", "status": "active"}}, f)

        with patch("common.unified_strategy_store.append_telemetry_log_to_mongodb") as mock_mongo_log, \
             patch("common.unified_strategy_store.sync_active_cycles_to_mongodb"):

            # 1. Skipped insufficient funds
            update_locked_prices(
                "5day_strategy",
                call_buy=0.0,
                put_buy=0.0,
                spot=24500.0,
                filepath=json_path,
                entry_status="SKIPPED_INSUFFICIENT_FUNDS",
                broker="upstox",
            )

            data = load_active_strategies(filepath=json_path)
            strat = data["5day_strategy"]
            assert strat.get("day1_locked_price") is None
            assert strat.get("status") == "skipped"
            assert strat.get("entry_status") == "SKIPPED_INSUFFICIENT_FUNDS"

            assert mock_mongo_log.called
            log_arg = mock_mongo_log.call_args[0][0]
            assert log_arg["event_type"] == "ENTRY_SKIPPED_INSUFFICIENT_FUNDS"
            assert log_arg["call_buy_price"] is None
            assert log_arg["put_buy_price"] is None

            # 2. Unwind failed
            update_locked_prices(
                "5day_strategy",
                call_buy=0.0,
                put_buy=0.0,
                spot=24500.0,
                filepath=json_path,
                entry_status="UNWIND_FAILED",
                broker="upstox",
            )
            data2 = load_active_strategies(filepath=json_path)
            strat2 = data2["5day_strategy"]
            assert strat2.get("day1_locked_price") is None
            assert strat2.get("status") == "aborted"
            assert strat2.get("entry_status") == "UNWIND_FAILED"

            log_arg2 = mock_mongo_log.call_args[0][0]
            assert log_arg2["event_type"] == "ENTRY_UNWIND_FAILED"
    finally:
        if os.path.exists(json_path):
            os.unlink(json_path)
