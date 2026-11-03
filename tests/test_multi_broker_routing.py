"""
tests/test_multi_broker_routing.py - Multi-Broker Routing & Interchangeability Tests

Tests:
1. Config resolution & validation (get_broker_for_track, case-insensitive, strip whitespace, rejection of unknown brokers/tracks).
2. All 4 strategy cycle broker combinations:
   - Tuesday Upstox / Thursday AngelOne (default)
   - Tuesday AngelOne / Thursday Upstox (swapped)
   - Tuesday Upstox / Thursday Upstox (both Upstox)
   - Tuesday AngelOne / Thursday AngelOne (both AngelOne)
3. Active cycle persistence & fail-closed protection when config contradicts stored broker.
4. Combined execution locks (cycle lock + broker lock). Same broker prevents concurrent mutations; different brokers run concurrently.
5. Exit authorization Redis keys formatted with persisted/configured broker.
6. Unified broker adapter interface polymorphic behavior.
"""

from __future__ import annotations

import json
import os
import sys
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

thu_spec = importlib.util.spec_from_file_location(
    "thursday_exit_orders",
    os.path.join(scripts_dir, "Thursday-Fno-Exit-Orders-V1.py")
)
thu_module = importlib.util.module_from_spec(thu_spec)
thu_spec.loader.exec_module(thu_module)

from common.broker_adapters import (
    BrokerAdapter,
    UpstoxAdapter,
    AngelOneAdapter,
    get_broker_adapter,
)
from config import get_broker_for_track, SUPPORTED_BROKERS
from common.unified_strategy_store import (
    get_active_leg_entry_details,
    publish_exit_authorization_to_redis,
)


class TestConfigBrokerRouting:
    """Test get_broker_for_track resolver and configuration precedence."""

    def test_default_track_broker_resolution(self):
        """Without env overrides, Tuesday is upstox and Thursday is angelone."""
        with patch.dict(os.environ, {}, clear=True):
            assert get_broker_for_track("5day_strategy") == "upstox"
            assert get_broker_for_track("tuesday") == "upstox"
            assert get_broker_for_track("3day_strategy") == "angelone"
            assert get_broker_for_track("thursday") == "angelone"

    def test_swapped_broker_configuration(self):
        """Tuesday on AngelOne, Thursday on Upstox."""
        with patch.dict(os.environ, {"TUESDAY_BROKER": "angelone", "THURSDAY_BROKER": "upstox"}):
            assert get_broker_for_track("5day_strategy") == "angelone"
            assert get_broker_for_track("tuesday") == "angelone"
            assert get_broker_for_track("3day_strategy") == "upstox"
            assert get_broker_for_track("thursday") == "upstox"

    def test_both_upstox_configuration(self):
        """Both cycles on Upstox."""
        with patch.dict(os.environ, {"TUESDAY_BROKER": "upstox", "THURSDAY_BROKER": "upstox"}):
            assert get_broker_for_track("5day_strategy") == "upstox"
            assert get_broker_for_track("3day_strategy") == "upstox"

    def test_both_angelone_configuration(self):
        """Both cycles on AngelOne."""
        with patch.dict(os.environ, {"TUESDAY_BROKER": "angelone", "THURSDAY_BROKER": "angelone"}):
            assert get_broker_for_track("5day_strategy") == "angelone"
            assert get_broker_for_track("3day_strategy") == "angelone"

    def test_whitespace_and_case_normalization(self):
        """Broker names with mixed case and whitespace normalize cleanly."""
        with patch.dict(os.environ, {"TUESDAY_BROKER": "  UpStOx  ", "THURSDAY_BROKER": "  AngelOne  "}):
            assert get_broker_for_track("5day_strategy") == "upstox"
            assert get_broker_for_track("3day_strategy") == "angelone"

    def test_unsupported_broker_in_env_raises(self):
        """Unsupported broker values raise ValueError fail-closed."""
        with patch.dict(os.environ, {"TUESDAY_BROKER": "zerodha"}):
            with pytest.raises(ValueError, match="Unsupported broker 'zerodha'"):
                get_broker_for_track("5day_strategy")

    def test_unknown_track_raises(self):
        """Unknown strategy track names raise ValueError."""
        with pytest.raises(ValueError, match="Unknown strategy track"):
            get_broker_for_track("scalping_strategy")


class TestBrokerAdapterPolymorphism:
    """Test unified broker adapter factory and mock-isolated responses."""

    def test_get_broker_adapter_returns_correct_types(self):
        upstox = get_broker_adapter("upstox")
        assert isinstance(upstox, UpstoxAdapter)
        assert upstox.name == "upstox"

        angel = get_broker_adapter("angelone")
        assert isinstance(angel, AngelOneAdapter)
        assert angel.name == "angelone"

        # Alias angel
        angel_alias = get_broker_adapter("angel")
        assert isinstance(angel_alias, AngelOneAdapter)

    def test_get_broker_adapter_unsupported_raises(self):
        with pytest.raises(ValueError, match="Unsupported broker 'dhan'"):
            get_broker_adapter("dhan")

    def test_upstox_adapter_mocked_calls(self):
        adapter = get_broker_adapter("upstox")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success",
            "data": [{"tradingsymbol": "NIFTY03NOV2622650CE", "instrument_token": "NSE_FO|53750", "quantity": 65}]
        }

        with patch("requests.get", return_value=mock_resp):
            pos = adapter.fetch_positions("TOKEN")
            assert pos.get("status") == "success"
            assert len(pos["data"]) == 1

            orders_resp = MagicMock()
            orders_resp.status_code = 200
            orders_resp.json.return_value = {"status": "success", "data": []}
            with patch("requests.get", return_value=orders_resp):
                orders = adapter.fetch_order_book("TOKEN")
                assert isinstance(orders, list)

            ltp_resp = MagicMock()
            ltp_resp.status_code = 200
            ltp_resp.json.return_value = {
                "status": "success",
                "data": {"NSE_FO:53750": {"last_price": 122.0}}
            }
            with patch("requests.get", return_value=ltp_resp):
                ltp = adapter.fetch_ltp("TOKEN", "NSE_FO|53750")
                assert ltp == 122.0

    def test_angelone_adapter_mocked_calls(self):
        adapter = get_broker_adapter("angelone")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": True,
            "data": [{"tradingsymbol": "NIFTY03NOV2622650CE", "symboltoken": "53750", "netqty": "65"}]
        }

        with patch("requests.get", return_value=mock_resp):
            pos = adapter.fetch_positions("TOKEN")
            assert pos.get("status") is True
            assert len(pos["data"]) == 1

        ltp_resp = MagicMock()
        ltp_resp.status_code = 200
        ltp_resp.json.return_value = {"status": True, "data": {"fetched": [{"ltp": 122.0}]}}
        with patch("requests.post", return_value=ltp_resp):
            ltp = adapter.fetch_ltp("TOKEN", "53750", trading_symbol="NIFTY03NOV2622650CE")
            assert ltp == 122.0


