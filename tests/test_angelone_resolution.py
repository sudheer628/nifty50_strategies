import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.angelone_client import (
    clear_token_cache,
    get_nifty_option_chain,
    get_nifty_spot,
    get_option_ltp,
    resolve_option_token,
)
from common.expiry import get_next_weekly_expiry, format_expiry_angelone
from config import NIFTY50_TOKEN, NIFTY50_TRADING_SYMBOL


CURRENT_CONTRACTS = [
    {
        "token": "41012",
        "symbol": "NIFTY11AUG2624500PE",
        "name": "NIFTY",
        "expiry": "11AUG2026",
        "strike": "2450000.000000",
        "lotsize": "65",
        "instrumenttype": "OPTIDX",
        "exch_seg": "NFO",
    },
    {
        "token": "41020",
        "symbol": "NIFTY11AUG2624700CE",
        "name": "NIFTY",
        "expiry": "11AUG2026",
        "strike": "2470000.000000",
        "lotsize": "65",
        "instrumenttype": "OPTIDX",
        "exch_seg": "NFO",
    },
]


class ExpiryTests(unittest.TestCase):
    def test_monday_selects_next_day_expiry(self):
        self.assertEqual(
            get_next_weekly_expiry(date(2026, 8, 10)),
            date(2026, 8, 11),
        )

    def test_expiry_tuesday_rolls_to_following_week(self):
        self.assertEqual(
            get_next_weekly_expiry(date(2026, 8, 11)),
            date(2026, 8, 18),
        )

    def test_holiday_tuesday_prepones_expiry_to_monday(self):
        # 2026-10-20 is Dussehra (holiday). On Tuesday 2026-10-13, the next expiry
        # must prepone to Monday 2026-10-19.
        next_expiry = get_next_weekly_expiry(date(2026, 10, 13))
        self.assertEqual(next_expiry, date(2026, 10, 19))
        self.assertEqual(format_expiry_angelone(next_expiry), "19OCT2026")

    def test_preponed_expiry_day_rolls_to_following_week(self):
        # On Monday 2026-10-19 (the preponed expiry day itself), looking up the next
        # weekly expiry must roll to the following week's Tuesday 2026-10-27.
        next_expiry = get_next_weekly_expiry(date(2026, 10, 19))
        self.assertEqual(next_expiry, date(2026, 10, 27))
        self.assertEqual(format_expiry_angelone(next_expiry), "27OCT2026")

    def test_holi_tuesday_prepones_to_monday(self):
        # 2026-03-03 is Holi (Tuesday holiday). From Wednesday 2026-02-25, the next
        # weekly expiry must prepone to Monday 2026-03-02.
        next_expiry = get_next_weekly_expiry(date(2026, 2, 25))
        self.assertEqual(next_expiry, date(2026, 3, 2))
        self.assertEqual(format_expiry_angelone(next_expiry), "02MAR2026")


