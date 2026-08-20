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

    @patch("scripts.send_weekly_report.smtplib.SMTP_SSL")
    def test_email_uses_legacy_gmail_environment_variables(self, smtp_ssl):
        with tempfile.TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / "chart.png"
            chart_path.write_bytes(b"test-png-content")
            smtp = smtp_ssl.return_value.__enter__.return_value
            with patch.dict(
                os.environ,
                {
                    "EMAIL_SENDER": "sender@gmail.com",
                    "EMAIL_APP_PASSWORD": "app-password",
                    "EMAIL_RECIPIENT": "recipient@example.com",
                    "EMAIL_SMTP_SERVER": "smtp.gmail.com",
                    "EMAIL_SMTP_PORT": "465",
                },
                clear=False,
            ):
                send_email(
                    "Weekly report",
                    "Plain body",
                    '<html><img src="cid:nifty-chart"></html>',
                    chart_path,
                )

            smtp_ssl.assert_called_once_with("smtp.gmail.com", 465, timeout=30)
            smtp.login.assert_called_once_with("sender@gmail.com", "app-password")
            self.assertEqual(smtp.sendmail.call_count, 1)


if __name__ == "__main__":
    unittest.main()
