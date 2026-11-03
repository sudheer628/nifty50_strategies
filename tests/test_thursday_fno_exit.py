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
thu_spec = importlib.util.spec_from_file_location(
    "thursday_exit_orders",
    os.path.join(scripts_dir, "Thursday-Fno-Exit-Orders-V1.py")
)
thu_module = importlib.util.module_from_spec(thu_spec)
thu_spec.loader.exec_module(thu_module)


def test_parse_tradingsymbol_formats():
    """Verify symbol regex handles 2-digit years at end, front, and legacy."""
    p1 = thu_module.parse_tradingsymbol("NIFTY03NOV2622650CE")
    assert p1["underlying"] == "NIFTY"
    assert p1["expiry"] == "03NOV26"
    assert p1["strike"] == "22650"
    assert p1["option_type"] == "CE"

    p2 = thu_module.parse_tradingsymbol("NIFTY26NOV22650PE")
    assert p2["underlying"] == "NIFTY"
    assert p2["expiry"] == "26NOV"
    assert p2["strike"] == "22650"
    assert p2["option_type"] == "PE"


def test_round_to_tick():
    assert thu_module.round_to_tick(102.32) == 102.30
    assert thu_module.round_to_tick(102.34) == 102.35


def test_thursday_redis_keys_isolation():
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY03NOV2622650CE", "NSE_FO|53750")
    assert ":thu:" in keys["enabled"]
    assert "fno:angelone:enabled:thu:NIFTY:03NOV26:22650:CE" in keys["enabled"]
    assert "fno:angelone:trail:thu:NIFTY:03NOV26:22650:CE" in keys["max"]


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
    res_1lot = thu_module.manage_thursday_exit_orders(
        token=thu_module.MOCK_TOKEN,
        positions=positions,
        orders=orders,
        target_lots=1,
        force=True,
    )
    action_1lot = res_1lot["actions"][0]
    assert action_1lot["quantity"] == 65
    assert action_1lot["total_broker_qty"] == 195

    # Run for 2 lots (130 qty)
    res_2lot = thu_module.manage_thursday_exit_orders(
        token=thu_module.MOCK_TOKEN,
        positions=positions,
        orders=orders,
        target_lots=2,
        force=True,
    )
    action_2lot = res_2lot["actions"][0]
    assert action_2lot["quantity"] == 130
    assert action_2lot["total_broker_qty"] == 195


def test_tuesday_cycle_order_reservation():
    """Verify that open orders from Tuesday cycle (tagged TUE-SL) are reserved against broker quantity."""
    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "product": "NRML",
        "quantity": 195,
        "average_price": 100.0,
        "last_price": 120.0,
    }]
    # Tuesday has an open order for 130 qty tagged TUE-SL
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
        "order_id": "TUE_ORDER_001",
    }]

    # Thursday with 1 lot (65 qty) should get exactly 195 - 130 = 65 qty
    res = thu_module.manage_thursday_exit_orders(
        token=thu_module.MOCK_TOKEN,
        positions=positions,
        orders=orders,
        target_lots=1,
        force=True,
    )
    assert len(res["actions"]) == 1
    action = res["actions"][0]
    assert action["quantity"] == 65
    assert action["status"] == "place"


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
        "order_id": "MANUAL_ORDER_THU_001",
    }]

    res = thu_module.manage_thursday_exit_orders(
        token=thu_module.MOCK_TOKEN,
        positions=positions,
        orders=orders,
        target_lots=1,
        force=True,
    )
    assert res["actions"][0]["status"] == "manual_sell_conflict"


def test_orphan_order_detection():
    """THU-SL order without active position is detected as orphan."""
    positions = []  # No open positions
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
        "order_id": "THU_ORPHAN_001",
    }]

    res = thu_module.manage_thursday_exit_orders(
        token=thu_module.MOCK_TOKEN,
        positions=positions,
        orders=orders,
        force=True,
    )
    assert len(res["orphan_managed_orders"]) == 1
    assert res["orphan_managed_orders"][0]["order_id"] == "THU_ORPHAN_001"


def test_redis_json_authorization_qty():
    """Verify strict fail-closed parsing of boolean and JSON authorization keys."""
    mock_redis = MagicMock()

    # Plain boolean string
    mock_redis.get.return_value = "1"
    enabled, qty = thu_module._get_redis_leg_authorization(mock_redis, "key1")
    assert enabled is True
    assert qty is None

    # JSON with explicit qty
    mock_redis.get.return_value = '{"enabled": true, "qty": 65}'
    enabled, qty = thu_module._get_redis_leg_authorization(mock_redis, "key2")
    assert enabled is True
    assert qty == 65

    # JSON disabled string ("false")
    mock_redis.get.return_value = '{"enabled": "false", "qty": 65}'
    enabled, qty = thu_module._get_redis_leg_authorization(mock_redis, "key3")
    assert enabled is False
    assert qty is None

    # Malformed JSON
    mock_redis.get.return_value = '{"enabled": true, "qty": broken}'
    enabled, qty = thu_module._get_redis_leg_authorization(mock_redis, "key4")
    assert enabled is False
    assert qty is None

    # Zero or negative quantity
    mock_redis.get.return_value = '{"enabled": true, "qty": 0}'
    enabled, qty = thu_module._get_redis_leg_authorization(mock_redis, "key5")
    assert enabled is False
    assert qty is None


def test_thursday_exit_orders_uses_redis_authoritative_qty():
    """If Redis contract key specifies qty=130, script manages 130 even if --lots=1 (65)."""
    mock_redis = MagicMock()
    mock_redis.get.side_effect = lambda k: '{"enabled": true, "qty": 130}' if "enabled:thu" in k else None

    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "product": "NRML",
        "quantity": 195,
        "average_price": 100.0,
        "last_price": 120.0,
    }]

    res = thu_module.manage_thursday_exit_orders(
        token=thu_module.MOCK_TOKEN,
        positions=positions,
        orders=[],
        target_lots=1,  # CLI specifies 1 lot (65 qty)
        execute=False,
        redis_client=mock_redis,
    )

    action = res["actions"][0]
    assert action["quantity"] == 130