class TestActiveCyclePersistedBrokerFailClosed:
    """Verify that exit managers enforce active cycle broker persistence against env changes."""

    def test_thursday_fails_closed_when_persisted_broker_mismatches_configured(self):
        """If active cycle was entered on AngelOne, but config was changed to Upstox, exit manager must fail closed."""
        with patch.object(thu_module, "get_active_strategy_broker", return_value="angelone"), \
             patch("sys.argv", ["Thursday-Fno-Exit-Orders-V1.py", "--broker", "upstox", "--mock"]):
            code = thu_module.main()
            assert code == 1

    def test_thursday_succeeds_when_persisted_broker_matches_configured(self):
        """When persisted broker matches configured broker, dry-run succeeds."""
        with patch.object(thu_module, "get_active_strategy_broker", return_value="upstox"), \
             patch("sys.argv", ["Thursday-Fno-Exit-Orders-V1.py", "--broker", "upstox", "--mock"]):
            code = thu_module.main()
            assert code == 0

    def test_tuesday_fails_closed_when_persisted_broker_mismatches_configured(self):
        """If Tuesday active cycle was entered on Upstox, but config was changed to AngelOne, fail closed."""
        with patch.object(tue_module, "get_active_strategy_broker", return_value="upstox"), \
             patch("sys.argv", ["Tuesday-Fno-Exit-Orders-V1.py", "--broker", "angelone", "--mock"]):
            code = tue_module.main()
            assert code == 1

    def test_tuesday_succeeds_when_persisted_broker_matches_configured(self):
        """When Tuesday persisted broker matches configured broker, dry-run succeeds."""
        with patch.object(tue_module, "get_active_strategy_broker", return_value="angelone"), \
             patch("sys.argv", ["Tuesday-Fno-Exit-Orders-V1.py", "--broker", "angelone", "--mock"]):
            code = tue_module.main()
            assert code == 0


class TestCombinedBrokerExecutionLocks:
    """Verify combined cycle + broker execution locks prevent concurrent operations on the same broker."""

    def test_combined_lock_acquires_cycle_and_broker_locks(self):
        """Verify lock keys: fno:lock:exit_order_manager:{cycle} AND fno:lock:broker:{broker}."""
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with thu_module.combined_execution_lock(
            mock_redis, cycle_name="thursday", broker="angelone", timeout=60
        ) as (acquired, reason):
            assert acquired is True
            assert reason is None
            # Must acquire cycle lock then broker lock
            set_calls = [call[0][0] for call in mock_redis.set.call_args_list]
            assert "fno:lock:exit_order_manager:thursday" in set_calls
            assert "fno:lock:broker:angelone" in set_calls

    def test_same_broker_contention_fails_closed(self):
        """If broker lock is held by another worker, the second manager fails closed gracefully."""
        mock_redis = MagicMock()
        # Cycle lock succeeds, but broker lock is busy
        def mock_set(key, token, nx=False, ex=None):
            if "broker:upstox" in key:
                return False
            return True

        mock_redis.set.side_effect = mock_set

        with tue_module.combined_execution_lock(
            mock_redis, cycle_name="tuesday", broker="upstox", timeout=60, max_wait=0.05
        ) as (acquired, reason):
            assert acquired is False
            assert "Broker lock" in reason


class TestExitAuthorizationBrokerRouting:
    """Verify publish_exit_authorization_to_redis routes keys to the correct broker prefix."""

    def test_publish_exit_authorization_uses_persisted_strategy_broker(self):
        mock_redis = MagicMock()
        strategy_data = {
            "cycle_id": "test_cycle_01",
            "cycle_status": "ENTERED",
            "broker": "angelone",  # Explicitly persisted broker
            "call_strike": 22650,
            "put_strike": 22800,
            "call_symbol": "NIFTY03NOV2622650CE",
            "put_symbol": "NIFTY03NOV2622800PE",
            "expiry_date": "2026-11-03",
        }

        published = publish_exit_authorization_to_redis(
            track="5day_strategy",
            call_exit=True,
            put_exit=True,
            strategy_data=strategy_data,
            redis_client=mock_redis,
        )
        assert len(published) == 2

        # Verify keys written to Redis have broker='angelone' prefix despite track being 5day_strategy
        set_keys = [call[0][0] for call in mock_redis.set.call_args_list]
        assert any("fno:angelone:enabled:tue:2026-11-03:22650:CE" in k for k in set_keys)
        assert any("fno:angelone:enabled:tue:2026-11-03:22800:PE" in k for k in set_keys)

    def test_publish_exit_authorization_falls_back_to_track_broker_when_not_persisted(self):
        mock_redis = MagicMock()
        strategy_data = {
            "cycle_id": "test_cycle_02",
            "cycle_status": "ENTERED",
            "call_strike": 22650,
            "put_strike": 22800,
            "call_symbol": "NIFTY03NOV2622650CE",
            "put_symbol": "NIFTY03NOV2622800PE",
            "expiry_date": "2026-11-03",
        }

        with patch.dict(os.environ, {"THURSDAY_BROKER": "upstox"}):
            published = publish_exit_authorization_to_redis(
                track="3day_strategy",
                call_exit=True,
                put_exit=True,
                strategy_data=strategy_data,
                redis_client=mock_redis,
            )
            assert len(published) == 2

            # Verify keys written to Redis have broker='upstox' prefix
            set_keys = [call[0][0] for call in mock_redis.set.call_args_list]
            assert any("fno:upstox:enabled:thu:2026-11-03:22650:CE" in k for k in set_keys)
            assert any("fno:upstox:enabled:thu:2026-11-03:22800:PE" in k for k in set_keys)


