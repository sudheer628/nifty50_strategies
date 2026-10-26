import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.storage import init_db, insert_buy_snapshot, insert_record
from scripts.send_weekly_report import (
    calculate_summary,
    create_chart,
    find_weekly_db,
    load_report_data,
    main,
    render_html,
    send_email,
    resolve_discord_url,
    resolve_discord_watchdog_url,
    send_discord_report,
    send_discord_watchdog,
    send_weekly_report_discord,
)


class WeeklyReportTests(unittest.TestCase):
    def _create_weekly_db(self, directory: str) -> Path:
        db_path = Path(directory) / "nifty50_weekly_data_20260804_20260811.db"
        init_db(str(db_path))
        insert_buy_snapshot(
            str(db_path),
            {
                "strategy_name": "nifty50_weekly_option_collector",
                "cycle_id": "20260804-test",
                "week_start_date": "20260804",
                "expiry_date": "20260811",
                "call_strike": 24700,
                "put_strike": 24500,
                "call_buy_price": 92.0,
                "put_buy_price": 82.0,
                "captured_at": 1785854400,
            },
        )
        for timestamp, nifty, call_ltp, put_ltp, gainloss in (
            (1785854400, 24500.0, 92.0, 82.0, 0.0),
            (1786121400, 24700.0, 70.0, 60.0, -44.0),
            (1786485600, 24600.0, 100.0, 90.0, 16.0),
        ):
            insert_record(
                str(db_path),
                {
                    "strategy_name": "nifty50_weekly_option_collector",
                    "collection_timestamp": timestamp,
                    "expiry_date": "20260811",
                    "nifty_open": 24480.0,
                    "nifty_ltp": nifty,
                    "nifty_previous_close": 24400.0,
                    "put_strike": 24500,
                    "put_ltp": put_ltp,
                    "call_strike": 24700,
                    "call_ltp": call_ltp,
                    "call_buy_price": 92.0,
                    "put_buy_price": 82.0,
                    "gainloss": gainloss,
                    "cycle_id": "20260804-test",
                },
            )
        return db_path

    def test_report_discovers_db_and_renders_chart_and_html(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._create_weekly_db(temp_dir)
            with patch("scripts.send_weekly_report.SQLITE_DIR", temp_dir):
                self.assertEqual(find_weekly_db(date(2026, 8, 10)), db_path)

            rows, snapshot = load_report_data(db_path)
            summary = calculate_summary(rows, snapshot, db_path)
            chart_path = Path(temp_dir) / "report.png"
            create_chart(rows, chart_path)
            report_html = render_html(rows, summary, "report.png")

            self.assertTrue(chart_path.is_file())
            self.assertGreater(chart_path.stat().st_size, 1000)
            self.assertEqual(summary["latest_gainloss"], 16.0)
            self.assertEqual(summary["best_gainloss"], 16.0)
            self.assertEqual(summary["worst_gainloss"], -44.0)
            self.assertIn("Hourly gain/loss table", report_html)
            self.assertIn("CALL 24700", report_html)
            self.assertIn("+16.00", report_html)

    def test_email_retired_noop(self):
        """send_email should safely log and return without error since email is retired."""
        with tempfile.TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / "chart.png"
            chart_path.write_bytes(b"test-png-content")
            # Should safely execute without error even if email credentials are absent
            send_email(
                "Weekly report",
                "Plain body",
                '<html><img src="cid:nifty-chart"></html>',
                chart_path,
            )

    def test_resolve_discord_url_from_env(self):
        with patch.dict(os.environ, {"DISCORD_URL": "https://discord.com/api/webhooks/test-url"}):
            self.assertEqual(resolve_discord_url(), "https://discord.com/api/webhooks/test-url")
            self.assertEqual(resolve_discord_watchdog_url(), "https://discord.com/api/webhooks/test-url")

    def test_send_discord_report_missing_url(self):
        with patch("scripts.send_weekly_report.resolve_discord_url", return_value=""):
            self.assertFalse(send_discord_report("Test message"))
            self.assertFalse(send_discord_watchdog("Test message"))

    @patch("scripts.send_weekly_report.urllib.request.urlopen")
    def test_send_discord_report_success(self, mock_urlopen):
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("scripts.send_weekly_report.resolve_discord_url", return_value="https://discord.com/api/webhooks/test"):
            res = send_discord_report("Test content", embed={"title": "Weekly Report", "fields": []})
            self.assertTrue(res)
            self.assertTrue(mock_urlopen.called)

    @patch("scripts.send_weekly_report.send_discord_watchdog")
    def test_send_weekly_report_discord(self, mock_send):
        mock_send.return_value = True
        summary = {
            "cycle_id": "20260804-test",
            "latest_gainloss": 16.0,
            "best_gainloss": 25.0,
            "worst_gainloss": -10.0,
            "call_strike": 24700,
            "put_strike": 24500,
            "call_buy_price": 92.0,
            "put_buy_price": 82.0,
            "nifty_start": 24500.0,
            "nifty_latest": 24600.0,
            "nifty_change": 100.0,
            "start_date": date(2026, 8, 4),
            "expiry_date": date(2026, 8, 11),
            "selection_mode": "AI_SELECT",
            "selection_rationale": "High IV skew favoring OTM strangle",
            "row_count": 45,
        }
        res = send_weekly_report_discord(summary)
        self.assertTrue(res)
        self.assertTrue(mock_send.called)
        headline, embed = mock_send.call_args[0]
        self.assertIn("20260804-test", headline)
        self.assertIn("+16.00 pts", headline)
        self.assertEqual(embed["color"], 0x16a34a)
        self.assertGreaterEqual(len(embed["fields"]), 5)

    def _create_thursday_db(self, directory: str) -> Path:
        db_path = Path(directory) / "nifty50_thursday_data_20261008_20261013.db"
        init_db(str(db_path))
        insert_buy_snapshot(
            str(db_path),
            {
                "strategy_name": "nifty50_thursday_3day_collector",
                "cycle_id": "CYCLE-THU-20261008",
                "week_start_date": "20261008",
                "expiry_date": "20261013",
                "call_strike": 25200,
                "put_strike": 25000,
                "call_buy_price": 85.0,
                "put_buy_price": 75.0,
                "captured_at": 1791450000,
            },
        )
        for timestamp, nifty, call_ltp, put_ltp, gainloss in (
            (1791450000, 25100.0, 85.0, 75.0, 0.0),
            (1791460000, 25150.0, 95.0, 60.0, -5.0),
            (1791470000, 25250.0, 120.0, 45.0, 5.0),
        ):
            insert_record(
                str(db_path),
                {
                    "strategy_name": "nifty50_thursday_3day_collector",
                    "collection_timestamp": timestamp,
                    "expiry_date": "20261013",
                    "nifty_open": 25080.0,
                    "nifty_ltp": nifty,
                    "nifty_previous_close": 25050.0,
                    "put_strike": 25000,
                    "put_ltp": put_ltp,
                    "call_strike": 25200,
                    "call_ltp": call_ltp,
                    "call_buy_price": 85.0,
                    "put_buy_price": 75.0,
                    "gainloss": gainloss,
                    "cycle_id": "CYCLE-THU-20261008",
                },
            )
        return db_path

    def test_thursday_track_loads_data_and_calculates_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._create_thursday_db(temp_dir)
            with patch("scripts.send_weekly_report.SQLITE_DIR", temp_dir):
                discovered = find_weekly_db(date(2026, 10, 12), track="thursday")
                self.assertEqual(discovered, db_path)

            rows, snapshot = load_report_data(db_path, track="thursday")
            self.assertEqual(len(rows), 3)
            self.assertEqual(snapshot.get("cycle_id"), "CYCLE-THU-20261008")

            summary = calculate_summary(rows, snapshot, db_path)
            self.assertEqual(summary["start_date"], date(2026, 10, 8))
            self.assertEqual(summary["expiry_date"], date(2026, 10, 13))
            self.assertEqual(summary["latest_gainloss"], 5.0)
            self.assertEqual(summary["best_gainloss"], 5.0)
            self.assertEqual(summary["worst_gainloss"], -5.0)

    def test_load_report_data_raises_value_error_on_empty_db(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_db = Path(temp_dir) / "nifty50_thursday_data_20261008_20261013.db"
            init_db(str(empty_db))
            with self.assertRaises(ValueError) as cm:
                load_report_data(empty_db, track="thursday")
            self.assertIn("No strategy rows found", str(cm.exception))

    def test_main_skips_cleanly_on_empty_db(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_db = Path(temp_dir) / "nifty50_thursday_data_20261008_20261013.db"
            init_db(str(empty_db))
            with patch("scripts.send_weekly_report.SQLITE_DIR", temp_dir), \
                 patch("sys.argv", ["send_weekly_report.py", "--track", "thursday", "--report-date", "2026-10-12"]):
                exit_code = main()
                self.assertEqual(exit_code, 0)



    def test_alpha_fsm_realized_pnl_takes_precedence_over_passive_hold_decay(self):
        """When Alpha FSM is FLAT, summary and Discord report must report FSM locked profit, demoting passive decay."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "nifty50_weekly_data_20261021_20261027.db"
            init_db(str(db_path))
            insert_buy_snapshot(
                str(db_path),
                {
                    "strategy_name": "nifty50_weekly_option_collector",
                    "cycle_id": "20261021-212080e3",
                    "week_start_date": "20261021",
                    "expiry_date": "20261027",
                    "call_strike": 22900,
                    "put_strike": 23100,
                    "call_buy_price": 303.6,
                    "put_buy_price": 160.25,
                    "captured_at": 1792500000,
                },
            )
            # Row 1: Entry baseline
            insert_record(
                str(db_path),
                {
                    "strategy_name": "nifty50_weekly_option_collector",
                    "collection_timestamp": 1792500000,
                    "expiry_date": "20261027",
                    "nifty_open": 23082.9,
                    "nifty_ltp": 23082.9,
                    "nifty_previous_close": 23050.0,
                    "put_strike": 23100,
                    "put_ltp": 160.25,
                    "call_strike": 22900,
                    "call_ltp": 303.6,
                    "call_buy_price": 303.6,
                    "put_buy_price": 160.25,
                    "gainloss": 0.0,
                    "cycle_id": "20261021-212080e3",
                    "fsm_state": "DUAL_LONG",
                    "fsm_call_status": "ACTIVE",
                    "fsm_put_status": "ACTIVE",
                    "fsm_total_gainloss": 0.0,
                    "fsm_roi_pct": 0.0,
                },
            )
            # Row 2: Final Monday close: raw unhedged decay = -89.45, but FSM locked at +302.0 pts
            insert_record(
                str(db_path),
                {
                    "strategy_name": "nifty50_weekly_option_collector",
                    "collection_timestamp": 1792900000,
                    "expiry_date": "20261027",
                    "nifty_open": 23082.9,
                    "nifty_ltp": 22753.2,
                    "nifty_previous_close": 23050.0,
                    "put_strike": 23100,
                    "put_ltp": 334.0,
                    "call_strike": 22900,
                    "call_ltp": 40.4,
                    "call_buy_price": 303.6,
                    "put_buy_price": 160.25,
                    "gainloss": -89.45,
                    "cycle_id": "20261021-212080e3",
                    "fsm_state": "FLAT",
                    "fsm_call_status": "SALVAGED_CLOSED",
                    "fsm_put_status": "HARVESTED_CLOSED",
                    "fsm_call_exit_price": 151.8,
                    "fsm_put_exit_price": 614.05,
                    "fsm_realized_pnl": 302.0,
                    "fsm_total_gainloss": 302.0,
                    "fsm_roi_pct": 165.0,
                },
            )

            rows, snapshot = load_report_data(db_path)
            summary = calculate_summary(rows, snapshot, db_path)

            # Single Source of Truth assertions
            self.assertEqual(summary["realized_gainloss"], 302.0)
            self.assertEqual(summary["latest_gainloss"], 302.0)
            self.assertEqual(summary["realized_inr"], 302.0 * 65)  # ₹19,630.00
            self.assertEqual(summary["exit_mode"], "ALPHA_FSM")
            self.assertEqual(summary["passive_gainloss"], -89.45)
            self.assertAlmostEqual(summary["alpha_saved_pts"], 391.45, places=2)
            self.assertEqual(summary["fsm_state"], "FLAT")

            # HTML Rendering assertions
            chart_path = Path(temp_dir) / "report.png"
            create_chart(rows, chart_path)
            report_html = render_html(rows, summary, "report.png")
            self.assertIn("+302.00 pts", report_html)
            self.assertIn("STRATEGY LOCKED P&L", report_html)
            self.assertIn("Alpha FSM (FLAT - Profit Locked ✓)", report_html)
            self.assertIn("Counterfactual: Passive Unhedged Hold", report_html)
            self.assertIn("-89.45 pts", report_html)

            # Discord Alert payload assertions
            with patch("scripts.send_weekly_report.send_discord_watchdog") as mock_watchdog:
                mock_watchdog.return_value = True
                success = send_weekly_report_discord(summary)
                self.assertTrue(success)
                self.assertTrue(mock_watchdog.called)

                headline, embed = mock_watchdog.call_args[0]
                self.assertIn("Locked P&L: **+302.00 pts", headline)
                self.assertIn("(₹+19,630)", headline)
                self.assertIn("🟢", headline)
                self.assertEqual(embed["color"], 0x16a34a)
                self.assertIn("PROFITABLE", embed["description"])

                field_names = [f["name"] for f in embed["fields"]]
                self.assertIn("📊 Strategy P&L (Alpha FSM Locked ✓)", field_names)
                self.assertIn("⚡ FSM Leg Execution Breakdown", field_names)
                self.assertIn("📉 Counterfactual: Passive Hold Decay", field_names)

                pnl_field = next(f for f in embed["fields"] if f["name"] == "📊 Strategy P&L (Alpha FSM Locked ✓)")
                self.assertIn("+302.00 pts", pnl_field["value"])
                self.assertIn("₹+19,630", pnl_field["value"])

                leg_field = next(f for f in embed["fields"] if f["name"] == "⚡ FSM Leg Execution Breakdown")
                self.assertIn("₹151.80 (Salvage Stop Hit", leg_field["value"])
                self.assertIn("₹614.05 (House Money Harvested ✓)", leg_field["value"])

                counter_field = next(f for f in embed["fields"] if f["name"] == "📉 Counterfactual: Passive Hold Decay")
                self.assertIn("-89.45 pts", counter_field["value"])
                self.assertIn("+391.45 pts", counter_field["value"])

    def test_partial_fsm_separates_realized_from_unrealized(self):
        """When FSM is partial (SOLO_PUT), report breaks down closed leg realized vs open leg outcome."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "nifty50_weekly_data_20261021_20261027.db"
            init_db(str(db_path))
            insert_buy_snapshot(
                str(db_path),
                {
                    "strategy_name": "nifty50_weekly_option_collector",
                    "cycle_id": "CYCLE-PARTIAL-FSM",
                    "week_start_date": "20261021",
                    "expiry_date": "20261027",
                    "call_strike": 22900,
                    "put_strike": 23100,
                    "call_buy_price": 200.0,
                    "put_buy_price": 200.0,
                    "captured_at": 1792500000,
                },
            )
            # CE was salvaged at -100 pts (realized = -100). PE remains active and finishes at +150 pts.
            insert_record(
                str(db_path),
                {
                    "strategy_name": "nifty50_weekly_option_collector",
                    "collection_timestamp": 1792900000,
                    "expiry_date": "20261027",
                    "nifty_open": 23000.0,
                    "nifty_ltp": 22800.0,
                    "nifty_previous_close": 23000.0,
                    "put_strike": 23100,
                    "put_ltp": 350.0,
                    "call_strike": 22900,
                    "call_ltp": 100.0,
                    "call_buy_price": 200.0,
                    "put_buy_price": 200.0,
                    "gainloss": 50.0,
                    "cycle_id": "CYCLE-PARTIAL-FSM",
                    "fsm_state": "SOLO_PUT",
                    "fsm_call_status": "SALVAGED_CLOSED",
                    "fsm_put_status": "ACTIVE",
                    "fsm_call_exit_price": 100.0,
                    "fsm_realized_pnl": -100.0,
                    "fsm_unrealized_pnl": 150.0,
                    "fsm_total_gainloss": 50.0,
                    "fsm_roi_pct": 12.5,
                },
            )

            rows, snapshot = load_report_data(db_path)
            summary = calculate_summary(rows, snapshot, db_path)

            self.assertEqual(summary["exit_mode"], "ALPHA_FSM_PARTIAL")
            self.assertEqual(summary["is_fsm_partial"], True)
            self.assertEqual(summary["is_fsm_flat"], False)
            self.assertEqual(summary["alpha_secured"], False)
            self.assertEqual(summary["fsm_realized_pnl"], -100.0)
            self.assertEqual(summary["fsm_unrealized_pnl"], 150.0)
            self.assertEqual(summary["realized_gainloss"], 50.0)

            # HTML card assertion
            chart_path = Path(temp_dir) / "report.png"
            create_chart(rows, chart_path)
            report_html = render_html(rows, summary, "report.png")
            self.assertIn("STRATEGY NET P&L (PARTIAL EXIT)", report_html)
            self.assertIn("Net FSM Advantage vs Passive Hold", report_html)

            with patch("scripts.send_weekly_report.send_discord_watchdog") as mock_watchdog:
                mock_watchdog.return_value = True
                send_weekly_report_discord(summary)
                headline, embed = mock_watchdog.call_args[0]
                self.assertIn("Net Cycle P&L: **+50.00 pts", headline)
                pnl_field = next(f for f in embed["fields"] if f["name"] == "📊 Strategy P&L (Partial FSM Exit)")
                self.assertIn("Closed Leg Realized: **-100.00 pts**", pnl_field["value"])
                self.assertIn("Remaining Leg Mark (Cycle Close): **+150.00 pts**", pnl_field["value"])
                counter_field = next(f for f in embed["fields"] if f["name"] == "📉 Counterfactual: Passive Hold Decay")
                self.assertIn("Net FSM Advantage vs Passive Hold", counter_field["value"])

    def test_peak_target_alert_noted_without_false_realized_claim(self):
        """When 20% Peak Target was hit but no FSM exits triggered, report retains expiry close as actual outcome."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "nifty50_weekly_data_20261021_20261027.db"
            init_db(str(db_path))
            insert_buy_snapshot(
                str(db_path),
                {
                    "strategy_name": "nifty50_weekly_option_collector",
                    "cycle_id": "CYCLE-PEAK-TEST",
                    "week_start_date": "20261021",
                    "expiry_date": "20261027",
                    "call_strike": 24800,
                    "put_strike": 24600,
                    "call_buy_price": 100.0,
                    "put_buy_price": 100.0,
                    "captured_at": 1792500000,
                },
            )
            # Mid-week peak reaches +50 pts (+25%), expiry decays to -120 pts
            insert_record(
                str(db_path),
                {
                    "strategy_name": "nifty50_weekly_option_collector",
                    "collection_timestamp": 1792600000,
                    "expiry_date": "20261027",
                    "nifty_open": 24700.0,
                    "nifty_ltp": 24750.0,
                    "nifty_previous_close": 24700.0,
                    "put_strike": 24600,
                    "put_ltp": 90.0,
                    "call_strike": 24800,
                    "call_ltp": 160.0,
                    "call_buy_price": 100.0,
                    "put_buy_price": 100.0,
                    "gainloss": 50.0,
                    "cycle_id": "CYCLE-PEAK-TEST",
                },
            )
            insert_record(
                str(db_path),
                {
                    "strategy_name": "nifty50_weekly_option_collector",
                    "collection_timestamp": 1792900000,
                    "expiry_date": "20261027",
                    "nifty_open": 24700.0,
                    "nifty_ltp": 24700.0,
                    "nifty_previous_close": 24700.0,
                    "put_strike": 24600,
                    "put_ltp": 40.0,
                    "call_strike": 24800,
                    "call_ltp": 40.0,
                    "call_buy_price": 100.0,
                    "put_buy_price": 100.0,
                    "gainloss": -120.0,
                    "cycle_id": "CYCLE-PEAK-TEST",
                },
            )

            rows, snapshot = load_report_data(db_path)
            # Attach peak profit metadata
            snapshot["peak_profit"] = {
                "peak_pnl_pts": 50.0,
                "peak_pnl_pct": 25.0,
                "peak_pnl_inr": 3250.0,
            }
            summary = calculate_summary(rows, snapshot, db_path)

            # Does not falsely claim +50 pts was realized
            self.assertEqual(summary["realized_gainloss"], -120.0)
            self.assertEqual(summary["exit_mode"], "HOLD_TO_EXPIRY")
            self.assertEqual(summary["is_peak_target_hit"], True)
            self.assertEqual(summary["peak_pnl_pts"], 50.0)

            # Render HTML and assert 20% Target Alert annotation is present
            chart_path = Path(temp_dir) / "report.png"
            create_chart(rows, chart_path)
            report_html = render_html(rows, summary, "report.png")
            self.assertIn("🎯 20% Target Alert: +50.00 pts (+25.0%)", report_html)

            # Render Discord embed and assert 20% Target Alert appears in Watermark Range
            with patch("scripts.send_weekly_report.send_discord_watchdog") as mock_watchdog:
                mock_watchdog.return_value = True
                send_weekly_report_discord(summary)
                headline, embed = mock_watchdog.call_args[0]
                watermark_field = next(f for f in embed["fields"] if f["name"] == "🌊 Watermark Range")
                self.assertIn("🎯 20% Target Alert: `+50.00 pts` (+25.0%)", watermark_field["value"])


if __name__ == "__main__":
    unittest.main()