def test_orphan_cancel_request_keeps_monitoring_active():
    """When orphan cancellation request succeeds, order is cancel_requested and kept non-terminal until order book confirms."""
    mock_redis = MagicMock()
    positions = []
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
        "order_id": "THU_ORPHAN_SUCCESS_001",
    }]

    with patch.object(thu_module, "get_redis_client", return_value=mock_redis), \
         patch.object(thu_module, "is_market_hours_ist", return_value=(True, "Market is open")), \
         patch.object(thu_module, "cancel_order", return_value={"success": True, "order_id": "THU_ORPHAN_SUCCESS_001"}), \
         patch.object(thu_module, "_cleanup_thursday_leg_state") as mock_cleanup, \
         patch.object(thu_module, "_check_and_reset_orders_live_thu") as mock_reset:

        res = thu_module.manage_thursday_exit_orders(
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
        mock_cleanup.assert_not_called()
        mock_reset.assert_not_called()


def test_parse_positions_separates_long_and_short():
    """Verify parse_positions correctly separates positive long and unexpected negative short positions."""
    raw_payload = {
        "status": "success",
        "data": {
            "net": [
                {"tradingsymbol": "NIFTY03NOV2622650CE", "exchange": "NFO", "instrument_token": "NSE_FO|53750", "quantity": 65},
                {"tradingsymbol": "NIFTY03NOV2622800PE", "exchange": "NFO", "instrument_token": "NSE_FO|53795", "quantity": -65},
            ]
        }
    }
    longs, shorts = thu_module.parse_positions(raw_payload)
    assert len(longs) == 1
    assert longs[0]["tradingsymbol"] == "NIFTY03NOV2622650CE"
    assert longs[0]["quantity"] == 65

    assert len(shorts) == 1
    assert shorts[0]["tradingsymbol"] == "NIFTY03NOV2622800PE"
    assert shorts[0]["quantity"] == -65


def test_parse_positions_raw_angelone_payload():
    """Verify parse_positions normalizes a raw AngelOne getPosition response with netqty and cfbuyavgprice."""
    raw_payload = {
        "status": True,
        "message": "SUCCESS",
        "errorcode": "",
        "data": [
            {
                "exchange": "NFO",
                "symboltoken": "53750",
                "symbolname": "NIFTY",
                "instrumenttype": "OPTIDX",
                "strikeprice": "22650.00",
                "optiontype": "CE",
                "expirydate": "03NOV2026",
                "netqty": "65",
                "cfbuyqty": "65",
                "cfsellqty": "0",
                "cfbuyavgprice": "105.50",
                "buyavgprice": "105.50",
                "tradingsymbol": "NIFTY03NOV2622650CE",
                "producttype": "CARRYFORWARD",
            },
            {
                "exchange": "NFO",
                "symboltoken": "53795",
                "symbolname": "NIFTY",
                "instrumenttype": "OPTIDX",
                "strikeprice": "22800.00",
                "optiontype": "PE",
                "expirydate": "03NOV2026",
                "netqty": "-65",
                "cfbuyqty": "0",
                "cfsellqty": "65",
                "cfbuyavgprice": "0.00",
                "cfsellavgprice": "140.00",
                "tradingsymbol": "NIFTY03NOV2622800PE",
                "producttype": "CARRYFORWARD",
            },
        ],
    }
    longs, shorts = thu_module.parse_positions(raw_payload)
    assert len(longs) == 1
    assert longs[0]["tradingsymbol"] == "NIFTY03NOV2622650CE"
    assert longs[0]["symboltoken"] == "53750"
    assert longs[0]["instrument_token"] == "53750"
    assert longs[0]["quantity"] == 65.0
    assert longs[0]["average_price"] == 105.50
    assert longs[0]["product"] == "CARRYFORWARD"

    assert len(shorts) == 1
    assert shorts[0]["tradingsymbol"] == "NIFTY03NOV2622800PE"
    assert shorts[0]["symboltoken"] == "53795"
    assert shorts[0]["quantity"] == -65.0


def test_strict_whitelist_authorization_values():
    """Verify that typos like 'treu' or 'maybe' are rejected fail-closed in Thursday script."""
    mock_redis = MagicMock()

    # Whitelisted strings
    for true_val in ("1", "true", "TRUE", "yes", "YES", "on", "enabled"):
        mock_redis.get.return_value = true_val
        enabled, _ = thu_module._get_redis_leg_authorization(mock_redis, "key")
        assert enabled is True, f"Expected {true_val} to be True"

    # Non-whitelisted strings / typos
    for invalid_val in ("treu", "maybe", "unknown", "2", "none", "false", "0"):
        mock_redis.get.return_value = invalid_val
        enabled, _ = thu_module._get_redis_leg_authorization(mock_redis, "key")
        assert enabled is False, f"Expected {invalid_val} to be False"


def test_reconcile_cycle_legs_cleans_terminal_leg_keeps_active_leg():
    """Verify that PUT leg with no position and no open order is retired while CALL leg is preserved in Thursday script."""
    mock_redis = MagicMock()
    # Two keys in Redis: CALL and PUT
    call_key = "fno:angelone:enabled:thu:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750"
    put_key = "fno:angelone:enabled:thu:NIFTY:03NOV26:22800:PE:NIFTY03NOV2622800PE:NSE_FO_53795"

    mock_redis.scan_iter.return_value = [call_key, put_key]
    mock_redis.get.return_value = "1"

    # In broker portfolio: ONLY CALL is open (PUT position is 0 / closed)
    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "quantity": 65,
    }]
    # In broker order book: ONLY CALL order is open (PUT order was filled/cancelled)
    orders = [{
        "exchange": "NFO",
        "product": "NRML",
        "quantity": 65,
        "status": "open",
        "tag": "THU-SL",
        "instrument_token": "NSE_FO|53750",
        "trading_symbol": "NIFTY03NOV2622650CE",
        "order_type": "SL-M",
        "transaction_type": "SELL",
        "pending_quantity": 65,
        "order_id": "CALL_ORD_002",
    }]

    with patch.object(thu_module, "_cleanup_thursday_leg_state") as mock_cleanup:
        cleaned = thu_module.reconcile_cycle_legs(mock_redis, positions, orders, broker="angelone", cycle="thu", tag="THU-SL")

        # PUT key was cleaned up because position is 0 and no open order exists
        assert len(cleaned) == 1
        assert cleaned[0]["key"] == put_key
        mock_cleanup.assert_called_once()
        # Verify CALL key was NOT passed to cleanup
        cleaned_leg_keys = mock_cleanup.call_args[0][1]
        assert "22800" in cleaned_leg_keys["enabled"]


def test_get_access_token_checks_redis_and_env():
    """Verify Thursday get_access_token checks Redis keys before falling back to env."""
    mock_redis = MagicMock()
    mock_redis.get.side_effect = lambda k: json.dumps({"jwtToken": "token_from_redis"}) if k == "angelone_jwt_feed" else None

    with patch.object(thu_module, "get_redis_client", return_value=mock_redis), \
         patch.dict("os.environ", {}, clear=True):
        token = thu_module.get_access_token()
        assert token == "token_from_redis"


def test_canonical_key_authorizes_thursday_position():
    """Verify that canonical key fno:angelone:enabled:thu:2026-11-03:22650:CE authorizes the position without token."""
    mock_redis = MagicMock()
    canonical_key = "fno:angelone:enabled:thu:2026-11-03:22650:CE"
    mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 65}) if k == canonical_key else None

    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "quantity": 65,
        "last_price": 100.0,
        "average_price": 90.0,
    }]
    orders = []

    with patch.object(thu_module, "fetch_ltp", return_value=122.0):
        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=False,
            redis_client=mock_redis,
        )
    assert res["authorized_positions"] == 1
    assert res["dry_run"] is True
    assert res["actions"][0]["status"] == "place"
    assert res["actions"][0]["quantity"] == 65