class TestPaperEntrySafetyInExitAuthorization:
    """Verify that paper entries and non-live brokers never publish live exit authorizations or set gates."""

    def test_paper_entry_status_refuses_publishing(self):
        mock_redis = MagicMock()
        strategy_data = {
            "cycle_id": "paper_cycle_01",
            "entry_status": "PAPER_ENTRY",
            "broker": "upstox",
            "call_strike": 22650,
            "put_strike": 22800,
            "expiry_date": "2026-11-03",
        }
        published = publish_exit_authorization_to_redis(
            track="5day_strategy",
            call_exit=True,
            put_exit=True,
            strategy_data=strategy_data,
            redis_client=mock_redis,
        )
        assert published == []
        mock_redis.set.assert_not_called()

    def test_legacy_cycle_status_alias_still_refuses_publishing(self):
        mock_redis = MagicMock()
        strategy_data = {
            "cycle_id": "paper_cycle_legacy",
            "cycle_status": "PAPER_ENTRY",
            "broker": "upstox",
            "call_strike": 22650,
            "put_strike": 22800,
            "expiry_date": "2026-11-03",
        }
        assert publish_exit_authorization_to_redis(
            track="5day_strategy",
            call_exit=True,
            put_exit=True,
            strategy_data=strategy_data,
            redis_client=mock_redis,
        ) == []
        mock_redis.set.assert_not_called()

    def test_broker_paper_refuses_publishing(self):
        mock_redis = MagicMock()
        strategy_data = {
            "cycle_id": "paper_cycle_02",
            "cycle_status": "ENTERED",
            "broker": "paper",
            "call_strike": 22650,
            "put_strike": 22800,
            "expiry_date": "2026-11-03",
        }
        published = publish_exit_authorization_to_redis(
            track="3day_strategy",
            call_exit=True,
            put_exit=True,
            strategy_data=strategy_data,
            redis_client=mock_redis,
        )
        assert published == []
        mock_redis.set.assert_not_called()


class TestCycleSpecificBuyAttribution:
    def test_shared_contract_returns_track_owned_entry_ids_and_fill_prices(self, tmp_path):
        """A shared broker contract exposes separate entry IDs and bases for each track."""
        import common.unified_strategy_store as store

        active = {
            "5day_strategy": {
                "cycle_id": "TUE_SHARED",
                "status": "active",
                "broker": "angelone",
                "entry_status": "FILLED",
                "expiry_date": "2026-11-03",
                "call_strike": 22650,
                "put_strike": 22800,
                "call_order_id": "TUE_CALL_BUY_01",
                "day1_locked_price": {
                    "call_buy_price": 100.0,
                    "call_order_id": "TUE_CALL_BUY_01",
                },
            },
            "3day_strategy": {
                "cycle_id": "THU_SHARED",
                "status": "active",
                "broker": "angelone",
                "entry_status": "FILLED",
                "expiry_date": "2026-11-03",
                "call_strike": 22650,
                "put_strike": 22750,
                "call_order_id": "THU_CALL_BUY_02",
                "day1_locked_price": {
                    "call_buy_price": 120.0,
                    "call_order_id": "THU_CALL_BUY_02",
                },
            },
        }
        with patch.object(store, "load_active_strategies", return_value=active), \
             patch.object(store, "ACTIVE_SNAPSHOT_FILE", str(tmp_path / "tuesday.json")), \
             patch.object(store, "THURSDAY_SNAPSHOT_FILE", str(tmp_path / "thursday.json")):
            tue_entry = get_active_leg_entry_details("5day_strategy", 22650, "CE", expiry="2026-11-03")
            thu_entry = get_active_leg_entry_details("3day_strategy", 22650, "CE", expiry="2026-11-03")

        assert tue_entry["entry_order_id"] == "TUE_CALL_BUY_01"
        assert tue_entry["entry_price"] == 100.0
        assert tue_entry["same_broker_contract_overlap"] is True
        assert tue_entry["ambiguous_entry_attribution"] is False
        assert thu_entry["entry_order_id"] == "THU_CALL_BUY_02"
        assert thu_entry["entry_price"] == 120.0
        assert thu_entry["same_broker_contract_overlap"] is True
        assert thu_entry["ambiguous_entry_attribution"] is False

    def test_missing_entry_id_is_ambiguous_for_shared_same_broker_contract(self, tmp_path):
        """Do not guess the most recent BUY when both tracks share the contract."""
        import common.unified_strategy_store as store

        active = {
            "5day_strategy": {
                "status": "active",
                "broker": "upstox",
                "entry_status": "FILLED",
                "expiry_date": "2026-11-03",
                "call_strike": 22650,
                "day1_locked_price": {"call_buy_price": 100.0},
            },
            "3day_strategy": {
                "status": "active",
                "broker": "upstox",
                "entry_status": "FILLED",
                "expiry_date": "2026-11-03",
                "call_strike": 22650,
                "day1_locked_price": {
                    "call_buy_price": 120.0,
                    "call_order_id": "THU_CALL_BUY_02",
                },
            },
        }
        with patch.object(store, "load_active_strategies", return_value=active), \
             patch.object(store, "ACTIVE_SNAPSHOT_FILE", str(tmp_path / "tuesday.json")), \
             patch.object(store, "THURSDAY_SNAPSHOT_FILE", str(tmp_path / "thursday.json")):
            tue_entry = get_active_leg_entry_details("5day_strategy", 22650, "CE", expiry="2026-11-03")

        assert tue_entry["same_broker_contract_overlap"] is True
        assert tue_entry["ambiguous_entry_attribution"] is True

    def test_unsupported_broker_refuses_publishing(self):
        mock_redis = MagicMock()
        strategy_data = {
            "cycle_id": "invalid_cycle_03",
            "cycle_status": "ENTERED",
            "broker": "zerodha",
            "call_strike": 22650,
            "put_strike": 22800,
            "expiry_date": "2026-11-03",
        }
        published = publish_exit_authorization_to_redis(
            track="5day_strategy",
            call_exit=True,
            put_exit=True,
            strategy_data=strategy_data,
            redis_client=mock_redis,
        )
        assert published == []
        mock_redis.set.assert_not_called()


