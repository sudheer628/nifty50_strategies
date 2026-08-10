"""
Weekly expiry resolution helpers for NIFTY50.

NIFTY weekly index options expire every Tuesday. The strategy always
targets a future expiry: on Monday it selects the next day, while on an
expiry Tuesday it rolls to the following Tuesday.
"""

from datetime import date, datetime, timedelta


# Weekday constants: Monday=0 ... Sunday=6
TUESDAY = 1


def get_next_weekly_expiry(from_date: date = None) -> date:
    """
    Return the next future weekly expiry (Tuesday) after ``from_date``.

    If ``from_date`` is a Tuesday, it returns the *following* Tuesday
    (per the plan rule: do not collect the current week's expiry).

    Args:
        from_date:  Reference date (defaults to today if None).

    Returns:
        The Tuesday date of the next weekly expiry.
    """
    ref = from_date if from_date else date.today()
    # Move to the next day first so an expiry Tuesday rolls to next week.
    ref += timedelta(days=1)
    days_until_tuesday = (TUESDAY - ref.weekday()) % 7
    return ref + timedelta(days=days_until_tuesday)


def is_tuesday(d: date = None) -> bool:
    """Return True if the given date (default: today) is a Tuesday."""
    ref = d if d else date.today()
    return ref.weekday() == TUESDAY


def format_expiry_angelone(expiry_date: date) -> str:
    """
    Format a date into the Angel One expiry string format ``DDMMMYYYY``.

    Example: ``date(2026, 8, 11)`` -> ``"11AUG2026"``.

    Args:
        expiry_date:  A ``datetime.date`` object.

    Returns:
        Uppercase string like ``"11AUG2026"``.
    """
    return expiry_date.strftime("%d%b%Y").upper()


def format_expiry_file(expiry_date: date) -> str:
    """
    Format a date into the file-name-safe expiry string ``YYYYMMDD``.

    Example: ``date(2026, 8, 11)`` -> ``"20260811"``.

    Args:
        expiry_date:  A ``datetime.date`` object.

    Returns:
        String like ``"20260811"``.
    """
    return expiry_date.strftime("%Y%m%d")


def strike_selector(reference_ltp: float) -> tuple:
    """
    Select one PUT and one CALL strike from the first-trigger NIFTY LTP.

    The LTP is floored to a 100-point anchor. The selected options are one
    100-point step below and above that anchor:

        - PUT  strike = anchor - 100
        - CALL strike = anchor + 100

    Example: reference_ltp=25000 -> PUT=24900, CALL=25100.

    Args:
        reference_ltp: NIFTY50 LTP at the cycle's first trigger.

    Returns:
        Tuple ``(put_strike, call_strike)`` as integers.
    """
    from config import STRIKE_STEP
    anchor = int(reference_ltp / STRIKE_STEP) * STRIKE_STEP
    put_strike = anchor - STRIKE_STEP
    call_strike = anchor + STRIKE_STEP

    return put_strike, call_strike
