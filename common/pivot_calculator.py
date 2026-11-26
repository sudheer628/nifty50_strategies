#!/usr/bin/env python3
"""
common/pivot_calculator.py - NIFTY 50 Daily Floor Pivot Point Engine.

Calculates, validates, caches, and evaluates Classical Floor Pivots (P, R1, R2, S1, S2)
for NIFTY 50 based on the completed prior trading session OHLC (holiday-aware).

Levels:
    P  = (H + L + C) / 3
    R1 = 2P - L
    S1 = 2P - H
    R2 = P + (H - L)
    S2 = P - (H - L)

Validation Invariant:
    R2 > R1 > P > S1 > S2 > 0
"""

import datetime
import json
import logging
import os
import sys
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    import pytz
    IST_TZ = pytz.timezone("Asia/Kolkata")
except ImportError:
    from zoneinfo import ZoneInfo
    IST_TZ = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger("pivot_calculator")


def get_today_ist() -> datetime.date:
    """Return current calendar date in Asia/Kolkata timezone."""
    return datetime.datetime.now(IST_TZ).date()


def calculate_floor_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    """
    Compute classical floor pivot points from prior session High, Low, Close.
    Returns:
        dict with keys: pivot, r1, r2, s1, s2, range
    """
    h = float(high)
    l = float(low)
    c = float(close)
    if h < l or l <= 0 or c <= 0:
        raise ValueError(f"Invalid OHLC values: High={high}, Low={low}, Close={close}")

    p = round((h + l + c) / 3.0, 2)
    h_minus_l = round(h - l, 2)
    r1 = round(2.0 * p - l, 2)
    s1 = round(2.0 * p - h, 2)
    r2 = round(p + h_minus_l, 2)
    s2 = round(p - h_minus_l, 2)

    return {
        "pivot": p,
        "r1": r1,
        "r2": r2,
        "s1": s1,
        "s2": s2,
        "range": h_minus_l,
    }


def validate_daily_pivots(
    pivots: Optional[Dict[str, Any]],
    today_ist: Optional[datetime.date] = None,
    current_ts: Optional[int] = None,
    max_age_seconds: int = 172800,  # 48 hours
) -> bool:
    """
    Strict validation of daily pivot levels against mathematical invariants,
    realistic index spot bounds, and trading date freshness metadata.

    Invariants required:
      1. R2 > R1 > P > S1 > S2 > 0
      2. 15,000 < P < 40,000 (realistic NIFTY bounds)
      3. valid_for_date present; matches today_ist (if today_ist is provided)
      4. prior_trading_date present; strictly precedes valid_for_date
      5. created_at_ts present, positive, not in the future, and not stale (<= 48h)
    """
    if not pivots or not isinstance(pivots, dict):
        return False

    required_keys = ["pivot", "r1", "r2", "s1", "s2", "valid_for_date", "prior_trading_date", "created_at_ts"]
    if not all(k in pivots for k in required_keys):
        missing = [k for k in required_keys if k not in pivots]
        logger.warning("Pivot payload missing required freshness keys: %s", missing)
        return False

    try:
        p = float(pivots["pivot"])
        r1 = float(pivots["r1"])
        r2 = float(pivots["r2"])
        s1 = float(pivots["s1"])
        s2 = float(pivots["s2"])
    except (TypeError, ValueError):
        return False

    # Mathematical ordering invariant
    if not (r2 > r1 > p > s1 > s2 > 0):
        logger.warning(
            "Pivot ordering invariant failed: R2=%s, R1=%s, P=%s, S1=%s, S2=%s",
            r2, r1, p, s1, s2
        )
        return False

    # Sanity range for NIFTY 50
    if not (15000.0 < p < 40000.0):
        logger.warning("Pivot sanity range failed for NIFTY: P=%s", p)
        return False

    # Relative bounds invariant (0.7P <= S2 and R2 <= 1.3P)
    if not (0.70 * p <= s2 and r2 <= 1.30 * p):
        logger.warning(
            "Pivot relative bounds invariant failed (0.7P <= S2 and R2 <= 1.3P): P=%s, S2=%s, R2=%s",
            p, s2, r2
        )
        return False

    # Date validations
    valid_date_str = str(pivots.get("valid_for_date") or "").strip()
    if not valid_date_str:
        logger.warning("Pivot valid_for_date is empty")
        return False

    if today_ist is not None:
        if valid_date_str != today_ist.isoformat():
            logger.warning(
                "Pivot date mismatch: valid_for_date=%s, expected=%s",
                valid_date_str, today_ist.isoformat()
            )
            return False

    prior_date_str = str(pivots.get("prior_trading_date") or "").strip()
    if not prior_date_str:
        logger.warning("Pivot prior_trading_date is missing or empty")
        return False

    if prior_date_str >= valid_date_str:
        logger.warning(
            "Prior trading date must precede valid date: prior=%s, valid=%s",
            prior_date_str, valid_date_str
        )
        return False

    # Freshness timestamp validation
    try:
        c_ts = int(pivots["created_at_ts"])
        if c_ts <= 0:
            logger.warning("Pivot created_at_ts must be positive: %s", c_ts)
            return False
    except (TypeError, ValueError):
        logger.warning("Pivot created_at_ts is invalid: %s", pivots.get("created_at_ts"))
        return False

    now_ts = current_ts or int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    # Reject timestamps > 300s in the future (clock skew guard)
    if c_ts > (now_ts + 300):
        logger.warning("Pivot created_at_ts is in the future: %s > %s", c_ts, now_ts)
        return False

    # Reject stale cache entries older than max_age_seconds
    if (now_ts - c_ts) > max_age_seconds:
        logger.warning("Pivot created_at_ts is stale (%s seconds old, max %s)", now_ts - c_ts, max_age_seconds)
        return False

    return True