class TestTuesdayExitOnAngelOne:
    """Verify Tuesday manager executing with broker='angelone'."""

    def test_tuesday_places_slm_order_on_angelone_and_stores_order_id(self):
        """Tuesday manages an AngelOne position, places SL-M order, and stores order_id in Redis."""
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 65}) if "enabled" in k else None
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        positions = [{
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "symboltoken": "53750",
            "exchange": "NFO",
            "quantity": 65,
            "producttype": "CARRYFORWARD",
            "average_price": 100.0,
            "last_price": 110.0,
        }]
        orders = []

        with patch.object(tue_module, "fetch_ltp", return_value=110.0), \
             patch.object(tue_module, "place_exit_order_slm") as mock_place:
            mock_place.return_value = {"success": True, "order_id": "ANGEL_TUE_101"}

            res = tue_module.manage_tuesday_exit_orders(
                token="REAL_TOKEN",
                positions=positions,
                orders=orders,
                execute=True,
                redis_client=mock_redis,
                broker="angelone",
                active_expiry="2026-11-03",
                force=True,
            )

            assert res["placed"] == 1
            mock_place.assert_called_once()
            call_kwargs = mock_place.call_args[1]
            assert call_kwargs.get("broker") == "angelone"
            # Verify order_id ANGEL_TUE_101 was saved to Redis
            set_calls = [call[0] for call in mock_pipe.set.call_args_list]
            assert any(call[1] == "ANGEL_TUE_101" for call in set_calls)

    def test_tuesday_trails_untagged_angelone_order_using_stored_id(self):
        """AngelOne returns untagged order. Tuesday matches it via stored_order_id and modifies stoploss."""
        mock_redis = MagicMock()
        stored_id = "ANGEL_TUE_101"

        def mock_get(key):
            if "enabled" in key:
                return json.dumps({"enabled": True, "qty": 65})
            if "order_id" in key:
                return stored_id
            if "max" in key:
                return "110.0"
            if "sl" in key:
                return "88.0"
            if "avg" in key:
                return "100.0"
            return None

        mock_redis.get.side_effect = mock_get
        mock_redis.scan_iter.return_value = []
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        positions = [{
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "symboltoken": "53750",
            "exchange": "NFO",
            "quantity": 65,
            "producttype": "CARRYFORWARD",
            "average_price": 100.0,
            "last_price": 130.0,
        }]
        # AngelOne order is UNTAGGED in order book (tag is "")
        orders = [{
            "order_id": stored_id,
            "orderid": stored_id,
            "trading_symbol": "NIFTY03NOV2622650CE",
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "symboltoken": "53750",
            "exchange": "NFO",
            "transaction_type": "SELL",
            "order_type": "SL-M",
            "status": "trigger pending",
            "quantity": 65,
            "pending_quantity": 65,
            "tag": "",  # AngelOne orders have empty tags
        }]

        with patch.object(tue_module, "fetch_ltp", return_value=130.0), \
             patch.object(tue_module, "modify_exit_order_slm") as mock_modify:
            mock_modify.return_value = {"success": True, "order_id": stored_id}

            res = tue_module.manage_tuesday_exit_orders(
                token="REAL_TOKEN",
                positions=positions,
                orders=orders,
                execute=True,
                redis_client=mock_redis,
                broker="angelone",
                active_expiry="2026-11-03",
                force=True,
            )

            # Untagged order was correctly identified as managed_sells (NOT manual_sell_conflict)
            assert res["modified"] == 1
            mock_modify.assert_called_once()
            call_kwargs = mock_modify.call_args[1]
            assert call_kwargs.get("broker") == "angelone"

    def test_tuesday_immediate_exit_on_angelone(self):
        """Tuesday squares off authorized leg with MARKET sell order on AngelOne."""
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 65}) if "enabled" in k else None

        positions = [{
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "symboltoken": "53750",
            "exchange": "NFO",
            "quantity": 65,
            "producttype": "CARRYFORWARD",
            "average_price": 100.0,
            "last_price": 110.0,
        }]
        orders = []

        with patch.object(tue_module, "place_exit_order_market") as mock_market:
            mock_market.return_value = {"success": True, "order_id": "ANGEL_MKT_301"}

            res = tue_module.manage_tuesday_exit_orders(
                token="REAL_TOKEN",
                positions=positions,
                orders=orders,
                execute=True,
                redis_client=mock_redis,
                broker="angelone",
                active_expiry="2026-11-03",
                exit_immediately=True,
                force=True,
            )

            assert res["placed"] == 1
            mock_market.assert_called_once()
            assert mock_market.call_args[1].get("broker") == "angelone"
            assert res["actions"][0]["status"] == "place_market"

    def test_tuesday_orphan_cancel_on_angelone(self):
        """When position is closed, Tuesday cancels untagged orphan order on AngelOne identified via Redis."""
        mock_redis = MagicMock()
        stored_id = "ANGEL_TUE_101"

        def mock_get(key):
            if "order_id" in key:
                return stored_id
            return None

        mock_redis.get.side_effect = mock_get
        mock_redis.scan_iter.return_value = ["fno:angelone:trail:tue:NIFTY03NOV2622650CE:order_id"]
        mock_redis.smembers.return_value = {stored_id.encode("utf-8")}

        # Position is empty (closed/flat)
        positions = []
        orders = [{
            "order_id": stored_id,
            "orderid": stored_id,
            "trading_symbol": "NIFTY03NOV2622650CE",
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "symboltoken": "53750",
            "exchange": "NFO",
            "transaction_type": "SELL",
            "order_type": "SL-M",
            "status": "trigger pending",
            "quantity": 65,
            "pending_quantity": 65,
            "tag": "",
        }]

        with patch.object(tue_module, "cancel_order") as mock_cancel:
            mock_cancel.return_value = {"success": True, "order_id": stored_id}

            res = tue_module.manage_tuesday_exit_orders(
                token="REAL_TOKEN",
                positions=positions,
                orders=orders,
                execute=True,
                redis_client=mock_redis,
                broker="angelone",
                active_expiry="2026-11-03",
                auto_cancel_orphans=True,
                force=True,
            )

            assert len(res["orphan_managed_orders"]) == 1
            assert res["orphan_managed_orders"][0]["action_taken"] == "cancel_requested"
            mock_cancel.assert_called_once()
            assert mock_cancel.call_args[1].get("broker") == "angelone"

    def test_tuesday_reconciliation_retains_untagged_open_order_id(self):
        """Post-manager reconciliation must not purge an AngelOne order still awaiting cancel."""
        broker = "angelone"
        symbol = "NIFTY03NOV2622650CE"
        token = "53750"
        order_id = "ANGEL_TUE_PENDING_CANCEL"
        enabled_key = tue_module.build_leg_enabled_key(
            "tue", "2026-11-03", 22650, "CE", broker=broker
        )
        trail_keys = tue_module.build_trail_redis_keys(
            broker, symbol, token, active_expiry="2026-11-03"
        )

        mock_redis = MagicMock()

        def mock_scan_iter(pattern, count=30):
            return [enabled_key] if pattern == f"fno:{broker}:enabled:tue:*" else []

        def mock_get(key):
            if key == enabled_key:
                return json.dumps({"enabled": True, "qty": 65})
            if key == trail_keys["order_id"]:
                return order_id
            return None

        mock_redis.scan_iter.side_effect = mock_scan_iter
        mock_redis.get.side_effect = mock_get
        mock_redis.smembers.return_value = {order_id.encode("utf-8")}

        untagged_open_order = {
            "order_id": order_id,
            "orderid": order_id,
            "tradingsymbol": symbol,
            "trading_symbol": symbol,
            "symboltoken": token,
            "exchange": "NFO",
            "transaction_type": "SELL",
            "order_type": "SL-M",
            "status": "trigger pending",
            "quantity": 65,
            "pending_quantity": 65,
            "tag": "",
        }

        cleaned = tue_module.reconcile_cycle_legs(
            mock_redis,
            positions=[],
            orders=[untagged_open_order],
            broker=broker,
            cycle="tue",
            active_expiry="2026-11-03",
        )

        assert cleaned == []
        mock_redis.pipeline.assert_not_called()


