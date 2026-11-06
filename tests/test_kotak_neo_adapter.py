"""
tests/test_kotak_neo_adapter.py - Unit & Integration Tests for KotakNeoAdapter.

Validates:
1. Token retrieval from Redis JSON session, raw token string, and .env fallback.
2. HTTP headers construction (consumer key vs Bearer token, sid, neo-fin-key).
3. Position retrieval, normalization, and parsing via parse_positions (handling 5203 No Data).
4. Order book retrieval, status mapping, and normalization via parse_order_book.
5. LTP quote fetching from neosymbol endpoint.
6. SL-M order placement with form-urlencoded payloads.
7. Immediate MARKET order placement.
8. SL-M order modification (using 'no' parameter).
9. Order cancellation (using 'on' parameter).
10. Product mapping (NRML vs MIS).
11. Mock token offline operations.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on sys.path
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from common.broker_adapters import (
    MOCK_TOKEN,
    get_broker_adapter,
    parse_order_book,
    parse_positions,
)
from common.kotak_neo_adapter import (
    DEFAULT_KOTAK_BASE_URL,
    DEFAULT_KOTAK_REDIS_KEY,
    NEO_FIN_KEY,
    KotakNeoAdapter,
)


@pytest.fixture
def adapter():
    return KotakNeoAdapter(
        base_url="https://test-napi.kotaksecurities.com",
        sid="test_sid_123",
        consumer_key="test_consumer_key_xyz",
    )


class TestKotakNeoTokenRetrieval:
    """Test session token acquisition from Redis Cloud and .env fallback."""

    def test_token_from_redis_json_session(self, adapter):
        redis_mock = MagicMock()
        session_data = {
            "token": "trade_token_abc_123",
            "sid": "dynamic_sid_456",
            "base_url": "https://gateway-datacenter.kotak.com",
        }
        redis_mock.get.return_value = json.dumps(session_data)

        token = adapter.get_access_token(redis_client=redis_mock)
        assert token == "trade_token_abc_123"
        assert adapter.sid == "dynamic_sid_456"
        assert adapter.base_url == "https://gateway-datacenter.kotak.com"

    def test_token_from_redis_plain_string(self, adapter):
        redis_mock = MagicMock()
        redis_mock.get.return_value = "plain_token_string_999"

        token = adapter.get_access_token(redis_client=redis_mock)
        assert token == "plain_token_string_999"

    def test_token_from_env_fallback(self):
        adapter = KotakNeoAdapter()
        redis_mock = MagicMock()
        redis_mock.get.return_value = None

        with patch.dict(os.environ, {"KOTAK_ACCESS_TOKEN": "env_trade_token_555"}, clear=False):
            token = adapter.get_access_token(redis_client=redis_mock)
            assert token == "env_trade_token_555"

    def test_token_missing_raises_runtime_error(self):
        adapter = KotakNeoAdapter()
        redis_mock = MagicMock()
        redis_mock.get.return_value = None

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError) as exc_info:
                adapter.get_access_token(redis_client=redis_mock)
            assert "No access token found for broker 'kotak'" in str(exc_info.value)


class TestKotakNeoHeaders:
    """Test header generation for authenticated API requests."""

    def test_headers_with_consumer_key(self, adapter):
        headers = adapter._headers("my_token")
        assert headers["Auth"] == "my_token"
        assert headers["Sid"] == "test_sid_123"
        assert headers["Authorization"] == "test_consumer_key_xyz"
        assert headers["neo-fin-key"] == NEO_FIN_KEY
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"

    def test_headers_fallback_bearer(self):
        adapter = KotakNeoAdapter(consumer_key="")
        headers = adapter._headers("my_token")
        assert headers["Authorization"] == "Bearer my_token"

    def test_mock_headers(self, adapter):
        headers = adapter._headers(MOCK_TOKEN)
        assert headers["Auth"] == MOCK_TOKEN
        assert headers["Authorization"] == f"Bearer {MOCK_TOKEN}"


class TestKotakNeoPositions:
    """Test fetching and normalizing Kotak positions."""

    @patch("requests.get")
    def test_fetch_positions_empty_5203_no_data(self, mock_get, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stat": "Ok",
            "stCode": 5203,
            "errMsg": "No Data",
        }
        mock_get.return_value = mock_resp

        result = adapter.fetch_positions("valid_token")
        assert result.get("stCode") == 200
        assert result.get("data") == []

        long_pos, short_pos = parse_positions(result)
        assert long_pos == []
        assert short_pos == []

    @patch("requests.get")
    def test_fetch_positions_active_fno_positions(self, mock_get, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stat": "Ok",
            "stCode": 200,
            "data": [
                {
                    "tok": "53750",
                    "trdSym": "NIFTY26N0624500CE",
                    "prod": "NRML",
                    "flBuyQty": "65",
                    "flSellQty": "0",
                    "buyAvgPrc": "150.50",
                    "exSeg": "nse_fo",
                },
                {
                    "tok": "53751",
                    "trdSym": "NIFTY26N0624500PE",
                    "prod": "NRML",
                    "flBuyQty": "65",
                    "flSellQty": "65",
                    "buyAvgPrc": "120.00",
                    "exSeg": "nse_fo",
                },
                {
                    "tok": "11536",
                    "trdSym": "TCS-EQ",
                    "prod": "CNC",
                    "flBuyQty": "10",
                    "flSellQty": "0",
                    "buyAvgPrc": "3800.00",
                    "exSeg": "nse_cm",
                },
            ],
        }
        mock_get.return_value = mock_resp

        result = adapter.fetch_positions("valid_token")
        long_pos, short_pos = parse_positions(result)

        assert len(long_pos) == 1
        pos = long_pos[0]
        assert pos["instrument_token"] == "53750"
        assert pos["tradingsymbol"] == "NIFTY26N0624500CE"
        assert pos["quantity"] == 65.0
        assert pos["buy_price"] == 150.50
        assert pos["product"] == "NRML"
        assert len(short_pos) == 0


class TestKotakNeoOrderBook:
    """Test fetching and normalizing Kotak order book."""

    @patch("requests.get")
    def test_fetch_order_book_normalization(self, mock_get, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stat": "Ok",
            "stCode": 200,
            "data": [
                {
                    "nOrdNo": "24110600012345",
                    "tok": "53750",
                    "trdSym": "NIFTY26N0624500CE",
                    "ordSt": "trig_pending",
                    "trnsTp": "S",
                    "ordType": "SL-M",
                    "prod": "NRML",
                    "qty": 65,
                    "fldQty": 0,
                    "unfldQty": 65,
                    "trgPrc": 125.50,
                    "ig": "TUE-SL",
                },
                {
                    "nOrdNo": "24110600012346",
                    "tok": "53751",
                    "trdSym": "NIFTY26N0624500PE",
                    "ordSt": "complete",
                    "trnsTp": "S",
                    "ordType": "MKT",
                    "prod": "NRML",
                    "qty": 65,
                    "fldQty": 65,
                    "unfldQty": 0,
                    "trgPrc": 0.0,
                    "ig": "TUE-SL",
                },
            ],
        }
        mock_get.return_value = mock_resp

        orders = adapter.fetch_order_book("valid_token")
        assert len(orders) == 2

        open_order = orders[0]
        assert open_order["order_id"] == "24110600012345"
        assert open_order["instrument_token"] == "53750"
        assert open_order["trading_symbol"] == "NIFTY26N0624500CE"
        assert open_order["status"] == "trigger_pending"
        assert open_order["transaction_type"] == "SELL"
        assert open_order["order_type"] == "STOPLOSS_MARKET"
        assert open_order["quantity"] == 65.0
        assert open_order["pending_quantity"] == 65.0
        assert open_order["trigger_price"] == 125.50
        assert open_order["tag"] == "TUE-SL"

        filled_order = orders[1]
        assert filled_order["status"] == "complete"
        assert filled_order["order_type"] == "MARKET"
        assert filled_order["filled_quantity"] == 65.0


class TestKotakNeoLTP:
    """Test LTP quote retrieval."""

    @patch("requests.get")
    def test_fetch_ltp_direct_value(self, mock_get, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({"stat": "Ok", "ltp": 165.75})
        mock_resp.json.return_value = {"stat": "Ok", "ltp": 165.75}
        mock_get.return_value = mock_resp

        ltp = adapter.fetch_ltp("token", instrument_token="53750", exchange="NFO")
        assert ltp == 165.75
        mock_get.assert_called_once()
        called_url = mock_get.call_args[0][0]
        assert "nse_fo|53750" in called_url

    @patch("requests.get")
    def test_fetch_ltp_nested_data(self, mock_get, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_resp.json.return_value = {
            "stat": "Ok",
            "data": [{"ltp": "188.40"}],
        }
        mock_get.return_value = mock_resp

        ltp = adapter.fetch_ltp("token", instrument_token="53750", exchange="NFO")
        assert ltp == 188.40

    def test_fetch_ltp_mock_token(self, adapter):
        assert adapter.fetch_ltp(MOCK_TOKEN, instrument_token="53750") is None


class TestKotakNeoOrderMutations:
    """Test place, modify, and cancel order execution."""

    @patch("requests.post")
    def test_place_exit_order_slm(self, mock_post, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stat": "Ok",
            "stCode": 200,
            "result": {"nOrdNo": "241106999901"},
        }
        mock_post.return_value = mock_resp

        position = {
            "tradingsymbol": "NIFTY26N0624500CE",
            "instrument_token": "53750",
            "product": "NRML",
        }
        result = adapter.place_exit_order_slm(
            token="valid_token",
            position=position,
            target_qty=65,
            trigger_price=120.03,  # Will round to nearest 0.05 tick (120.05)
            tag="TUE-SL",
        )

        assert result["success"] is True
        assert result["order_id"] == "241106999901"
        assert result["request"]["tp"] == "120.05"
        assert result["request"]["pt"] == "SL-M"
        assert result["request"]["tt"] == "S"
        assert result["request"]["es"] == "nse_fo"
        assert result["request"]["ig"] == "TUE-SL"

    @patch("requests.post")
    def test_place_exit_order_market(self, mock_post, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stat": "Ok",
            "stCode": 200,
            "result": {"nOrdNo": "241106999902"},
        }
        mock_post.return_value = mock_resp

        position = {
            "tradingsymbol": "NIFTY26N0624500CE",
            "instrument_token": "53750",
            "product": "NRML",
        }
        result = adapter.place_exit_order_market(
            token="valid_token",
            position=position,
            target_qty=65,
            tag="TUE-SL",
        )

        assert result["success"] is True
        assert result["order_id"] == "241106999902"
        assert result["request"]["pt"] == "MKT"
        assert result["request"]["tp"] == "0"

    @patch("requests.post")
    def test_modify_exit_order_slm(self, mock_post, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stat": "Ok",
            "stCode": 200,
            "result": {"nOrdNo": "241106999901"},
        }
        mock_post.return_value = mock_resp

        order = {
            "order_id": "241106999901",
            "quantity": 65,
            "filled_quantity": 0,
            "pending_quantity": 65,
        }
        result = adapter.modify_exit_order_slm(
            token="valid_token",
            order=order,
            target_qty=65,
            trigger_price=135.52,  # Rounds to 135.50
            tag="TUE-SL",
        )

        assert result["success"] is True
        assert result["order_id"] == "241106999901"
        assert result["request"]["no"] == "241106999901"
        assert result["request"]["tp"] == "135.50"
        assert result["request"]["pt"] == "SL-M"
        assert result["request"]["qt"] == "65"

    @patch("requests.post")
    def test_cancel_order(self, mock_post, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stat": "Ok",
            "stCode": 200,
            "result": "Order Cancelled",
        }
        mock_post.return_value = mock_resp

        result = adapter.cancel_order("valid_token", "241106999901")
        assert result["success"] is True
        assert result["order_id"] == "241106999901"
        assert mock_post.call_args[1]["data"]["on"] == "241106999901"
        assert mock_post.call_args[1]["data"]["am"] == "NO"


class TestKotakNeoProductAndFactory:
    """Test product mapping and get_broker_adapter factory."""

    def test_product_resolution(self, adapter):
        assert adapter.get_exit_product({"product": "NRML"}) == "NRML"
        assert adapter.get_exit_product({"product": "CARRYFORWARD"}) == "NRML"
        assert adapter.get_exit_product({"product": "CNC"}) == "NRML"
        assert adapter.get_exit_product({"product": "D"}) == "NRML"
        assert adapter.get_exit_product({"product": "MIS"}) == "MIS"
        assert adapter.get_exit_product({"product": "INTRADAY"}) == "MIS"
        assert adapter.get_exit_product({}, cli_product="MIS") == "MIS"
        assert adapter.get_exit_product({}) == "NRML"

    def test_factory_returns_kotak_neo_adapter(self):
        for name in ("kotak", "kotakneo", "kotak_neo", "neo"):
            ad = get_broker_adapter(name)
            assert isinstance(ad, KotakNeoAdapter)
            assert ad.name == "kotak"

    def test_mock_token_operations(self, adapter):
        pos = {"tradingsymbol": "NIFTY26NOVCE", "product": "NRML"}
        res = adapter.place_exit_order_slm(MOCK_TOKEN, pos, 65, 100.0)
        assert res["success"] is True
        assert res["order_id"].startswith("mock_kotak_")

        res_mkt = adapter.place_exit_order_market(MOCK_TOKEN, pos, 65)
        assert res_mkt["success"] is True

        res_mod = adapter.modify_exit_order_slm(MOCK_TOKEN, {"order_id": "123"}, 65, 110.0)
        assert res_mod["success"] is True

        res_cnc = adapter.cancel_order(MOCK_TOKEN, "123")
        assert res_cnc["success"] is True
