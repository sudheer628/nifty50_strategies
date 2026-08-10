import unittest
from datetime import date
from unittest.mock import patch

from strategies.weekly_option_collector import (
    _active_cycle,
    _cycle_start_for_day,
    collect_once,
)
from common.expiry import strike_selector


MANUAL_SNAPSHOT = {
    "strategy_name": "nifty50_weekly_option_collector",
    "cycle_id": "20260804-manual",
    "week_start_date": "20260804",
    "expiry_date": "20260811",
    "call_strike": 24700,
    "put_strike": 24500,
    "call_buy_price": 92.0,
    "put_buy_price": 82.0,
    "captured_at": "2026-08-04T04:00:00+00:00",
}


class CycleSnapshotTests(unittest.TestCase):
    def test_strikes_are_symmetric_around_first_trigger_ltp_anchor(self):
        self.assertEqual(strike_selector(25000.0), (24900, 25100))
        self.assertEqual(strike_selector(25087.5), (24900, 25100))

    def test_monday_reuses_complete_manual_cycle(self):
        cycle = _active_cycle(MANUAL_SNAPSHOT, date(2026, 8, 10))
        self.assertEqual(cycle["expiry"], date(2026, 8, 11))
        self.assertEqual(cycle["call_strike"], 24700)
        self.assertEqual(cycle["put_strike"], 24500)

    def test_expiry_tuesday_rejects_previous_cycle(self):
        self.assertEqual(
            _active_cycle(MANUAL_SNAPSHOT, date(2026, 8, 11)),
            {},
        )

    def test_midweek_cycle_start_is_previous_tuesday(self):
        self.assertEqual(
            _cycle_start_for_day(date(2026, 8, 10)),
            date(2026, 8, 4),
        )

    @patch("strategies.weekly_option_collector.insert_record")
    @patch("strategies.weekly_option_collector.insert_buy_snapshot")
    @patch("strategies.weekly_option_collector._ensure_db", return_value="cycle.db")
    @patch("strategies.weekly_option_collector.get_nifty_option_chain")
    @patch("strategies.weekly_option_collector.get_nifty_spot")
    @patch("strategies.weekly_option_collector.load_active_snapshot")
    @patch(
        "strategies.weekly_option_collector._today_ist",
        return_value=date(2026, 8, 10),
    )
    def test_collection_uses_snapshot_expiry_strikes_and_buy_prices(
        self,
        _today,
        load_snapshot,
        get_spot,
        get_options,
        _ensure_db,
        insert_snapshot,
        insert_record,
    ):
        load_snapshot.return_value = MANUAL_SNAPSHOT
        get_spot.return_value = {
            "ltp": 24610.0,
            "open": 24600.0,
            "close": 24500.0,
        }
        get_options.return_value = {
            "call": {"ltp": 105.0},
            "put": {"ltp": 75.0},
        }

        self.assertTrue(collect_once())

        get_options.assert_called_once_with("11AUG2026", 24700, 24500)
        record = insert_record.call_args.args[1]
        self.assertEqual(record["expiry_date"], "20260811")
        self.assertEqual(record["call_strike"], 24700)
        self.assertEqual(record["put_strike"], 24500)
        self.assertEqual(record["call_buy_price"], 92.0)
        self.assertEqual(record["put_buy_price"], 82.0)
        self.assertEqual(record["gainloss"], 6.0)
        insert_snapshot.assert_called_once()

    @patch("strategies.weekly_option_collector.insert_record")
    @patch("strategies.weekly_option_collector.insert_buy_snapshot")
    @patch("strategies.weekly_option_collector.save_active_snapshot")
    @patch("strategies.weekly_option_collector._ensure_db", return_value="new.db")
    @patch("strategies.weekly_option_collector.get_nifty_option_chain")
    @patch("strategies.weekly_option_collector.get_nifty_spot")
    @patch(
        "strategies.weekly_option_collector.load_active_snapshot",
        return_value=MANUAL_SNAPSHOT,
    )
    @patch(
        "strategies.weekly_option_collector._today_ist",
        return_value=date(2026, 8, 11),
    )
    def test_tuesday_uses_first_trigger_ltp_but_stores_day_open(
        self,
        _today,
        _load_snapshot,
        get_spot,
        get_options,
        _ensure_db,
        save_snapshot,
        _insert_snapshot,
        insert_record,
    ):
        get_spot.return_value = {
            "ltp": 25000.0,
            "open": 24875.0,
            "close": 24820.0,
        }
        get_options.return_value = {
            "call": {"ltp": 91.0},
            "put": {"ltp": 83.0},
        }

        self.assertTrue(collect_once())

        get_options.assert_called_once_with("18AUG2026", 25100, 24900)
        snapshot = save_snapshot.call_args.args[0]
        self.assertEqual(snapshot["call_strike"], 25100)
        self.assertEqual(snapshot["put_strike"], 24900)
        record = insert_record.call_args.args[1]
        self.assertEqual(record["nifty_open"], 24875.0)
        self.assertEqual(record["nifty_ltp"], 25000.0)
        self.assertEqual(record["call_strike"], 25100)
        self.assertEqual(record["put_strike"], 24900)
        self.assertEqual(record["gainloss"], 0.0)


if __name__ == "__main__":
    unittest.main()