def test_reconcile_cycle_legs_blocks_retiring_leg_with_short_position_thu():
    """Verify reconcile_cycle_legs NEVER deletes Redis state if a short position exists for that leg in Thursday."""
    mock_redis = MagicMock()
    call_key = "fno:angelone:enabled:thu:2026-11-03:22650:CE"
    mock_redis.scan_iter.return_value = [call_key]
    mock_redis.get.return_value = "1"

    long_positions = []
    short_positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "quantity": -65,
    }]
    orders = []

    with patch.object(thu_module, "_cleanup_thursday_leg_state") as mock_cleanup:
        cleaned = thu_module.reconcile_cycle_legs(
            mock_redis,
            long_positions,
            orders,
            broker="angelone",
            cycle="thu",
            tag="THU-SL",
            short_positions=short_positions,
        )

        assert len(cleaned) == 0
        mock_cleanup.assert_not_called()


def test_two_expiries_at_same_strike_only_authorizes_target_expiry_thu():
    """Verify that having two expiries at the same strike only authorizes the target expiry in Thursday."""
    mock_redis = MagicMock()
    target_key = "fno:angelone:enabled:thu:2026-11-03:22650:CE"
    mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 65}) if k == target_key else None

    positions = [
        {
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "exchange": "NFO",
            "instrument_token": "NSE_FO|53750",
            "quantity": 65,
            "last_price": 100.0,
            "average_price": 90.0,
        },
        {
            "tradingsymbol": "NIFTY10NOV2622650CE",
            "exchange": "NFO",
            "instrument_token": "NSE_FO|53800",
            "quantity": 65,
            "last_price": 120.0,
            "average_price": 110.0,
        },
    ]
    orders = []

    with patch.object(thu_module, "fetch_ltp", return_value=105.0):
        res = thu_module.manage_thursday_exit_orders(
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


def test_disabled_canonical_key_does_not_fall_through_to_detailed_key_thu():
    """Verify canonical authority in Thursday: disabled canonical key must NOT fall through to enabled detailed key."""
    mock_redis = MagicMock()
    canonical_key = "fno:angelone:enabled:thu:2026-11-03:22650:CE"
    detailed_key = "fno:angelone:enabled:thu:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750"

    def redis_get(k):
        if k == canonical_key:
            return "0"  # Disabled
        if k == detailed_key:
            return "1"  # Enabled
        return None

    mock_redis.get.side_effect = redis_get

    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "quantity": 65,
        "last_price": 100.0,
        "average_price": 90.0,
    }]
    orders = []

    res = thu_module.manage_thursday_exit_orders(
        token="REAL_TOKEN",
        positions=positions,
        orders=orders,
        execute=False,
        redis_client=mock_redis,
    )

    assert res["authorized_positions"] == 0
    assert len(res["actions"]) == 1
    assert res["actions"][0]["status"] == "authorization_skipped"


def test_reconcile_cycle_legs_purges_matching_trailing_keys_thu():
    """Verify reconcile_cycle_legs purges all detailed trailing keys when retiring a closed canonical leg in Thursday."""
    mock_redis = MagicMock()
    call_key = "fno:angelone:enabled:thu:2026-11-03:22650:CE"
    trail_max = "fno:angelone:trail:thu:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:max"
    trail_sl = "fno:angelone:trail:thu:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:sl"

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

    cleaned = thu_module.reconcile_cycle_legs(
        mock_redis,
        positions=[],
        orders=[],
        broker="angelone",
        cycle="thu",
        tag="THU-SL",
    )

    assert len(cleaned) == 1
    assert cleaned[0]["strike"] == 22650
    deleted_keys = [call[0][0] for call in mock_pipe.delete.call_args_list]
    assert trail_max in deleted_keys
    assert trail_sl in deleted_keys


