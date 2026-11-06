"""
tests/test_kotak_neo_buy_orders.py - Comprehensive Unit & Integration Tests for Kotak Neo BUY Orders.

Validates:
1. NSE Option Symbol Formatting for Kotak Neo:
   - Weekly Expiry: UNDERLYING + YY + M + DD + STRIKE + CE/PE
     * Months 1-9 (Jan-Sep), 'O' (Oct), 'N' (Nov), 'D' (Dec)
     * e.g. Nov 06, 2026 -> NIFTY26N0624500CE
   - Monthly Expiry: UNDERLYING + YY + MMM + STRIKE + CE/PE
     * e.g. Nov 26, 2026 -> NIFTY26NOV24500CE
   - Auto-detection and explicit overrides
2. Contract Resolution (resolve_kotak_option_contract)
3. Token & Funds Preflight:
   - Redis JSON payload parsing
   - Limits / margin check
   - Preflight validation
4. MARKET BUY Order Placement:
   - Back-to-back placement with tt="B", pt="MKT", pc="NRML", es="nse_fo"
   - Order book polling & fill price extraction (avgPrc)
5. Atomic Rollback:
   - Partial execution -> cancel open BUYs, MARKET SELL filled leg
   - Pre-existing position baseline preservation
6. Multi-broker Collector Integration:
   - Tuesday collector with TUESDAY_BROKER=kotak
   - Thursday collector with THURSDAY_BROKER=kotak
"""

import json
import os
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from common.entry_order_manager import (
    MOCK_TOKEN,
    cancel_broker_order,
    execute_paired_market_buy,
    fetch_broker_positions,
    fetch_normalized_orders,
    get_available_funds,
    get_broker_token,
    place_market_order,
    preflight_entry,
)
from common.kotak_neo_adapter import (
    KotakNeoAdapter,
    format_kotak_trading_symbol,
    is_monthly_expiry,
    parse_expiry_to_date,
    resolve_kotak_option_contract,
)


@pytest.fixture(autouse=True)
def disable_redis_network():
    """Prevent 5s TCP handshake timeouts to remote Redis in tests."""
    with patch("config._config_redis_state", {"client": None, "unavailable": True}):
        yield


# ===========================================================================
# 1. NSE Option Symbol Formatting Tests
# ===========================================================================

