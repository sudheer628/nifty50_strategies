import sys
import os
import json
import threading
import time
from unittest.mock import MagicMock, patch
import pytest

# Ensure repo root and scripts/ are importable
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
scripts_dir = os.path.join(repo_root, "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import importlib.util
tue_spec = importlib.util.spec_from_file_location(
    "tuesday_exit_orders",
    os.path.join(scripts_dir, "Tuesday-Fno-Exit-Orders-V1.py")
)
tue_module = importlib.util.module_from_spec(tue_spec)
tue_spec.loader.exec_module(tue_module)


def test_parse_tradingsymbol_formats():
    """Verify symbol regex handles 2-digit years at end, front, and legacy."""
    # 1. 2-digit year at end of expiry (Upstox format: 03NOV26)
    p1 = tue_module.parse_tradingsymbol("NIFTY03NOV2622650CE")
    assert p1["underlying"] == "NIFTY"
    assert p1["expiry"] == "03NOV26"
    assert p1["strike"] == "22650"
    assert p1["option_type"] == "CE"

    # 2. 2-digit year at front of expiry (26NOV)
    p2 = tue_module.parse_tradingsymbol("NIFTY26NOV22650PE")
    assert p2["underlying"] == "NIFTY"
    assert p2["expiry"] == "26NOV"
    assert p2["strike"] == "22650"
    assert p2["option_type"] == "PE"

    # 3. Legacy day-month without year (4AUG)
    p3 = tue_module.parse_tradingsymbol("NIFTY4AUG24000CE")
    assert p3["underlying"] == "NIFTY"
    assert p3["expiry"] == "4AUG"
    assert p3["strike"] == "24000"
    assert p3["option_type"] == "CE"


def test_round_to_tick():
    assert tue_module.round_to_tick(102.32) == 102.30
    assert tue_module.round_to_tick(102.34) == 102.35
    assert tue_module.round_to_tick(102.37) == 102.35
    assert tue_module.round_to_tick(102.38) == 102.40


def test_tuesday_redis_keys_isolation():
    keys = tue_module.build_trail_redis_keys("upstox", "NIFTY03NOV2622650CE", "NSE_FO|53750")
    assert ":tue:" in keys["enabled"]
    assert "fno:upstox:enabled:tue:NIFTY:03NOV26:22650:CE" in keys["enabled"]
    assert "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE" in keys["max"]


def test_multi_lot_quantity_slicing():
    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "product": "NRML",
        "quantity": 195,  # 3 lots total in broker account
        "average_price": 100.0,
        "last_price": 120.0,
    }]
    orders = []

    # Run for 1 lot (65 qty)
    res_1lot = tue_module.manage_tuesday_exit_orders(
        token=tue_module.MOCK_TOKEN,
        positions=positions,
        orders=orders,
        target_lots=1,
        force=True,
    )
    action_1lot = res_1lot["actions"][0]
    assert action_1lot["quantity"] == 65
    assert action_1lot["total_broker_qty"] == 195

    # Run for 2 lots (130 qty)
    res_2lot = tue_module.manage_tuesday_exit_orders(
        token=tue_module.MOCK_TOKEN,
        positions=positions,
        orders=orders,
        target_lots=2,
        force=True,
    )
    action_2lot = res_2lot["actions"][0]
    assert action_2lot["quantity"] == 130
    assert action_2lot["total_broker_qty"] == 195


def test_other_cycle_order_reservation():
    """Verify that open orders from other cycles (e.g. THU-SL) are reserved against broker quantity."""
    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "product": "NRML",
        "quantity": 195,
        "average_price": 100.0,
        "last_price": 120.0,
    }]
    # Thursday has an open order for 65 qty tagged THU-SL
    orders = [{
        "exchange": "NFO",
        "product": "NRML",
        "quantity": 65,
        "status": "open",
        "tag": "THU-SL",
        "instrument_token": "NSE_FO|53750",
        "trading_symbol": "NIFTY03NOV2622650CE",
        "order_type": "SL-M",
        "validity": "DAY",
        "trigger_price": 95.0,
        "transaction_type": "SELL",
        "pending_quantity": 65,
        "order_id": "THU_ORDER_001",
    }]

    # Tuesday with 2 lots (130 qty) should get exactly 195 - 65 = 130 qty
    res = tue_module.manage_tuesday_exit_orders(
        token=tue_module.MOCK_TOKEN,
        positions=positions,
        orders=orders,
        target_lots=2,
        force=True,
    )
    assert len(res["actions"]) == 1
    action = res["actions"][0]
    assert action["quantity"] == 130
    assert action["status"] == "place"

    # Now simulate other cycle orders exhausting all 195 qty
    orders_exhausted = [{
        "exchange": "NFO",
        "product": "NRML",
        "quantity": 195,
        "status": "open",
        "tag": "THU-SL",
        "instrument_token": "NSE_FO|53750",
        "trading_symbol": "NIFTY03NOV2622650CE",
        "order_type": "SL-M",
        "validity": "DAY",
        "trigger_price": 95.0,
        "transaction_type": "SELL",
        "pending_quantity": 195,
        "order_id": "THU_ORDER_002",
    }]
    res_exhausted = tue_module.manage_tuesday_exit_orders(
        token=tue_module.MOCK_TOKEN,
        positions=positions,
        orders=orders_exhausted,
        target_lots=2,
        force=True,
    )
    action_exhausted = res_exhausted["actions"][0]
    assert action_exhausted["status"] == "quantity_exhausted_by_other_cycle"


