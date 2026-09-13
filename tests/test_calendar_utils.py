#!/usr/bin/env python3
"""
Unit tests for calendar_utils.py and run_weekly_close.py
"""

import os
import sys
import unittest
from datetime import date

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.calendar_utils import (
    check_nse_holiday,
    is_strategy_closing_day,
    is_strategy_start_day,
    get_strategy_cycle_role,
    KNOWN_NSE_HOLIDAYS,
)


class TestCalendarUtils(unittest.TestCase):

    def test_weekend_detection(self):
        """Weekends (Saturday & Sunday) must always be identified as market closed."""
        saturday = date(2026, 9, 12)
        sunday = date(2026, 9, 13)
        self.assertTrue(check_nse_holiday(saturday))
        self.assertTrue(check_nse_holiday(sunday))
        self.assertEqual(get_strategy_cycle_role(saturday), "HOLIDAY")
        self.assertEqual(get_strategy_cycle_role(sunday), "HOLIDAY")

    def test_normal_week_schedule(self):
        """In a normal week: Monday is CLOSING_DAY, Tuesday is START_DAY, Wed is REGULAR."""
        mon_normal = date(2026, 9, 7)
        tue_normal = date(2026, 9, 8)
        wed_normal = date(2026, 9, 9)

        # Monday: Closes the previous week's strategy
        self.assertFalse(check_nse_holiday(mon_normal))
        self.assertTrue(is_strategy_closing_day(mon_normal))
        self.assertFalse(is_strategy_start_day(mon_normal))
        self.assertEqual(get_strategy_cycle_role(mon_normal), "CLOSING_DAY")

        # Tuesday: Begins the new cycle
        self.assertFalse(check_nse_holiday(tue_normal))
        self.assertFalse(is_strategy_closing_day(tue_normal))
        self.assertTrue(is_strategy_start_day(tue_normal))
        self.assertEqual(get_strategy_cycle_role(tue_normal), "START_DAY")

        # Wednesday: Regular holding day
        self.assertFalse(check_nse_holiday(wed_normal))
        self.assertFalse(is_strategy_closing_day(wed_normal))
        self.assertFalse(is_strategy_start_day(wed_normal))
        self.assertEqual(get_strategy_cycle_role(wed_normal), "REGULAR_DAY")

    def test_monday_holiday_shift_ganesh_chaturthi(self):
        """
        When Monday is an NSE holiday (2026-09-14 Ganesh Chaturthi):
        - Monday is HOLIDAY (closed early morning before VM shutdown).
        - Tuesday (2026-09-15) is START_DAY for the fresh cycle.
        - Wednesday (2026-09-16) is REGULAR_DAY.
        """
        mon_holiday = date(2026, 9, 14)  # Ganesh Chaturthi
        tue_start = date(2026, 9, 15)
        wed_reg = date(2026, 9, 16)

        # Monday: Market is closed (closed early morning via --morning-holiday-check)
        self.assertTrue(check_nse_holiday(mon_holiday))
        self.assertFalse(is_strategy_closing_day(mon_holiday))
        self.assertFalse(is_strategy_start_day(mon_holiday))
        self.assertEqual(get_strategy_cycle_role(mon_holiday), "HOLIDAY")

        # Tuesday: Start day for the new cycle
        self.assertFalse(check_nse_holiday(tue_start))
        self.assertFalse(is_strategy_closing_day(tue_start))
        self.assertTrue(is_strategy_start_day(tue_start))
        self.assertEqual(get_strategy_cycle_role(tue_start), "START_DAY")

        # Wednesday: Regular mid-cycle day
        self.assertFalse(check_nse_holiday(wed_reg))
        self.assertFalse(is_strategy_closing_day(wed_reg))
        self.assertFalse(is_strategy_start_day(wed_reg))
        self.assertEqual(get_strategy_cycle_role(wed_reg), "REGULAR_DAY")

    def test_tuesday_holiday_shift(self):
        """
        If Tuesday is a holiday:
        - Monday closes normally.
        - Tuesday is HOLIDAY.
        - Wednesday begins the fresh cycle.
        """
        # Simulate Tuesday being a holiday by picking a known Tuesday holiday:
        # 2026-03-03 (Holi) was Tuesday
        tue_holi = date(2026, 3, 3)
        wed_after = date(2026, 3, 4)

        self.assertTrue(check_nse_holiday(tue_holi))
        self.assertFalse(is_strategy_start_day(tue_holi))
        self.assertEqual(get_strategy_cycle_role(tue_holi), "HOLIDAY")

        # Wednesday becomes the start day
        self.assertTrue(is_strategy_start_day(wed_after))
        self.assertEqual(get_strategy_cycle_role(wed_after), "START_DAY")

    def test_cycle_status_lifecycle(self):
        """Verify that a closed status in active snapshot is recognized and retired."""
        from strategies.weekly_option_collector import _active_cycle

        # Snapshot with status: closed
        closed_snapshot = {
            "week_start_date": "20260908",
            "expiry_date": "20260915",
            "call_strike": 25000,
            "put_strike": 24800,
            "call_buy_price": 100.0,
            "put_buy_price": 90.0,
            "status": "closed",
        }
        # Even mid-cycle, a closed status must retire the cycle
        self.assertEqual(_active_cycle(closed_snapshot, date(2026, 9, 11)), {})

        # Snapshot with status: ongoing
        ongoing_snapshot = {
            "week_start_date": "20260908",
            "expiry_date": "20260915",
            "call_strike": 25000,
            "put_strike": 24800,
            "call_buy_price": 100.0,
            "put_buy_price": 90.0,
            "status": "ongoing",
        }
        result = _active_cycle(ongoing_snapshot, date(2026, 9, 11))
        self.assertEqual(result.get("call_strike"), 25000)
        self.assertEqual(result.get("status"), "ongoing")


if __name__ == "__main__":
    unittest.main()
