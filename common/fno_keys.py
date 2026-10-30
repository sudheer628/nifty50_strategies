"""
fno_keys.py - Canonical Redis Key Schema and Utilities for F&O Exit Order Management.

Defines unified key formats shared across:
1. Alpha FSM & UnifiedStrategyStore (Publisher)
2. Tuesday Exit Order Manager (scripts/Tuesday-Fno-Exit-Orders-V1.py)
3. Thursday Exit Order Manager (scripts/Thursday-Fno-Exit-Orders-V1.py)
4. Sentinel Hermes / Inference Runner
"""

from datetime import date, datetime
import re
from typing import Any, Dict, Optional, Tuple, Union


def normalize_cycle(cycle: str) -> str:
    """Normalize strategy cycle name to 3-letter shorthand ('tue' or 'thu')."""
    c = str(cycle or "").lower().strip()
    if c.startswith("tu") or "5day" in c or "weekly" in c:
        return "tue"
    if c.startswith("th") or "3day" in c:
        return "thu"
    return c


def normalize_expiry(
    expiry: Union[str, date, datetime, None],
    active_expiry: Optional[str] = None,
    ref_year: Optional[int] = None,
) -> str:
    """
    Normalize various expiry date formats to standard ISO 'YYYY-MM-DD'.
    
    Supported input formats:
      - datetime.date or datetime.datetime object -> 'YYYY-MM-DD'
      - ISO date string: '2026-11-03' -> '2026-11-03'
      - Compact date: '20261103' -> '2026-11-03'
      - Upstox tradingsymbol weekly: '03NOV26' -> '2026-11-03'
      - Angel One expiry: '03NOV2026' -> '2026-11-03'
      - Legacy format without year: '4AUG' or '04AUG' -> '2026-08-04'
        (uses active_expiry or ref_year/current year)
      - Monthly format with year first: '26NOV' -> matches active_expiry if same year/month
    """
    if not expiry:
        if active_expiry:
            return normalize_expiry(active_expiry)
        return "UNKNOWN"

    if isinstance(expiry, (datetime, date)):
        return expiry.strftime("%Y-%m-%d")

    exp_str = str(expiry).strip().upper()

    # 1. ISO format: '2026-11-03'
    if re.match(r"^\d{4}-\d{2}-\d{2}$", exp_str):
        return exp_str

    # 2. Compact format: '20261103'
    if re.match(r"^\d{8}$", exp_str):
        try:
            return datetime.strptime(exp_str, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # 3. Upstox symbol weekly expiry: '03NOV26' (Day + Month + 2-digit Year)
    if re.match(r"^\d{1,2}[A-Z]{3}\d{2}$", exp_str):
        try:
            return datetime.strptime(exp_str, "%d%b%y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # 4. Angel One format: '03NOV2026' (Day + Month + 4-digit Year)
    if re.match(r"^\d{1,2}[A-Z]{3}\d{4}$", exp_str):
        try:
            return datetime.strptime(exp_str, "%d%b%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # 5. Legacy format without year: '4AUG' or '04AUG' (Day + Month)
    m_legacy = re.match(r"^(\d{1,2})([A-Z]{3})$", exp_str)
    if m_legacy:
        day = int(m_legacy.group(1))
        month_str = m_legacy.group(2)
        # If active_expiry is provided and has the same day and month, resolve to active_expiry
        if active_expiry:
            try:
                act_norm = normalize_expiry(active_expiry)
                act_dt = datetime.strptime(act_norm, "%Y-%m-%d")
                if act_dt.day == day and act_dt.strftime("%b").upper() == month_str:
                    return act_norm
            except Exception:
                pass
        # Fallback: use ref_year or current year
        yr = ref_year if ref_year else date.today().year
        try:
            return datetime.strptime(f"{day:02d}{month_str}{yr}", "%d%b%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # 6. Monthly format with 2-digit year first: '26NOV' (Year + Month)
    m_monthly = re.match(r"^(\d{2})([A-Z]{3})$", exp_str)
    if m_monthly:
        yr_2d = m_monthly.group(1)
        month_str = m_monthly.group(2)
        if active_expiry:
            try:
                act_norm = normalize_expiry(active_expiry)
                act_dt = datetime.strptime(act_norm, "%Y-%m-%d")
                if act_dt.strftime("%y") == yr_2d and act_dt.strftime("%b").upper() == month_str:
                    return act_norm
            except Exception:
                pass

    return exp_str


def build_leg_enabled_key(
    cycle: str,
    expiry: Union[str, date, datetime],
    strike: Union[int, float, str],
    option_type: str,
    broker: str = "upstox",
    active_expiry: Optional[str] = None,
) -> str:
    """
    Build canonical Redis authorization key for a cycle's option leg.
    Includes expiry to guarantee strict multi-expiry isolation.
    
    Format:
        fno:{broker}:enabled:{cycle}:{expiry_iso}:{strike}:{option_type}
    
    Examples:
        fno:upstox:enabled:tue:2026-11-03:22650:CE
        fno:upstox:enabled:thu:2026-11-03:22800:PE
    """
    b = str(broker or "upstox").lower().strip()
    c = normalize_cycle(cycle)
    exp_iso = normalize_expiry(expiry, active_expiry=active_expiry)
    st = int(float(strike))
    opt = str(option_type or "").upper().strip()
    return f"fno:{b}:enabled:{c}:{exp_iso}:{st}:{opt}"


def build_detailed_enabled_key(
    cycle: str,
    contract_suffix: str,
    broker: str = "upstox",
) -> str:
    """
    Build detailed Redis authorization key with full contract suffix.
    
    Format:
        fno:{broker}:enabled:{cycle}:{contract_suffix}
    """
    b = str(broker or "upstox").lower().strip()
    c = normalize_cycle(cycle)
    return f"fno:{b}:enabled:{c}:{contract_suffix}"


def parse_enabled_key(key: str, broker: str = "upstox", cycle: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Parse an enabled key from Redis into structured contract details.
    Supports:
      1. Canonical with expiry (3-part suffix): {expiry}:{strike}:{option_type}
      2. Legacy canonical (2-part suffix): {strike}:{option_type}
      3. Detailed (6-part suffix): {underlying}:{expiry}:{strike}:{option_type}:{symbol}:{token}
    
    Returns dict with keys:
        cycle, expiry, strike, option_type, tradingsymbol, instrument_token, is_canonical
    or None if the key doesn't match the pattern.
    """
    b = str(broker or "upstox").lower().strip()
    c_norm = normalize_cycle(cycle) if cycle else None
    
    prefix = f"fno:{b}:enabled:"
    if not key.startswith(prefix):
        return None
        
    remainder = key[len(prefix):]
    parts = remainder.split(":")
    if len(parts) < 3:
        return None
        
    key_cycle = parts[0]
    if c_norm and key_cycle != c_norm:
        return None
        
    suffix_parts = parts[1:]
    
    # 1. Canonical format with expiry: {expiry}:{strike}:{option_type}
    if len(suffix_parts) == 3:
        try:
            exp_iso = normalize_expiry(suffix_parts[0])
            strike = int(float(suffix_parts[1]))
            opt = suffix_parts[2].upper()
            return {
                "cycle": key_cycle,
                "expiry": exp_iso,
                "strike": strike,
                "option_type": opt,
                "tradingsymbol": None,
                "instrument_token": None,
                "is_canonical": True,
                "raw_key": key,
            }
        except (ValueError, TypeError):
            return None

    # 2. Legacy canonical format without expiry: {strike}:{option_type}
    if len(suffix_parts) == 2:
        try:
            strike = int(float(suffix_parts[0]))
            opt = suffix_parts[1].upper()
            return {
                "cycle": key_cycle,
                "expiry": None,
                "strike": strike,
                "option_type": opt,
                "tradingsymbol": None,
                "instrument_token": None,
                "is_canonical": True,
                "raw_key": key,
            }
        except (ValueError, TypeError):
            return None
            
    # 3. Detailed format: {underlying}:{expiry}:{strike}:{option_type}:{symbol}:{token}
    if len(suffix_parts) >= 6:
        try:
            exp_iso = normalize_expiry(suffix_parts[1])
            strike = int(float(suffix_parts[2]))
            opt = suffix_parts[3].upper()
            sym = suffix_parts[4]
            tok = suffix_parts[5]
            return {
                "cycle": key_cycle,
                "expiry": exp_iso,
                "strike": strike,
                "option_type": opt,
                "tradingsymbol": sym,
                "instrument_token": tok,
                "is_canonical": False,
                "raw_key": key,
            }
        except (ValueError, TypeError):
            return None
            
    return None


def get_orders_live_key(cycle: str) -> str:
    """Return fast-gate Redis key name for the given cycle."""
    c = normalize_cycle(cycle)
    return "ORDERS_LIVE_TUE" if c == "tue" else "ORDERS_LIVE_THU"


def get_live_exit_trigger_env(cycle: str) -> str:
    """Return .env master switch variable name for the given cycle."""
    c = normalize_cycle(cycle)
    return "LIVE_EXIT_TRIGGER_TUESDAY" if c == "LIVE_EXIT_TRIGGER_TUESDAY" or c == "tue" else "LIVE_EXIT_TRIGGER_THURSDAY"


def get_order_tag(cycle: str) -> str:
    """Return Upstox order tag for the given cycle."""
    c = normalize_cycle(cycle)
    return "TUE-SL" if c == "tue" else "THU-SL"