def test_manual_untagged_order_conflict():
    """Manual untagged SELL order must never be touched or duplicated."""
    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "product": "NRML",
        "quantity": 130,
        "average_price": 100.0,
        "last_price": 120.0,
    }]
    orders = [{
        "exchange": "NFO",
        "product": "NRML",
        "quantity": 65,
        "status": "open",
        "tag": "",  # Manual untagged SELL order
        "instrument_token": "NSE_FO|53750",
        "trading_symbol": "NIFTY03NOV2622650CE",
        "order_type": "SL-M",
        "validity": "DAY",
        "trigger_price": 95.0,
        "transaction_type": "SELL",
        "pending_quantity": 65,
        "order_id": "MANUAL_ORDER_001",
    }]

    res = tue_module.manage_tuesday_exit_orders(
        token=tue_module.MOCK_TOKEN,
        positions=positions,
        orders=orders,
        target_lots=2,
        force=True,
    )
    assert res["actions"][0]["status"] == "manual_sell_conflict"


def test_orphan_order_detection():
    """TUE-SL order without active position is detected as orphan."""
    positions = []  # No open positions
    orders = [{
        "exchange": "NFO",
        "product": "NRML",
        "quantity": 130,
        "status": "open",
        "tag": "TUE-SL",
        "instrument_token": "NSE_FO|53750",
        "trading_symbol": "NIFTY03NOV2622650CE",
        "order_type": "SL-M",
        "validity": "DAY",
        "trigger_price": 95.0,
        "transaction_type": "SELL",
        "pending_quantity": 130,
        "order_id": "ORPHAN_001",
    }]

    res = tue_module.manage_tuesday_exit_orders(
        token=tue_module.MOCK_TOKEN,
        positions=positions,
        orders=orders,
        force=True,
    )
    assert len(res["orphan_managed_orders"]) == 1
    assert res["orphan_managed_orders"][0]["order_id"] == "ORPHAN_001"


def test_redis_json_authorization_qty():
    """Verify strict fail-closed parsing of boolean and JSON authorization keys."""
    mock_redis = MagicMock()

    # Plain boolean string
    mock_redis.get.return_value = "1"
    enabled, qty = tue_module._get_redis_leg_authorization(mock_redis, "key1")
    assert enabled is True
    assert qty is None

    # JSON with explicit qty
    mock_redis.get.return_value = '{"enabled": true, "qty": 130}'
    enabled, qty = tue_module._get_redis_leg_authorization(mock_redis, "key2")
    assert enabled is True
    assert qty == 130

    # JSON disabled boolean
    mock_redis.get.return_value = '{"enabled": false, "qty": 130}'
    enabled, qty = tue_module._get_redis_leg_authorization(mock_redis, "key3")
    assert enabled is False
    assert qty is None

    # JSON disabled string ("false") - must NOT evaluate to True via bool("false")
    mock_redis.get.return_value = '{"enabled": "false", "qty": 130}'
    enabled, qty = tue_module._get_redis_leg_authorization(mock_redis, "key4")
    assert enabled is False
    assert qty is None

    # Malformed JSON - must fail closed
    mock_redis.get.return_value = '{"enabled": true, "qty": broken}'
    enabled, qty = tue_module._get_redis_leg_authorization(mock_redis, "key5")
    assert enabled is False
    assert qty is None

    # Zero or negative quantity - must fail closed
    mock_redis.get.return_value = '{"enabled": true, "qty": 0}'
    enabled, qty = tue_module._get_redis_leg_authorization(mock_redis, "key6")
    assert enabled is False
    assert qty is None

    mock_redis.get.return_value = '{"enabled": true, "qty": -65}'
    enabled, qty = tue_module._get_redis_leg_authorization(mock_redis, "key7")
    assert enabled is False
    assert qty is None


