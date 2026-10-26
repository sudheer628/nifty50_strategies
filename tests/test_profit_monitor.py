import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytz

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.profit_monitor import (
    calculate_strangle_pnl,
    evaluate_peak_profit,
    send_peak_alert_discord,
    on_new_peak,
)

IST = pytz.timezone("Asia/Kolkata")
TS_TUE_0930 = int(datetime(2026, 9, 22, 9, 30, tzinfo=IST).timestamp())
TS_TUE_1000 = int(datetime(2026, 9, 22, 10, 0, tzinfo=IST).timestamp())
TS_TUE_1100 = int(datetime(2026, 9, 22, 11, 0, tzinfo=IST).timestamp())
TS_TUE_1200 = int(datetime(2026, 9, 22, 12, 0, tzinfo=IST).timestamp())
TS_TUE_1300 = int(datetime(2026, 9, 22, 13, 0, tzinfo=IST).timestamp())
TS_TUE_1400 = int(datetime(2026, 9, 22, 14, 0, tzinfo=IST).timestamp())
TS_TUE_1500 = int(datetime(2026, 9, 22, 15, 0, tzinfo=IST).timestamp())
TS_WED_0930 = int(datetime(2026, 9, 23, 9, 30, tzinfo=IST).timestamp())


class ProfitMonitorTests(unittest.TestCase):
    def setUp(self):
        self.mock_snapshot = {
            "strategy_name": "nifty50_weekly_option_collector",
            "cycle_id": "20260922-test1234",
            "week_start_date": "20260922",
            "expiry_date": "20260929",
            "call_strike": 25000,
            "put_strike": 24800,
            "call_buy_price": 100.0,
            "put_buy_price": 100.0,
        }

    def test_calculate_strangle_pnl(self):
        pnl = calculate_strangle_pnl(
            call_buy=100.0,
            call_ltp=120.0,
            put_buy=100.0,
            put_ltp=110.0,
            lot_size=65,
        )
        self.assertEqual(pnl["call_pnl_pts"], 20.0)
        self.assertEqual(pnl["put_pnl_pts"], 10.0)
        self.assertEqual(pnl["combined_pnl_pts"], 30.0)
        self.assertEqual(pnl["combined_pnl_pct"], 15.0)  # 30 / 200 * 100
        self.assertEqual(pnl["combined_pnl_inr"], 1950.0)  # 30 * 65

    @patch("common.profit_monitor.update_active_peak_state")
    @patch("common.profit_monitor.on_new_peak")
    def test_first_tick_baseline_initialization_no_alert(self, mock_on_peak, mock_save_peak):
        snapshot = dict(self.mock_snapshot)
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=24900.0,
            call_strike=25000,
            call_ltp=100.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=100.0,
            put_buy=100.0,
            is_first_tick=True,
            current_ts=TS_TUE_0930,
        )
        self.assertFalse(triggered)
        self.assertEqual(state["peak_pnl_pts"], 0.0)
        self.assertEqual(state["peak_pnl_pct"], 0.0)
        self.assertIsNone(state["last_notified_pts"])
        self.assertEqual(state["alerts_sent_today"], 0)
        mock_on_peak.assert_not_called()
        self.assertTrue(mock_save_peak.called)

    @patch("common.profit_monitor.update_active_peak_state")
    @patch("common.profit_monitor.on_new_peak")
    def test_profit_below_20_pct_filter_sends_no_alert(self, mock_on_peak, mock_save_peak):
        snapshot = dict(self.mock_snapshot)
        snapshot["peak_profit"] = {
            "peak_pnl_pts": 0.0,
            "peak_pnl_pct": 0.0,
            "peak_pnl_inr": 0.0,
            "peak_timestamp": TS_TUE_0930,
            "last_notified_pts": None,
            "last_notified_pct": None,
            "last_notified_timestamp": None,
            "alerts_sent_today": 0,
            "last_alert_date": "2026-09-22",
        }

        # Step 1: Profit is +5% (+10 pts) -> Updates peak, zero alerts
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=24950.0,
            call_strike=25000,
            call_ltp=110.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=100.0,
            put_buy=100.0,
            is_first_tick=False,
            current_ts=TS_TUE_1000,
        )
        self.assertFalse(triggered)
        self.assertEqual(state["peak_pnl_pts"], 10.0)
        self.assertEqual(state["peak_pnl_pct"], 5.0)
        mock_on_peak.assert_not_called()

        # Step 2: Profit rises to +15% (+30 pts) -> Below 20%, zero alerts
        snapshot["peak_profit"] = state
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=25000.0,
            call_strike=25000,
            call_ltp=125.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=105.0,
            put_buy=100.0,
            is_first_tick=False,
            current_ts=TS_TUE_1100,
        )
        self.assertFalse(triggered)
        self.assertEqual(state["peak_pnl_pts"], 30.0)
        self.assertEqual(state["peak_pnl_pct"], 15.0)
        mock_on_peak.assert_not_called()

        # Step 3: Profit rises to +19.5% (+39 pts) -> Still below 20%, zero alerts
        snapshot["peak_profit"] = state
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=25050.0,
            call_strike=25000,
            call_ltp=134.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=105.0,
            put_buy=100.0,
            is_first_tick=False,
            current_ts=TS_TUE_1200,
        )
        self.assertFalse(triggered)
        self.assertEqual(state["peak_pnl_pts"], 39.0)
        self.assertEqual(state["peak_pnl_pct"], 19.5)
        self.assertIn("below minimum 20.0%", msg)
        mock_on_peak.assert_not_called()

    @patch("common.profit_monitor.update_active_peak_state")
    @patch("common.profit_monitor.on_new_peak")
    def test_first_alert_at_or_above_20_pct(self, mock_on_peak, mock_save_peak):
        snapshot = dict(self.mock_snapshot)
        snapshot["peak_profit"] = {
            "peak_pnl_pts": 39.0,
            "peak_pnl_pct": 19.5,
            "peak_pnl_inr": 2535.0,
            "peak_timestamp": TS_TUE_1200,
            "last_notified_pts": None,
            "last_notified_pct": None,
            "last_notified_timestamp": None,
            "alerts_sent_today": 0,
            "last_alert_date": "2026-09-22",
        }

        # Profit hits +21.5% (+43 pts) -> Meets >= 20.0%!
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=25100.0,
            call_strike=25000,
            call_ltp=138.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=105.0,
            put_buy=100.0,
            is_first_tick=False,
            current_ts=TS_TUE_1300,
        )
        self.assertTrue(triggered)
        self.assertEqual(state["peak_pnl_pts"], 43.0)
        self.assertEqual(state["peak_pnl_pct"], 21.5)
        self.assertEqual(state["last_notified_pts"], 43.0)
        self.assertEqual(state["last_notified_pct"], 21.5)
        self.assertEqual(state["alerts_sent_today"], 1)
        self.assertEqual(mock_on_peak.call_count, 1)

    @patch("common.profit_monitor.update_active_peak_state")
    @patch("common.profit_monitor.on_new_peak")
    def test_single_alert_policy_suppresses_subsequent_expansions(self, mock_on_peak, mock_save_peak):
        snapshot = dict(self.mock_snapshot)
        # Previously alerted at +21.5% (+43.0 pts)
        snapshot["peak_profit"] = {
            "peak_pnl_pts": 43.0,
            "peak_pnl_pct": 21.5,
            "peak_pnl_inr": 2795.0,
            "peak_timestamp": TS_TUE_1300,
            "last_notified_pts": 43.0,
            "last_notified_pct": 21.5,
            "last_notified_timestamp": TS_TUE_1300,
            "alerts_sent_today": 1,
            "last_alert_date": "2026-09-22",
            "alert_dispatched": True,
        }

        # Small expansion to +22.5% (+45.0 pts): +1.0% / +2 pts bump -> Suppressed under single-alert policy!
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=25120.0,
            call_strike=25000,
            call_ltp=142.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=103.0,
            put_buy=100.0,
            is_first_tick=False,
            current_ts=TS_TUE_1400,
        )
        self.assertFalse(triggered)
        self.assertEqual(state["peak_pnl_pts"], 45.0)  # Peak tracked in snapshot
        self.assertEqual(state["last_notified_pts"], 43.0)  # Last notified remains unchanged
        self.assertEqual(state["alerts_sent_today"], 1)
        self.assertIn("already dispatched for cycle", msg)
        mock_on_peak.assert_not_called()

        # Surge to +27.5% (+55.0 pts): +6.0% / +12 pts bump -> Still suppressed! Single-alert policy.
        snapshot["peak_profit"] = state
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=25200.0,
            call_strike=25000,
            call_ltp=160.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=95.0,
            put_buy=100.0,
            is_first_tick=False,
            current_ts=TS_TUE_1500,
        )
        self.assertFalse(triggered)
        self.assertEqual(state["peak_pnl_pts"], 55.0)  # Peak updated faithfully
        self.assertEqual(state["peak_pnl_pct"], 27.5)
        self.assertEqual(state["last_notified_pts"], 43.0)
        self.assertEqual(state["alerts_sent_today"], 1)
        self.assertIn("Single-alert policy suppresses expansion alerts", msg)
        mock_on_peak.assert_not_called()

    @patch("common.profit_monitor.update_active_peak_state")
    @patch("common.profit_monitor.on_new_peak")
    def test_rollback_on_failed_dispatch_allows_retry(self, mock_on_peak, mock_save_peak):
        mock_on_peak.return_value = False  # Simulate Discord delivery failure
        snapshot = dict(self.mock_snapshot)
        snapshot["peak_profit"] = {
            "peak_pnl_pts": 39.0,
            "peak_pnl_pct": 19.5,
            "peak_pnl_inr": 2535.0,
            "peak_timestamp": TS_TUE_1200,
            "last_notified_pts": None,
            "last_notified_pct": None,
            "last_notified_timestamp": None,
            "alerts_sent_today": 0,
            "last_alert_date": "2026-09-22",
            "alert_dispatched": False,
        }

        # Tick 1: Reaches +21.5% but dispatch fails -> State rolled back
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=25100.0,
            call_strike=25000,
            call_ltp=138.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=105.0,
            put_buy=100.0,
            is_first_tick=False,
            current_ts=TS_TUE_1300,
        )
        self.assertFalse(triggered)
        self.assertFalse(state.get("alert_dispatched"))
        self.assertIsNone(state.get("last_notified_pts"))
        self.assertIn("alert delivery failed; state rolled back", msg)

        # Tick 2: Discord connectivity restored -> Retries and succeeds!
        mock_on_peak.return_value = True
        snapshot["peak_profit"] = state
        state2, triggered2, msg2 = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=25120.0,
            call_strike=25000,
            call_ltp=140.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=105.0,
            put_buy=100.0,
            is_first_tick=False,
            current_ts=TS_TUE_1400,
        )
        self.assertTrue(triggered2)
        self.assertTrue(state2.get("alert_dispatched"))
        self.assertEqual(state2["last_notified_pts"], 45.0)
        self.assertEqual(state2["alerts_sent_today"], 1)

    @patch("common.profit_monitor.update_active_peak_state")
    @patch("common.profit_monitor.on_new_peak")
    def test_day_rollover_resets_daily_counter(self, mock_on_peak, mock_save_peak):
        snapshot = dict(self.mock_snapshot)
        snapshot["peak_profit"] = {
            "peak_pnl_pts": 70.0,
            "peak_pnl_pct": 35.0,
            "peak_pnl_inr": 4550.0,
            "peak_timestamp": TS_TUE_1500,
            "last_notified_pts": 55.0,
            "last_notified_pct": 27.5,
            "last_notified_timestamp": TS_TUE_1400,
            "alerts_sent_today": 4,
            "last_alert_date": "2026-09-22",  # Yesterday
        }

        # Next day tick at Wednesday 09:30 AM with new peak +40% (+80 pts)
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=25400.0,
            call_strike=25000,
            call_ltp=190.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=90.0,
            put_buy=100.0,
            is_first_tick=False,
            current_ts=TS_WED_0930,  # 2026-09-23 in IST
        )
        self.assertFalse(triggered)
        self.assertEqual(state["peak_pnl_pts"], 80.0)
        self.assertEqual(state["alerts_sent_today"], 0)
        self.assertEqual(state["last_alert_date"], "2026-09-23")
        mock_on_peak.assert_not_called()

    @patch("common.profit_monitor.requests.post")
    def test_send_peak_alert_discord_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_post.return_value = mock_response

        event = {
            "cycle_id": "20260922-test",
            "week_start_date": "20260922",
            "expiry_date": "20260929",
            "timestamp": TS_TUE_1300,
            "nifty_ltp": 25100.0,
            "call_strike": 25000,
            "call_buy": 100.0,
            "call_ltp": 138.0,
            "call_pnl_pts": 38.0,
            "put_strike": 24800,
            "put_buy": 100.0,
            "put_ltp": 105.0,
            "put_pnl_pts": 5.0,
            "combined_pnl_pts": 43.0,
            "combined_pnl_pct": 21.5,
            "combined_pnl_inr": 2795.0,
            "lot_size": 65,
            "alerts_sent_today": 1,
            "max_alerts_per_day": 4,
            "dry_run": False,
        }
        with patch.dict(
            os.environ,
            {"DISCORD_URL": "https://discord.com/api/webhooks/test-webhook-url", "DISCORD_EXIT_5DAY": "", "DISCORD_URL_5DAY": ""},
            clear=False,
        ):
            with patch("common.profit_monitor.DISCORD_EXIT_5DAY", ""):
                success = send_peak_alert_discord(event)
            self.assertTrue(success)
            mock_post.assert_called_once()
            called_url = mock_post.call_args[0][0]
            called_kwargs = mock_post.call_args[1]
            self.assertEqual(called_url, "https://discord.com/api/webhooks/test-webhook-url")
            self.assertEqual(called_kwargs["headers"], {"Content-Type": "application/json"})
            self.assertEqual(called_kwargs["timeout"], 10)
            
            payload = called_kwargs["json"]
            self.assertIn("content", payload)
            self.assertIn("embeds", payload)
            self.assertIn("+21.5%", payload["content"])
            
            embed = payload["embeds"][0]
            self.assertEqual(embed["color"], 0x10B981)
            self.assertIn("20260922-test", embed["description"])
            field_names = [f["name"] for f in embed["fields"]]
            self.assertTrue(any("Combined Peak Profit" in name for name in field_names))
            self.assertTrue(any("CALL 25000" in name for name in field_names))
            self.assertTrue(any("PUT 24800" in name for name in field_names))

    @patch("common.profit_monitor.requests.post")
    def test_send_peak_alert_discord_http_error(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_post.return_value = mock_response

        event = {
            "cycle_id": "20260922-test",
            "week_start_date": "20260922",
            "expiry_date": "20260929",
            "timestamp": TS_TUE_1300,
            "nifty_ltp": 25100.0,
            "call_strike": 25000,
            "call_buy": 100.0,
            "call_ltp": 138.0,
            "call_pnl_pts": 38.0,
            "put_strike": 24800,
            "put_buy": 100.0,
            "put_ltp": 105.0,
            "put_pnl_pts": 5.0,
            "combined_pnl_pts": 43.0,
            "combined_pnl_pct": 21.5,
            "combined_pnl_inr": 2795.0,
            "lot_size": 65,
            "alerts_sent_today": 1,
            "max_alerts_per_day": 4,
            "dry_run": False,
        }
        with patch.dict(
            os.environ,
            {"DISCORD_URL": "https://discord.com/api/webhooks/test-webhook-url", "DISCORD_EXIT_5DAY": "", "DISCORD_URL_5DAY": ""},
            clear=False,
        ):
            with patch("common.profit_monitor.DISCORD_EXIT_5DAY", ""):
                success = send_peak_alert_discord(event)
            self.assertFalse(success)

    @patch("common.profit_monitor.requests.post", side_effect=Exception("Connection refused"))
    def test_discord_failure_is_isolated_and_never_raises(self, mock_post):
        event = {
            "cycle_id": "20260922-test",
            "week_start_date": "20260922",
            "expiry_date": "20260929",
            "timestamp": TS_TUE_1300,
            "nifty_ltp": 25100.0,
            "call_strike": 25000,
            "call_buy": 100.0,
            "call_ltp": 138.0,
            "call_pnl_pts": 38.0,
            "put_strike": 24800,
            "put_buy": 100.0,
            "put_ltp": 105.0,
            "put_pnl_pts": 5.0,
            "combined_pnl_pts": 43.0,
            "combined_pnl_pct": 21.5,
            "combined_pnl_inr": 2795.0,
            "lot_size": 65,
            "alerts_sent_today": 1,
            "max_alerts_per_day": 4,
            "dry_run": False,
        }
        with patch.dict(
            os.environ,
            {"DISCORD_URL": "https://discord.com/api/webhooks/test-webhook-url", "DISCORD_EXIT_5DAY": "", "DISCORD_URL_5DAY": ""},
            clear=False,
        ):
            # Should safely catch exception and return False
            with patch("common.profit_monitor.DISCORD_EXIT_5DAY", ""):
                success = send_peak_alert_discord(event)
            self.assertFalse(success)

    def test_send_peak_alert_discord_missing_url(self):
        event = {
            "cycle_id": "20260922-test",
            "timestamp": TS_TUE_1300,
            "nifty_ltp": 25100.0,
            "combined_pnl_pct": 21.5,
            "combined_pnl_pts": 43.0,
            "combined_pnl_inr": 2795.0,
        }
        with patch.dict(os.environ, {"DISCORD_URL": "", "DISCORD_EXIT_5DAY": "", "DISCORD_URL_5DAY": ""}, clear=False):
            with patch("common.profit_monitor.DISCORD_URL", ""), patch("common.profit_monitor.DISCORD_EXIT_5DAY", ""):

                success = send_peak_alert_discord(event)
                self.assertFalse(success)

    def test_send_peak_alert_discord_milestone_formatting(self):
        posted = []

        def mock_post(url, json=None, headers=None, timeout=None):
            class Resp:
                status_code = 204
            posted.append(json)
            return Resp()

        event = {
            "cycle_id": "20261013-test",
            "week_start_date": "20261013",
            "expiry_date": "20261019",
            "timestamp": TS_TUE_1300,
            "nifty_ltp": 22750.0,
            "combined_pnl_pts": 45.0,
            "combined_pnl_pct": 22.5,
            "combined_pnl_inr": 2925.0,
            "call_strike": 22600,
            "call_buy": 297.5,
            "call_ltp": 400.0,
            "call_pnl_pts": 102.5,
            "put_strike": 22800,
            "put_buy": 135.0,
            "put_ltp": 77.5,
            "put_pnl_pts": -57.5,
            "alerts_sent_today": 1,
            "track_label": "5-Day Tuesday Strategy",
        }

        with patch.dict(os.environ, {"DISCORD_URL": "https://discord.com/mock"}), \
             patch("requests.post", side_effect=mock_post):
            with patch("common.profit_monitor.DISCORD_EXIT_5DAY", ""):
                success = send_peak_alert_discord(event)
            self.assertTrue(success)
            self.assertEqual(len(posted), 1)
            payload = posted[0]

            self.assertIn("[PORTFOLIO MILESTONE +20% — NO EXIT]", payload["content"])
            embed = payload["embeds"][0]
            self.assertIn("[PORTFOLIO MILESTONE — NO EXIT]", embed["title"])
            self.assertIn("+20% Target Achieved", embed["title"])

            status_field = [f for f in embed["fields"] if f["name"] == "🏁 Milestone Status & Next Steps"][0]
            self.assertIn("Single-Alert Policy", status_field["value"])
            self.assertIn("Alpha FSM", status_field["value"])

    @patch("common.profit_monitor.send_peak_alert_discord")
    def test_dry_run_skips_discord_dispatch(self, mock_discord):
        event = {
            "cycle_id": "20260922-test",
            "week_start_date": "20260922",
            "expiry_date": "20260929",
            "timestamp": TS_TUE_1300,
            "nifty_ltp": 25100.0,
            "combined_pnl_pts": 43.0,
            "combined_pnl_pct": 21.5,
            "combined_pnl_inr": 2795.0,
            "alerts_sent_today": 1,
            "max_alerts_per_day": 4,
            "dry_run": True,
        }
        success = on_new_peak(event)
        self.assertTrue(success)
        mock_discord.assert_not_called()

    @patch("common.profit_monitor.update_active_peak_state")
    @patch("common.profit_monitor.send_peak_alert_discord")
    def test_discord_failure_rolls_back_notification_state_for_retry(self, mock_send_discord, mock_save_peak):
        # When sending Discord alert fails, last_notified_* and alerts_sent_today roll back
        mock_send_discord.return_value = False

        snapshot = dict(self.mock_snapshot)
        snapshot["peak_profit"] = {
            "peak_pnl_pts": 39.0,
            "peak_pnl_pct": 19.5,
            "peak_pnl_inr": 2535.0,
            "peak_timestamp": TS_TUE_1200,
            "last_notified_pts": None,
            "last_notified_pct": None,
            "last_notified_timestamp": None,
            "alerts_sent_today": 0,
            "last_alert_date": "2026-09-22",
        }

        # Tick 1: Reaches +21.5% (+43 pts), but Discord fails
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=25100.0,
            call_strike=25000,
            call_ltp=138.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=105.0,
            put_buy=100.0,
            is_first_tick=False,
            current_ts=TS_TUE_1300,
        )
        self.assertFalse(triggered)
        self.assertEqual(state["peak_pnl_pts"], 43.0)  # High-water mark preserved
        self.assertIsNone(state["last_notified_pts"])  # Rolled back!
        self.assertIsNone(state["last_notified_pct"])  # Rolled back!
        self.assertEqual(state["alerts_sent_today"], 0)  # Counter not consumed!
        self.assertIn("delivery failed; state rolled back to retry next tick", msg)

        # Tick 2: 30 minutes later, market is still at +21.5% (+43 pts), Discord succeeds!
        mock_send_discord.return_value = True
        snapshot["peak_profit"] = state
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=25100.0,
            call_strike=25000,
            call_ltp=138.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=105.0,
            put_buy=100.0,
            is_first_tick=False,
            current_ts=TS_TUE_1300 + 1800,
        )
        self.assertTrue(triggered)
        self.assertEqual(state["peak_pnl_pts"], 43.0)
        self.assertEqual(state["last_notified_pts"], 43.0)  # Successfully locked
        self.assertEqual(state["alerts_sent_today"], 1)

    def test_closed_cycle_skips_peak_evaluation(self):
        snapshot = dict(self.mock_snapshot)
        snapshot["status"] = "closed"
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=25100.0,
            call_strike=25000,
            call_ltp=150.0,
            call_buy=100.0,
            put_strike=24800,
            put_ltp=100.0,
            put_buy=100.0,
        )
        self.assertFalse(triggered)
        self.assertIn("Cycle is closed", msg)

    @patch("common.profit_monitor.update_active_peak_state")
    @patch("common.profit_monitor.on_new_peak")
    def test_itm_strangle_strict_total_premium_qualification(self, mock_on_peak, mock_save_peak):
        # ITM Strangle: Call 22900 (Buy 303.60), Put 23100 (Buy 160.25) -> Total Buy = 463.85
        # Total Extrinsic = 463.85 - 200 = 263.85
        snapshot = {
            "strategy_name": "nifty50_weekly_option_collector",
            "cycle_id": "20261021-test",
            "week_start_date": "20261021",
            "expiry_date": "20261027",
            "moneyness": "ITM",
            "total_extrinsic": 263.85,
            "call_strike": 22900,
            "put_strike": 23100,
            "call_buy_price": 303.60,
            "put_buy_price": 160.25,
            "peak_profit": {
                "peak_pnl_pts": 0.0,
                "peak_pnl_pct": 0.0,
                "peak_pnl_inr": 0.0,
                "peak_timestamp": TS_TUE_0930,
                "last_notified_pts": None,
                "last_notified_pct": None,
                "last_notified_timestamp": None,
                "alerts_sent_today": 0,
                "last_alert_date": "2026-10-21",
                "alert_dispatched": False,
            },
        }

        # Case 1: Combined profit = +60.60 pts.
        # Extrinsic P&L = +22.97% (>=20%), BUT Total Premium P&L = +13.06% (<20%)
        # Under Option-A (strict total premium), this must NOT trigger an alert!
        state, triggered, msg = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=22737.20,
            call_strike=22900,
            call_ltp=116.40,
            call_buy=303.60,
            put_strike=23100,
            put_ltp=408.05,
            put_buy=160.25,
            is_first_tick=False,
            current_ts=TS_TUE_1200,
        )
        self.assertFalse(triggered)
        self.assertEqual(state["peak_pnl_pts"], 60.60)
        self.assertEqual(state["peak_pnl_pct"], 13.06)
        self.assertIn("below minimum 20.0% threshold", msg)
        mock_on_peak.assert_not_called()

        # Case 2: Profit increases to +93.0 pts (Total Premium P&L = +20.05% >= 20.0%)
        # Now it MUST trigger!
        snapshot["peak_profit"] = state
        state2, triggered2, msg2 = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=22700.00,
            call_strike=22900,
            call_ltp=110.00,
            call_buy=303.60,
            put_strike=23100,
            put_ltp=446.85,
            put_buy=160.25,
            is_first_tick=False,
            current_ts=TS_TUE_1300,
        )
        self.assertTrue(triggered2)
        self.assertEqual(state2["peak_pnl_pts"], 93.0)
        self.assertAlmostEqual(state2["peak_pnl_pct"], 20.05, places=1)
        self.assertEqual(mock_on_peak.call_count, 1)


if __name__ == "__main__":
    unittest.main()