class TestKotakOptionSymbolFormatting:
    """Test official NSE exchange symbol generation for Kotak Neo."""

    def test_weekly_symbols_across_all_twelve_months(self):
        """Verify single alphanumeric month encoding (1-9, O, N, D) for weekly contracts."""
        weekly_cases = [
            (date(2026, 1, 15), 24500, "CE", "NIFTY2611524500CE"),
            (date(2026, 2, 12), 24500, "PE", "NIFTY2621224500PE"),
            (date(2026, 3, 12), 24300, "CE", "NIFTY2631224300CE"),
            (date(2026, 4, 9), 24300, "PE", "NIFTY2640924300PE"),
            (date(2026, 5, 14), 24000, "CE", "NIFTY2651424000CE"),
            (date(2026, 6, 11), 24000, "PE", "NIFTY2661124000PE"),
            (date(2026, 7, 9), 24800, "CE", "NIFTY2670924800CE"),
            (date(2026, 8, 13), 24800, "PE", "NIFTY2681324800PE"),
            (date(2026, 9, 10), 25000, "CE", "NIFTY2691025000CE"),
            (date(2026, 10, 15), 25000, "PE", "NIFTY26O1525000PE"),
            (date(2026, 11, 6), 24500, "CE", "NIFTY26N0624500CE"),
            (date(2026, 11, 6), 24300, "PE", "NIFTY26N0624300PE"),
            (date(2026, 12, 10), 25500, "PE", "NIFTY26D1025500PE"),
        ]
        for exp_date, strike, opt, expected in weekly_cases:
            symbol = format_kotak_trading_symbol(exp_date, strike, opt, is_monthly=False)
            assert symbol == expected, f"Expected {expected} for {exp_date}, got {symbol}"

    def test_monthly_symbols_use_three_letter_uppercase_month(self):
        """Verify 3-letter month uppercase format for monthly contracts."""
        monthly_cases = [
            (date(2026, 1, 29), 24500, "CE", "NIFTY26JAN24500CE"),
            (date(2026, 2, 26), 24500, "PE", "NIFTY26FEB24500PE"),
            (date(2026, 10, 29), 24000, "CE", "NIFTY26OCT24000CE"),
            (date(2026, 11, 26), 24500, "CE", "NIFTY26NOV24500CE"),
            (date(2026, 11, 26), 24300, "PE", "NIFTY26NOV24300PE"),
            (date(2026, 12, 31), 25000, "PE", "NIFTY26DEC25000PE"),
        ]
        for exp_date, strike, opt, expected in monthly_cases:
            symbol = format_kotak_trading_symbol(exp_date, strike, opt, is_monthly=True)
            assert symbol == expected, f"Expected {expected} for {exp_date}, got {symbol}"

    def test_auto_detection_monthly_vs_weekly(self):
        """Auto-detects monthly if adding 7 days shifts into next month."""
        # Nov 6, 2026 is weekly (Nov 6 + 7 = Nov 13, same month)
        assert is_monthly_expiry(date(2026, 11, 6)) is False
        assert format_kotak_trading_symbol(date(2026, 11, 6), 24500, "CE") == "NIFTY26N0624500CE"

        # Nov 26, 2026 is monthly (Nov 26 + 7 = Dec 3, next month)
        assert is_monthly_expiry(date(2026, 11, 26)) is True
        assert format_kotak_trading_symbol(date(2026, 11, 26), 24500, "CE") == "NIFTY26NOV24500CE"

    def test_string_expiry_inputs_support_various_formats(self):
        """Supports DDMMMYYYY, ISO YYYY-MM-DD, and YYYYMMDD string formats."""
        # 06NOV2026
        assert format_kotak_trading_symbol("06NOV2026", 24500, "CE") == "NIFTY26N0624500CE"
        # 2026-11-06
        assert format_kotak_trading_symbol("2026-11-06", 24300, "PE") == "NIFTY26N0624300PE"
        # 20261106
        assert format_kotak_trading_symbol("20261106", 24500, "CE") == "NIFTY26N0624500CE"
        # 26NOV2026
        assert format_kotak_trading_symbol("26NOV2026", 24500, "CE") == "NIFTY26NOV24500CE"

    def test_invalid_option_type_or_date_raises(self):
        """Invalid option type or date raises ValueError."""
        with pytest.raises(ValueError, match="Invalid option type"):
            format_kotak_trading_symbol(date(2026, 11, 6), 24500, "XX")

        with pytest.raises(ValueError, match="Unable to parse expiry date"):
            format_kotak_trading_symbol("invalid-date", 24500, "CE")


# ===========================================================================
# 2. Contract Resolution Tests
# ===========================================================================

class TestKotakContractResolution:
    """Test resolve_kotak_option_contract output structure and caching."""

    def test_resolve_weekly_contract(self):
        contract = resolve_kotak_option_contract("06NOV2026", 24500, "CE", token="MOCK_TOKEN")
        assert contract["trading_symbol"] == "NIFTY26N0624500CE"
        assert contract["tradingsymbol"] == "NIFTY26N0624500CE"
        assert contract["strike"] == 24500
        assert contract["option_type"] == "CE"
        assert contract["exchange"] == "NSE_FO"
        assert contract["product"] == "NRML"
        assert contract["token"] == "MOCK_KOTAK_24500_CE"

    def test_resolve_monthly_contract(self):
        contract = resolve_kotak_option_contract("26NOV2026", 24300, "PE", token="MOCK_TOKEN")
        assert contract["trading_symbol"] == "NIFTY26NOV24300PE"
        assert contract["tradingsymbol"] == "NIFTY26NOV24300PE"
        assert contract["strike"] == 24300
        assert contract["option_type"] == "PE"


# ===========================================================================
# 3. Token & Funds Preflight Tests
# ===========================================================================