def test_tuesday_exit_orders_uses_redis_authoritative_qty():
    """If Redis contract key specifies qty=130, script manages 130 even if --lots=1 (65)."""
    mock_redis = MagicMock()
    mock_redis.get.side_effect = lambda k: '{"enabled": true, "qty": 130}' if "enabled:tue" in k else None

    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "product": "NRML",
        "quantity": 195,
        "average_price": 100.0,
        "last_price": 120.0,
    }]

    res = tue_module.manage_tuesday_exit_orders(
        token=tue_module.MOCK_TOKEN,
        positions=positions,
        orders=[],
        target_lots=1,  # CLI specifies 1 lot (65 qty)
        execute=False,
        redis_client=mock_redis,
    )

    action = res["actions"][0]
    # Authoritative Redis quantity (130) overrides CLI target_lots (65)
    assert action["quantity"] == 130


def test_orphan_cancel_failure_preserves_state():
    """When orphan cancellation fails, Redis state must NOT be cleared."""
    mock_redis = MagicMock()
    positions = []
    orders = [{
        "exchange": "NFO",
        "product": "NRML",
        "quantity": 130,
        "status": "open",
        "tag": "TUE-SL",
        "instrument_token": "NSE_FO|53750",
        "trading_symbol": "NIFTY03NOV2622650CE",
        "order_type": "SL-M",
        "validity": "DAY",
        "trigger_price": 95.0,
        "transaction_type": "SELL",
        "pending_quantity": 130,
        "order_id": "ORPHAN_FAIL_001",
    }]

    with patch.object(tue_module, "get_redis_client", return_value=mock_redis), \
         patch.object(tue_module, "is_market_hours_ist", return_value=(True, "Market is open")), \
         patch.object(tue_module, "cancel_order", return_value={"success": False, "error": "Rate limit exceeded"}), \
         patch.object(tue_module, "_cleanup_tuesday_leg_state") as mock_cleanup, \
         patch.object(tue_module, "_check_and_reset_orders_live_tue") as mock_reset:

        res = tue_module.manage_tuesday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            auto_cancel_orphans=True,
        )

        assert len(res["orphan_managed_orders"]) == 1
        orphan = res["orphan_managed_orders"][0]
        assert orphan["action_taken"] == "cancel_failed"
        assert orphan["is_terminal"] is False
        # Cleanup must NOT be called on cancellation failure
        mock_cleanup.assert_not_called()
        mock_reset.assert_not_called()


def test_orphan_cancel_request_keeps_monitoring_active():
    """When orphan cancellation request succeeds, order is cancel_requested and kept non-terminal until order book confirms."""
    mock_redis = MagicMock()
    positions = []
    orders = [{
        "exchange": "NFO",
        "product": "NRML",
        "quantity": 130,
        "status": "open",
        "tag": "TUE-SL",
        "instrument_token": "NSE_FO|53750",
        "trading_symbol": "NIFTY03NOV2622650CE",
        "order_type": "SL-M",
        "validity": "DAY",
        "trigger_price": 95.0,
        "transaction_type": "SELL",
        "pending_quantity": 130,
        "order_id": "ORPHAN_SUCCESS_001",
    }]

    with patch.object(tue_module, "get_redis_client", return_value=mock_redis), \
         patch.object(tue_module, "is_market_hours_ist", return_value=(True, "Market is open")), \
         patch.object(tue_module, "cancel_order", return_value={"success": True, "order_id": "ORPHAN_SUCCESS_001"}), \
         patch.object(tue_module, "_cleanup_tuesday_leg_state") as mock_cleanup, \
         patch.object(tue_module, "_check_and_reset_orders_live_tue") as mock_reset:

        res = tue_module.manage_tuesday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            auto_cancel_orphans=True,
        )

        assert len(res["orphan_managed_orders"]) == 1
        orphan = res["orphan_managed_orders"][0]
        assert orphan["action_taken"] == "cancel_requested"
        assert orphan["is_terminal"] is False
        # Redis state is preserved until exchange confirms terminal status in next order-book fetch
        mock_cleanup.assert_not_called()
        mock_reset.assert_not_called()


