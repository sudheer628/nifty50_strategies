"""
calendar_utils.py - Exchange Trading Holiday & Dynamic Strategy Lifecycle Resolver.

Dynamically determines NIFTY 50 trading holidays and aligns weekly options strategy
milestones according to exchange operating schedules:
1. Normal week:
   - Monday (15:30 IST): Strategy Closing Day.
   - Tuesday (09:30 IST): Strategy Beginning Day.
2. Monday Holiday shift:
   - Monday: Closed (Holiday).
   - Tuesday (15:30 IST): Deferred Strategy Closing Day for the active cycle.
   - Wednesday (09:30 IST): Deferred Strategy Beginning Day for the fresh cycle.
3. Tuesday Holiday shift:
   - Monday (15:30 IST): Strategy Closing Day.
   - Tuesday: Closed (Holiday).
   - Wednesday (09:30 IST): Deferred Strategy Beginning Day for the fresh cycle.
"""

import os
import sys
import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional, Set

import requests

logger = logging.getLogger("calendar_utils")

# In-memory cache for resolved holiday status: "YYYY-MM-DD" -> bool
_HOLIDAY_CACHE = {}

# Known fallback NSE / NFO trading holidays (2025 - 2027)
# Used as immediate offline fallback if the Upstox API is unreachable.
KNOWN_NSE_HOLIDAYS: Set[str] = {
    # 2026 Holidays
    "2026-01-26",  # Republic Day
    "2026-03-03",  # Holi
    "2026-03-20",  # Id-Ul-Fitr
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-27",  # Bakri Id
    "2026-08-15",  # Independence Day
    "2026-09-14",  # Ganesh Chaturthi (Next Monday)
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-09",  # Diwali Laxmi Pujan
    "2026-11-10",  # Diwali Balipratipada
    "2026-11-24",  # Gurunanak Jayanti
    "2026-12-25",  # Christmas
}


def check_nse_holiday(target_date: Optional[date] = None) -> bool:
    """
    Determines whether a given calendar date is an NSE trading holiday or weekend.

    Args:
        target_date: The date to inspect. Defaults to today's local date.

    Returns:
        True if the market is closed (Weekend or Holiday), False if it is an active trading day.
    """
    ref_date = target_date if target_date else date.today()
    date_str = ref_date.strftime("%Y-%m-%d")

    # 1. Weekends (Saturday=5, Sunday=6) are always non-trading
    if ref_date.weekday() >= 5:
        return True

    # 2. Check in-memory cache
    if date_str in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[date_str]

    # 3. Query Upstox Free Public Holiday Endpoint (no auth required)
    url = f"https://api.upstox.com/v2/market/holidays/{date_str}"
    headers = {"Accept": "application/json"}

    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            payload = resp.json()
            holidays = payload.get("data", [])
            for h in holidays:
                closed = h.get("closed_exchanges", [])
                if "NSE" in closed or "NFO" in closed:
                    logger.info(f"NSE holiday confirmed via Upstox API for {date_str}: {h.get('description')}")
                    _HOLIDAY_CACHE[date_str] = True
                    return True
            # Not closed on NSE
            _HOLIDAY_CACHE[date_str] = False
            return False
    except Exception as e:
        logger.debug(f"Upstox holiday API check failed for {date_str} ({e}); falling back to static calendar.")

    # 4. Fallback to curated static exchange holiday set
    is_known_holiday = date_str in KNOWN_NSE_HOLIDAYS
    _HOLIDAY_CACHE[date_str] = is_known_holiday
    return is_known_holiday


def is_strategy_closing_day(target_date: Optional[date] = None) -> bool:
    """
    Returns True if target_date is the strategy closing day:
    - Normal week: Monday (if not a holiday).
    - Holiday week: Tuesday (if Monday was an NSE holiday and Tuesday is open).
    """
    today = target_date if target_date else date.today()

    # If today itself is a holiday/weekend, it cannot be an active closing day
    if check_nse_holiday(today):
        return False

    weekday = today.weekday()

    # Case A: Monday open -> standard closing day
    if weekday == 0:
        return True

    # Case B: Tuesday open, but Monday was a holiday -> deferred closing day
    if weekday == 1:
        monday = today - timedelta(days=1)
        if check_nse_holiday(monday):
            logger.info(f"Monday ({monday}) was an NSE holiday. Tuesday ({today}) is active Strategy Closing Day.")
            return True

    return False


def is_strategy_start_day(target_date: Optional[date] = None) -> bool:
    """
    Returns True if target_date is the strategy beginning day:
    - Normal week: Tuesday (if Monday was a normal trading day and Tuesday is open).
    - Holiday week: Wednesday (if Monday was a holiday, making Tuesday closing day,
                    OR if Tuesday was itself a holiday).
    """
    today = target_date if target_date else date.today()

    # If today itself is a holiday/weekend, it cannot be an active start day
    if check_nse_holiday(today):
        return False

    weekday = today.weekday()

    # Case A: Tuesday open, and Monday was open (normal week start)
    if weekday == 1:
        monday = today - timedelta(days=1)
        if not check_nse_holiday(monday):
            return True

    # Case B: Wednesday open, but either Monday or Tuesday was a holiday
    if weekday == 2:
        tuesday = today - timedelta(days=1)
        monday = today - timedelta(days=2)
        if check_nse_holiday(tuesday) or check_nse_holiday(monday):
            logger.info(f"Prior trading days shifted. Wednesday ({today}) is active Strategy Beginning Day.")
            return True

    return False


def get_strategy_cycle_role(target_date: Optional[date] = None) -> str:
    """
    Returns the designated role for target_date in the strategy lifecycle:
    - 'HOLIDAY': Market closed.
    - 'CLOSING_DAY': Strategy closes today (Monday, or Tuesday if Monday holiday).
    - 'START_DAY': Strategy begins today (Tuesday, or Wednesday if Monday/Tuesday holiday).
    - 'REGULAR_DAY': Mid-cycle holding day.
    """
    today = target_date if target_date else date.today()

    if check_nse_holiday(today):
        return "HOLIDAY"

    if is_strategy_closing_day(today):
        return "CLOSING_DAY"

    if is_strategy_start_day(today):
        return "START_DAY"

    return "REGULAR_DAY"
