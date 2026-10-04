"""
Weekly expiry resolution helpers for NIFTY50.

NIFTY weekly index options expire every Tuesday. The strategy always
targets a future expiry: on Monday it selects the next day, while on an
expiry Tuesday it rolls to the following Tuesday.
"""

import math
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


def strike_selector(
    reference_ltp: float,
    mode: str = None,
    strike_step: int = None
) -> tuple:
    """
    Select one PUT and one CALL strike from the first-trigger NIFTY LTP.

    The LTP is floored to a strike_step anchor (default 100-pt).
    Supports moneyness modes:
        - "ITM" (default per empirical backtesting evidence):
            PUT strike  = anchor + step  (Strike > Spot -> In-The-Money Put)
            CALL strike = anchor - step  (Strike < Spot -> In-The-Money Call)
            Forming a Long Gut with intrinsic floor W = 2 * step.
        - "ATM":
            PUT strike  = anchor
            CALL strike = anchor
        - "OTM" (legacy mode):
            PUT strike  = anchor - step  (Strike < Spot -> Out-Of-The-Money Put)
            CALL strike = anchor + step  (Strike > Spot -> Out-Of-The-Money Call)

    Args:
        reference_ltp: NIFTY50 LTP at the cycle's first trigger.
        mode: Moneyness mode ('ITM', 'ATM', 'OTM'). Defaults to STRIKE_MONEYNESS_MODE.
        strike_step: Grid step increment (defaults to config.STRIKE_STEP).

    Returns:
        Tuple ``(put_strike, call_strike)`` as integers.
    """
    from config import STRIKE_STEP, STRIKE_MONEYNESS_MODE
    step = strike_step or STRIKE_STEP
    effective_mode = (mode or STRIKE_MONEYNESS_MODE).upper()

    anchor = int(math.floor(reference_ltp / step)) * step
    if effective_mode == "ITM":
        put_strike = anchor + step
        call_strike = anchor - step
    elif effective_mode == "ATM":
        put_strike = anchor
        call_strike = anchor
    else:  # OTM
        put_strike = anchor - step
        call_strike = anchor + step

    return put_strike, call_strike


def compute_intrinsic_extrinsic(
    spot: float,
    call_strike: int,
    put_strike: int,
    call_ltp: float,
    put_ltp: float,
) -> dict:
    """
    Decompose option prices into intrinsic value (floor) and extrinsic value (time decay risk).

    Returns:
        Dict with call_intrinsic, call_extrinsic, put_intrinsic, put_extrinsic,
        total_intrinsic, total_extrinsic, and spread_width.
    """
    call_intrinsic = max(0.0, float(spot) - float(call_strike))
    call_extrinsic = max(0.0, float(call_ltp or 0.0) - call_intrinsic)
    put_intrinsic = max(0.0, float(put_strike) - float(spot))
    put_extrinsic = max(0.0, float(put_ltp or 0.0) - put_intrinsic)
    spread_width = abs(int(put_strike) - int(call_strike))
    total_intrinsic = round(call_intrinsic + put_intrinsic, 2)
    total_extrinsic = round(call_extrinsic + put_extrinsic, 2)

    return {
        "call_intrinsic": round(call_intrinsic, 2),
        "call_extrinsic": round(call_extrinsic, 2),
        "put_intrinsic": round(put_intrinsic, 2),
        "put_extrinsic": round(put_extrinsic, 2),
        "total_intrinsic": total_intrinsic,
        "total_extrinsic": total_extrinsic,
        "spread_width": spread_width,
    }