def test_parse_positions_separates_long_and_short():
    """Verify parse_positions correctly separates positive long and unexpected negative short positions."""
    raw_payload = {
        "status": "success",
        "data": {
            "net": [
                {"tradingsymbol": "NIFTY03NOV2622650CE", "exchange": "NFO", "instrument_token": "NSE_FO|53750", "quantity": 130},
                {"tradingsymbol": "NIFTY03NOV2622800PE", "exchange": "NFO", "instrument_token": "NSE_FO|53795", "quantity": -65},
                {"tradingsymbol": "RELIANCE", "exchange": "NSE_EQ", "instrument_token": "NSE_EQ|2885", "quantity": 50},
            ]
        }
    }
    longs, shorts = tue_module.parse_positions(raw_payload)
    assert len(longs) == 1
    assert longs[0]["tradingsymbol"] == "NIFTY03NOV2622650CE"
    assert longs[0]["quantity"] == 130

    assert len(shorts) == 1
    assert shorts[0]["tradingsymbol"] == "NIFTY03NOV2622800PE"
    assert shorts[0]["quantity"] == -65


def test_strict_whitelist_authorization_values():
    """Verify that typos like 'treu' or 'maybe' are rejected fail-closed."""
    mock_redis = MagicMock()

    # Whitelisted strings
    for true_val in ("1", "true", "TRUE", "yes", "YES", "on", "enabled"):
        mock_redis.get.return_value = true_val
        enabled, _ = tue_module._get_redis_leg_authorization(mock_redis, "key")
        assert enabled is True, f"Expected {true_val} to be True"

    # Non-whitelisted strings / typos
    for invalid_val in ("treu", "maybe", "unknown", "2", "none", "false", "0"):
        mock_redis.get.return_value = invalid_val
        enabled, _ = tue_module._get_redis_leg_authorization(mock_redis, "key")
        assert enabled is False, f"Expected {invalid_val} to be False"


def test_reconcile_cycle_legs_cleans_terminal_leg_keeps_active_leg():
    """Verify that PUT leg with no position and no open order is retired while CALL leg is preserved."""
    mock_redis = MagicMock()
    # Two keys in Redis: CALL and PUT
    call_key = "fno:upstox:enabled:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750"
    put_key = "fno:upstox:enabled:tue:NIFTY:03NOV26:22800:PE:NIFTY03NOV2622800PE:NSE_FO_53795"

    mock_redis.scan_iter.return_value = [call_key, put_key]
    mock_redis.get.return_value = "1"

    # In broker portfolio: ONLY CALL is open (PUT position is 0 / closed)
    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "quantity": 130,
    }]
    # In broker order book: ONLY CALL order is open (PUT order was filled/cancelled)
    orders = [{
        "exchange": "NFO",
        "product": "NRML",
        "quantity": 130,
        "status": "open",
        "tag": "TUE-SL",
        "instrument_token": "NSE_FO|53750",
        "trading_symbol": "NIFTY03NOV2622650CE",
        "order_type": "SL-M",
        "transaction_type": "SELL",
        "pending_quantity": 130,
        "order_id": "CALL_ORD_001",
    }]

    with patch.object(tue_module, "_cleanup_tuesday_leg_state") as mock_cleanup:
        cleaned = tue_module.reconcile_cycle_legs(mock_redis, positions, orders, broker="upstox", cycle="tue", tag="TUE-SL")

        # PUT key was cleaned up because position is 0 and no open order exists
        assert len(cleaned) == 1
        assert cleaned[0]["key"] == put_key
        mock_cleanup.assert_called_once()
        # Verify CALL key was NOT passed to cleanup
        cleaned_leg_keys = mock_cleanup.call_args[0][1]
        assert "22800" in cleaned_leg_keys["enabled"]


def test_canonical_key_authorizes_tuesday_position():
    """Verify that canonical key fno:upstox:enabled:tue:2026-11-03:22650:CE authorizes the position without token."""
    mock_redis = MagicMock()
    # Canonical key is set with JSON target quantity 130
    canonical_key = "fno:upstox:enabled:tue:2026-11-03:22650:CE"
    mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 130}) if k == canonical_key else None

    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "quantity": 130,
        "last_price": 100.0,
        "average_price": 90.0,
    }]
    orders = []

    with patch.object(tue_module, "fetch_ltp", return_value=122.0):
        res = tue_module.manage_tuesday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=False,
            redis_client=mock_redis,
        )
    assert res["authorized_positions"] == 1
    assert res["dry_run"] is True
    assert res["actions"][0]["status"] == "place"
    assert res["actions"][0]["quantity"] == 130


