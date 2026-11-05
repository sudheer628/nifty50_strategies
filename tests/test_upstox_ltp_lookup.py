"""
Upstox LTP lookup for the trailing SL-M path.

Regression: Upstox /v2/market-quote/quotes keys its response by "<segment>:<trading symbol>"
(e.g. "NSE_FO:NIFTY26N0924200CE"), not by the requested instrument key ("NSE_FO|39971").
fetch_ltp returned None, so trailing SL-M orders were never placed on Upstox.
"""
import json
from unittest.mock import MagicMock, patch

from common.broker_adapters import UpstoxAdapter
from tests.test_shared_contract_isolation import FakeRedis, thu_module, tue_module

# Shape captured from a live Upstox quote call on 2026-11-05 (values trimmed)
REAL_RESPONSE = {
    "status": "success",
    "data": {
        "NSE_FO:NIFTY26N0924200CE": {
            "instrument_token": "NSE_FO|39971",
            "symbol": "NIFTY26N0924200CE",
            "last_price": 386.0,
            "ohlc": {"open": 250.0, "high": 421.25, "low": 240.0, "close": 257.0},
        }
    },
}


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = json.dumps(payload)
    return r


def test_fetch_ltp_matches_instrument_token_field():
    with patch("common.broker_adapters.requests.get", return_value=_resp(REAL_RESPONSE)):
        assert UpstoxAdapter().fetch_ltp("TOKEN", "NSE_FO|39971") == 386.0


def test_fetch_ltp_still_accepts_instrument_key_keyed_response():
    legacy = {"data": {"NSE_FO:39971": {"last_price": 101.5}}}
    with patch("common.broker_adapters.requests.get", return_value=_resp(legacy)):
        assert UpstoxAdapter().fetch_ltp("TOKEN", "NSE_FO|39971") == 101.5


def test_fetch_ltp_symbol_keyed_response_without_token_field():
    payload = {"data": {"NSE_FO:NIFTY26N0924500PE": {"last_price": 140.25}}}
    with patch("common.broker_adapters.requests.get", return_value=_resp(payload)):
        assert UpstoxAdapter().fetch_ltp("TOKEN", "NSE_FO|40004", trading_symbol="NIFTY26N0924500PE") == 140.25


def test_fetch_ltp_ignores_other_instruments():
    with patch("common.broker_adapters.requests.get", return_value=_resp(REAL_RESPONSE)):
        assert UpstoxAdapter().fetch_ltp("TOKEN", "NSE_FO|40004") is None


def test_fetch_ltp_http_error_returns_none():
    with patch("common.broker_adapters.requests.get", return_value=_resp({"status": "error"}, status=500)):
        assert UpstoxAdapter().fetch_ltp("TOKEN", "NSE_FO|39971") is None


def _trailing_run(module, fn, cycle, tag):
    """Authorized leg in trailing mode (exit_immediately=False) with only the HTTP quote mocked."""
    sym, tok, expiry = "NIFTY26N0924200CE", "NSE_FO|39971", "2026-11-09"
    r = FakeRedis({
        f"fno:upstox:enabled:{cycle}:{expiry}:24200:CE": json.dumps({"enabled": True, "qty": 65}),
        f"ORDERS_LIVE_{cycle.upper()}": "1",
    })
    positions = [{"tradingsymbol": sym, "instrument_token": tok, "exchange": "NSE_FO", "quantity": 65,
                  "product": "D", "average_price": 257.45, "last_price": 386.0}]
    entry = {"entry_order_id": "BUY_1", "entry_price": 257.45, "entry_status": "FILLED",
             "same_broker_contract_overlap": False, "ambiguous_entry_attribution": False}
    with patch("common.broker_adapters.requests.get", return_value=_resp(REAL_RESPONSE)), \
         patch("common.unified_strategy_store.get_active_leg_entry_details", return_value=entry), \
         patch.object(module, "is_market_hours_ist", return_value=(True, "open")), \
         patch.object(module, "place_exit_order_slm", return_value={"success": True, "order_id": "SLM_1"}) as mock_slm, \
         patch.object(module, "place_exit_order_market") as mock_mkt, \
         patch.object(module, "dispatch_order_failure_discord"):
        res = getattr(module, fn)(token="SIM_TOKEN", positions=positions, orders=[], execute=True, redis_client=r,
                                  broker="upstox", active_expiry=expiry, force=False, exit_immediately=False,
                                  target_lots=1, tag=tag)
    return res, mock_slm, mock_mkt


def test_thursday_trailing_mode_places_slm_on_upstox_with_real_quote_shape():
    res, mock_slm, mock_mkt = _trailing_run(thu_module, "manage_thursday_exit_orders", "thu", "THU-SL")
    mock_mkt.assert_not_called()
    mock_slm.assert_called_once()
    trigger = mock_slm.call_args.args[3]
    assert 0 < trigger < 386.0, "SL-M trigger must sit below the live LTP"
    assert mock_slm.call_args.args[2] == 65
    assert not any(a.get("status") == "skipped" and "no fresh quote" in str(a.get("reason")) for a in res["actions"])


def test_tuesday_trailing_mode_places_slm_on_upstox_with_real_quote_shape():
    res, mock_slm, mock_mkt = _trailing_run(tue_module, "manage_tuesday_exit_orders", "tue", "TUE-SL")
    mock_mkt.assert_not_called()
    mock_slm.assert_called_once()
    assert 0 < mock_slm.call_args.args[3] < 386.0
