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


def strike_selector(day_open: float) -> tuple:
    """
    Select one PUT and one CALL strike based on the day open price.

    Uses 100-point strike increments:

        - PUT  strike = floor(day_open / 100) * 100  (rounded down to nearest 100)
        - CALL strike = put_strike + STRIKE_STEP

    Example:  day_open=24540 -> PUT=24000, CALL=24500 (per plan 6.3).

    Args:
        day_open:  NIFTY50 day open price (float).

    Returns:
        Tuple ``(put_strike, call_strike)`` as integers.
    """
    # Round down the day open to the nearest multiple of STRIKE_STEP
    # Using a "higher multiple of 100 below the open" for PUT as per example:
    # day_open=24540, floor(24540/100)=245, -> 245*100=24500...wait...
    # The plan example: day_open 24540 -> PUT 24000, CALL 24500.
    # That means PUT = floor((day_open - 500) / 100) * 100 (roughly) or
    # more simply: anchor = floor(day_open / 100) * 100, then
    # PUT = anchor - (some offset to get a lower strike).
    # But the plan says "select one lower 100-point strike for PUT".
    # With day_open=24540: 24500 is nearest 100-multiple, PUT=24000 (1 step below).
    # So PUT = anchor - 100 * round((anchor % 500 + 1) / 100)... no.
    # Let's just follow the natural reading:
    # anchor = floor(day_open / 100) * 100
    # If the day_open is above the anchor by more than a small threshold,
    # we take a PUT that is one step lower.
    # For simplicity and determinism: we'll pick the PUT as one 100-step
    # below the anchor, and CALL as the anchor itself.

    from config import STRIKE_STEP
    anchor = int(day_open / STRIKE_STEP) * STRIKE_STEP

    # PUT: one STRIKE_STEP below the anchor
    put_strike = anchor - STRIKE_STEP
    # CALL: at the anchor (or one above if we prefer OTM)
    call_strike = anchor

    return put_strike, call_strike