def test_reconcile_cycle_legs_blocks_retiring_leg_with_short_position():
    """Verify reconcile_cycle_legs NEVER deletes Redis state if a short position exists for that leg."""
    mock_redis = MagicMock()
    call_key = "fno:upstox:enabled:tue:2026-11-03:22650:CE"
    mock_redis.scan_iter.return_value = [call_key]
    mock_redis.get.return_value = "1"

    # Broker has NO long position, but has a SHORT position (e.g. qty = -65)
    long_positions = []
    short_positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "quantity": -65,
    }]
    orders = []  # No open orders

    with patch.object(tue_module, "_cleanup_tuesday_leg_state") as mock_cleanup:
        cleaned = tue_module.reconcile_cycle_legs(
            mock_redis,
            long_positions,
            orders,
            broker="upstox",
            cycle="tue",
            tag="TUE-SL",
            short_positions=short_positions,
        )

        # Leg MUST NOT be cleaned up because a short position exists!
        assert len(cleaned) == 0
        mock_cleanup.assert_not_called()


def test_two_expiries_at_same_strike_only_authorizes_target_expiry():
    """Verify that having two expiries at the same strike only authorizes the target expiry."""
    mock_redis = MagicMock()
    target_key = "fno:upstox:enabled:tue:2026-11-03:22650:CE"
    mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 130}) if k == target_key else None

    # Account has 22650 CE from 03-Nov-2026 and another 22650 CE from 10-Nov-2026
    positions = [
        {
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "exchange": "NFO",
            "instrument_token": "NSE_FO|53750",
            "quantity": 130,
            "last_price": 100.0,
            "average_price": 90.0,
        },
        {
            "tradingsymbol": "NIFTY10NOV2622650CE",
            "exchange": "NFO",
            "instrument_token": "NSE_FO|53800",
            "quantity": 130,
            "last_price": 120.0,
            "average_price": 110.0,
        },
    ]
    orders = []

    with patch.object(tue_module, "fetch_ltp", return_value=105.0):
        res = tue_module.manage_tuesday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=False,
            redis_client=mock_redis,
        )

    # Strictly 1 position authorized (03NOV26); 10NOV26 is skipped!
    assert res["authorized_positions"] == 1
    authorized_actions = [a for a in res["actions"] if a.get("status") != "authorization_skipped"]
    assert len(authorized_actions) == 1
    assert authorized_actions[0]["symbol"] == "NIFTY03NOV2622650CE"
    assert authorized_actions[0]["status"] == "place"

    skipped = [a for a in res["actions"] if a.get("status") == "authorization_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["symbol"] == "NIFTY10NOV2622650CE"


def test_disabled_canonical_key_does_not_fall_through_to_detailed_key():
    """Verify canonical authority: an explicitly disabled canonical key must NOT fall through to an enabled detailed key."""
    mock_redis = MagicMock()
    canonical_key = "fno:upstox:enabled:tue:2026-11-03:22650:CE"
    detailed_key = "fno:upstox:enabled:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750"

    def redis_get(k):
        if k == canonical_key:
            return "0"  # Disabled
        if k == detailed_key:
            return "1"  # Enabled (stale migration key)
        return None

    mock_redis.get.side_effect = redis_get

    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "quantity": 130,
        "last_price": 100.0,
        "average_price": 90.0,
    }]
    orders = []

    res = tue_module.manage_tuesday_exit_orders(
        token="REAL_TOKEN",
        positions=positions,
        orders=orders,
        execute=False,
        redis_client=mock_redis,
    )

    # Must be rejected because canonical key says 0!
    assert res["authorized_positions"] == 0
    assert len(res["actions"]) == 1
    assert res["actions"][0]["status"] == "authorization_skipped"


def test_reconcile_cycle_legs_purges_matching_trailing_keys():
    """Verify reconcile_cycle_legs purges all detailed trailing keys when retiring a closed canonical leg."""
    mock_redis = MagicMock()
    call_key = "fno:upstox:enabled:tue:2026-11-03:22650:CE"
    trail_max = "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:max"
    trail_sl = "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:sl"

    def scan_mock(pattern, count=30):
        if "enabled" in pattern:
            return [call_key]
        if "trail" in pattern:
            return [trail_max, trail_sl]
        return []

    mock_redis.scan_iter.side_effect = scan_mock
    mock_redis.get.return_value = "1"
    mock_pipe = MagicMock()
    mock_redis.pipeline.return_value = mock_pipe

    cleaned = tue_module.reconcile_cycle_legs(
        mock_redis,
        positions=[],
        orders=[],
        broker="upstox",
        cycle="tue",
        tag="TUE-SL",
    )

    assert len(cleaned) == 1
    assert cleaned[0]["strike"] == 22650
    # Verify trailing keys were deleted via pipeline
    deleted_keys = [call[0][0] for call in mock_pipe.delete.call_args_list]
    assert trail_max in deleted_keys
    assert trail_sl in deleted_keys