class TestThursdayExitOnUpstox:
    """Verify Thursday manager executing with broker='upstox'."""

    def test_thursday_places_slm_order_on_upstox(self):
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 65}) if "enabled" in k else None
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        positions = [{
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "instrument_token": "NSE_FO|53750",
            "quantity": 65,
            "product": "D",
            "average_price": 100.0,
            "last_price": 110.0,
        }]
        orders = []

        with patch.object(thu_module, "fetch_ltp", return_value=110.0), \
             patch.object(thu_module, "place_exit_order_slm") as mock_place:
            mock_place.return_value = {"success": True, "order_id": "UPSTOX_THU_201"}

            res = thu_module.manage_thursday_exit_orders(
                token="REAL_TOKEN",
                positions=positions,
                orders=orders,
                execute=True,
                redis_client=mock_redis,
                broker="upstox",
                active_expiry="2026-11-03",
                force=True,
            )

            assert res["placed"] == 1
            mock_place.assert_called_once()
            assert mock_place.call_args[1].get("broker") == "upstox"

    def test_thursday_trails_slm_order_on_upstox(self):
        mock_redis = MagicMock()
        stored_id = "UPSTOX_THU_201"

        def mock_get(key):
            if "enabled" in key:
                return json.dumps({"enabled": True, "qty": 65})
            if "order_id" in key:
                return stored_id
            if "max" in key:
                return "110.0"
            if "sl" in key:
                return "88.0"
            if "avg" in key:
                return "100.0"
            return None

        mock_redis.get.side_effect = mock_get
        mock_redis.scan_iter.return_value = []
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        positions = [{
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "instrument_token": "NSE_FO|53750",
            "quantity": 65,
            "product": "D",
            "average_price": 100.0,
            "last_price": 130.0,
        }]
        orders = [{
            "order_id": stored_id,
            "trading_symbol": "NIFTY03NOV2622650CE",
            "instrument_token": "NSE_FO|53750",
            "transaction_type": "SELL",
            "order_type": "SL-M",
            "status": "trigger pending",
            "quantity": 65,
            "pending_quantity": 65,
            "tag": "THU-SL",
        }]

        with patch.object(thu_module, "fetch_ltp", return_value=130.0), \
             patch.object(thu_module, "modify_exit_order_slm") as mock_modify:
            mock_modify.return_value = {"success": True, "order_id": stored_id}

            res = thu_module.manage_thursday_exit_orders(
                token="REAL_TOKEN",
                positions=positions,
                orders=orders,
                execute=True,
                redis_client=mock_redis,
                broker="upstox",
                active_expiry="2026-11-03",
                force=True,
            )

            assert res["modified"] == 1
            mock_modify.assert_called_once()
            assert mock_modify.call_args[1].get("broker") == "upstox"

    def test_thursday_immediate_exit_on_upstox(self):
        """Thursday squares off authorized leg with MARKET sell order on Upstox."""
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 65}) if "enabled" in k else None

        positions = [{
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "instrument_token": "NSE_FO|53750",
            "quantity": 65,
            "product": "D",
            "average_price": 100.0,
            "last_price": 110.0,
        }]
        orders = []

        with patch.object(thu_module, "place_exit_order_market") as mock_market:
            mock_market.return_value = {"success": True, "order_id": "UPSTOX_MKT_401"}

            res = thu_module.manage_thursday_exit_orders(
                token="REAL_TOKEN",
                positions=positions,
                orders=orders,
                execute=True,
                redis_client=mock_redis,
                broker="upstox",
                active_expiry="2026-11-03",
                exit_immediately=True,
                force=True,
            )

            assert res["placed"] == 1
            mock_market.assert_called_once()
            assert mock_market.call_args[1].get("broker") == "upstox"
            assert res["actions"][0]["status"] == "place_market"

    def test_thursday_orphan_cancel_on_upstox(self):
        """When position is closed, Thursday cancels orphan order on Upstox."""
        mock_redis = MagicMock()
        stored_id = "UPSTOX_THU_201"

        def mock_get(key):
            if "order_id" in key:
                return stored_id
            return None

        mock_redis.get.side_effect = mock_get
        mock_redis.scan_iter.return_value = ["fno:upstox:trail:thu:NIFTY03NOV2622650CE:order_id"]
        mock_redis.smembers.return_value = {stored_id.encode("utf-8")}

        # Position is empty (closed/flat)
        positions = []
        orders = [{
            "order_id": stored_id,
            "trading_symbol": "NIFTY03NOV2622650CE",
            "instrument_token": "NSE_FO|53750",
            "transaction_type": "SELL",
            "order_type": "SL-M",
            "status": "trigger pending",
            "quantity": 65,
            "pending_quantity": 65,
            "tag": "THU-SL",
        }]

        with patch.object(thu_module, "cancel_order") as mock_cancel:
            mock_cancel.return_value = {"success": True, "order_id": stored_id}

            res = thu_module.manage_thursday_exit_orders(
                token="REAL_TOKEN",
                positions=positions,
                orders=orders,
                execute=True,
                redis_client=mock_redis,
                broker="upstox",
                active_expiry="2026-11-03",
                auto_cancel_orphans=True,
                force=True,
            )

            assert len(res["orphan_managed_orders"]) == 1
            assert res["orphan_managed_orders"][0]["action_taken"] == "cancel_requested"
            mock_cancel.assert_called_once()
            assert mock_cancel.call_args[1].get("broker") == "upstox"


