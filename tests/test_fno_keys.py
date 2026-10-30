"""
Unit tests for common/fno_keys.py (Canonical Redis key builder and parser).
"""

from datetime import date, datetime
from common.fno_keys import (
    build_detailed_enabled_key,
    build_leg_enabled_key,
    get_live_exit_trigger_env,
    get_order_tag,
    get_orders_live_key,
    normalize_cycle,
    normalize_expiry,
    parse_enabled_key,
)


def test_normalize_cycle():
    assert normalize_cycle("tuesday") == "tue"
    assert normalize_cycle("5day_strategy") == "tue"
    assert normalize_cycle("weekly") == "tue"
    assert normalize_cycle("thursday") == "thu"
    assert normalize_cycle("3day_strategy") == "thu"
    assert normalize_cycle("tue") == "tue"
    assert normalize_cycle("thu") == "thu"


def test_normalize_expiry():
    # ISO string
    assert normalize_expiry("2026-11-03") == "2026-11-03"
    # Upstox symbol expiry format
    assert normalize_expiry("03NOV26") == "2026-11-03"
    assert normalize_expiry("3NOV26") == "2026-11-03"
    # Angel One format
    assert normalize_expiry("03NOV2026") == "2026-11-03"
    # Compact format
    assert normalize_expiry("20261103") == "2026-11-03"
    # date / datetime objects
    assert normalize_expiry(date(2026, 11, 3)) == "2026-11-03"
    assert normalize_expiry(datetime(2026, 11, 3, 10, 30)) == "2026-11-03"
    # Legacy format without year (e.g. 4AUG / 04AUG)
    assert normalize_expiry("4AUG", active_expiry="2026-08-04") == "2026-08-04"
    assert normalize_expiry("4AUG", ref_year=2026) == "2026-08-04"
    assert normalize_expiry("04AUG", ref_year=2026) == "2026-08-04"
    # Monthly format with year first (e.g. 26NOV)
    assert normalize_expiry("26NOV", active_expiry="2026-11-26") == "2026-11-26"
    # None / empty fallback
    assert normalize_expiry(None) == "UNKNOWN"


def test_build_leg_enabled_key():
    key_tue = build_leg_enabled_key("tue", "2026-11-03", 22650, "CE")
    assert key_tue == "fno:upstox:enabled:tue:2026-11-03:22650:CE"

    # From Upstox format expiry
    key_thu = build_leg_enabled_key("3day_strategy", "03NOV26", "22800.0", "pe")
    assert key_thu == "fno:upstox:enabled:thu:2026-11-03:22800:PE"

    # From legacy 4AUG with active strategy expiry
    key_legacy = build_leg_enabled_key("tue", "4AUG", 24000, "CE", active_expiry="2026-08-04")
    assert key_legacy == "fno:upstox:enabled:tue:2026-08-04:24000:CE"
    assert key_thu == "fno:upstox:enabled:thu:2026-11-03:22800:PE"


def test_build_detailed_enabled_key():
    suffix = "NIFTY:03NOV26:22650:CE:NIFTY03NOV2622650CE:NSE_FO_53750"
    key = build_detailed_enabled_key("tuesday", suffix)
    assert key == f"fno:upstox:enabled:tue:{suffix}"


def test_parse_enabled_key_canonical_with_expiry():
    key = "fno:upstox:enabled:tue:2026-11-03:22650:CE"
    parsed = parse_enabled_key(key, cycle="tue")
    assert parsed is not None
    assert parsed["cycle"] == "tue"
    assert parsed["expiry"] == "2026-11-03"
    assert parsed["strike"] == 22650
    assert parsed["option_type"] == "CE"
    assert parsed["is_canonical"] is True


def test_parse_enabled_key_legacy_canonical_without_expiry():
    key = "fno:upstox:enabled:tue:22650:CE"
    parsed = parse_enabled_key(key, cycle="tue")
    assert parsed is not None
    assert parsed["cycle"] == "tue"
    assert parsed["expiry"] is None
    assert parsed["strike"] == 22650
    assert parsed["option_type"] == "CE"
    assert parsed["is_canonical"] is True


def test_parse_enabled_key_detailed():
    key = "fno:upstox:enabled:thu:NIFTY:03NOV26:22800:PE:NIFTY03NOV2622800PE:NSE_FO_53795"
    parsed = parse_enabled_key(key, cycle="thu")
    assert parsed is not None
    assert parsed["cycle"] == "thu"
    assert parsed["expiry"] == "2026-11-03"
    assert parsed["strike"] == 22800
    assert parsed["option_type"] == "PE"
    assert parsed["tradingsymbol"] == "NIFTY03NOV2622800PE"
    assert parsed["instrument_token"] == "NSE_FO_53795"
    assert parsed["is_canonical"] is False


def test_get_constants():
    assert get_orders_live_key("tuesday") == "ORDERS_LIVE_TUE"
    assert get_orders_live_key("thursday") == "ORDERS_LIVE_THU"
    assert get_live_exit_trigger_env("tuesday") == "LIVE_EXIT_TRIGGER_TUESDAY"
    assert get_live_exit_trigger_env("thursday") == "LIVE_EXIT_TRIGGER_THURSDAY"
    assert get_order_tag("tuesday") == "TUE-SL"
    assert get_order_tag("thursday") == "THU-SL"