def test_reconcile_cycle_legs_purges_only_target_expiry_trailing_keys():
    """Verify that retiring an older expiry strictly preserves the trailing state of another active expiry at the same strike."""
    mock_redis = MagicMock()
    # Expiring / closed contract: 03-Nov-2026
    target_key = "fno:upstox:enabled:tue:2026-11-03:22650:CE"
    target_trail_max = "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:max"
    target_trail_sl = "fno:upstox:trail:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:sl"

    # Separate future active contract: 10-Nov-2026
    future_trail_max = "fno:upstox:trail:tue:NIFTY:10NOV26:22650:CE:NIFTY10NOV2622650CE:NSE_FO_53800:max"
    future_trail_sl = "fno:upstox:trail:tue:NIFTY:10NOV26:22650:CE:NIFTY10NOV2622650CE:NSE_FO_53800:sl"

    def scan_mock(pattern, count=30):
        if "enabled" in pattern:
            return [target_key]
        if "trail" in pattern:
            # Redis scan returns trailing keys from both expiries
            return [target_trail_max, target_trail_sl, future_trail_max, future_trail_sl]
        return []

    mock_redis.scan_iter.side_effect = scan_mock
    mock_redis.get.return_value = "1"
    mock_pipe = MagicMock()
    mock_redis.pipeline.return_value = mock_pipe

    cleaned = tue_module.reconcile_cycle_legs(
        mock_redis,
        positions=[],
        orders=[],
        broker="upstox",
        cycle="tue",
        tag="TUE-SL",
    )

    assert len(cleaned) == 1
    assert cleaned[0]["expiry"] == "2026-11-03"

    deleted_keys = [call[0][0] for call in mock_pipe.delete.call_args_list]
    # Target expiry keys MUST be deleted
    assert target_trail_max in deleted_keys
    assert target_trail_sl in deleted_keys

    # Other expiry keys MUST NOT be deleted (strictly preserved)
    assert future_trail_max not in deleted_keys
    assert future_trail_sl not in deleted_keys


def test_legacy_symbol_4aug_matches_publisher_key():
    """Verify legacy symbol NIFTY4AUG24000CE resolves to publisher key with active strategy expiry."""
    mock_redis = MagicMock()
    # Publisher wrote canonical key using active strategy's ISO date
    publisher_key = "fno:upstox:enabled:tue:2026-08-04:24000:CE"
    mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 130}) if k == publisher_key else None

    # Upstox broker returned legacy tradingsymbol without year
    positions = [{
        "tradingsymbol": "NIFTY4AUG24000CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|51000",
        "quantity": 130,
        "last_price": 100.0,
        "average_price": 90.0,
    }]
    orders = []

    with patch.object(tue_module, "fetch_ltp", return_value=122.0):
        res = tue_module.manage_tuesday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=False,
            redis_client=mock_redis,
            active_expiry="2026-08-04",
        )

    # Successfully authorized matching publisher key
    assert res["authorized_positions"] == 1
    assert res["actions"][0]["symbol"] == "NIFTY4AUG24000CE"
    assert res["actions"][0]["status"] == "place"
    assert res["actions"][0]["quantity"] == 130


def test_reconcile_cycle_legs_year_boundary_legacy_symbol():
    """Verify reconcile_cycle_legs does NOT retire a yearless broker position across year boundary (e.g. 4JAN in 2027)."""
    mock_redis = MagicMock()
    # Contract in Redis has ISO expiry 2027-01-04
    canonical_key = "fno:upstox:enabled:tue:2027-01-04:24000:CE"
    mock_redis.scan_iter.return_value = [canonical_key]
    mock_redis.get.return_value = "1"

    # Broker has active position with yearless tradingsymbol NIFTY4JAN24000CE
    positions = [{
        "tradingsymbol": "NIFTY4JAN24000CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|60000",
        "quantity": 130,
        "last_price": 110.0,
        "average_price": 95.0,
    }]
    orders = []

    # Run reconciliation passing active_expiry="2027-01-04"
    cleaned = tue_module.reconcile_cycle_legs(
        mock_redis,
        positions=positions,
        orders=orders,
        broker="upstox",
        cycle="tue",
        tag="TUE-SL",
        active_expiry="2027-01-04",
    )

    # Must NOT retire this leg because active long position exists!
    assert len(cleaned) == 0
    mock_redis.delete.assert_not_called()