def test_reconcile_cycle_legs_purges_only_target_expiry_trailing_keys():
    """Verify that retiring an older expiry strictly preserves the trailing state of another active expiry at the same strike for Thursday."""
    mock_redis = MagicMock()
    # Expiring / closed contract: 03-Nov-2026
    target_key = "fno:angelone:enabled:thu:2026-11-03:22650:CE"
    target_trail_max = "fno:angelone:trail:thu:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:max"
    target_trail_sl = "fno:angelone:trail:thu:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750:sl"

    # Separate future active contract: 10-Nov-2026
    future_trail_max = "fno:angelone:trail:thu:NIFTY:10NOV26:22650:CE:NIFTY10NOV2622650CE:NSE_FO_53800:max"
    future_trail_sl = "fno:angelone:trail:thu:NIFTY:10NOV26:22650:CE:NIFTY10NOV2622650CE:NSE_FO_53800:sl"

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

    cleaned = thu_module.reconcile_cycle_legs(
        mock_redis,
        positions=[],
        orders=[],
        broker="angelone",
        cycle="thu",
        tag="THU-SL",
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
    """Verify legacy symbol NIFTY4AUG24000CE resolves to publisher key with active strategy expiry for Thursday."""
    mock_redis = MagicMock()
    # Publisher wrote canonical key using active strategy's ISO date
    publisher_key = "fno:angelone:enabled:thu:2026-08-04:24000:CE"
    mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 130}) if k == publisher_key else None

    # Broker returned legacy tradingsymbol without year
    positions = [{
        "tradingsymbol": "NIFTY4AUG24000CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|51000",
        "quantity": 130,
        "last_price": 100.0,
        "average_price": 90.0,
    }]
    orders = []

    with patch.object(thu_module, "fetch_ltp", return_value=122.0):
        res = thu_module.manage_thursday_exit_orders(
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
    """Verify reconcile_cycle_legs does NOT retire a yearless broker position across year boundary (e.g. 4JAN in 2027) for Thursday."""
    mock_redis = MagicMock()
    canonical_key = "fno:angelone:enabled:thu:2027-01-04:24000:CE"
    mock_redis.scan_iter.return_value = [canonical_key]
    mock_redis.get.return_value = "1"

    positions = [{
        "tradingsymbol": "NIFTY4JAN24000CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|60000",
        "quantity": 65,
        "last_price": 110.0,
        "average_price": 95.0,
    }]
    orders = []

    cleaned = thu_module.reconcile_cycle_legs(
        mock_redis,
        positions=positions,
        orders=orders,
        broker="angelone",
        cycle="thu",
        tag="THU-SL",
        active_expiry="2027-01-04",
    )

    assert len(cleaned) == 0
    mock_redis.delete.assert_not_called()


def test_is_market_hours_ist():
    """Verify is_market_hours_ist correctly gates pre-market, trading hours, post-market, weekends, and holidays for Thursday."""
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
    except ImportError:
        import pytz
        ist = pytz.timezone("Asia/Kolkata")

    # Thursday 11:30 IST (Open non-holiday)
    dt_open = datetime(2026, 11, 5, 11, 30, tzinfo=ist)
    is_open, _ = thu_module.is_market_hours_ist(dt_open)
    assert is_open is True

    # Thursday 09:05 IST (Pre-market)
    dt_pre = datetime(2026, 11, 5, 9, 5, tzinfo=ist)
    is_open, reason = thu_module.is_market_hours_ist(dt_pre)
    assert is_open is False
    assert "Pre-market" in reason

    # Thursday 15:29:59 IST (Open - last trading second)
    dt_last_sec = datetime(2026, 11, 5, 15, 29, 59, tzinfo=ist)
    is_open, _ = thu_module.is_market_hours_ist(dt_last_sec)
    assert is_open is True

    # Thursday 15:30:00 IST (Closed - exact market close)
    dt_close = datetime(2026, 11, 5, 15, 30, 0, tzinfo=ist)
    is_open, reason = thu_module.is_market_hours_ist(dt_close)
    assert is_open is False
    assert "Post-market" in reason

    # Thursday 15:30:40 IST (Closed - post-market)
    dt_post_40 = datetime(2026, 11, 5, 15, 30, 40, tzinfo=ist)
    is_open, reason = thu_module.is_market_hours_ist(dt_post_40)
    assert is_open is False
    assert "Post-market" in reason

    # Thursday 15:35 IST (Post-market)
    dt_post = datetime(2026, 11, 5, 15, 35, tzinfo=ist)
    is_open, reason = thu_module.is_market_hours_ist(dt_post)
    assert is_open is False
    assert "Post-market" in reason

    # Sunday 14:00 IST (Weekend)
    dt_sun = datetime(2026, 11, 8, 14, 0, tzinfo=ist)
    is_open, reason = thu_module.is_market_hours_ist(dt_sun)
    assert is_open is False
    assert "Weekend" in reason

    # Monday Republic Day 11:00 IST (Exchange Holiday)
    dt_holiday = datetime(2026, 1, 26, 11, 0, tzinfo=ist)
    is_open, reason = thu_module.is_market_hours_ist(dt_holiday)
    assert is_open is False
    assert "Exchange Holiday" in reason


def test_fno_execution_lock_mutual_exclusion():
    """Verify Thursday manager acquires shared Redis lock with TTL, heartbeat, and releases via Lua script."""
    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    with thu_module.fno_execution_lock(mock_redis, lock_key="fno:lock:exit_order_manager", timeout=60) as acquired:
        assert acquired is True
        mock_redis.set.assert_called_once()
        call_args, call_kwargs = mock_redis.set.call_args
        assert call_args[0] == "fno:lock:exit_order_manager"
        assert call_kwargs.get("nx") is True
        assert call_kwargs.get("ex") == 60

    mock_redis.eval.assert_called_once()
    assert "redis.call" in mock_redis.eval.call_args[0][0]


def test_fno_execution_lock_fail_closed_skips_broker_actions():
    """Verify Thursday manager fails closed if lock is busy."""
    mock_redis = MagicMock()
    mock_redis.set.return_value = False

    with thu_module.fno_execution_lock(mock_redis, lock_key="fno:lock:exit_order_manager", timeout=60, max_wait=0.1) as acquired:
        assert acquired is False

    mock_redis.eval.assert_not_called()


def test_fno_execution_lock_heartbeat_failure_aborts_run():
    """Verify that if the lock heartbeat lease extension fails, abort_event is set and Thursday manager halts."""
    mock_redis = MagicMock()
    mock_redis.set.return_value = True
    # Make eval return 0 during heartbeat renewal
    mock_redis.eval.return_value = 0
    abort_event = threading.Event()

    # 1. Lock heartbeat triggers abort_event on renewal failure
    with thu_module.fno_execution_lock(
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
        "quantity": 65,
        "average_price": 100.0,
        "last_price": 120.0
    }]
    orders = []
    res = thu_module.manage_thursday_exit_orders(
        token="MOCK_TOKEN",
        positions=positions,
        orders=orders,
        execute=True,
        abort_event=abort_event,
    )
    assert res.get("fatal_error") == "lock_lost"