class TestSameAccountOverlappingContractsBothOnAngelOne:
    """
    Verify same-broker operation when Tuesday and Thursday share the exact same AngelOne account.
    Because AngelOne drops custom tags, order ownership must be strictly partitioned via Redis order IDs.
    """

    def test_both_on_angelone_order_ownership_and_quantity_reservation(self):
        """
        Total portfolio holds 130 qty of NIFTY03NOV2622650CE (65 for Tuesday, 65 for Thursday).
        Order book has two untagged orders: ANGEL_TUE_101 (65 qty) and ANGEL_THU_202 (65 qty).
        Tuesday manager must recognize ANGEL_TUE_101 as its own and ANGEL_THU_202 as other_cycle_sells.
        Thursday manager must recognize ANGEL_THU_202 as its own and ANGEL_TUE_101 as other_cycle_sells.
        Neither order is classified as manual_sell_conflict.
        """
        positions = [{
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "symboltoken": "53750",
            "exchange": "NFO",
            "quantity": 130,  # Combined 2 lots across strategies
            "producttype": "CARRYFORWARD",
            "average_price": 110.0,  # Net broker average is not either track's own BUY basis.
            "last_price": 125.0,
        }]
        orders = [
            {
                "order_id": "ANGEL_TUE_BUY_001",
                "orderid": "ANGEL_TUE_BUY_001",
                "tradingsymbol": "NIFTY03NOV2622650CE",
                "symboltoken": "53750",
                "exchange": "NFO",
                "transaction_type": "BUY",
                "status": "complete",
                "quantity": 65,
                "filled_quantity": 65,
                "average_price": 100.0,
            },
            {
                "order_id": "ANGEL_THU_BUY_002",
                "orderid": "ANGEL_THU_BUY_002",
                "tradingsymbol": "NIFTY03NOV2622650CE",
                "symboltoken": "53750",
                "exchange": "NFO",
                "transaction_type": "BUY",
                "status": "complete",
                "quantity": 65,
                "filled_quantity": 65,
                "average_price": 120.0,
            },
            {
                "order_id": "ANGEL_TUE_101",
                "orderid": "ANGEL_TUE_101",
                "trading_symbol": "NIFTY03NOV2622650CE",
                "symboltoken": "53750",
                "exchange": "NFO",
                "transaction_type": "SELL",
                "order_type": "SL-M",
                "status": "trigger pending",
                "quantity": 65,
                "pending_quantity": 65,
                "tag": "",  # Empty on AngelOne
            },
            {
                "order_id": "ANGEL_THU_202",
                "orderid": "ANGEL_THU_202",
                "trading_symbol": "NIFTY03NOV2622650CE",
                "symboltoken": "53750",
                "exchange": "NFO",
                "transaction_type": "SELL",
                "order_type": "SL-M",
                "status": "trigger pending",
                "quantity": 65,
                "pending_quantity": 65,
                "tag": "",  # Empty on AngelOne
            },
        ]

        # 1. Tuesday's Perspective
        mock_redis_tue = MagicMock()
        def tue_redis_get(key):
            if "enabled" in key:
                return json.dumps({"enabled": True, "qty": 65})
            if "entry_order_id" in key:
                return "ANGEL_TUE_BUY_001"
            if "tue" in key and key.endswith(":order_id"):
                return "ANGEL_TUE_101"
            if "max" in key:
                return "110.0"
            if "sl" in key:
                return "88.0"
            if "avg" in key:
                return "100.0"
            return None

        mock_redis_tue.get.side_effect = tue_redis_get
        # Thursday's order is returned when scanning other cycle keys
        def tue_scan_iter(pat, count=100):
            if ":tue:" in pat:
                return ["fno:angelone:trail:tue:NIFTY03NOV2622650CE:order_id"]
            if ":thu:" in pat:
                return ["fno:angelone:trail:thu:NIFTY03NOV2622650CE:order_id"]
            return []

        mock_redis_tue.scan_iter.side_effect = tue_scan_iter
        mock_redis_tue.smembers.side_effect = lambda k: {b"ANGEL_TUE_101"} if ":tue" in k else ({b"ANGEL_THU_202"} if ":thu" in k else set())

        with patch("common.unified_strategy_store.get_active_leg_entry_details", return_value={
                 "entry_order_id": "ANGEL_TUE_BUY_001",
                 "entry_price": 100.0,
                 "entry_status": "FILLED",
                 "same_broker_contract_overlap": True,
                 "ambiguous_entry_attribution": False,
             }), \
             patch.object(tue_module, "fetch_ltp", return_value=125.0), \
             patch.object(tue_module, "modify_exit_order_slm") as mock_modify_tue:
            mock_modify_tue.return_value = {"success": True, "order_id": "ANGEL_TUE_101"}

            res_tue = tue_module.manage_tuesday_exit_orders(
                token="REAL_TOKEN",
                positions=positions,
                orders=orders,
                execute=True,
                redis_client=mock_redis_tue,
                broker="angelone",
                active_expiry="2026-11-03",
                force=True,
            )

            # Verified: Tuesday managed its 65 qty, recognized Thursday's order as other_cycle_sells, no manual conflict
            assert res_tue["modified"] == 1
            assert len(res_tue["alerts"]) == 0
            assert res_tue["actions"][0]["existing_order_id"] == "ANGEL_TUE_101"
            assert res_tue["actions"][0]["quantity"] == 65
            assert res_tue["actions"][0]["avg_price"] == 100.0

        # 2. Thursday's Perspective
        mock_redis_thu = MagicMock()
        def thu_redis_get(key):
            if "enabled" in key:
                return json.dumps({"enabled": True, "qty": 65})
            if "entry_order_id" in key:
                return "ANGEL_THU_BUY_002"
            if "thu" in key and key.endswith(":order_id"):
                return "ANGEL_THU_202"
            if "max" in key:
                return "110.0"
            if "sl" in key:
                return "88.0"
            if "avg" in key:
                return "100.0"
            return None

        mock_redis_thu.get.side_effect = thu_redis_get
        def thu_scan_iter(pat, count=100):
            if ":thu:" in pat:
                return ["fno:angelone:trail:thu:NIFTY03NOV2622650CE:order_id"]
            if ":tue:" in pat:
                return ["fno:angelone:trail:tue:NIFTY03NOV2622650CE:order_id"]
            return []

        mock_redis_thu.scan_iter.side_effect = thu_scan_iter
        mock_redis_thu.smembers.side_effect = lambda k: {b"ANGEL_THU_202"} if ":thu" in k else ({b"ANGEL_TUE_101"} if ":tue" in k else set())

        with patch("common.unified_strategy_store.get_active_leg_entry_details", return_value={
                 "entry_order_id": "ANGEL_THU_BUY_002",
                 "entry_price": 120.0,
                 "entry_status": "FILLED",
                 "same_broker_contract_overlap": True,
                 "ambiguous_entry_attribution": False,
             }), \
             patch.object(thu_module, "fetch_ltp", return_value=125.0), \
             patch.object(thu_module, "modify_exit_order_slm") as mock_modify_thu:
            mock_modify_thu.return_value = {"success": True, "order_id": "ANGEL_THU_202"}

            res_thu = thu_module.manage_thursday_exit_orders(
                token="REAL_TOKEN",
                positions=positions,
                orders=orders,
                execute=True,
                redis_client=mock_redis_thu,
                broker="angelone",
                active_expiry="2026-11-03",
                force=True,
            )

            # Verified: Thursday managed its 65 qty, recognized Tuesday's order as other_cycle_sells, no manual conflict
            assert res_thu["modified"] == 1
            assert len(res_thu["alerts"]) == 0
            assert res_thu["actions"][0]["existing_order_id"] == "ANGEL_THU_202"
            assert res_thu["actions"][0]["quantity"] == 65
            assert res_thu["actions"][0]["avg_price"] == 120.0

    def test_both_on_angelone_unrecognized_untagged_order_is_flagged_as_manual_conflict(self):
        """If an untagged order belongs to neither Tuesday nor Thursday Redis keys, it is flagged as manual_sell_conflict."""
        positions = [{
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "symboltoken": "53750",
            "exchange": "NFO",
            "quantity": 65,
            "producttype": "CARRYFORWARD",
            "average_price": 100.0,
            "last_price": 125.0,
        }]
        orders = [{
            "order_id": "ANGEL_MANUAL_999",
            "orderid": "ANGEL_MANUAL_999",
            "trading_symbol": "NIFTY03NOV2622650CE",
            "symboltoken": "53750",
            "exchange": "NFO",
            "transaction_type": "SELL",
            "order_type": "SL-M",
            "status": "trigger pending",
            "quantity": 65,
            "pending_quantity": 65,
            "tag": "",
        }]

        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 65}) if "enabled" in k else None
        mock_redis.scan_iter.return_value = []
        mock_redis.smembers.return_value = set()

        res_tue = tue_module.manage_tuesday_exit_orders(
            token="REAL_TOKEN",
            positions=positions,
            orders=orders,
            execute=True,
            redis_client=mock_redis,
            broker="angelone",
            active_expiry="2026-11-03",
            force=True,
        )
        assert len(res_tue["actions"]) == 1
        assert res_tue["actions"][0]["status"] == "manual_sell_conflict"

    def test_reconcile_cycle_legs_thursday_retains_untagged_angelone_order(self):
        """Thursday reconcile_cycle_legs must recognize untagged AngelOne open order via managed_order_ids:thu and not retire the leg."""
        mock_redis = MagicMock()
        enabled_key = "fno:angelone:enabled:thu:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:53750"
        mock_redis.scan_iter.return_value = [enabled_key]
        mock_redis.get.return_value = json.dumps({"enabled": True, "qty": 65})
        mock_redis.smembers.return_value = {b"ANGEL_THU_SL_999"}

        # Long position is 0 (closed)
        positions = []
        # But broker order book still has untagged open SELL order
        orders = [{
            "order_id": "ANGEL_THU_SL_999",
            "orderid": "ANGEL_THU_SL_999",
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "symboltoken": "53750",
            "transaction_type": "SELL",
            "order_type": "SL-M",
            "status": "trigger pending",
            "quantity": 65,
            "tag": "",  # Untagged on AngelOne
        }]

        with patch.object(thu_module, "_cleanup_thursday_leg_state") as mock_cleanup:
            cleaned = thu_module.reconcile_cycle_legs(
                mock_redis, positions, orders, broker="angelone", cycle="thu", tag="THU-SL"
            )
            # Leg must NOT be cleaned up because the order is recognized as managed and still open
            assert len(cleaned) == 0
            mock_cleanup.assert_not_called()

    def test_reconcile_cycle_legs_tuesday_retains_untagged_angelone_order(self):
        """Tuesday reconcile_cycle_legs must recognize untagged AngelOne open order via managed_order_ids:tue and not retire the leg."""
        mock_redis = MagicMock()
        enabled_key = "fno:angelone:enabled:tue:NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:53750"
        mock_redis.scan_iter.return_value = [enabled_key]
        mock_redis.get.return_value = json.dumps({"enabled": True, "qty": 65})
        mock_redis.smembers.return_value = {b"ANGEL_TUE_SL_888"}

        positions = []
        orders = [{
            "order_id": "ANGEL_TUE_SL_888",
            "orderid": "ANGEL_TUE_SL_888",
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "symboltoken": "53750",
            "transaction_type": "SELL",
            "order_type": "SL-M",
            "status": "trigger pending",
            "quantity": 65,
            "tag": "",  # Untagged on AngelOne
        }]

        with patch.object(tue_module, "_cleanup_tuesday_leg_state") as mock_cleanup:
            cleaned = tue_module.reconcile_cycle_legs(
                mock_redis, positions, orders, broker="angelone", cycle="tue", tag="TUE-SL"
            )
            assert len(cleaned) == 0
            mock_cleanup.assert_not_called()

    def test_ambiguous_entry_attribution_fails_closed(self):
        """When contracts overlap on same broker but leg has no BUY order ID, manager fails closed."""
        positions = [{
            "tradingsymbol": "NIFTY03NOV2622650CE",
            "symboltoken": "53750",
            "quantity": 130,
            "producttype": "CARRYFORWARD",
            "average_price": 110.0,
            "last_price": 125.0,
        }]
        orders = []
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda k: json.dumps({"enabled": True, "qty": 65}) if "enabled" in k else None
        mock_redis.scan_iter.return_value = []
        mock_redis.smembers.return_value = set()

        with patch("common.unified_strategy_store.get_active_leg_entry_details", return_value={
                 "entry_order_id": "",
                 "entry_price": 0.0,
                 "entry_status": "FILLED",
                 "same_broker_contract_overlap": True,
                 "ambiguous_entry_attribution": True,
             }):
            res_tue = tue_module.manage_tuesday_exit_orders(
                token="REAL_TOKEN",
                positions=positions,
                orders=orders,
                execute=True,
                redis_client=mock_redis,
                broker="angelone",
                active_expiry="2026-11-03",
                force=True,
            )
            assert len(res_tue["actions"]) == 1
            assert res_tue["actions"][0]["status"] == "entry_order_attribution_ambiguous"
            assert any(a.get("severity") == "CRITICAL" for a in res_tue["alerts"])


