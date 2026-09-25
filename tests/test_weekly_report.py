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
    render_html,
    send_email,
    resolve_discord_watchdog_url,
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

    def test_resolve_discord_watchdog_url_from_env(self):
        with patch.dict(os.environ, {"DISCORD_WATCHDOG": "https://discord.com/api/webhooks/test-url"}):
            self.assertEqual(resolve_discord_watchdog_url(), "https://discord.com/api/webhooks/test-url")

    def test_send_discord_watchdog_missing_url(self):
        with patch("scripts.send_weekly_report.resolve_discord_watchdog_url", return_value=""):
            self.assertFalse(send_discord_watchdog("Test message"))

    @patch("scripts.send_weekly_report.urllib.request.urlopen")
    def test_send_discord_watchdog_success(self, mock_urlopen):
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("scripts.send_weekly_report.resolve_discord_watchdog_url", return_value="https://discord.com/api/webhooks/test"):
            res = send_discord_watchdog("Test content", embed={"title": "Weekly Report", "fields": []})
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


if __name__ == "__main__":
    unittest.main()