def test_lock_loss_during_ltp_fetch_blocks_broker_order_mutations():
    """Verify that if the lock is lost while fetching LTP, Thursday order placement and modification are strictly blocked."""
    mock_redis = MagicMock()
    mock_redis.get.return_value = json.dumps({"enabled": True, "qty": 65})
    abort_event = threading.Event()

    def fake_fetch_ltp(token, token_key, broker="upstox"):
        # Simulate lock lease expiration / heartbeat failure during network quote fetch
        abort_event.set()
        return 125.0

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "instrument_token": "NSE_FO_53750",
        "quantity": 65,
        "average_price": 100.0,
        "last_price": 120.0
    }]
    orders = []

    with patch.object(thu_module, "fetch_ltp", side_effect=fake_fetch_ltp) as mock_ltp, \
         patch.object(thu_module, "place_exit_order_slm") as mock_place, \
         patch.object(thu_module, "modify_exit_order_slm") as mock_modify:

        res = thu_module.manage_thursday_exit_orders(
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
    """Verify that if lock is lost, Thursday orphan order cancellation is strictly blocked."""
    abort_event = threading.Event()
    abort_event.set()  # Lock already lost

    orders = [{
        "order_id": "ORD_ORPHAN_123",
        "trading_symbol": "NIFTY26NOV22650CE",
        "instrument_token": "NSE_FO_53750",
        "transaction_type": "SELL",
        "order_type": "SL-M",
        "status": "trigger pending",
        "quantity": 65,
        "tag": "THU-SL",
    }]

    with patch.object(thu_module, "cancel_order") as mock_cancel:
        res = thu_module.manage_thursday_exit_orders(
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
    """Verify that if market closes (15:30:00 IST reached), Thursday order placement and modifications are aborted."""
    mock_redis = MagicMock()
    mock_redis.get.return_value = json.dumps({"enabled": True, "qty": 65})

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "instrument_token": "NSE_FO_53750",
        "quantity": 65,
        "average_price": 100.0,
        "last_price": 120.0
    }]
    orders = []

    with patch.object(thu_module, "fetch_ltp", return_value=125.0) as mock_ltp, \
         patch.object(thu_module, "is_market_hours_ist", return_value=(False, "Post-market (15:30:05 IST >= 15:30:00 IST); market is closed")), \
         patch.object(thu_module, "place_exit_order_slm") as mock_place, \
         patch.object(thu_module, "modify_exit_order_slm") as mock_modify:

        res = thu_module.manage_thursday_exit_orders(
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


def test_unsupported_broker_rejection():
    """Verify Thursday manager rejects unsupported brokers fail-closed."""
    with pytest.raises((RuntimeError, ValueError), match="Unsupported broker"):
        thu_module.get_access_token(broker="zerodha")

    with patch("sys.argv", ["Thursday-Fno-Exit-Orders-V1.py", "--broker", "zerodha", "--mock"]):
        code = thu_module.main()
        assert code == 1


def test_angelone_place_order_slm_payload():
    """Verify AngelOne place_exit_order_slm constructs valid SmartAPI STOPLOSS payload."""
    pos = {
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "symboltoken": "53750",
        "exchange": "NFO",
        "producttype": "CARRYFORWARD",
    }
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": True, "data": {"orderid": "ANGEL_12345"}}
        mock_post.return_value = mock_resp

        res = thu_module.place_exit_order_slm(
            token="JWT_TOKEN",
            position=pos,
            target_qty=65,
            trigger_price=95.50,
            product="CARRYFORWARD",
            tag="THU-SL",
            broker="angelone",
        )
        assert res["success"] is True
        assert res["order_id"] == "ANGEL_12345"
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["variety"] == "STOPLOSS"
        assert payload["ordertype"] == "STOPLOSS_MARKET"
        assert payload["producttype"] == "CARRYFORWARD"
        assert payload["transactiontype"] == "SELL"
        assert payload["symboltoken"] == "53750"
        assert payload["triggerprice"] == "95.50"
        assert payload["price"] == "0"


def test_angelone_order_recognized_via_redis_stored_id_without_tag():
    """Verify an open order without custom tag THU-SL is recognized as managed via Redis stored order_id."""
    mock_redis = MagicMock()
    call_key = "fno:angelone:enabled:thu:2026-11-03:22650:CE"

    def redis_get(k):
        if k == call_key:
            return json.dumps({"enabled": True, "qty": 65})
        if "order_id" in k:
            return "ANGEL_SL_999"
        if "sl" in k:
            return "90.0"
        if "max" in k:
            return "110.0"
        return None

    mock_redis.get.side_effect = redis_get
    mock_redis.scan_iter.return_value = []

    positions = [{
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "exchange": "NFO",
        "instrument_token": "NSE_FO|53750",
        "symboltoken": "53750",
        "quantity": 65,
        "last_price": 120.0,
        "average_price": 100.0,
    }]
    orders = [{
        "orderid": "ANGEL_SL_999",
        "tradingsymbol": "NIFTY03NOV2622650CE",
        "symboltoken": "53750",
        "orderstatus": "trigger pending",
        "transactiontype": "SELL",
        "ordertype": "STOPLOSS_MARKET",
        "producttype": "CARRYFORWARD",
        "quantity": 65,
        "unfilledshares": 65,
        "triggerprice": 90.0,
        "tag": "",
    }]

    with patch.object(thu_module, "fetch_ltp", return_value=120.0):
        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=False,
            redis_client=mock_redis,
        )

    assert res["authorized_positions"] == 1
    assert len(res["actions"]) == 1
    action = res["actions"][0]
    assert action["status"] != "manual_sell_conflict"
    assert action["existing_order_id"] == "ANGEL_SL_999"


def test_cycle_isolated_execution_lock_defaults():
    """Verify Thursday uses isolated lock key fno:lock:exit_order_manager:thursday (Option A)."""
    assert thu_module.DEFAULT_LOCK_KEY == "fno:lock:exit_order_manager:thursday"


def test_parse_order_book_raw_angelone_payload():
    """Verify parse_order_book normalizes a raw AngelOne SmartAPI orderbook response."""
    raw_payload = {
        "status": True,
        "message": "SUCCESS",
        "errorcode": "",
        "data": [
            {
                "orderid": "241103000123",
                "tradingsymbol": "NIFTY03NOV2622650CE",
                "symboltoken": "53750",
                "transactiontype": "SELL",
                "exchange": "NFO",
                "ordertype": "STOPLOSS_MARKET",
                "producttype": "CARRYFORWARD",
                "duration": "DAY",
                "price": 0.0,
                "triggerprice": 105.50,
                "quantity": 65,
                "filledshares": 0,
                "unfilledshares": 65,
                "orderstatus": "trigger pending",
            }
        ]
    }
    parsed = thu_module.parse_order_book(raw_payload)
    assert len(parsed) == 1
    o = parsed[0]
    assert o["order_id"] == "241103000123"
    assert o["symboltoken"] == "53750"
    assert o["instrument_token"] == "53750"
    assert o["trading_symbol"] == "NIFTY03NOV2622650CE"
    assert o["transaction_type"] == "SELL"
    assert o["order_type"] == "STOPLOSS_MARKET"
    assert o["product"] == "CARRYFORWARD"
    assert o["quantity"] == 65
    assert o["pending_quantity"] == 65
    assert o["trigger_price"] == 105.50
    assert o["status"] == "trigger pending"


def test_parse_order_book_unexpected_shape_raises():
    """Verify parse_order_book fails closed (raises ValueError) on unexpected response shapes."""
    # Data is an unexpected object instead of a list
    with pytest.raises(ValueError, match="Unexpected order book response shape"):
        thu_module.parse_order_book({"status": False, "data": {"error": "Corrupted structure"}})

    # Non-dict and non-list payload
    with pytest.raises(ValueError, match="Unexpected order book response shape"):
        thu_module.parse_order_book("raw_string_error")


def test_angelone_modify_order_slm_payload():
    """Verify AngelOne modify_exit_order_slm constructs valid SmartAPI modifyOrder payload."""
    order = {
        "order_id": "ANGEL_12345",
        "trading_symbol": "NIFTY03NOV2622650CE",
        "instrument_token": "53750",
        "exchange": "NFO",
        "product": "CARRYFORWARD",
        "filled_quantity": 0,
    }
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": True, "data": {"orderid": "ANGEL_12345"}}
        mock_post.return_value = mock_resp

        res = thu_module.modify_exit_order_slm(
            token="JWT_TOKEN",
            order=order,
            target_qty=65,
            trigger_price=105.50,
            tag="THU-SL",
            broker="angelone",
        )
        assert res["success"] is True
        assert res["order_id"] == "ANGEL_12345"
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["variety"] == "STOPLOSS"
        assert payload["orderid"] == "ANGEL_12345"
        assert payload["ordertype"] == "STOPLOSS_MARKET"
        assert payload["producttype"] == "CARRYFORWARD"
        assert payload["triggerprice"] == "105.50"
        assert payload["quantity"] == "65"
        assert payload["symboltoken"] == "53750"


def test_angelone_cancel_order_payload():
    """Verify AngelOne cancel_order constructs valid SmartAPI cancelOrder payload."""
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": True, "message": "SUCCESS"}
        mock_post.return_value = mock_resp

        res = thu_module.cancel_order(
            token="JWT_TOKEN",
            order_id="ANGEL_ORPHAN_999",
            broker="angelone",
        )
        assert res["success"] is True
        assert res["order_id"] == "ANGEL_ORPHAN_999"
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "cancelOrder" in url
        payload = mock_post.call_args[1]["json"]
        assert payload["variety"] == "STOPLOSS"
        assert payload["orderid"] == "ANGEL_ORPHAN_999"