def test_is_market_hours_ist():
    """Verify is_market_hours_ist correctly gates pre-market, trading hours, post-market, weekends, and holidays."""
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
    except ImportError:
        import pytz
        ist = pytz.timezone("Asia/Kolkata")

    # Tuesday 10:30 IST (Open non-holiday)
    dt_open = datetime(2026, 11, 3, 10, 30, tzinfo=ist)
    is_open, _ = tue_module.is_market_hours_ist(dt_open)
    assert is_open is True

    # Tuesday 09:00 IST (Pre-market)
    dt_pre = datetime(2026, 11, 3, 9, 0, tzinfo=ist)
    is_open, reason = tue_module.is_market_hours_ist(dt_pre)
    assert is_open is False
    assert "Pre-market" in reason

    # Tuesday 15:29:59 IST (Open - last trading second)
    dt_last_sec = datetime(2026, 11, 3, 15, 29, 59, tzinfo=ist)
    is_open, _ = tue_module.is_market_hours_ist(dt_last_sec)
    assert is_open is True

    # Tuesday 15:30:00 IST (Closed - exact market close)
    dt_close = datetime(2026, 11, 3, 15, 30, 0, tzinfo=ist)
    is_open, reason = tue_module.is_market_hours_ist(dt_close)
    assert is_open is False
    assert "Post-market" in reason

    # Tuesday 15:30:40 IST (Closed - post-market)
    dt_post_40 = datetime(2026, 11, 3, 15, 30, 40, tzinfo=ist)
    is_open, reason = tue_module.is_market_hours_ist(dt_post_40)
    assert is_open is False
    assert "Post-market" in reason

    # Tuesday 15:45 IST (Post-market)
    dt_post = datetime(2026, 11, 3, 15, 45, tzinfo=ist)
    is_open, reason = tue_module.is_market_hours_ist(dt_post)
    assert is_open is False
    assert "Post-market" in reason

    # Saturday 11:00 IST (Weekend)
    dt_sat = datetime(2026, 11, 7, 11, 0, tzinfo=ist)
    is_open, reason = tue_module.is_market_hours_ist(dt_sat)
    assert is_open is False
    assert "Weekend" in reason

    # Monday Republic Day 11:00 IST (Exchange Holiday)
    dt_holiday = datetime(2026, 1, 26, 11, 0, tzinfo=ist)
    is_open, reason = tue_module.is_market_hours_ist(dt_holiday)
    assert is_open is False
    assert "Exchange Holiday" in reason


def test_fno_execution_lock_mutual_exclusion():
    """Verify fno_execution_lock acquires Redis lock with TTL, heartbeat, and releases via eval Lua script."""
    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    with tue_module.fno_execution_lock(mock_redis, lock_key="fno:lock:exit_order_manager", timeout=60) as acquired:
        assert acquired is True
        # Verify NX=True and EX=60 was called
        mock_redis.set.assert_called_once()
        call_args, call_kwargs = mock_redis.set.call_args
        assert call_args[0] == "fno:lock:exit_order_manager"
        assert call_kwargs.get("nx") is True
        assert call_kwargs.get("ex") == 60

    # Verify release evaluated Lua script
    mock_redis.eval.assert_called_once()
    assert "redis.call" in mock_redis.eval.call_args[0][0]


def test_fno_execution_lock_fail_closed_skips_broker_actions():
    """Verify that if the distributed lock is busy, the script fails closed and exits without broker calls."""
    mock_redis = MagicMock()
    # Simulate lock acquisition failure (already held by Thursday manager)
    mock_redis.set.return_value = False

    with tue_module.fno_execution_lock(mock_redis, lock_key="fno:lock:exit_order_manager", timeout=60, max_wait=0.1) as acquired:
        assert acquired is False

    # Ensure release was NOT called when not acquired
    mock_redis.eval.assert_not_called()


