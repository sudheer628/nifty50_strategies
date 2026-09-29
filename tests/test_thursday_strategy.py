"""
test_thursday_strategy.py - Comprehensive Unit Tests for 3-Day Thursday Strategy Track
Tests calendar helpers, snapshot isolation, peak monitoring, report dispatch, and close orchestration.
"""

from datetime import date, datetime
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from common.calendar_utils import (
    get_thursday_strategy_cycle_role,
    is_thursday_strategy_active_day,
    is_thursday_strategy_start_day,
)
from common.storage import (
    build_thursday_db_path,
    load_active_snapshot,
    mark_active_cycle_closed,
    save_active_snapshot,
    update_active_peak_state,
)
from scripts.send_weekly_report import send_weekly_report_discord
from strategies.peak_profit_monitor import monitor_once
from strategies.thursday_option_collector import collect_once, THURSDAY_SNAPSHOT_FILE
from common.entry_gate import EntryGateResult


class ThursdayStrategyTests(unittest.TestCase):

    def test_thursday_calendar_roles_and_active_days(self):
        """Test Thursday strategy cycle recognition across weekdays."""
        # 2026-10-08 is a regular non-holiday Thursday
        thu_date = date(2026, 10, 8)
        self.assertEqual(thu_date.strftime("%A"), "Thursday")
        self.assertTrue(is_thursday_strategy_start_day(thu_date))
        self.assertTrue(is_thursday_strategy_active_day(thu_date))
        self.assertEqual(get_thursday_strategy_cycle_role(thu_date), "START_DAY")

        # 2026-10-09 is a regular non-holiday Friday
        fri_date = date(2026, 10, 9)
        self.assertEqual(fri_date.strftime("%A"), "Friday")
        self.assertFalse(is_thursday_strategy_start_day(fri_date))
        self.assertTrue(is_thursday_strategy_active_day(fri_date))
        self.assertEqual(get_thursday_strategy_cycle_role(fri_date), "REGULAR_DAY")

        # 2026-10-12 is Monday (expiry closing day)
        mon_date = date(2026, 10, 12)
        self.assertEqual(mon_date.strftime("%A"), "Monday")
        self.assertTrue(is_thursday_strategy_active_day(mon_date))
        self.assertEqual(get_thursday_strategy_cycle_role(mon_date), "CLOSING_DAY")

        # 2026-10-13 is Tuesday (off-cycle for Thursday track unless deferred)
        tue_date = date(2026, 10, 13)
        self.assertEqual(tue_date.strftime("%A"), "Tuesday")
        self.assertFalse(is_thursday_strategy_start_day(tue_date))

        # 2026-10-14 is Wednesday (off-cycle)
        wed_date = date(2026, 10, 14)
        self.assertEqual(wed_date.strftime("%A"), "Wednesday")
        self.assertFalse(is_thursday_strategy_active_day(wed_date))
        self.assertEqual(get_thursday_strategy_cycle_role(wed_date), "OFF_CYCLE")

        # Verify Mahatma Gandhi Jayanti (2026-10-02) is recognized as holiday
        gandhi_jayanti = date(2026, 10, 2)
        self.assertFalse(is_thursday_strategy_active_day(gandhi_jayanti))
        self.assertEqual(get_thursday_strategy_cycle_role(gandhi_jayanti), "HOLIDAY")

    def test_thursday_db_path_construction(self):
        """Test naming convention for Thursday cycle sqlite DBs."""
        db_path = build_thursday_db_path("20261001", "20261006")
        self.assertTrue(db_path.endswith("nifty50_thursday_data_20261001_20261006.db"))
        self.assertIn("strategies", db_path)

    def test_custom_snapshot_isolation(self):
        """Test that writing to Thursday snapshot does not affect weekly snapshot."""
        with tempfile.TemporaryDirectory() as tmpdir:
            thu_snap_path = os.path.join(tmpdir, "current_thursday_buy.json")
            weekly_snap_path = os.path.join(tmpdir, "current_week_buy.json")

            # Save weekly snapshot
            weekly_data = {
                "strategy_name": "nifty50_weekly_option_collector",
                "cycle_id": "20260929-test",
                "status": "ongoing",
                "call_strike": 25200,
                "put_strike": 24800,
            }
            save_active_snapshot(weekly_data, filepath=weekly_snap_path)

            # Save Thursday snapshot
            thu_data = {
                "strategy_name": "nifty50_thursday_3day_collector",
                "cycle_id": "20261001-thu-test",
                "status": "ongoing",
                "call_strike": 25400,
                "put_strike": 25000,
            }
            save_active_snapshot(thu_data, filepath=thu_snap_path)

            # Load and verify isolation
            loaded_weekly = load_active_snapshot(filepath=weekly_snap_path)
            loaded_thu = load_active_snapshot(filepath=thu_snap_path)

            self.assertEqual(loaded_weekly["strategy_name"], "nifty50_weekly_option_collector")
            self.assertEqual(loaded_weekly["call_strike"], 25200)

            self.assertEqual(loaded_thu["strategy_name"], "nifty50_thursday_3day_collector")
            self.assertEqual(loaded_thu["call_strike"], 25400)

            # Update peak state on Thursday snapshot only
            peak_update = {
                "peak_pnl_pct": 18.5,
                "peak_pnl_pts": 37.0,
                "alerts_sent_today": 1,
            }
            update_active_peak_state(peak_update, filepath=thu_snap_path)

            reloaded_weekly = load_active_snapshot(filepath=weekly_snap_path)
            reloaded_thu = load_active_snapshot(filepath=thu_snap_path)

            self.assertNotIn("peak_profit", reloaded_weekly)
            self.assertEqual(reloaded_thu["peak_profit"]["peak_pnl_pct"], 18.5)

            # Close Thursday cycle
            mark_active_cycle_closed(filepath=thu_snap_path)
            reloaded_thu = load_active_snapshot(filepath=thu_snap_path)
            reloaded_weekly = load_active_snapshot(filepath=weekly_snap_path)

            self.assertEqual(reloaded_thu["status"], "closed")
            self.assertEqual(reloaded_weekly["status"], "ongoing")

    @patch("strategies.peak_profit_monitor.check_nse_holiday", return_value=False)
    @patch("strategies.peak_profit_monitor._today_ist", return_value=date(2026, 10, 8))
    @patch("strategies.peak_profit_monitor._is_market_time", return_value=True)
    @patch("strategies.peak_profit_monitor.load_active_snapshot")
    @patch("strategies.peak_profit_monitor.get_nifty_spot")
    @patch("strategies.peak_profit_monitor.get_nifty_option_chain")
    @patch("strategies.peak_profit_monitor.evaluate_peak_profit")
    def test_peak_monitor_thursday_track_dispatch(
        self, mock_eval, mock_chain, mock_spot, mock_snap, mock_market, mock_today, mock_holiday
    ):
        """Test peak profit monitor invoked with track='thursday'."""
        mock_snap.return_value = {
            "strategy_name": "nifty50_thursday_3day_collector",
            "cycle_id": "20261008-thu",
            "week_start_date": "20261008",
            "expiry_date": "20261013",
            "call_strike": 25400,
            "put_strike": 25000,
            "call_buy_price": 95.0,
            "put_buy_price": 85.0,
            "status": "ongoing",
            "alpha_fsm": {"state": "DUAL_LONG", "total_gainloss": 10.0},
        }
        mock_spot.return_value = {"ltp": 25200.0}
        mock_chain.return_value = {
            "call": {"ltp": 125.0},
            "put": {"ltp": 95.0},
        }
        mock_eval.return_value = (
            {"peak_pnl_pts": 40.0, "peak_pnl_pct": 22.2, "alerts_sent_today": 1},
            True,
            "Qualifying peak alert triggered",
        )

        success = monitor_once(force=False, no_email=False, track="thursday")
        self.assertTrue(success)
        mock_spot.assert_called_once()

        # Verify evaluate_peak_profit received track_label for Thursday
        mock_eval.assert_called_once()
        call_kwargs = mock_eval.call_args[1]
        self.assertEqual(call_kwargs.get("track_label"), "3-Day Thursday Strategy")

    @patch("scripts.send_weekly_report.send_discord_watchdog")
    def test_send_weekly_report_thursday_report_card(self, mock_send):
        """Test Discord report card formatting for Thursday track."""
        mock_send.return_value = True
        summary = {
            "cycle_id": "20261001-thu",
            "latest_gainloss": 28.5,
            "best_gainloss": 35.0,
            "worst_gainloss": -5.0,
            "call_strike": 25400,
            "put_strike": 25000,
            "call_buy_price": 95.0,
            "put_buy_price": 85.0,
            "nifty_start": 25150.0,
            "nifty_latest": 25300.0,
            "nifty_change": 150.0,
            "start_date": date(2026, 10, 1),
            "expiry_date": date(2026, 10, 6),
            "selection_mode": "STATIC_RULE",
            "row_count": 25,
            "track": "thursday",
        }
        res = send_weekly_report_discord(summary, track="thursday")
        self.assertTrue(res)
        mock_send.assert_called_once()

        # Check headline and embed payload
        headline = mock_send.call_args[0][0]
        embed = mock_send.call_args[0][1]
        self.assertIn("3-Day Thursday Strategy Close", headline)
        self.assertIn("3-Day Thursday Strategy", embed.get("title", ""))

    @patch("strategies.thursday_option_collector.insert_record")
    @patch("strategies.thursday_option_collector.insert_buy_snapshot")
    @patch("strategies.thursday_option_collector.save_active_snapshot")
    @patch("strategies.thursday_option_collector._ensure_thursday_db", return_value="test_thursday.db")
    @patch("strategies.thursday_option_collector.get_nifty_option_chain")
    @patch("strategies.thursday_option_collector.get_nifty_spot")
    @patch("strategies.thursday_option_collector.select_strikes")
    @patch("strategies.thursday_option_collector.evaluate_entry_gate")
    @patch("strategies.thursday_option_collector.load_active_snapshot", return_value={})
    @patch("strategies.thursday_option_collector.check_nse_holiday", return_value=False)
    @patch("strategies.thursday_option_collector._today_ist", return_value=date(2026, 10, 8))
    def test_thursday_collect_once_fresh_entry_success(
        self, mock_today, mock_holiday, mock_snap, mock_gate, mock_strikes, mock_spot,
        mock_chain, mock_db, mock_save, mock_buy_snap, mock_rec
    ):
        """Test full end-to-end execution of Thursday morning fresh cycle entry."""
        mock_gate.return_value = EntryGateResult(
            should_enter=True,
            status="CLEARED",
            defer_reason="All filters passed",
            iv_slope=0.1,
            current_iv=13.5,
            vwap_distance=0.05,
            adx_14=22.0,
            opening_range=25.0,
            evaluated_at_ist="09:31:00",
        )
        mock_strikes.return_value = (
            25100, 25300, 25100, 25300, "DYNAMIC_AI", "Balanced volatility profile",
            0.15, "BULLISH", 0.35, -0.35
        )
        mock_spot.return_value = {"ltp": 25200.0, "open": 25180.0, "close": 25150.0}
        mock_chain.return_value = {
            "call": {"ltp": 120.0},
            "put": {"ltp": 95.0},
        }

        ok = collect_once(force_static=False, force_entry=False, dry_run=True)
        self.assertTrue(ok)

        # 1. Gate evaluated with reference LTP
        mock_gate.assert_called_once()
        self.assertEqual(mock_gate.call_args[1].get("reference_ltp"), 25200.0)

        # 2. Strikes selected
        mock_strikes.assert_called_once()

        # 3. Snapshot saved with Thursday strikes and buy prices to THURSDAY_SNAPSHOT_FILE
        mock_save.assert_called_once()
        saved_snap = mock_save.call_args[0][0]
        self.assertEqual(saved_snap["call_strike"], 25300)
        self.assertEqual(saved_snap["put_strike"], 25100)
        self.assertEqual(saved_snap["call_buy_price"], 120.0)
        self.assertEqual(saved_snap["put_buy_price"], 95.0)
        self.assertEqual(mock_save.call_args[1].get("filepath"), THURSDAY_SNAPSHOT_FILE)

        # 4. Hourly record inserted
        mock_rec.assert_called_once()
        rec = mock_rec.call_args[0][1]
        self.assertEqual(rec["strategy_name"], "nifty50_thursday_3day_collector")
        self.assertEqual(rec["call_ltp"], 120.0)
        self.assertEqual(rec["put_ltp"], 95.0)
        self.assertEqual(rec["gainloss"], 0.0)

    @patch("strategies.thursday_option_collector.insert_gate_deferred_shadow")
    @patch("strategies.thursday_option_collector.save_active_snapshot")
    @patch("strategies.thursday_option_collector._ensure_thursday_db", return_value="test_thursday.db")
    @patch("strategies.thursday_option_collector.get_nifty_option_chain")
    @patch("strategies.thursday_option_collector.get_nifty_spot")
    @patch("strategies.thursday_option_collector.select_strikes")
    @patch("strategies.thursday_option_collector.evaluate_entry_gate")
    @patch("strategies.thursday_option_collector.load_active_snapshot", return_value={})
    @patch("strategies.thursday_option_collector.check_nse_holiday", return_value=False)
    @patch("strategies.thursday_option_collector._today_ist", return_value=date(2026, 10, 8))
    def test_thursday_collect_once_gate_deferred(
        self, mock_today, mock_holiday, mock_snap, mock_gate, mock_strikes, mock_spot,
        mock_chain, mock_db, mock_save, mock_shadow
    ):
        """Test that Smart Entry Gate deferral records counterfactual shadow and skips active entry."""
        mock_gate.return_value = EntryGateResult(
            should_enter=False,
            status="DEFERRED",
            defer_reason="Opening chop range < 15 pts",
            iv_slope=0.1,
            current_iv=13.5,
            vwap_distance=0.01,
            adx_14=12.0,
            opening_range=8.0,
            evaluated_at_ist="09:31:00",
        )
        mock_strikes.return_value = (
            25100, 25300, 25100, 25300, "DYNAMIC_AI", "Counterfactual",
            0.0, "NEUTRAL", 0.35, -0.35
        )
        mock_spot.return_value = {"ltp": 25200.0, "open": 25195.0, "close": 25190.0}
        mock_chain.return_value = {
            "call": {"ltp": 115.0},
            "put": {"ltp": 90.0},
        }

        ok = collect_once(force_static=False, force_entry=False, dry_run=True)
        self.assertTrue(ok)

        # Counterfactual shadow must be recorded
        mock_shadow.assert_called_once()
        shadow_rec = mock_shadow.call_args[0][1]
        self.assertEqual(shadow_rec["gate_status"], "DEFERRED")
        self.assertEqual(shadow_rec["defer_reason"], "Opening chop range < 15 pts")
        self.assertEqual(shadow_rec["hypothetical_call_strike"], 25300)
        self.assertEqual(shadow_rec["hypothetical_put_strike"], 25100)

        # Active snapshot MUST NOT be saved
        mock_save.assert_not_called()

    @patch("strategies.thursday_option_collector.insert_record")
    @patch("strategies.thursday_option_collector.insert_buy_snapshot")
    @patch("strategies.thursday_option_collector._ensure_thursday_db", return_value="test_thursday.db")
    @patch("strategies.thursday_option_collector.get_nifty_option_chain")
    @patch("strategies.thursday_option_collector.get_nifty_spot")
    @patch("strategies.thursday_option_collector.load_active_snapshot")
    @patch("strategies.thursday_option_collector.check_nse_holiday", return_value=False)
    @patch("strategies.thursday_option_collector._today_ist", return_value=date(2026, 10, 9))
    def test_thursday_collect_once_ongoing_tick_reuses_strikes(
        self, mock_today, mock_holiday, mock_snap, mock_spot, mock_chain, mock_db,
        mock_buy_snap, mock_rec
    ):
        """Test that Friday ongoing tick reuses locked Thursday strikes and buy prices."""
        active_snap = {
            "strategy_name": "nifty50_thursday_3day_collector",
            "cycle_id": "CYCLE-THU-20261008",
            "week_start_date": "20261008",
            "expiry_date": "20261013",
            "call_strike": 25300,
            "put_strike": 25100,
            "call_buy_price": 120.0,
            "put_buy_price": 95.0,
            "status": "ongoing",
            "alpha_fsm": {"state": "DUAL_LONG", "total_gainloss": 5.0},
        }
        mock_snap.return_value = active_snap
        mock_spot.return_value = {"ltp": 25250.0, "open": 25210.0, "close": 25200.0}
        mock_chain.return_value = {
            "call": {"ltp": 145.0},
            "put": {"ltp": 80.0},
        }

        ok = collect_once(dry_run=True)
        self.assertTrue(ok)

        # Verified options called with locked strikes
        mock_chain.assert_called_once_with("13OCT2026", 25300, 25100)

        # Verified gainloss calculated: (145 - 120) + (80 - 95) = 25 - 15 = 10 pts
        mock_rec.assert_called_once()
        rec = mock_rec.call_args[0][1]
        self.assertEqual(rec["gainloss"], 10.0)
        self.assertEqual(rec["call_buy_price"], 120.0)
        self.assertEqual(rec["put_buy_price"], 95.0)

    @patch("strategies.thursday_option_collector.get_nifty_spot")
    @patch("strategies.thursday_option_collector.check_nse_holiday", return_value=False)
    @patch("strategies.thursday_option_collector._today_ist", return_value=date(2026, 10, 7))
    def test_thursday_collect_once_off_cycle_day_no_op(self, mock_today, mock_holiday, mock_spot):
        """Test that Wednesday cleanly no-ops without fetching spot or executing strategy."""
        ok = collect_once()
        self.assertTrue(ok)
        mock_spot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