class MockRedis:
    def __init__(self):
        self.data = {}
    def get(self, key):
        return self.data.get(key)
    def set(self, key, value):
        self.data[key] = str(value)
        return True
    def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)
        return True
    def pipeline(self):
        return self
    def execute(self):
        return True
    def scan_iter(self, match=None, count=None):
        return [k for k in self.data.keys() if match is None or match.replace("*", "") in k]
    def expire(self, key, seconds):
        return True
    def sadd(self, key, *values):
        s = self.data.setdefault(key, set())
        if isinstance(s, set):
            for v in values:
                s.add(str(v))
        return True
    def smembers(self, key):
        return self.data.get(key, set())


def test_angelone_place_order_market_payload():
    """Verify AngelOne place_exit_order_market constructs valid SmartAPI MARKET sell payload."""
    pos = {
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "exchange": "NFO",
        "producttype": "CARRYFORWARD",
    }
    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": True, "data": {"orderid": "angel_mkt_123"}}
        mock_post.return_value = mock_resp

        res = thu_module.place_exit_order_market(
            token="JWT_TOKEN",
            position=pos,
            target_qty=65,
            product="CARRYFORWARD",
            tag="THU-SL",
            broker="angelone",
        )

        assert res.get("success") is True
        assert res.get("order_id") == "angel_mkt_123"
        payload = mock_post.call_args[1]["json"]
        assert payload["quantity"] == "65"
        assert payload["producttype"] == "CARRYFORWARD"
        assert payload["duration"] == "DAY"
        assert payload["ordertype"] == "MARKET"
        assert payload["transactiontype"] == "SELL"
        assert payload["price"] == "0"
        assert payload["triggerprice"] == "0"
        assert payload["variety"] == "NORMAL"
        assert payload["symboltoken"] == "53750"


def test_thursday_exit_immediately_places_market_order_and_skips_ltp_and_slm():
    """Verify exit_immediately=True skips fetch_ltp, skips SL-M, and places MARKET sell order."""
    mock_redis = MockRedis()
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY26NOV22650CE", "53750")
    mock_redis.set(keys["enabled_canonical"], "1")

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "exchange": "NFO",
        "quantity": 65,
        "netqty": "65",
        "average_price": 100.0,
        "cfbuyavgprice": "100.0",
        "last_price": 110.0,
    }]
    orders = []

    with patch.object(thu_module, "fetch_ltp") as mock_ltp, \
         patch.object(thu_module, "place_exit_order_slm") as mock_place_slm, \
         patch.object(thu_module, "modify_exit_order_slm") as mock_modify_slm, \
         patch.object(thu_module, "is_market_hours_ist", return_value=(True, "Open")), \
         patch.object(thu_module, "place_exit_order_market", return_value={"success": True, "order_id": "angel_mkt_999"}) as mock_place_market:

        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            exit_immediately=True,
            redis_client=mock_redis,
        )

        mock_ltp.assert_not_called()
        mock_place_slm.assert_not_called()
        mock_modify_slm.assert_not_called()

        mock_place_market.assert_called_once()
        assert res.get("placed") == 1
        action = res["actions"][0]
        assert action["status"] == "place_market"
        assert action["mode"] == "immediate"
        assert action["order_id"] == "angel_mkt_999"

        assert mock_redis.get(keys["mode"]) == "immediate"
        assert mock_redis.get(keys["market_order_id"]) == "angel_mkt_999"
        assert "angel_mkt_999" in mock_redis.smembers("fno:angelone:managed_order_ids")


def test_thursday_exit_immediately_cancels_existing_slm_before_market_sell():
    """Verify an open SL-M is cancelled and confirmed before submitting the MARKET sell order."""
    mock_redis = MockRedis()
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY26NOV22650CE", "53750")
    mock_redis.set(keys["enabled_canonical"], "1")

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "exchange": "NFO",
        "quantity": 65,
        "netqty": "65",
        "average_price": 100.0,
        "cfbuyavgprice": "100.0",
        "last_price": 110.0,
    }]
    orders = [{
        "order_id": "angel_slm_12345",
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "transactiontype": "SELL",
        "transaction_type": "SELL",
        "status": "trigger_pending",
        "ordertype": "STOPLOSS_MARKET",
        "order_type": "STOPLOSS_MARKET",
        "variety": "STOPLOSS",
        "quantity": 65,
        "pending_quantity": 65,
        "tag": "THU-SL",
    }]

    terminal_orders = [{
        "order_id": "angel_slm_12345",
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "transactiontype": "SELL",
        "status": "cancelled",
        "ordertype": "STOPLOSS_MARKET",
        "variety": "STOPLOSS",
        "tag": "THU-SL",
    }]

    with patch.object(thu_module, "cancel_order", return_value={"success": True, "action": "cancel"}) as mock_cancel, \
         patch.object(thu_module, "fetch_order_book", return_value=terminal_orders), \
         patch.object(thu_module, "fetch_positions", return_value={"data": positions}), \
         patch.object(thu_module, "is_market_hours_ist", return_value=(True, "Open")), \
         patch.object(thu_module, "place_exit_order_market", return_value={"success": True, "order_id": "angel_mkt_777"}) as mock_place_market:

        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            exit_immediately=True,
            redis_client=mock_redis,
        )

        mock_cancel.assert_called_once_with("REAL_TOKEN", "angel_slm_12345", broker="angelone", variety="STOPLOSS")
        mock_place_market.assert_called_once()
        assert res.get("placed") == 1
        assert res["actions"][0]["status"] == "place_market"
        assert res["actions"][0]["order_id"] == "angel_mkt_777"


def test_thursday_exit_immediately_defers_market_order_if_slm_cancel_missing_from_order_book():
    """Verify that if follow-up order book omits the cancelled SL-M order, market order is deferred."""
    mock_redis = MockRedis()
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY26NOV22650CE", "53750")
    mock_redis.set(keys["enabled_canonical"], "1")
    mock_redis.set(keys["order_id"], "angel_slm_12345")

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "exchange": "NFO",
        "quantity": 65,
        "netqty": "65",
        "average_price": 100.0,
        "cfbuyavgprice": "100.0",
        "last_price": 110.0,
    }]
    orders = [{
        "order_id": "angel_slm_12345",
        "orderid": "angel_slm_12345",
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "transaction_type": "SELL",
        "transactiontype": "SELL",
        "status": "trigger pending",
        "order_type": "STOPLOSS_MARKET",
        "ordertype": "STOPLOSS_MARKET",
        "tag": "THU-SL",
        "quantity": 65,
        "pending_quantity": 65,
    }]

    with patch.object(thu_module, "cancel_order", return_value={"success": True, "action": "cancel"}) as mock_cancel, \
         patch.object(thu_module, "fetch_order_book", return_value=[]), \
         patch.object(thu_module, "is_market_hours_ist", return_value=(True, "Open")), \
         patch.object(thu_module, "place_exit_order_market") as mock_place_market:

        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            exit_immediately=True,
            redis_client=mock_redis,
        )

        mock_cancel.assert_called_once_with("REAL_TOKEN", "angel_slm_12345", broker="angelone", variety="STOPLOSS")
        mock_place_market.assert_not_called()
        assert res.get("placed") == 0
        assert res["actions"][0]["status"] == "cancel_slm_missing"
        assert res["actions"][0]["order_id"] == "angel_slm_12345"
        # Redis state is preserved
        assert mock_redis.get(keys["order_id"]) == "angel_slm_12345"