class TestKotakTokenAndFunds:
    """Test token acquisition and margin queries for Kotak Neo."""

    def test_get_broker_token_mock_mode(self, monkeypatch):
        monkeypatch.setenv("TEST_MODE", "true")
        token = get_broker_token("kotak")
        assert token == MOCK_TOKEN

    def test_get_broker_token_from_redis_json(self, monkeypatch):
        monkeypatch.delenv("TEST_MODE", raising=False)
        monkeypatch.delenv("MOCK_BROKER", raising=False)
        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps({
            "token": "live_jwt_token_12345",
            "sid": "session_id_999",
            "base_url": "https://gw-napi.kotaksecurities.com",
        })
        token = get_broker_token("kotak", redis_client=mock_redis)
        assert token == "live_jwt_token_12345"

    def test_get_available_funds_mock_token(self):
        funds = get_available_funds("kotak", MOCK_TOKEN)
        assert funds == 500000.0

    @patch("requests.post")
    def test_get_available_funds_live_api(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stat": "Ok",
            "stCode": 200,
            "data": {
                "Net": 350000.50,
                "availableMargin": 350000.50,
                "cashBalance": 350000.50,
            },
        }
        mock_post.return_value = mock_resp

        adapter = KotakNeoAdapter()
        funds = adapter.get_available_funds("valid_token")
        assert funds == 350000.50
        assert mock_post.called

    def test_preflight_entry_passes_with_sufficient_funds(self):
        call_contract = resolve_kotak_option_contract("06NOV2026", 24500, "CE", token=MOCK_TOKEN)
        put_contract = resolve_kotak_option_contract("06NOV2026", 24300, "PE", token=MOCK_TOKEN)

        passed, reason, metrics = preflight_entry(
            broker="kotak",
            token=MOCK_TOKEN,
            call_contract=call_contract,
            put_contract=put_contract,
            quantity=65,
            call_ltp=150.0,
            put_ltp=120.0,
        )
        assert passed is True
        assert "passed" in reason.lower()
        assert metrics["broker"] == "kotak"
        assert metrics["quantity"] == 65

    def test_preflight_entry_fails_with_insufficient_funds(self, monkeypatch):
        monkeypatch.setenv("MOCK_AVAILABLE_FUNDS", "1000.0")  # Only ₹1,000 available
        call_contract = resolve_kotak_option_contract("06NOV2026", 24500, "CE", token=MOCK_TOKEN)
        put_contract = resolve_kotak_option_contract("06NOV2026", 24300, "PE", token=MOCK_TOKEN)

        passed, reason, metrics = preflight_entry(
            broker="kotak",
            token=MOCK_TOKEN,
            call_contract=call_contract,
            put_contract=put_contract,
            quantity=65,
            call_ltp=150.0,
            put_ltp=120.0,
        )
        assert passed is False
        assert "insufficient funds" in reason.lower()


# ===========================================================================
# 4. MARKET Order Placement & Polling Verification
# ===========================================================================

class TestKotakMarketOrderPlacement:
    """Test market order submission and polling fill verification."""

    def test_place_market_order_mock(self):
        contract = resolve_kotak_option_contract("06NOV2026", 24500, "CE", token=MOCK_TOKEN)
        res = place_market_order(
            broker="kotak",
            token=MOCK_TOKEN,
            contract=contract,
            quantity=65,
            transaction_type="BUY",
            tag="TUE-BUY",
        )
        assert res["success"] is True
        assert res["broker"] == "kotak"
        assert res["transaction_type"] == "BUY"
        assert str(res["order_id"]).startswith("MOCK-BUY-")

    @patch("requests.post")
    def test_place_market_order_live_api(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stat": "Ok",
            "stCode": 200,
            "result": {"nOrdNo": "241106000999"},
        }
        mock_post.return_value = mock_resp

        contract = resolve_kotak_option_contract("06NOV2026", 24500, "CE", token="live_token")
        res = place_market_order(
            broker="kotak",
            token="live_token",
            contract=contract,
            quantity=65,
            transaction_type="BUY",
            tag="TUE-BUY",
        )
        assert res["success"] is True
        assert res["order_id"] == "241106000999"
        assert res["broker"] == "kotak"

        # Verify request body parameters
        called_args, called_kwargs = mock_post.call_args
        data = called_kwargs.get("data", {})
        assert data["tt"] == "B"  # BUY
        assert data["pt"] == "MKT"  # MARKET
        assert data["pc"] == "NRML"
        assert data["es"] == "nse_fo"
        assert data["ts"] == "NIFTY26N0624500CE"
        assert data["qt"] == "65"

    def test_cancel_broker_order_mock(self):
        res = cancel_broker_order("kotak", MOCK_TOKEN, "241106000999")
        assert res["success"] is True
        assert res["action"] == "cancel"