def fetch_nifty_daily_ohlc(
    token: str,
    instrument_key: str = "NSE_INDEX|Nifty 50",
    lookback_days: int = 10,
    include_today: bool = False,
    timeout: int = 15,
) -> List[Dict[str, Any]]:
    """
    Fetch completed daily OHLC candles from Upstox Historical Candle API.
    Returns candles sorted descending (newest completed trading day at index 0).
    """
    import requests

    if token == "MOCK_TOKEN" or str(token).startswith("MOCK") or str(token).startswith("SIM") or os.getenv("TEST_MODE") == "true":
        return []

    encoded_key = urllib.parse.quote(instrument_key)
    today = get_today_ist()
    calendar_lookback = max(30, int(lookback_days * 3.5))
    from_date = today - datetime.timedelta(days=calendar_lookback)

    to_str = today.strftime("%Y-%m-%d")
    from_str = from_date.strftime("%Y-%m-%d")

    url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/day/{to_str}/{from_str}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    resp = requests.get(url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Upstox API returned HTTP {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Upstox API returned error: {body.get('errors')}")

    raw_candles = body.get("data", {}).get("candles", [])
    if not raw_candles:
        logger.warning("No candle data returned by Upstox for %s", instrument_key)
        return []

    parsed_candles = []
    today_str = today.strftime("%Y-%m-%d")

    for c in raw_candles:
        if len(c) < 6:
            continue
        ts_str = str(c[0])
        date_str = ts_str[:10]

        # Incomplete live session candle must be excluded
        if not include_today and date_str == today_str:
            continue

        open_p = float(c[1])
        high_p = float(c[2])
        low_p = float(c[3])
        close_p = float(c[4])
        volume = int(c[5])
        oi = int(c[6]) if len(c) > 6 else 0

        parsed_candles.append({
            "date": date_str,
            "timestamp": ts_str,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": volume,
            "open_interest": oi,
        })

    parsed_candles.sort(key=lambda x: x["date"], reverse=True)
    return parsed_candles[:lookback_days]


def calculate_proximity_margin(nifty_spot: float) -> float:
    """
    Compute adaptive proximity margin for pivot zones.
    Default: 0.10% of NIFTY spot capped at 30.0 points.
    At 25,500 NIFTY -> 25.5 points.
    """
    try:
        spot = float(nifty_spot)
        return min(round(spot * 0.0010, 1), 30.0)
    except Exception:
        return 25.0


def get_pivot_zone(
    nifty_spot: Optional[float],
    daily_pivots: Optional[Dict[str, Any]],
    margin: Optional[float] = None,
) -> Optional[str]:
    """
    Identify whether NIFTY spot is currently inside a key Resistance or Support zone.

    Returns:
        "R2" | "R1" | "S1" | "S2" | None
    """
    if nifty_spot is None or not daily_pivots:
        return None

    try:
        spot = float(nifty_spot)
        eff_margin = margin if margin is not None else calculate_proximity_margin(spot)

        r2 = float(daily_pivots["r2"])
        r1 = float(daily_pivots["r1"])
        s1 = float(daily_pivots["s1"])
        s2 = float(daily_pivots["s2"])

        if spot >= (r2 - eff_margin):
            return "R2"
        elif spot >= (r1 - eff_margin):
            return "R1"
        elif spot <= (s2 + eff_margin):
            return "S2"
        elif spot <= (s1 + eff_margin):
            return "S1"
    except Exception as exc:
        logger.debug("Failed to evaluate pivot zone: %s", exc)

    return None


def get_or_calculate_daily_pivots(
    redis_client: Optional[Any] = None,
    upstox_token: Optional[str] = None,
    today_date: Optional[datetime.date] = None,
    force_refresh: bool = False,
    instrument_key: str = "NSE_INDEX|Nifty 50",
    skip_cache_write: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Retrieve validated daily floor pivots for today's trading session.
    Checks Redis cache first (`fno:nifty:daily_pivots:YYYY-MM-DD`).
    If missing or invalid, fetches completed D-1 candle from Upstox, computes, validates,
    and caches with 24-hour TTL.

    Fails safe to None if data cannot be retrieved or validated.
    """
    today = today_date or get_today_ist()
    today_str = today.isoformat()
    redis_key = f"fno:nifty:daily_pivots:{today_str}"

    r = redis_client
    if r is None:
        try:
            from config import get_redis_client
            r = get_redis_client()
        except Exception:
            r = None

    # 1. Read from Redis Cache if not forcing refresh
    if r and not force_refresh:
        try:
            raw = r.get(redis_key)
            if raw:
                data = json.loads(raw)
                if validate_daily_pivots(data, today_ist=today):
                    return data
                else:
                    logger.warning("Cached Redis pivots for %s failed validation. Refreshing...", today_str)
        except Exception as e_redis:
            logger.debug("Failed reading pivots from Redis: %s", e_redis)

    # 2. Resolve Upstox Access Token
    token = upstox_token
    if not token:
        try:
            from common.entry_order_manager import get_broker_token
            token = get_broker_token("upstox", redis_client=r)
        except Exception:
            pass

    if not token:
        # Fallback candidates from env
        candidates = ["upstox_access_token", "UPSTOX_ACCESS_TOKEN", "UPSTOX_TOKEN", "upstox_token"]
        if r:
            for k in candidates:
                try:
                    val = r.get(k)
                    if val:
                        token = str(val).strip()
                        break
                except Exception:
                    pass
        if not token:
            for k in candidates:
                val = os.environ.get(k)
                if val:
                    token = str(val).strip()
                    break

    if not token:
        logger.warning("No Upstox token available to calculate daily pivots. Failing safe.")
        return None

    # 3. Fetch Historical Daily Candles
    try:
        candles = fetch_nifty_daily_ohlc(
            token=token,
            instrument_key=instrument_key,
            lookback_days=5,
            include_today=False,
        )
    except Exception as e_fetch:
        logger.warning("Failed to fetch NIFTY OHLC candles from Upstox: %s", e_fetch)
        return None

    if not candles:
        logger.warning("Upstox returned 0 completed daily candles for NIFTY. Failing safe.")
        return None

    # candles[0] is strictly the most recent completed trading day (holiday-aware)
    prior_candle = candles[0]
    prior_date = prior_candle["date"]

    # 4. Calculate Floor Pivots
    try:
        levels = calculate_floor_pivots(
            high=prior_candle["high"],
            low=prior_candle["low"],
            close=prior_candle["close"],
        )
    except Exception as e_calc:
        logger.error("Failed calculating floor pivots: %s", e_calc)
        return None

    pivot_data: Dict[str, Any] = {
        "valid_for_date": today_str,
        "prior_trading_date": prior_date,
        "prev_open": prior_candle["open"],
        "prev_high": prior_candle["high"],
        "prev_low": prior_candle["low"],
        "prev_close": prior_candle["close"],
        "pivot": levels["pivot"],
        "r1": levels["r1"],
        "r2": levels["r2"],
        "s1": levels["s1"],
        "s2": levels["s2"],
        "range": levels["range"],
        "source": "upstox_historical_d1",
        "created_at_ts": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
    }

    # 5. Validate Payload
    if not validate_daily_pivots(pivot_data, today_ist=today):
        logger.error("Calculated pivot payload failed validation invariants: %s", pivot_data)
        return None

    # 6. Cache to Redis (24-hour TTL) unless skip_cache_write is requested (e.g. during dry-run)
    if r and not skip_cache_write:
        try:
            r.set(redis_key, json.dumps(pivot_data), ex=86400)
            logger.info("Cached validated daily pivots for %s in Redis: %s", today_str, redis_key)
        except Exception as e_save:
            logger.warning("Failed to cache pivots to Redis: %s", e_save)

    return pivot_data