def test_thursday_exit_immediately_defers_market_order_if_slm_cancel_has_unknown_status():
    """Verify that if follow-up order book returns an unknown status, market order is deferred."""
    mock_redis = MockRedis()
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY26NOV22650CE", "53750")
    mock_redis.set(keys["enabled_canonical"], "1")
    mock_redis.set(keys["order_id"], "angel_slm_12345")

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "exchange": "NFO",
        "quantity": 65,
        "netqty": "65",
        "average_price": 100.0,
        "cfbuyavgprice": "100.0",
        "last_price": 110.0,
    }]
    orders = [{
        "order_id": "angel_slm_12345",
        "orderid": "angel_slm_12345",
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "transaction_type": "SELL",
        "transactiontype": "SELL",
        "status": "trigger pending",
        "order_type": "STOPLOSS_MARKET",
        "ordertype": "STOPLOSS_MARKET",
        "tag": "THU-SL",
        "quantity": 65,
        "pending_quantity": 65,
    }]

    unrecognized_orders = [{
        "order_id": "angel_slm_12345",
        "orderid": "angel_slm_12345",
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "transaction_type": "SELL",
        "status": "broker_sync_wait",  # Unrecognized status
        "order_type": "STOPLOSS_MARKET",
        "tag": "THU-SL",
    }]

    with patch.object(thu_module, "cancel_order", return_value={"success": True, "action": "cancel"}) as mock_cancel, \
         patch.object(thu_module, "fetch_order_book", return_value=unrecognized_orders), \
         patch.object(thu_module, "is_market_hours_ist", return_value=(True, "Open")), \
         patch.object(thu_module, "place_exit_order_market") as mock_place_market:

        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            exit_immediately=True,
            redis_client=mock_redis,
        )

        mock_cancel.assert_called_once_with("REAL_TOKEN", "angel_slm_12345", broker="angelone", variety="STOPLOSS")
        mock_place_market.assert_not_called()
        assert res.get("placed") == 0
        assert res["actions"][0]["status"] == "cancel_slm_unknown_status"
        assert res["actions"][0]["order_id"] == "angel_slm_12345"


def test_thursday_exit_immediately_defers_market_order_if_prior_stored_slm_missing_from_order_book():
    """Verify that if existing is None but Redis has prior SL-M that is missing from order book, market order is deferred."""
    mock_redis = MockRedis()
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY26NOV22650CE", "53750")
    mock_redis.set(keys["enabled_canonical"], "1")
    mock_redis.set(keys["order_id"], "prior_angel_slm_888")

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "exchange": "NFO",
        "quantity": 65,
        "netqty": "65",
        "average_price": 100.0,
        "cfbuyavgprice": "100.0",
        "last_price": 110.0,
    }]
    orders = []  # Prior SL-M is missing from initial order book

    with patch.object(thu_module, "is_market_hours_ist", return_value=(True, "Open")), \
         patch.object(thu_module, "place_exit_order_market") as mock_place_market:

        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            exit_immediately=True,
            redis_client=mock_redis,
        )

        mock_place_market.assert_not_called()
        assert res.get("placed") == 0
        assert res["actions"][0]["status"] == "cancel_slm_missing"
        assert res["actions"][0]["order_id"] == "prior_angel_slm_888"


def test_thursday_exit_immediately_manual_sell_conflict():
    """Verify that an untagged manual open SELL order prevents market sell order placement."""
    mock_redis = MockRedis()
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY26NOV22650CE", "53750")
    mock_redis.set(keys["enabled_canonical"], "1")

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "exchange": "NFO",
        "quantity": 65,
        "netqty": "65",
        "average_price": 100.0,
        "cfbuyavgprice": "100.0",
        "last_price": 110.0,
    }]
    orders = [{
        "order_id": "manual_sell_888",
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "transaction_type": "SELL",
        "transactiontype": "SELL",
        "status": "open",
        "order_type": "LIMIT",
        "ordertype": "LIMIT",
        "tag": "",  # Manual untagged order
        "quantity": 65,
        "pending_quantity": 65,
    }]

    with patch.object(thu_module, "place_exit_order_market") as mock_place_market:
        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            exit_immediately=True,
            redis_client=mock_redis,
        )

        mock_place_market.assert_not_called()
        action = res["actions"][0]
        assert action["status"] == "manual_sell_conflict"
        assert action["mode"] == "immediate"


def test_thursday_exit_immediately_persists_across_runs_via_redis():
    """Verify that once immediate mode is recorded in Redis, subsequent runs without CLI flag keep immediate mode."""
    mock_redis = MockRedis()
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY26NOV22650CE", "53750")
    mock_redis.set(keys["enabled_canonical"], "1")
    mock_redis.set(keys["mode"], "immediate")

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "exchange": "NFO",
        "quantity": 65,
        "netqty": "65",
        "average_price": 100.0,
        "cfbuyavgprice": "100.0",
        "last_price": 110.0,
    }]
    orders = []

    with patch.object(thu_module, "fetch_ltp") as mock_ltp, \
         patch.object(thu_module, "is_market_hours_ist", return_value=(True, "Open")), \
         patch.object(thu_module, "place_exit_order_market", return_value={"success": True, "order_id": "angel_mkt_888"}) as mock_place_market:

        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            exit_immediately=False,  # CLI flag omitted!
            redis_client=mock_redis,
        )

        mock_ltp.assert_not_called()
        mock_place_market.assert_called_once()
        assert res["actions"][0]["mode"] == "immediate"
        assert res["actions"][0]["status"] == "place_market"


