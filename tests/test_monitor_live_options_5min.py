#!/usr/bin/env python3
"""
Unit tests for scripts.monitor_live_options_5min.
"""

import os
import unittest
from unittest.mock import patch, MagicMock

from scripts.monitor_live_options_5min import is_market_open, poll_cycle_telemetry


class MonitorLiveOptions5MinTests(unittest.TestCase):
    def test_is_market_open_force(self):
        self.assertTrue(is_market_open(force=True))

    @patch("scripts.monitor_live_options_5min.fetch_live_quotes")
    @patch("scripts.monitor_live_options_5min.update_latest_ltp")
    def test_poll_cycle_telemetry_active_cycle(self, mock_update, mock_quotes):
        mock_quotes.return_value = {
            "spot": 23050.0,
            "call_ltp": 320.0,
            "put_ltp": 150.0,
        }

        strat = {
            "cycle_id": "CYCLE-TUE-20261027",
            "status": "active",
            "call_strike": 22900,
            "put_strike": 23100,
            "expiry_date": "20261102",
            "day1_locked_price": {
                "combined_buy_price": 460.0,
            },
        }

        res = poll_cycle_telemetry("5day_strategy", strat, force=True, verbose=True)
        self.assertTrue(res)
        mock_update.assert_called_once_with(
            track="5day_strategy",
            spot=23050.0,
            call_ltp=320.0,
            put_ltp=150.0,
        )

    def test_poll_cycle_telemetry_pending_cycle(self):
        strat = {
            "cycle_id": None,
            "status": "pending_entry",
            "call_strike": None,
            "put_strike": None,
        }
        res = poll_cycle_telemetry("3day_strategy", strat, force=True, verbose=False)
        self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
