import os
import sys
import tempfile
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
        self._temp_dir = tempfile.TemporaryDirectory()
        self._temp_db = os.path.join(self._temp_dir.name, 'test_dedup.db')
        self._env_patch = patch.dict(os.environ, {'PEAK_DEDUP_DB_PATH': self._temp_db})
        self._env_patch.start()
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

    def tearDown(self):
        self._env_patch.stop()
        self._temp_dir.cleanup()

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
            self.assertTrue(any("Investment & Return" in name or "Combined Peak Profit" in name for name in field_names))
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

            self.assertIn("20% PEAK Tuesday Strategy", payload["content"])
            embed = payload["embeds"][0]
            self.assertIn("20% PEAK Tuesday Strategy", embed["title"])
            self.assertIn("+22.50%", embed["title"])

            invest_field = [f for f in embed["fields"] if f["name"] == "💰 Investment & Return"][0]
            self.assertIn("Total Invested", invest_field["value"])
            self.assertIn("Total Return", invest_field["value"])

            status_field = [f for f in embed["fields"] if f["name"] == "📌 Leg Status"][0]
            self.assertIn("Both legs open", status_field["value"])

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

    def test_resolve_discord_peak_url_track_routing(self):
        from common.profit_monitor import resolve_discord_peak_url
        env_vars = {
            "DISCORD_EXIT_3DAY": "https://discord.com/mock-3day",
            "DISCORD_EXIT_5DAY": "https://discord.com/mock-5day",
            "DISCORD_URL": "https://discord.com/mock-general",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            with patch("common.profit_monitor.DISCORD_EXIT_3DAY", ""), \
                 patch("common.profit_monitor.DISCORD_EXIT_5DAY", ""), \
                 patch("common.profit_monitor.DISCORD_URL", ""):
                self.assertEqual(resolve_discord_peak_url(track="thursday"), "https://discord.com/mock-3day")
                self.assertEqual(resolve_discord_peak_url(track="3day"), "https://discord.com/mock-3day")
                self.assertEqual(resolve_discord_peak_url(track="3-day"), "https://discord.com/mock-3day")
                self.assertEqual(resolve_discord_peak_url(track_label="3-Day Thursday Strategy"), "https://discord.com/mock-3day")
                self.assertEqual(resolve_discord_peak_url(track="weekly"), "https://discord.com/mock-5day")
                self.assertEqual(resolve_discord_peak_url(track="5day"), "https://discord.com/mock-5day")
                self.assertEqual(resolve_discord_peak_url(track_label="5-Day Tuesday Strategy"), "https://discord.com/mock-5day")

    def test_resolve_discord_peak_url_candidate_env_precedence(self):
        import tempfile
        from common.profit_monitor import resolve_discord_peak_url
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_env = Path(tmpdir) / ".env"
            fake_env.write_text("DISCORD_EXIT_3DAY=https://discord.com/from-candidate-3day\n", encoding="utf-8")
            with patch.dict(os.environ, {"DISCORD_URL": "https://discord.com/general-fallback"}, clear=True):
                with patch("common.profit_monitor.DISCORD_EXIT_3DAY", ""), \
                     patch("common.profit_monitor.DISCORD_EXIT_5DAY", ""), \
                     patch("common.profit_monitor.DISCORD_URL", ""):
                    with patch("common.profit_monitor.Path.exists", lambda p: str(p) == str(fake_env) or Path(p).name == ".env"):
                        with patch("common.profit_monitor.Path.open", side_effect=lambda *args, **kwargs: open(fake_env, *args, **kwargs)):
                            resolved = resolve_discord_peak_url(track="thursday")
                            self.assertEqual(resolved, "https://discord.com/from-candidate-3day")


    def test_reset_snapshot_suppressed_by_sqlite_db(self):
        """Verify that when a snapshot peak_profit is wiped/reset, existing SQLite DB suppresses repeat alerts."""
        from common.profit_monitor import record_peak_alert_dispatched, evaluate_peak_profit
        cycle_id = "CYCLE-THU-20261029"
        track = "thursday"
        # Pre-record in SQLite
        record_peak_alert_dispatched(cycle_id, track, 1793248353, 47.33, 230.79)

        # Snapshot with wiped/null peak_profit
        reset_snapshot = {
            "cycle_id": cycle_id,
            "call_strike": 22600,
            "put_strike": 22900,
            "peak_profit": None,
        }

        with patch("common.profit_monitor.on_new_peak") as mock_on_peak:
            state, triggered, reason = evaluate_peak_profit(
                snapshot=reset_snapshot,
                nifty_ltp=23261.25,
                call_strike=22600,
                call_ltp=663.50,
                call_buy=263.45,
                put_strike=22900,
                put_ltp=17.55,
                put_buy=224.10,
                track="thursday",
            )
            self.assertFalse(triggered)
            self.assertIn("already dispatched", reason)
            mock_on_peak.assert_not_called()

    def test_atomic_claim_prevents_duplicate_dispatch(self):
        """Verify that atomic SQLite claim ensures only one process can claim dispatch."""
        from common.profit_monitor import claim_peak_alert_dispatch
        c_id = "CYCLE-TUE-TEST-ATOMIC"
        trk = "weekly"
        res1 = claim_peak_alert_dispatch(c_id, trk, 123456, 25.0, 50.0)
        self.assertTrue(res1)
        # Second concurrent claim must return False
        res2 = claim_peak_alert_dispatch(c_id, trk, 123457, 26.0, 52.0)
        self.assertFalse(res2)

    def test_fsm_solo_call_and_flat_leg_status(self):
        """Verify that SOLO_CALL maps to PUT closed, and FLAT requires actual closed leg data."""
        posted = []
        def mock_post(url, json=None, headers=None, timeout=None):
            class Resp:
                status_code = 204
            posted.append(json)
            return Resp()

        base_event = {
            "cycle_id": "20261029-test",
            "timestamp": TS_TUE_1300,
            "nifty_ltp": 23260.0,
            "combined_pnl_pts": 50.0,
            "combined_pnl_pct": 25.0,
            "combined_pnl_inr": 3250.0,
            "call_strike": 22600,
            "call_buy": 250.0,
            "call_ltp": 450.0,
            "call_pnl_pts": 200.0,
            "put_strike": 22900,
            "put_buy": 250.0,
            "put_ltp": 100.0,
            "put_pnl_pts": -150.0,
            "track_label": "3-Day Thursday Strategy",
        }

        with patch.dict(os.environ, {"DISCORD_URL": "https://discord.com/mock"}), \
             patch("requests.post", side_effect=mock_post):
            # Case 1: SOLO_CALL -> Closed: PUT | Pending to close: CALL
            event1 = dict(base_event, fsm_state="SOLO_CALL")
            send_peak_alert_discord(event1)
            status_val = [f for f in posted[-1]["embeds"][0]["fields"] if f["name"] == "📌 Leg Status"][0]["value"]
            self.assertIn("Closed: PUT", status_val)
            self.assertIn("Pending to close: CALL", status_val)

            # Case 2: SOLO_PUT -> Closed: CALL | Pending to close: PUT
            event2 = dict(base_event, fsm_state="SOLO_PUT")
            send_peak_alert_discord(event2)
            status_val = [f for f in posted[-1]["embeds"][0]["fields"] if f["name"] == "📌 Leg Status"][0]["value"]
            self.assertIn("Closed: CALL", status_val)
            self.assertIn("Pending to close: PUT", status_val)

            # Case 3: FLAT with empty legs -> NOT cycle complete
            event3 = dict(base_event, fsm_state="FLAT", call_is_closed=False, put_is_closed=False)
            send_peak_alert_discord(event3)
            status_val = [f for f in posted[-1]["embeds"][0]["fields"] if f["name"] == "📌 Leg Status"][0]["value"]
            self.assertIn("Both legs open", status_val)

            # Case 4: FLAT with closed legs -> Cycle Complete
            event4 = dict(base_event, fsm_state="FLAT", call_is_closed=True, put_is_closed=True)
            send_peak_alert_discord(event4)
            status_val = [f for f in posted[-1]["embeds"][0]["fields"] if f["name"] == "📌 Leg Status"][0]["value"]
            self.assertIn("Both Legs Closed — Cycle Complete", status_val)

            # Case 5: FLAT with only ONE closed leg -> Must NOT show Cycle Complete
            event5 = dict(base_event, fsm_state="FLAT", call_is_closed=True, put_is_closed=False)
            send_peak_alert_discord(event5)
            status_val = [f for f in posted[-1]["embeds"][0]["fields"] if f["name"] == "📌 Leg Status"][0]["value"]
            self.assertNotIn("Cycle Complete", status_val)
            self.assertIn("Closed: CALL", status_val)
            self.assertIn("Pending to close: PUT", status_val)

    def test_seeding_sqlite_from_json_snapshot(self):
        """Verify that when JSON snapshot has alert_dispatched=True, it seeds SQLite so subsequent wipes cannot duplicate."""
        from common.profit_monitor import evaluate_peak_profit, has_peak_alert_dispatched
        cycle_id = "CYCLE-SEED-TEST-2026"
        track = "thursday"

        self.assertFalse(has_peak_alert_dispatched(cycle_id, track))

        json_notified_snapshot = {
            "cycle_id": cycle_id,
            "call_strike": 22600,
            "put_strike": 22900,
            "peak_profit": {
                "cycle_id": cycle_id,
                "alert_dispatched": True,
                "last_notified_pts": 55.0,
                "last_notified_pct": 27.5,
                "last_notified_timestamp": 1793248353,
                "alerts_sent_today": 1,
            }
        }

        # 1. First run: JSON has alert_dispatched=True, SQLite is empty. Must seed SQLite!
        with patch("common.profit_monitor.on_new_peak") as mock_on_peak:
            state, triggered, reason = evaluate_peak_profit(
                snapshot=json_notified_snapshot,
                nifty_ltp=23261.25,
                call_strike=22600,
                call_ltp=663.50,
                call_buy=263.45,
                put_strike=22900,
                put_ltp=17.55,
                put_buy=224.10,
                track="thursday",
                dry_run=False,
            )
            self.assertFalse(triggered)
            self.assertTrue(has_peak_alert_dispatched(cycle_id, track), "SQLite must be seeded from JSON snapshot!")
            mock_on_peak.assert_not_called()

        # 2. Verify seeding happens even when current profit drops below previous stored peak (e.g. 248 pts < 257.35 pts peak)
        cycle_id_lower = "CYCLE-SEED-LOWER-THAN-PEAK"
        snapshot_lower = {
            "cycle_id": cycle_id_lower,
            "call_strike": 22600,
            "put_strike": 22900,
            "peak_profit": {
                "cycle_id": cycle_id_lower,
                "peak_pnl_pts": 257.35,
                "alert_dispatched": True,
                "last_notified_pts": 257.35,
                "last_notified_pct": 52.8,
                "last_notified_timestamp": 1793248353,
                "alerts_sent_today": 1,
            }
        }
        with patch("common.profit_monitor.on_new_peak") as mock_on_peak:
            state, triggered, reason = evaluate_peak_profit(
                snapshot=snapshot_lower,
                nifty_ltp=23261.25,
                call_strike=22600,
                call_ltp=450.00,  # Profit is lower: 450 + 17.55 - (263.45 + 224.10) = -20 pts < 257.35 peak
                call_buy=263.45,
                put_strike=22900,
                put_ltp=17.55,
                put_buy=224.10,
                track="thursday",
                dry_run=False,
            )
            self.assertFalse(triggered)
            self.assertTrue(has_peak_alert_dispatched(cycle_id_lower, track), "SQLite must be seeded even when current profit < stored peak!")
            mock_on_peak.assert_not_called()

        # 2. Now wipe JSON peak_profit completely (as happened on server during morning reset)
        wiped_snapshot = {
            "cycle_id": cycle_id,
            "call_strike": 22600,
            "put_strike": 22900,
            "peak_profit": None,
        }
        with patch("common.profit_monitor.on_new_peak") as mock_on_peak:
            state2, triggered2, reason2 = evaluate_peak_profit(
                snapshot=wiped_snapshot,
                nifty_ltp=23261.25,
                call_strike=22600,
                call_ltp=663.50,
                call_buy=263.45,
                put_strike=22900,
                put_ltp=17.55,
                put_buy=224.10,
                track="thursday",
                dry_run=False,
            )
            self.assertFalse(triggered2)
            self.assertIn("already dispatched", reason2)
            mock_on_peak.assert_not_called()

    def test_alpha_fsm_status_key_recognized(self):
        """Verify that alpha_fsm status key ('status') is recognized for closed legs."""
        from common.profit_monitor import evaluate_peak_profit
        snapshot = {
            "cycle_id": "CYCLE-STATUS-KEY-TEST",
            "call_strike": 22600,
            "put_strike": 22900,
            "alpha_fsm": {
                "call_leg": {
                    "status": "HARVESTED_CLOSED",  # Uses status key instead of state
                    "exit_price": 500.0,
                },
                "put_leg": {
                    "status": "ACTIVE",
                    "exit_price": None,
                }
            }
        }
        state, triggered, reason = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=23200.0,
            call_strike=22600,
            call_ltp=999.0,  # Live LTP drifted
            call_buy=250.0,
            put_strike=22900,
            put_ltp=50.0,
            put_buy=250.0,
            dry_run=True,
        )
        self.assertAlmostEqual(state["peak_pnl_pts"], 50.0, places=1)

    def test_closed_leg_exit_price_preserved(self):
        """Verify that evaluate_peak_profit preserves closed leg exit_price rather than live fluctuating LTP."""
        from common.profit_monitor import evaluate_peak_profit
        snapshot = {
            "cycle_id": "CYCLE-CLOSED-LEG-TEST",
            "call_strike": 22600,
            "put_strike": 22900,
            "alpha_fsm": {
                "call_leg": {
                    "state": "HARVESTED_CLOSED",
                    "exit_price": 500.0,
                },
                "put_leg": {
                    "state": "ACTIVE",
                    "exit_price": None,
                }
            }
        }
        with patch("common.profit_monitor.on_new_peak"):
            state, triggered, reason = evaluate_peak_profit(
                snapshot=snapshot,
                nifty_ltp=23200.0,
                call_strike=22600,
                call_ltp=999.0,  # Live LTP drifted to 999.0
                call_buy=250.0,
                put_strike=22900,
                put_ltp=50.0,
                put_buy=250.0,
                dry_run=True,
            )
            # Call profit should be 500 - 250 = 250 pts (NOT 999 - 250 = 749 pts)
            # Put profit is 50 - 250 = -200 pts
            # Combined pts = 250 - 200 = 50 pts
            # Combined pct = 50 / (250 + 250) = 10.0%
            self.assertAlmostEqual(state["peak_pnl_pts"], 50.0, places=1)

    def test_closed_cycle_seeds_dispatched_alert_to_sqlite(self):
        """Verify that evaluate_peak_profit seeds SQLite even if snapshot status is 'closed'."""
        from common.profit_monitor import evaluate_peak_profit, has_peak_alert_dispatched
        cycle_id = "CYCLE-CLOSED-SEED-TEST"
        track = "weekly"
        snapshot = {
            "status": "closed",
            "cycle_id": cycle_id,
            "call_strike": 22600,
            "call_buy_price": 250.0,
            "put_strike": 22900,
            "put_buy_price": 250.0,
            "peak_profit": {
                "alert_dispatched": True,
                "last_notified_pts": 120.0,
                "last_notified_pct": 24.0,
                "last_notified_timestamp": 1793248000,
            }
        }
        state, triggered, reason = evaluate_peak_profit(
            snapshot=snapshot,
            nifty_ltp=23000.0,
            call_strike=22600,
            call_ltp=0.0,
            call_buy=250.0,
            put_strike=22900,
            put_ltp=0.0,
            put_buy=250.0,
            track=track,
            dry_run=False,
        )
        self.assertFalse(triggered)
        self.assertIn("closed", reason)
        self.assertTrue(has_peak_alert_dispatched(cycle_id, track), "Closed cycle must seed dispatched alert to SQLite!")



if __name__ == "__main__":
    unittest.main()
