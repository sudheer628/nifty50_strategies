"""
Reader for the local Upstox V3 websocket feed cache (upstox_market_feed_service).

Reads /tmp/upstox_live_quotes.json written atomically by the feed service.
Raises FeedUnavailableError on any issue (missing file, corrupted JSON,
absent instrument, stale timestamp, or non-positive LTP) so that callers
can cleanly route into their legacy fallback paths.
"""
import json
import os
import time
from typing import Any, Dict, Optional


class FeedUnavailableError(Exception):
    """Feed file missing, unreadable, instrument absent, or quote too stale."""
    pass


DEFAULT_CACHE_PATH = "/tmp/upstox_live_quotes.json"
DEFAULT_MAX_AGE_SEC = 300


def read_quote(
    instrument_key: str,
    max_age_sec: Optional[int] = None,
    cache_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return parsed quote dict for instrument_key, or raise FeedUnavailableError.
    
    Never returns None — raising is what routes callers into their except path.

    Args:
        instrument_key: Canonical Upstox instrument key (e.g. 'GLOBAL_INDICATOR|USDINR')
        max_age_sec: Maximum age in seconds based on quote's updated_at_epoch (defaults to env or 300)
        cache_path: Path to the JSON cache file (defaults to env or /tmp/upstox_live_quotes.json)

    Returns:
        Dict containing the live quote fields (ltp, change_pct, open, high, low, close, etc.)

    Raises:
        FeedUnavailableError: If the cache is missing, corrupted, stale, or lacks valid LTP.
    """
    if cache_path is None:
        cache_path = os.environ.get("UPSTOX_FEED_CACHE_PATH", DEFAULT_CACHE_PATH)
    if max_age_sec is None:
        try:
            max_age_sec = int(os.environ.get("UPSTOX_FEED_MAX_AGE_SEC", str(DEFAULT_MAX_AGE_SEC)))
        except (ValueError, TypeError):
            max_age_sec = DEFAULT_MAX_AGE_SEC

    # 1. File exists check
    if not os.path.exists(cache_path):
        raise FeedUnavailableError(f"Feed cache file does not exist: {cache_path}")

    # 2. Parse JSON
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        raise FeedUnavailableError(f"Failed to read/parse feed cache at {cache_path}: {exc}")

    # 3. Extract quotes map and target instrument
    if not isinstance(payload, dict) or "quotes" not in payload:
        raise FeedUnavailableError(f"Malformed feed cache: missing 'quotes' dictionary in {cache_path}")

    quotes = payload.get("quotes")
    if not isinstance(quotes, dict):
        raise FeedUnavailableError(f"Malformed feed cache: 'quotes' is not a dictionary in {cache_path}")

    quote = quotes.get(instrument_key)
    if not quote or not isinstance(quote, dict):
        raise FeedUnavailableError(f"Instrument '{instrument_key}' not present in feed cache")

    # 4. Check freshness via updated_at_epoch
    updated_at_epoch = quote.get("updated_at_epoch")
    if updated_at_epoch is None:
        raise FeedUnavailableError(f"Quote for '{instrument_key}' missing 'updated_at_epoch'")

    try:
        age_sec = time.time() - float(updated_at_epoch)
    except (ValueError, TypeError) as exc:
        raise FeedUnavailableError(f"Invalid 'updated_at_epoch' value for '{instrument_key}': {exc}")

    if age_sec > max_age_sec:
        raise FeedUnavailableError(
            f"Quote for '{instrument_key}' is stale: age {age_sec:.1f}s exceeds limit {max_age_sec}s"
        )

    # 5. Validate LTP
    ltp = quote.get("ltp")
    if ltp is None:
        raise FeedUnavailableError(f"Quote for '{instrument_key}' missing 'ltp'")

    try:
        ltp_val = float(ltp)
    except (ValueError, TypeError) as exc:
        raise FeedUnavailableError(f"Invalid 'ltp' value for '{instrument_key}': {exc}")

    if ltp_val <= 0:
        raise FeedUnavailableError(f"Non-positive 'ltp' ({ltp_val}) for '{instrument_key}'")

    # 6. Return verified quote dict
    return quote