def test_thursday_exit_immediately_prevents_duplicate_in_flight_market_order():
    """Verify that if a market order is already in-flight in order book, duplicate placement is prevented."""
    mock_redis = MockRedis()
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY26NOV22650CE", "53750")
    mock_redis.set(keys["enabled_canonical"], "1")
    mock_redis.set(keys["mode"], "immediate")
    mock_redis.set(keys["market_order_id"], "angel_mkt_in_flight_001")

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "exchange": "NFO",
        "quantity": 65,
        "netqty": "65",
        "average_price": 100.0,
        "cfbuyavgprice": "100.0",
        "last_price": 110.0,
    }]
    orders = [{
        "order_id": "angel_mkt_in_flight_001",
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "transactiontype": "SELL",
        "transaction_type": "SELL",
        "status": "open",
        "ordertype": "MARKET",
        "order_type": "MARKET",
        "tag": "THU-SL",
        "quantity": 65,
        "pending_quantity": 65,
    }]

    with patch.object(thu_module, "place_exit_order_market") as mock_place_market:
        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            exit_immediately=True,
            redis_client=mock_redis,
        )

        mock_place_market.assert_not_called()
        action = res["actions"][0]
        assert action["status"] == "market_order_in_flight"
        assert action["order_id"] == "angel_mkt_in_flight_001"


def test_thursday_exit_immediately_dry_run():
    """Verify dry-run mode (--dry-run or execute=False) reports place_market without API calls."""
    mock_redis = MockRedis()
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY26NOV22650CE", "53750")
    mock_redis.set(keys["enabled_canonical"], "1")

    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "exchange": "NFO",
        "quantity": 65,
        "netqty": "65",
        "average_price": 100.0,
        "cfbuyavgprice": "100.0",
        "last_price": 110.0,
    }]
    orders = []

    with patch.object(thu_module, "place_exit_order_market") as mock_place_market:
        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=False,  # Dry run
            exit_immediately=True,
            redis_client=mock_redis,
        )

        mock_place_market.assert_not_called()
        assert res.get("dry_run") is True
        action = res["actions"][0]
        assert action["status"] == "place_market"
        assert action["mode"] == "immediate"
        assert action["dry_run"] is True
        assert action["action_needed"] is True


def test_thursday_exit_immediately_partial_fill_retry_reconciles_quantity_when_position_stale():
    """Verify that when a prior market order was partially filled (40 filled, 25 cancelled)
    and the position snapshot is still stale at 65, the manager submits a retry for only the
    remaining 25 contracts, preventing a short position."""
    mock_redis = MockRedis()
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY26NOV22650CE", "53750")
    mock_redis.set(keys["enabled_canonical"], "1")
    mock_redis.set(keys["mode"], "immediate")
    mock_redis.set(keys["market_order_id"], "angel_mkt_partial_001")

    # Position snapshot is STILL STALE at 65
    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "exchange": "NFO",
        "quantity": 65,
        "netqty": "65",
        "average_price": 100.0,
        "cfbuyavgprice": "100.0",
        "last_price": 110.0,
    }]

    # Prior market order: 40 filled, 25 cancelled (terminal)
    orders = [{
        "order_id": "angel_mkt_partial_001",
        "orderid": "angel_mkt_partial_001",
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "transaction_type": "SELL",
        "transactiontype": "SELL",
        "status": "cancelled",
        "ordertype": "MARKET",
        "order_type": "MARKET",
        "tag": "THU-SL",
        "quantity": 65,
        "filled_quantity": 40,
        "pending_quantity": 0,
        "cancelsize": 25,
    }]

    with patch.object(thu_module, "is_market_hours_ist", return_value=(True, "Open")), \
         patch.object(thu_module, "fetch_positions", return_value={"data": positions}), \
         patch.object(thu_module, "place_exit_order_market", return_value={"success": True, "order_id": "angel_mkt_retry_002"}) as mock_place_market:

        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            target_lots=1,
            lot_size=65,
            execute=True,
            exit_immediately=True,
            redis_client=mock_redis,
        )

        # MUST submit only the remaining 25 contracts, NOT 65
        mock_place_market.assert_called_once()
        call_args = mock_place_market.call_args
        submitted_qty = call_args[1].get("target_qty") or call_args[0][2]
        assert submitted_qty == 25, f"Expected 25 qty to be submitted, but got {submitted_qty}"

        assert res.get("placed") == 1
        action = res["actions"][0]
        assert action["status"] == "place_market"
        assert action["quantity"] == 25
        assert action["order_id"] == "angel_mkt_retry_002"
        # Redis market_order_id updated to the retry order
        assert mock_redis.get(keys["market_order_id"]) == "angel_mkt_retry_002"


def test_thursday_exit_immediately_partial_fill_retry_with_updated_position_snapshot():
    """Verify that when a prior market order was partially filled (40 filled out of 65)
    and the broker position snapshot has already updated to 25, the manager submits a retry
    for exactly the remaining 25 contracts, not skipping the exit."""
    mock_redis = MockRedis()
    keys = thu_module.build_trail_redis_keys("angelone", "NIFTY26NOV22650CE", "53750")
    mock_redis.set(keys["enabled_canonical"], "1")
    mock_redis.set(keys["mode"], "immediate")
    mock_redis.set(keys["market_order_id"], "angel_mkt_partial_001")

    # Position snapshot has ALREADY UPDATED to 25 long
    positions = [{
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "exchange": "NFO",
        "quantity": 25,
        "netqty": "25",
        "average_price": 100.0,
        "cfbuyavgprice": "100.0",
        "last_price": 110.0,
    }]

    # Prior market order: 40 filled, 25 cancelled (terminal)
    orders = [{
        "order_id": "angel_mkt_partial_001",
        "orderid": "angel_mkt_partial_001",
        "tradingsymbol": "NIFTY26NOV22650CE",
        "symboltoken": "53750",
        "instrument_token": "53750",
        "transaction_type": "SELL",
        "transactiontype": "SELL",
        "status": "cancelled",
        "ordertype": "MARKET",
        "order_type": "MARKET",
        "tag": "THU-SL",
        "quantity": 65,
        "filled_quantity": 40,
        "pending_quantity": 0,
        "cancelsize": 25,
    }]

    with patch.object(thu_module, "is_market_hours_ist", return_value=(True, "Open")), \
         patch.object(thu_module, "fetch_positions", return_value={"data": positions}), \
         patch.object(thu_module, "place_exit_order_market", return_value={"success": True, "order_id": "angel_mkt_retry_003"}) as mock_place_market:

        res = thu_module.manage_thursday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            target_lots=1,
            lot_size=65,
            execute=True,
            exit_immediately=True,
            redis_client=mock_redis,
        )

        # MUST submit retry for remaining 25 contracts, NOT skip
        mock_place_market.assert_called_once()
        call_args = mock_place_market.call_args
        submitted_qty = call_args[1].get("target_qty") or call_args[0][2]
        assert submitted_qty == 25, f"Expected 25 qty to be submitted, but got {submitted_qty}"

        assert res.get("placed") == 1
        action = res["actions"][0]
        assert action["status"] == "place_market"
        assert action["quantity"] == 25
        assert action["order_id"] == "angel_mkt_retry_003"
        assert mock_redis.get(keys["market_order_id"]) == "angel_mkt_retry_003"








