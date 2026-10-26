import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
    "captured_at": 1785854400,
}

# Prior week's cycle (started Tue 2026-10-13, expiry preponed to Mon 2026-10-19
# because Tue 2026-10-20/Dussehra is a holiday), already closed post-market.
CLOSED_PRIOR_SNAPSHOT = {
    "strategy_name": "nifty50_weekly_option_collector",
    "cycle_id": "20261013-test",
    "week_start_date": "20261013",
    "expiry_date": "20261019",
    "call_strike": 25100,
    "put_strike": 24900,
    "call_buy_price": 100.0,
    "put_buy_price": 90.0,
    "status": "closed",
}


class CycleSnapshotTests(unittest.TestCase):
    def test_strikes_are_symmetric_around_first_trigger_ltp_anchor(self):
        # ITM mode (default per 6-year backtesting data: Put=25100, Call=24900)
        self.assertEqual(strike_selector(25000.0, mode="ITM"), (25100, 24900))
        self.assertEqual(strike_selector(25087.5, mode="ITM"), (25100, 24900))
        # Legacy OTM mode (Put=24900, Call=25100)
        self.assertEqual(strike_selector(25000.0, mode="OTM"), (24900, 25100))
        self.assertEqual(strike_selector(25087.5, mode="OTM"), (24900, 25100))

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

    @patch("strategies.weekly_option_collector.select_strikes", return_value=(25250, 24800, 25100, 24900, "ITM", "Mocked selection", 0.0, "NEUTRAL", 0.68, -0.68))
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
        _mock_ai,
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

        # In ITM mode, delta-calibrated selection chooses Call=24800, Put=25250 (7 DTE)
        get_options.assert_called_once_with("18AUG2026", 24800, 25250)
        snapshot = save_snapshot.call_args.args[0]
        self.assertEqual(snapshot["call_strike"], 24800)
        self.assertEqual(snapshot["put_strike"], 25250)
        record = insert_record.call_args.args[1]
        self.assertEqual(record["nifty_open"], 24875.0)
        self.assertEqual(record["nifty_ltp"], 25000.0)
        self.assertEqual(record["call_strike"], 24800)
        self.assertEqual(record["put_strike"], 25250)
        self.assertEqual(record["gainloss"], 0.0)

    @patch("strategies.weekly_option_collector.insert_timing_shadow_entry")
    @patch("strategies.weekly_option_collector.get_timing_shadow_entries", return_value=[])
    @patch("strategies.weekly_option_collector.insert_record")
    @patch("strategies.weekly_option_collector.insert_buy_snapshot")
    @patch("strategies.weekly_option_collector._ensure_db", return_value="cycle.db")
    @patch("strategies.weekly_option_collector.get_nifty_option_chain")
    @patch("strategies.weekly_option_collector.get_nifty_spot")
    @patch("strategies.weekly_option_collector.load_active_snapshot")
    @patch("strategies.weekly_option_collector._now_ist")
    @patch("strategies.weekly_option_collector._today_ist", return_value=date(2026, 8, 11))
    def test_tuesday_1001_mid_cycle_records_timing_shadows(
        self,
        _today,
        now_ist,
        load_snapshot,
        get_spot,
        get_options,
        _ensure_db,
        insert_snapshot,
        insert_record,
        get_shadows,
        insert_shadow,
    ):
        from datetime import datetime
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now_ist.return_value = datetime(2026, 8, 11, 10, 1, 0, tzinfo=ist)
        load_snapshot.return_value = {
            "strategy_name": "nifty50_weekly_option_collector",
            "cycle_id": "20260811-test",
            "week_start_date": "20260811",
            "expiry_date": "20260818",
            "call_strike": 24800,
            "put_strike": 25250,
            "call_buy_price": 150.0,
            "put_buy_price": 140.0,
            "entry_spot": 25000.0,
            "captured_at": 1786452660,
        }
        get_spot.return_value = {
            "ltp": 25050.0,
            "open": 24900.0,
            "close": 24850.0,
        }
        get_options.return_value = {
            "call": {"ltp": 135.0},
            "put": {"ltp": 125.0},
        }

        self.assertTrue(collect_once())

        # Assert insert_timing_shadow_entry was called for V1, V2, V3, V4
        self.assertEqual(insert_shadow.call_count, 4)
        variants_recorded = {call.args[1]["variant"]: call.args[1] for call in insert_shadow.call_args_list}
        self.assertIn("V1_delayed_1000", variants_recorded)
        self.assertIn("V2_staggered_c0931_p1000", variants_recorded)
        self.assertIn("V3_staggered_p0931_c1000", variants_recorded)
        self.assertIn("V4_trend_stagger", variants_recorded)

        # Verify spot prices recorded per leg in V2 and V3 (N1 fix & F4 verification)
        v2 = variants_recorded["V2_staggered_c0931_p1000"]
        self.assertEqual(v2["spot_at_call_entry"], 25000.0)  # 09:31 spot from active_cycle
        self.assertEqual(v2["spot_at_put_entry"], 25050.0)   # 10:01 current spot

        v3 = variants_recorded["V3_staggered_p0931_c1000"]
        self.assertEqual(v3["spot_at_call_entry"], 25050.0)  # 10:01 current spot
        self.assertEqual(v3["spot_at_put_entry"], 25000.0)   # 09:31 spot from active_cycle

    def test_cycle_start_defers_to_wednesday_after_tuesday_holiday(self):
        # 2026-10-20 (Dussehra) is an NSE holiday: the 5-day cycle starts Wed 2026-10-21.
        self.assertEqual(_cycle_start_for_day(date(2026, 10, 21)), date(2026, 10, 21))
        self.assertEqual(_cycle_start_for_day(date(2026, 10, 22)), date(2026, 10, 21))
        self.assertEqual(_cycle_start_for_day(date(2026, 10, 23)), date(2026, 10, 21))
        # The following Monday still belongs to the Wednesday-started cycle.
        self.assertEqual(_cycle_start_for_day(date(2026, 10, 26)), date(2026, 10, 21))

    def test_cycle_start_stays_tuesday_after_monday_holiday(self):
        # 2026-09-14 (Ganesh Chaturthi) is an NSE holiday. The prior cycle's close
        # defers to Tuesday, but the fresh cycle still starts Tuesday 2026-09-15
        # (matches is_strategy_start_day and the Monday-holiday shift test).
        self.assertEqual(_cycle_start_for_day(date(2026, 9, 16)), date(2026, 9, 15))
        self.assertEqual(_cycle_start_for_day(date(2026, 9, 17)), date(2026, 9, 15))

    @patch("strategies.weekly_option_collector.insert_timing_shadow_entry")
    @patch("strategies.weekly_option_collector.get_timing_shadow_entries", return_value=[])
    @patch("strategies.weekly_option_collector.insert_record")
    @patch("strategies.weekly_option_collector.insert_buy_snapshot")
    @patch("strategies.weekly_option_collector.save_active_snapshot")
    @patch("strategies.weekly_option_collector._ensure_db", return_value="cycle.db")
    @patch("strategies.weekly_option_collector.get_nifty_option_chain")
    @patch("strategies.weekly_option_collector.get_nifty_spot")
    @patch("strategies.weekly_option_collector.evaluate_entry_gate")
    @patch(
        "strategies.weekly_option_collector.load_active_snapshot",
        return_value=CLOSED_PRIOR_SNAPSHOT,
    )
    @patch(
        "strategies.weekly_option_collector._today_ist",
        return_value=date(2026, 10, 21),
    )
    def test_wednesday_after_tuesday_holiday_starts_fresh_cycle(
        self,
        _today,
        _load_snapshot,
        gate,
        get_spot,
        get_options,
        _ensure_db,
        save_snapshot,
        _insert_snapshot,
        insert_record,
        _get_shadows,
        _insert_shadow,
    ):
        # Scenario: Tue 2026-10-20 is a Dussehra holiday, VM was down, and the
        # collector's first run of the week happens on Wed 2026-10-21.
        gate.return_value = MagicMock(
            should_enter=True,
            status="OPEN",
            defer_reason="",
            to_dict=MagicMock(return_value={"should_enter": True}),
        )
        get_spot.return_value = {"ltp": 25000.0, "open": 24950.0, "close": 24900.0}
        get_options.return_value = {"call": {"ltp": 95.0}, "put": {"ltp": 88.0}}

        self.assertTrue(collect_once())

        # Entry gate is evaluated because Wednesday is the deferred start day.
        gate.assert_called_once()

        # Fresh cycle locked on Wednesday, targeting next Tuesday's expiry.
        snapshot = save_snapshot.call_args.args[0]
        self.assertEqual(snapshot["week_start_date"], "20261021")
        self.assertEqual(snapshot["expiry_date"], "20261027")
        self.assertEqual(snapshot["cycle_id"], "CYCLE-TUE-20261021")

        record = insert_record.call_args.args[1]
        self.assertEqual(record["expiry_date"], "20261027")
        self.assertEqual(record["cycle_id"], "CYCLE-TUE-20261021")


if __name__ == "__main__":
    unittest.main()
