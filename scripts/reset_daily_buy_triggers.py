"""
Post-Market Daily Paired BUY Trigger Re-arm Script.

Runs at 15:45 IST on trading days (Tue & Thu) to re-arm the daily paired BUY trigger
in Redis for the next cycle:
    - Tuesday 15:45 IST:   Sets LIVE_BUY_TRIGGER_TUESDAY=true (for next Tuesday)
    - Thursday 15:45 IST:  Sets LIVE_BUY_TRIGGER_THURSDAY=true (for next Thursday)

Safety Guards:
    - Fails closed if invoked during live market hours (09:15-15:30 IST) unless --force is supplied.
    - Supports --dry-run, --day, --all, and --status CLI flags.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, date, time
from typing import Dict, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytz
from config import logger, get_redis_client, get_config_flag, set_config_value
from common.calendar_utils import check_nse_holiday

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

DAY_KEY_MAP = {
    "tuesday": "LIVE_BUY_TRIGGER_TUESDAY",
    "thursday": "LIVE_BUY_TRIGGER_THURSDAY",
}

ALL_BUY_KEYS = list(DAY_KEY_MAP.values())


def _now_ist() -> datetime:
    return datetime.now(IST)


def _today_ist() -> date:
    return _now_ist().date()


def is_market_hours_ist(ref_dt: Optional[datetime] = None) -> Tuple[bool, str]:
    dt = ref_dt or _now_ist()
    if dt.weekday() >= 5:
        return False, "Weekend"
    if check_nse_holiday(dt.date()):
        return False, "NSE Holiday"
    if dt.time() < MARKET_OPEN:
        return False, f"Pre-market ({dt.strftime('%H:%M:%S')} IST < 09:15:00 IST)"
    if dt.time() >= MARKET_CLOSE:
        return False, f"Post-market ({dt.strftime('%H:%M:%S')} IST >= 15:30:00 IST)"
    return True, f"Market is open ({dt.strftime('%H:%M:%S')} IST)"


def get_triggers_status(redis_client=None) -> Dict[str, Optional[str]]:
    return {key: get_config_flag(key, default=None, redis_client=redis_client) for key in ALL_BUY_KEYS}


def reset_triggers_for_day(
    day_name: str,
    dry_run: bool = False,
    force: bool = False,
    redis_client=None,
) -> bool:
    day_lower = day_name.lower().strip()
    key = DAY_KEY_MAP.get(day_lower)
    if not key:
        logger.error("[Trigger Reset] Unsupported trading day: %s. Supported: %s", day_name, list(DAY_KEY_MAP.keys()))
        return False

    is_open, reason = is_market_hours_ist()
    if is_open and not force:
        logger.warning(
            "[Trigger Reset] Refusing to re-arm %s during active market hours: %s. Use --force to override.",
            key, reason,
        )
        return False

    if dry_run:
        logger.info("[Trigger Reset] [DRY RUN] Would set %s=true in Redis (reason: %s)", key, reason)
        return True

    success = set_config_value(key, "true", redis_client=redis_client)
    if success:
        logger.info("[Trigger Reset] Successfully re-armed %s=true in Redis (%s)", key, reason)
    else:
        logger.error("[Trigger Reset] Failed to set %s=true in Redis", key)
    return success


def reset_triggers_all(
    dry_run: bool = False,
    force: bool = False,
    redis_client=None,
) -> bool:
    is_open, reason = is_market_hours_ist()
    if is_open and not force:
        logger.warning(
            "[Trigger Reset] Refusing to re-arm all triggers during active market hours: %s. Use --force to override.",
            reason,
        )
        return False

    all_ok = True
    for key in ALL_BUY_KEYS:
        if dry_run:
            logger.info("[Trigger Reset] [DRY RUN] Would set %s=true in Redis", key)
        else:
            ok = set_config_value(key, "true", redis_client=redis_client)
            if ok:
                logger.info("[Trigger Reset] Successfully re-armed %s=true in Redis", key)
            else:
                logger.error("[Trigger Reset] Failed to set %s=true in Redis", key)
                all_ok = False
    return all_ok


def main():
    logger.info(
        "[DEPRECATED] scripts/reset_daily_buy_triggers.py is decommissioned. "
        "BUY triggers (LIVE_BUY_TRIGGER_*) are now static in .env, and duplicate entry prevention "
        "is strictly enforced via local snapshot, SQLite DB, and broker order checks. "
        "Please remove this script from crontab."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
