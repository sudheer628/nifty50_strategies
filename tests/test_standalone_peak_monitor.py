import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from strategies.peak_profit_monitor import monitor_once


class StandalonePeakMonitorTests(unittest.TestCase):
    def setUp(self):
        self.mock_snapshot = {
            "strategy_name": "nifty50_weekly_option_collector",
            "cycle_id": "20260922-test",
            "week_start_date": "20260922",
            "expiry_date": "20260929",
            "call_strike": 25000,
            "put_strike": 24800,
            "call_buy_price": 100.0,
            "put_buy_price": 100.0,
            "status": "ongoing",
            "alpha_fsm": {
                "state": "DUAL_LONG",
                "total_gainloss": 25.0,
            },
            "peak_profit": {
                "peak_pnl_pts": 25.0,
                "peak_pnl_pct": 12.5,
                "last_notified_pts": None,
                "alerts_sent_today": 0,
            }
        }

    @patch("strategies.peak_profit_monitor.check_nse_holiday", return_value=True)
    def test_holiday_skips(self, mock_holiday):
        result = monitor_once(force=False)
        self.assertTrue(result)

    @patch("strategies.peak_profit_monitor.check_nse_holiday", return_value=False)
    @patch("strategies.peak_profit_monitor._is_market_time", return_value=False)
    def test_outside_market_hours_skips(self, mock_time, mock_holiday):
        result = monitor_once(force=False)
        self.assertTrue(result)

    @patch("strategies.peak_profit_monitor.check_nse_holiday", return_value=False)
    @patch("strategies.peak_profit_monitor._is_market_time", return_value=True)
    @patch("strategies.peak_profit_monitor.load_active_snapshot", return_value={})
    def test_missing_snapshot_skips(self, mock_snap, mock_time, mock_holiday):
        result = monitor_once(force=False)
        self.assertTrue(result)

    @patch("strategies.peak_profit_monitor.check_nse_holiday", return_value=False)
    @patch("strategies.peak_profit_monitor._is_market_time", return_value=True)
    @patch("strategies.peak_profit_monitor.load_active_snapshot")
    def test_closed_cycle_skips(self, mock_snap, mock_time, mock_holiday):
        mock_snap.return_value = {"status": "closed", "cycle_id": "c1"}
        result = monitor_once(force=False)
        self.assertTrue(result)

    @patch("strategies.peak_profit_monitor.check_nse_holiday", return_value=False)
    @patch("strategies.peak_profit_monitor._is_market_time", return_value=True)
    @patch("strategies.peak_profit_monitor.load_active_snapshot")
    def test_unlocked_buy_prices_skips(self, mock_snap, mock_time, mock_holiday):
        mock_snap.return_value = {
            "status": "ongoing",
            "call_buy_price": None,
            "put_buy_price": None,
        }
        result = monitor_once(force=False)
        self.assertTrue(result)

    @patch("strategies.peak_profit_monitor.check_nse_holiday", return_value=False)
    @patch("strategies.peak_profit_monitor._is_market_time", return_value=True)
    @patch("strategies.peak_profit_monitor.load_active_snapshot")
    @patch("strategies.peak_profit_monitor.get_nifty_spot")
    @patch("strategies.peak_profit_monitor.get_nifty_option_chain")
    @patch("strategies.peak_profit_monitor.evaluate_peak_profit")
    def test_full_monitor_run_success(
        self, mock_eval, mock_chain, mock_spot, mock_snap, mock_time, mock_holiday
    ):
        mock_snap.return_value = dict(self.mock_snapshot)
        mock_spot.return_value = {"ltp": 25100.0}
        mock_chain.return_value = {
            "call": {"ltp": 138.0},
            "put": {"ltp": 105.0},
        }
        mock_eval.return_value = (
            {"peak_pnl_pts": 43.0, "peak_pnl_pct": 21.5, "alerts_sent_today": 1},
            True,
            "Qualifying peak alert triggered",
        )

        success = monitor_once(force=False, no_email=False)
        self.assertTrue(success)
        mock_spot.assert_called_once()
        mock_chain.assert_called_once_with("29SEP2026", 25000, 24800)
        mock_eval.assert_called_once_with(
            snapshot=self.mock_snapshot,
            nifty_ltp=25100.0,
            call_strike=25000,
            call_ltp=138.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=105.0,
            put_buy=100.0,
            is_first_tick=False,
            dry_run=False,
            fsm_state="DUAL_LONG",
            fsm_total_gainloss=25.0,
        )

    @patch("strategies.peak_profit_monitor.check_nse_holiday", return_value=False)
    @patch("strategies.peak_profit_monitor._is_market_time", return_value=True)
    @patch("strategies.peak_profit_monitor.load_active_snapshot")
    @patch("strategies.peak_profit_monitor.get_nifty_spot")
    @patch("strategies.peak_profit_monitor.get_nifty_option_chain")
    @patch("strategies.peak_profit_monitor.evaluate_peak_profit")
    def test_no_email_flag_passed_as_dry_run(
        self, mock_eval, mock_chain, mock_spot, mock_snap, mock_time, mock_holiday
    ):
        mock_snap.return_value = dict(self.mock_snapshot)
        mock_spot.return_value = {"ltp": 25100.0}
        mock_chain.return_value = {
            "call": {"ltp": 138.0},
            "put": {"ltp": 105.0},
        }
        mock_eval.return_value = (
            {"peak_pnl_pts": 43.0, "peak_pnl_pct": 21.5, "alerts_sent_today": 1},
            True,
            "Qualifying peak alert triggered",
        )

        success = monitor_once(force=True, no_email=True)
        self.assertTrue(success)
        self.assertTrue(mock_eval.call_args[1]["dry_run"])

    @patch("strategies.peak_profit_monitor.check_nse_holiday", return_value=False)
    @patch("strategies.peak_profit_monitor._is_market_time", return_value=True)
    @patch("strategies.peak_profit_monitor.load_active_snapshot")
    @patch("strategies.peak_profit_monitor.get_nifty_spot")
    @patch("strategies.peak_profit_monitor.get_nifty_option_chain")
    @patch("strategies.peak_profit_monitor.evaluate_peak_profit")
    def test_no_alert_flag_passed_as_dry_run(
        self, mock_eval, mock_chain, mock_spot, mock_snap, mock_time, mock_holiday
    ):
        mock_snap.return_value = dict(self.mock_snapshot)
        mock_spot.return_value = {"ltp": 25100.0}
        mock_chain.return_value = {
            "call": {"ltp": 138.0},
            "put": {"ltp": 105.0},
        }
        mock_eval.return_value = (
            {"peak_pnl_pts": 43.0, "peak_pnl_pct": 21.5, "alerts_sent_today": 1},
            True,
            "Qualifying peak alert triggered",
        )

        success = monitor_once(force=True, no_alert=True)
        self.assertTrue(success)
        self.assertTrue(mock_eval.call_args[1]["dry_run"])

    @patch("strategies.peak_profit_monitor.check_nse_holiday", return_value=False)
    @patch("strategies.peak_profit_monitor._is_market_time", return_value=True)
    @patch("strategies.peak_profit_monitor.load_active_snapshot")
    @patch("strategies.peak_profit_monitor.get_nifty_spot", side_effect=Exception("API timeout"))
    def test_api_error_returns_false_and_does_not_crash(
        self, mock_spot, mock_snap, mock_time, mock_holiday
    ):
        mock_snap.return_value = dict(self.mock_snapshot)
        success = monitor_once(force=True, no_email=False)
        self.assertFalse(success)


if __name__ == "__main__":
    unittest.main()
