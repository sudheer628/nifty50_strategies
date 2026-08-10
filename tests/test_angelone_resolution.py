import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.angelone_client import (
    get_nifty_option_chain,
    get_nifty_spot,
    resolve_option_token,
)
from common.expiry import get_next_weekly_expiry
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


class InstrumentResolutionTests(unittest.TestCase):
    def test_nifty_spot_constants(self):
        self.assertEqual(NIFTY50_TOKEN, "99926000")
        self.assertEqual(NIFTY50_TRADING_SYMBOL, "Nifty 50")

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


if __name__ == "__main__":
    unittest.main()