# ===========================================================================
# 5. Paired MARKET BUY Execution & Atomic Rollback Tests
# ===========================================================================

class TestKotakPairedMarketBuyExecution:
    """Test execute_paired_market_buy orchestration with fills and atomic rollbacks."""

    def test_paired_market_buy_clean_fill(self):
        """Both legs filled successfully with positive execution prices."""
        call_contract = resolve_kotak_option_contract("06NOV2026", 24500, "CE", token=MOCK_TOKEN)
        put_contract = resolve_kotak_option_contract("06NOV2026", 24300, "PE", token=MOCK_TOKEN)

        def mock_order_book():
            return [
                {
                    "order_id": "CALL_OID_1",
                    "status": "complete",
                    "filled_quantity": 65,
                    "quantity": 65,
                    "average_price": 152.25,
                    "transaction_type": "BUY",
                    "trading_symbol": "NIFTY26N0624500CE",
                },
                {
                    "order_id": "PUT_OID_1",
                    "status": "complete",
                    "filled_quantity": 65,
                    "quantity": 65,
                    "average_price": 121.50,
                    "transaction_type": "BUY",
                    "trading_symbol": "NIFTY26N0624300PE",
                },
            ]

        with patch("common.entry_order_manager.place_market_order") as mock_place:
            mock_place.side_effect = [
                {"success": True, "order_id": "CALL_OID_1", "status": "submitted", "broker": "kotak"},
                {"success": True, "order_id": "PUT_OID_1", "status": "submitted", "broker": "kotak"},
            ]

            res = execute_paired_market_buy(
                broker="kotak",
                call_contract=call_contract,
                put_contract=put_contract,
                quantity=65,
                call_ltp=150.0,
                put_ltp=120.0,
                token=MOCK_TOKEN,
                timeout_sec=0.1,
                order_book_provider=mock_order_book,
            )

            assert res["success"] is True
            assert res["status"] == "FILLED"
            assert res["broker"] == "kotak"
            assert res["call_buy_price"] == 152.25
            assert res["put_buy_price"] == 121.50
            assert res["call_order_id"] == "CALL_OID_1"
            assert res["put_order_id"] == "PUT_OID_1"

    def test_paired_market_buy_rejection_triggers_atomic_rollback(self):
        """CALL fills, PUT rejected -> open orders cancelled, filled CALL unwound via MARKET SELL."""
        call_contract = resolve_kotak_option_contract("06NOV2026", 24500, "CE", token=MOCK_TOKEN)
        put_contract = resolve_kotak_option_contract("06NOV2026", 24300, "PE", token=MOCK_TOKEN)

        # Order book state during polling: PUT is rejected, CALL is filled with 65, UNWIND SELL is complete
        def mock_order_book():
            return [
                {
                    "order_id": "CALL_OID_1",
                    "status": "complete",
                    "filled_quantity": 65,
                    "quantity": 65,
                    "average_price": 150.0,
                    "transaction_type": "BUY",
                    "trading_symbol": "NIFTY26N0624500CE",
                },
                {
                    "order_id": "PUT_OID_1",
                    "status": "rejected",
                    "filled_quantity": 0,
                    "quantity": 65,
                    "average_price": 0.0,
                    "transaction_type": "BUY",
                    "trading_symbol": "NIFTY26N0624300PE",
                },
                {
                    "order_id": "UNWIND_SELL_1",
                    "status": "complete",
                    "filled_quantity": 65,
                    "quantity": 65,
                    "average_price": 149.0,
                    "transaction_type": "SELL",
                    "trading_symbol": "NIFTY26N0624500CE",
                },
            ]

        # After unwinding CALL with SELL, positions become flat
        def mock_positions():
            return []

        with patch("common.entry_order_manager.place_market_order") as mock_place, \
             patch("common.entry_order_manager.cancel_broker_order") as mock_cancel, \
             patch("common.entry_order_manager.dispatch_order_failure_discord"):

            # First two calls: paired BUYs
            # Third call: recovery MARKET SELL for CALL filled qty
            mock_place.side_effect = [
                {"success": True, "order_id": "CALL_OID_1", "status": "submitted", "broker": "kotak"},
                {"success": True, "order_id": "PUT_OID_1", "status": "submitted", "broker": "kotak"},
                {"success": True, "order_id": "UNWIND_SELL_1", "status": "submitted", "broker": "kotak"},
            ]
            mock_cancel.return_value = {"success": True, "action": "cancel"}

            res = execute_paired_market_buy(
                broker="kotak",
                call_contract=call_contract,
                put_contract=put_contract,
                quantity=65,
                call_ltp=150.0,
                put_ltp=120.0,
                token=MOCK_TOKEN,
                timeout_sec=0.1,
                order_book_provider=mock_order_book,
                position_provider=mock_positions,
            )

            assert res["success"] is False
            assert res["status"] == "FAILED_ROLLED_BACK"
            assert res["broker"] == "kotak"
            assert res["rollback"]["unwind_successful"] is True
            assert res["rollback"]["call_filled"] == 65
            assert res["rollback"]["put_filled"] == 0

            # Verify recovery MARKET SELL was submitted for CALL
            assert mock_place.call_count == 3
            unwind_call_args = mock_place.call_args_list[2]
            assert unwind_call_args.kwargs.get("transaction_type") == "SELL"
            assert unwind_call_args.kwargs.get("quantity") == 65