class TestBootstrapPreservesBrokerMetadata:
    """Verify scripts/bootstrap_active_strategies.py preserves broker metadata from snapshot files."""

    def test_bootstrap_preserves_tuesday_angelone_and_thursday_upstox(self, tmp_path):
        import importlib
        import scripts.bootstrap_active_strategies as bootstrap

        tue_snap_file = str(tmp_path / "tuesday_snapshot.json")
        thu_snap_file = str(tmp_path / "thursday_snapshot.json")
        active_json_file = str(tmp_path / "active_strategies.json")

        tue_snap_data = {
            "cycle_id": "TUE_20261103_01",
            "start_date": "2026-11-03",
            "expiry_date": "2026-11-03",
            "call_strike": 22650,
            "put_strike": 22800,
            "call_buy_price": 105.0,
            "put_buy_price": 140.0,
            "call_order_id": "TUE_CALL_ENTRY_001",
            "put_order_id": "TUE_PUT_ENTRY_001",
            "call_ref_ltp": 104.0,
            "put_ref_ltp": 139.0,
            "entry_spot": 22700.0,
            "broker": "angelone",  # Non-default: Tuesday on AngelOne
            "status": "ongoing",  # Snapshot lifecycle status must not override entry_status.
            "entry_status": "FILLED",
        }
        thu_snap_data = {
            "cycle_id": "THU_20261105_01",
            "start_date": "2026-11-05",
            "expiry_date": "2026-11-10",
            "call_strike": 22700,
            "put_strike": 22700,
            "call_buy_price": 95.0,
            "put_buy_price": 110.0,
            "call_order_id": "THU_CALL_ENTRY_001",
            "put_order_id": "THU_PUT_ENTRY_001",
            "call_ref_ltp": 94.0,
            "put_ref_ltp": 109.0,
            "entry_spot": 22700.0,
            "broker": "upstox",  # Non-default: Thursday on Upstox
            "status": "ongoing",
            "entry_status": "FILLED",
        }

        with open(tue_snap_file, "w", encoding="utf-8") as f:
            json.dump(tue_snap_data, f)
        with open(thu_snap_file, "w", encoding="utf-8") as f:
            json.dump(thu_snap_data, f)

        with patch("scripts.bootstrap_active_strategies.ACTIVE_SNAPSHOT_FILE", tue_snap_file), \
             patch("scripts.bootstrap_active_strategies.THURSDAY_SNAPSHOT_FILE", thu_snap_file), \
             patch("scripts.bootstrap_active_strategies.ACTIVE_STRATEGIES_JSON", active_json_file), \
             patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", active_json_file), \
             patch("scripts.bootstrap_active_strategies.sync_active_cycles_to_mongodb"), \
             patch("common.unified_strategy_store.sync_active_cycles_to_mongodb"), \
             patch("common.unified_strategy_store.append_telemetry_log_to_mongodb"):

            bootstrap.bootstrap_from_snapshots(poll_now=False)

        # Verify active_strategies.json was created and preserved brokers
        assert os.path.exists(active_json_file)
        with open(active_json_file, "r", encoding="utf-8") as f:
            active = json.load(f)

        assert active["5day_strategy"]["broker"] == "angelone"
        assert active["5day_strategy"]["day1_locked_price"]["broker"] == "angelone"
        assert active["5day_strategy"]["entry_status"] == "FILLED"
        assert active["5day_strategy"]["day1_locked_price"]["call_order_id"] == "TUE_CALL_ENTRY_001"
        assert active["5day_strategy"]["day1_locked_price"]["put_order_id"] == "TUE_PUT_ENTRY_001"
        assert active["5day_strategy"]["status"] == "active"

        assert active["3day_strategy"]["broker"] == "upstox"
        assert active["3day_strategy"]["day1_locked_price"]["broker"] == "upstox"
        assert active["3day_strategy"]["entry_status"] == "FILLED"
        assert active["3day_strategy"]["day1_locked_price"]["call_order_id"] == "THU_CALL_ENTRY_001"
        assert active["3day_strategy"]["day1_locked_price"]["put_order_id"] == "THU_PUT_ENTRY_001"
        assert active["3day_strategy"]["status"] == "active"