class InstrumentResolutionTests(unittest.TestCase):
    def setUp(self):
        clear_token_cache()

    def test_nifty_spot_constants(self):
        self.assertEqual(NIFTY50_TOKEN, "99926000")
        self.assertEqual(NIFTY50_TRADING_SYMBOL, "Nifty 50")

    @patch.dict(os.environ, {"UPSTOX_FEED_CACHE_PATH": "/tmp/nonexistent_upstox_feed_for_test.json"})
    @patch("common.angelone_client.build_headers", return_value={"test": "header"})
    @patch("common.angelone_client.requests.post")
    def test_nifty_spot_uses_official_nse_token(self, post, _headers):
        response = Mock(status_code=200)
        response.text = '{"status": true}'
        response.json.return_value = {
            "status": True,
            "data": {
                "fetched": [
                    {
                        "symbolToken": "99926000",
                        "ltp": 24600.0,
                        "open": 24550.0,
                        "close": 24480.0,
                        "high": 24650.0,
                        "low": 24500.0,
                    }
                ],
                "unfetched": [],
            },
        }
        post.return_value = response

        result = get_nifty_spot()

        self.assertEqual(result["ltp"], 24600.0)
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "mode": "FULL",
                "exchangeTokens": {"NSE": ["99926000"]},
            },
        )

    @patch(
        "common.angelone_client.get_instrument_master",
        return_value=CURRENT_CONTRACTS,
    )
    def test_current_week_put(self, _master):
        self.assertEqual(
            resolve_option_token("11AUG2026", 24500, "PE"),
            {
                "token": "41012",
                "trading_symbol": "NIFTY11AUG2624500PE",
                "exchange": "NFO",
                "lotsize": 65,
            },
        )

    @patch(
        "common.angelone_client.get_instrument_master",
        return_value=CURRENT_CONTRACTS,
    )
    def test_current_week_call(self, _master):
        self.assertEqual(
            resolve_option_token("11AUG2026", 24700, "CE"),
            {
                "token": "41020",
                "trading_symbol": "NIFTY11AUG2624700CE",
                "exchange": "NFO",
                "lotsize": 65,
            },
        )

    @patch("common.angelone_client.get_instrument_master", return_value=[])
    @patch("common.angelone_client.search_instruments")
    def test_search_scrip_fallback_uses_api_field_names(
        self, search_instruments, _master
    ):
        search_instruments.return_value = [
            {
                "exchange": "NFO",
                "tradingsymbol": "NIFTY11AUG2624500PE",
                "symboltoken": "41012",
            }
        ]

        result = resolve_option_token("11AUG2026", 24500, "PE")

        self.assertEqual(result["token"], "41012")
        self.assertEqual(result["trading_symbol"], "NIFTY11AUG2624500PE")
        search_instruments.assert_called_once_with(
            "NIFTY11AUG2624500PE", exchange="NFO"
        )

    @patch(
        "common.angelone_client.get_instrument_master",
        return_value=CURRENT_CONTRACTS,
    )
    @patch("common.angelone_client.build_headers", return_value={"test": "header"})
    @patch("common.angelone_client.requests.post")
    def test_option_chain_fetches_both_tokens_in_one_request(
        self, post, _headers, _master
    ):
        response = Mock(status_code=200)
        response.text = '{"status": true}'
        response.json.return_value = {
            "status": True,
            "data": {
                "fetched": [
                    {"symbolToken": "41020", "ltp": 101.25},
                    {"symbolToken": "41012", "ltp": 88.5},
                ],
                "unfetched": [],
            },
        }
        post.return_value = response

        result = get_nifty_option_chain("11AUG2026", 24700, 24500)

        self.assertEqual(post.call_count, 1)
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "mode": "LTP",
                "exchangeTokens": {"NFO": ["41020", "41012"]},
            },
        )
        self.assertEqual(result["call"]["ltp"], 101.25)
        self.assertEqual(result["put"]["ltp"], 88.5)

    @patch(
        "common.angelone_client.get_instrument_master",
        return_value=CURRENT_CONTRACTS,
    )
    def test_token_caching_avoids_repeated_resolution(self, mock_master):
        res1 = resolve_option_token("11AUG2026", 24500, "PE")
        self.assertEqual(res1["token"], "41012")
        self.assertEqual(mock_master.call_count, 1)

        # Second call should hit the cache without calling get_instrument_master again
        res2 = resolve_option_token("11AUG2026", 24500, "PE")
        self.assertEqual(res2["token"], "41012")
        self.assertEqual(mock_master.call_count, 1)

    @patch("common.angelone_client.get_redis_client")
    @patch("common.angelone_client.requests.get")
    @patch("common.angelone_client.get_instrument_master", return_value=CURRENT_CONTRACTS)
    @patch("common.angelone_client.build_headers", return_value={"test": "header"})
    @patch("common.angelone_client.requests.post")
    def test_option_chain_falls_back_to_upstox_when_angelone_ltp_empty(
        self, mock_post, _headers, _master, mock_upstox_get, mock_redis
    ):
        # Angel One returns empty fetched list
        response = Mock(status_code=200)
        response.text = '{"status": true}'
        response.json.return_value = {
            "status": True,
            "data": {"fetched": [], "unfetched": []},
        }
        mock_post.return_value = response

        # Mock Redis returning an Upstox access token
        mock_r = Mock()
        mock_r.get.return_value = "fake_upstox_token"
        mock_redis.return_value = mock_r

        # Mock Upstox option chain response
        upstox_resp = Mock(status_code=200)
        upstox_resp.json.return_value = {
            "status": "success",
            "data": [
                {
                    "strike_price": 24700,
                    "call_options": {
                        "instrument_key": "NSE_FO|41020",
                        "market_data": {"ltp": 105.50},
                    },
                },
                {
                    "strike_price": 24500,
                    "put_options": {
                        "instrument_key": "NSE_FO|41012",
                        "market_data": {"ltp": 92.00},
                    },
                },
            ],
        }
        mock_upstox_get.return_value = upstox_resp

        result = get_nifty_option_chain("11AUG2026", 24700, 24500)
        self.assertEqual(result["call"]["ltp"], 105.50)
        self.assertEqual(result["call"]["source"], "upstox_api")
        self.assertEqual(result["put"]["ltp"], 92.00)
        self.assertEqual(result["put"]["source"], "upstox_api")

    @patch("common.angelone_client.get_redis_client")
    @patch("common.angelone_client.requests.get")
    @patch("common.angelone_client.build_headers", return_value={"test": "header"})
    @patch("common.angelone_client.requests.post")
    def test_get_option_ltp_falls_back_to_upstox_on_angelone_failure(
        self, mock_post, _headers, mock_upstox_get, mock_redis
    ):
        # Angel One call fails (HTTP 500)
        mock_post.return_value = Mock(status_code=500, text="Internal Error")

        # Mock Redis
        mock_r = Mock()
        mock_r.get.return_value = "fake_upstox_token"
        mock_redis.return_value = mock_r

        # Mock Upstox option chain response
        upstox_resp = Mock(status_code=200)
        upstox_resp.json.return_value = {
            "status": "success",
            "data": [
                {
                    "strike_price": 24500,
                    "put_options": {
                        "instrument_key": "NSE_FO|41012",
                        "market_data": {"ltp": 95.25},
                    },
                },
            ],
        }
        mock_upstox_get.return_value = upstox_resp

        result = get_option_ltp("NFO", "NIFTY11AUG2624500PE", "41012")
        self.assertEqual(result["ltp"], 95.25)
        self.assertEqual(result["source"], "upstox_api")


if __name__ == "__main__":
    unittest.main()