def test_fno_execution_lock_heartbeat_failure_aborts_run():
    """Verify that if the lock heartbeat lease extension fails, abort_event is set and manager halts."""
    mock_redis = MagicMock()
    mock_redis.set.return_value = True
    # Make eval return 0 during heartbeat renewal
    mock_redis.eval.return_value = 0
    abort_event = threading.Event()

    # 1. Lock heartbeat triggers abort_event on renewal failure
    with tue_module.fno_execution_lock(
        mock_redis, lock_key="fno:lock:exit_order_manager", timeout=0.15, retry_delay=0.01, max_wait=0.1, abort_event=abort_event
    ) as acquired:
        assert acquired is True
        # Allow heartbeat thread to execute renewal attempt
        time.sleep(0.1)
        assert abort_event.is_set() is True

    # 2. Manager detects abort_event and safely aborts without broker modifications
    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "instrument_token": "NSE_FO_53750",
        "quantity": 130,
        "average_price": 100.0,
        "last_price": 120.0
    }]
    orders = []
    res = tue_module.manage_tuesday_exit_orders(
        token="MOCK_TOKEN",
        positions=positions,
        orders=orders,
        execute=True,
        abort_event=abort_event,
    )
    assert res.get("fatal_error") == "lock_lost"


def test_lock_loss_during_ltp_fetch_blocks_broker_order_mutations():
    """Verify that if the lock is lost while fetching LTP, order placement and modification are strictly blocked."""
    mock_redis = MagicMock()
    mock_redis.get.return_value = json.dumps({"enabled": True, "qty": 130})
    abort_event = threading.Event()

    def fake_fetch_ltp(token, token_key, broker="upstox"):
        # Simulate lock lease expiration / heartbeat failure during network quote fetch
        abort_event.set()
        return 125.0

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "instrument_token": "NSE_FO_53750",
        "quantity": 130,
        "average_price": 100.0,
        "last_price": 120.0
    }]
    orders = []

    with patch.object(tue_module, "fetch_ltp", side_effect=fake_fetch_ltp) as mock_ltp, \
         patch.object(tue_module, "place_exit_order_slm") as mock_place, \
         patch.object(tue_module, "modify_exit_order_slm") as mock_modify:

        res = tue_module.manage_tuesday_exit_orders(
            token="REAL_TOKEN_TEST",
            positions=positions,
            orders=orders,
            execute=True,
            redis_client=mock_redis,
            abort_event=abort_event,
        )

        mock_ltp.assert_called_once()
        mock_place.assert_not_called()
        mock_modify.assert_not_called()
        assert res.get("fatal_error") == "lock_lost"


def test_lock_loss_blocks_orphan_order_cancellation():
    """Verify that if lock is lost, orphan order cancellation is strictly blocked."""
    abort_event = threading.Event()
    abort_event.set()  # Lock already lost

    orders = [{
        "order_id": "ORD_ORPHAN_123",
        "trading_symbol": "NIFTY26NOV22650CE",
        "instrument_token": "NSE_FO_53750",
        "transaction_type": "SELL",
        "order_type": "SL-M",
        "status": "trigger pending",
        "quantity": 130,
        "tag": "TUE-SL",
    }]

    with patch.object(tue_module, "cancel_order") as mock_cancel:
        res = tue_module.manage_tuesday_exit_orders(
            token="REAL_TOKEN_TEST",
            positions=[],
            orders=orders,
            execute=True,
            redis_client=MagicMock(),
            abort_event=abort_event,
        )
        mock_cancel.assert_not_called()
        assert res.get("fatal_error") == "lock_lost"


def test_market_close_blocks_broker_order_mutations():
    """Verify that if market closes (15:30:00 IST reached), order placement and modifications are aborted."""
    mock_redis = MagicMock()
    mock_redis.get.return_value = json.dumps({"enabled": True, "qty": 130})

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "instrument_token": "NSE_FO_53750",
        "quantity": 130,
        "average_price": 100.0,
        "last_price": 120.0
    }]
    orders = []

    with patch.object(tue_module, "fetch_ltp", return_value=125.0) as mock_ltp, \
         patch.object(tue_module, "is_market_hours_ist", return_value=(False, "Post-market (15:30:05 IST >= 15:30:00 IST); market is closed")), \
         patch.object(tue_module, "place_exit_order_slm") as mock_place, \
         patch.object(tue_module, "modify_exit_order_slm") as mock_modify:

        res = tue_module.manage_tuesday_exit_orders(
            token="REAL_TOKEN_TEST",
            positions=positions,
            orders=orders,
            execute=True,
            redis_client=mock_redis,
        )

        mock_ltp.assert_called_once()
        mock_place.assert_not_called()
        mock_modify.assert_not_called()
        assert res.get("fatal_error") == "market_closed"