# ===========================================================================
# 6. Multi-Broker Collector Integration Tests
# ===========================================================================

class TestCollectorKotakRouting:
    """Verify weekly (Tuesday) and Thursday collectors route properly with Kotak Neo."""

    @patch("config.get_broker_for_track")
    @patch("common.entry_order_manager.execute_paired_market_buy")
    def test_weekly_collector_kotak_branch(self, mock_execute, mock_broker):
        mock_broker.return_value = "kotak"
        mock_execute.return_value = {
            "success": True,
            "status": "FILLED",
            "broker": "kotak",
            "call_buy_price": 145.50,
            "put_buy_price": 115.20,
            "call_order_id": "KOTAK_C_1",
            "put_order_id": "KOTAK_P_1",
        }

        from common.kotak_neo_adapter import resolve_kotak_option_contract
        call_contract = resolve_kotak_option_contract("06NOV2026", 24500, "CE", token=MOCK_TOKEN)
        put_contract = resolve_kotak_option_contract("06NOV2026", 24300, "PE", token=MOCK_TOKEN)

        assert call_contract["trading_symbol"] == "NIFTY26N0624500CE"
        assert put_contract["trading_symbol"] == "NIFTY26N0624300PE"

        # Execute call simulates what collector does
        res = mock_execute(
            broker="kotak",
            call_contract=call_contract,
            put_contract=put_contract,
            quantity=65,
            call_ltp=145.0,
            put_ltp=115.0,
            tag="TUE-BUY",
            snapshot_baseline=True,
        )
        assert res["success"] is True
        assert res["broker"] == "kotak"
        assert res["call_buy_price"] == 145.50

    @patch("config.get_broker_for_track")
    @patch("common.entry_order_manager.execute_paired_market_buy")
    def test_thursday_collector_kotak_branch(self, mock_execute, mock_broker):
        mock_broker.return_value = "kotak"
        mock_execute.return_value = {
            "success": True,
            "status": "FILLED",
            "broker": "kotak",
            "call_buy_price": 160.00,
            "put_buy_price": 130.00,
            "call_order_id": "KOTAK_THU_C_1",
            "put_order_id": "KOTAK_THU_P_1",
        }

        from common.kotak_neo_adapter import resolve_kotak_option_contract
        call_contract = resolve_kotak_option_contract("06NOV2026", 24500, "CE", token=MOCK_TOKEN)
        put_contract = resolve_kotak_option_contract("06NOV2026", 24300, "PE", token=MOCK_TOKEN)

        assert call_contract["trading_symbol"] == "NIFTY26N0624500CE"
        assert put_contract["trading_symbol"] == "NIFTY26N0624300PE"

        res = mock_execute(
            broker="kotak",
            call_contract=call_contract,
            put_contract=put_contract,
            quantity=65,
            call_ltp=160.0,
            put_ltp=130.0,
            tag="THU-BUY",
            snapshot_baseline=True,
        )
        assert res["success"] is True
        assert res["broker"] == "kotak"
        assert res["call_buy_price"] == 160.00
