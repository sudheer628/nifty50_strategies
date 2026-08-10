"""
Angel One SmartAPI client.

Provides authentication via Redis-stored JWT tokens and helpers to fetch
NIFTY50 spot data, option LTPs, and search for option instrument tokens.

Follows the same auth pattern as the existing
``news-analyzer-for-market-sentiment/scripts/check_active_positions.py``
script: static API key from .env, dynamic JWT from Redis Cloud.
"""

import os
import json
from datetime import datetime
from functools import lru_cache
import requests

from config import (
    logger,
    get_redis_client,
    ANGELONE_BASE_URL,
    ANGELONE_INSTRUMENT_MASTER_URL,
    NIFTY50_TOKEN,
    NIFTY50_SYMBOL,
    NIFTY50_TRADING_SYMBOL,
)


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------

def get_jwt_token() -> str:
    """
    Retrieve a valid Angel One JWT token from Redis.

    Reads the ``angelone_jwt_feed`` key (populated by the external
    ``generate_trading_keys`` Lambda), parses the JSON, and returns the
    raw JWT string (without the ``Bearer `` prefix).

    Returns:
        JWT string if found; empty string otherwise.

    Raises:
        RuntimeError: if Redis host / credentials are not configured.
    """
    r = get_redis_client()
    raw = r.get("angelone_jwt_feed")
    if not raw:
        logger.error("AngelOne: angelone_jwt_feed key missing in Redis")
        return ""

    tokens = json.loads(raw)
    jwt_token = tokens.get("jwtToken", "")
    if jwt_token.startswith("Bearer "):
        jwt_token = jwt_token[7:]
    if not jwt_token:
        logger.error("AngelOne: jwtToken field is empty in stored JSON")
    return jwt_token


def build_headers() -> dict:
    """
    Construct standard Angel One HTTP headers required for every SmartAPI call.

    Requires ``ANGELONE_API_KEY`` in the environment (``.env`` file).

    Returns:
        Dictionary of headers.  Returns empty dict if the API key is missing.
    """
    api_key = os.environ.get("ANGELONE_API_KEY", "")
    if not api_key:
        logger.error("AngelOne: ANGELONE_API_KEY missing from environment")
        return {}

    jwt_token = get_jwt_token()
    if not jwt_token:
        logger.error("AngelOne: JWT token retrieval failed; check Redis "
                     "key 'angelone_jwt_feed'")
        return {}

    # Log a masked preview of the JWT for diagnosis
    jwt_preview = jwt_token[:20] + "..." if len(jwt_token) > 20 else jwt_token
    logger.info("AngelOne: JWT retrieved (preview: %s), API key length=%d",
                jwt_preview, len(api_key))

    return {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": api_key,
    }


# ---------------------------------------------------------------------------
# Public data fetchers
# ---------------------------------------------------------------------------

def _resolve_nifty50_token() -> tuple:
    """
    Return the official NIFTY 50 cash-index identifiers.

    Returns:
        Tuple of (token, exchange, trading_symbol).
    """
    return (NIFTY50_TOKEN, "NSE", NIFTY50_TRADING_SYMBOL)


def get_nifty_spot() -> dict:
    """
    Fetch current NIFTY50 spot market data via the Market Data API.

    Queries the official NIFTY 50 NSE index token in FULL mode.

    Returns:
        Dictionary with keys:
            - ``ltp`` (float)   : last traded price
            - ``open`` (float)  : day open price
            - ``close`` (float) : previous close
            - ``high`` (float)  : day high
            - ``low`` (float)   : day low
        Returns empty dict on failure.
    """
    headers = build_headers()
    if not headers:
        return {}

    token, exchange, _trading_symbol = _resolve_nifty50_token()

    url = f"{ANGELONE_BASE_URL}/rest/secure/angelbroking/market/v1/quote/"
    payload = {
        "mode": "FULL",
        "exchangeTokens": {
            exchange: [token]
        }
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        logger.info(
            "NIFTY quote HTTP %s  body=%s",
            resp.status_code,
            resp.text[:500] if resp.text else "(empty)"
        )

        if resp.status_code != 200 or not resp.text.strip():
            logger.error("NIFTY quote call failed: status=%s", resp.status_code)
            return {}

        body = resp.json()

        if body.get("status") is not True:
            logger.error("NIFTY quote API error: %s",
                         body.get("message", body))
            return {}

        fetched = (body.get("data") or {}).get("fetched", [])
        if not fetched:
            logger.warning("NIFTY quote returned no fetched data: %s", body)
            return {}

        item = fetched[0]
        return {
            "ltp": float(item.get("ltp", 0) or 0),
            "open": float(item.get("open", 0) or 0),
            "close": float(item.get("close", 0) or 0),
            "high": float(item.get("high", 0) or 0),
            "low": float(item.get("low", 0) or 0),
        }
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.error("NIFTY spot fetch failed: %s", exc)
        return {}


def get_option_ltp(exchange: str, trading_symbol: str, token: str) -> dict:
    """
    Fetch LTP for a specific option contract via the Market Data API.

    Uses LTP mode for efficiency (only need the last traded price).

    Args:
        exchange:        Exchange code, e.g. ``"NFO"``.
        trading_symbol:  Trading symbol, e.g. ``"NIFTY04AUG2424500CE"``.
        token:           Angel One symbol token for the contract.

    Returns:
        Dictionary with ``ltp`` (float).  Empty dict on failure.
    """
    url = f"{ANGELONE_BASE_URL}/rest/secure/angelbroking/market/v1/quote/"
    headers = build_headers()
    if not headers:
        return {}

    payload = {
        "mode": "LTP",
        "exchangeTokens": {
            exchange: [token]
        }
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        logger.info(
            "Option LTP %s HTTP %s body=%s",
            trading_symbol, resp.status_code,
            resp.text[:300] if resp.text else "(empty)"
        )

        if resp.status_code != 200 or not resp.text.strip():
            logger.error("Option LTP HTTP %s for %s", resp.status_code, trading_symbol)
            return {}

        body = resp.json()

        if body.get("status") is not True:
            logger.error("Option LTP API error for %s: %s",
                         trading_symbol, body.get("message", body))
            return {}

        fetched = (body.get("data") or {}).get("fetched", [])
        if not fetched:
            unfetched = (body.get("data") or {}).get("unfetched", [])
            if unfetched:
                logger.error("Option LTP unfetched for %s: %s",
                             trading_symbol, unfetched[0])
            return {}

        item = fetched[0]
        return {
            "ltp": float(item.get("ltp", 0) or 0),
        }
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.error("Option LTP fetch failed for %s: %s", trading_symbol, exc)
        return {}


def get_option_ltps(option_infos: list) -> dict:
    """Fetch several resolved option contracts in one Market Data request."""
    if not option_infos:
        return {}

    exchange_tokens = {}
    for info in option_infos:
        exchange = info.get("exchange", "NFO")
        token = str(info.get("token", ""))
        if token:
            exchange_tokens.setdefault(exchange, []).append(token)

    if not exchange_tokens:
        return {}

    headers = build_headers()
    if not headers:
        return {}

    url = f"{ANGELONE_BASE_URL}/rest/secure/angelbroking/market/v1/quote/"
    payload = {
        "mode": "LTP",
        "exchangeTokens": exchange_tokens,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        logger.info(
            "Option batch LTP HTTP %s body=%s",
            resp.status_code,
            resp.text[:500] if resp.text else "(empty)",
        )
        if resp.status_code != 200 or not resp.text.strip():
            logger.error("Option batch LTP HTTP %s", resp.status_code)
            return {}

        body = resp.json()
        if body.get("status") is not True:
            logger.error("Option batch LTP API error: %s", body)
            return {}

        quotes = {}
        for item in (body.get("data") or {}).get("fetched", []):
            token = str(item.get("symbolToken") or item.get("symboltoken") or "")
            if token:
                quotes[token] = {
                    "ltp": float(item.get("ltp", 0) or 0),
                }

        for item in (body.get("data") or {}).get("unfetched", []):
            logger.error("Option batch LTP unfetched: %s", item)
        return quotes
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.error("Option batch LTP fetch failed: %s", exc)
        return {}


@lru_cache(maxsize=1)
def get_instrument_master() -> list:
    """Download and cache Angel One's current instrument catalogue."""
    try:
        resp = requests.get(ANGELONE_INSTRUMENT_MASTER_URL, timeout=30)
        resp.raise_for_status()
        instruments = resp.json()
        if not isinstance(instruments, list):
            logger.error("Angel One instrument master is not a list")
            return []
        logger.info("Loaded %d instruments from Angel One master", len(instruments))
        return instruments
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.error("Failed to load Angel One instrument master: %s", exc)
        return []


def search_instruments(search_text: str, exchange: str = "NFO") -> list:
    """
    Search for instrument tokens and metadata by name.

    Uses the Angel One ``searchScrip`` endpoint to look up option contracts
    or other instruments.

    Args:
        search_text:  Partial or full trading symbol / name to search.
        exchange:     Exchange, defaults to ``"NFO"`` for F&O.

    Returns:
        List of matching instrument dicts.  Returns empty list on failure.
    """
    url = f"{ANGELONE_BASE_URL}/rest/secure/angelbroking/order/v1/searchScrip"
    headers = build_headers()
    if not headers:
        return []

    payload = {
        "exchange": exchange,
        "searchscrip": search_text,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        logger.info("searchScrip '%s' HTTP %s", search_text, resp.status_code)

        if resp.status_code != 200 or not resp.text.strip():
            logger.error("searchScrip HTTP %s (empty response)", resp.status_code)
            return []

        body = resp.json()
        if body is None:
            logger.error("searchScrip returned null body")
            return []

        if body.get("status") is False:
            logger.error("searchScrip API error: %s", body.get("message", ""))
            return []

        data = body.get("data", [])
        if not isinstance(data, list):
            logger.warning("searchScrip data is not a list: %s", type(data))
            return []

        logger.info("searchScrip '%s' returned %d results",
                    search_text, len(data))
        return data
    except (requests.RequestException, json.JSONDecodeError) as exc:
        logger.error("searchScrip failed: %s", exc)
        return []


def resolve_option_token(
    expiry_str: str,
    strike: int,
    option_type: str
) -> dict:
    """
    Resolve the Angel One symbol token for a NIFTY option contract.

    Searches for the given expiry, strike, and option type (CE / PE).

    Args:
        expiry_str:   Expiry date formatted as ``"DDMMMYYYY"``, e.g. ``"11AUG2026"``.
        strike:       Strike price, e.g. ``24500``.
        option_type:  ``"CE"`` for CALL, ``"PE"`` for PUT.

    Returns:
        Dict with ``token`` (str), ``trading_symbol`` (str), ``exchange`` (str),
        and ``lotsize`` (int).  Returns empty dict if not found.
    """
    expiry_str = expiry_str.upper()
    option_type = option_type.upper()
    if option_type not in ("CE", "PE"):
        raise ValueError("option_type must be CE or PE")

    # In the instrument master, equity/index option strikes are represented
    # in paise (24500 is stored as 2450000.000000).
    expected_master_strike = int(strike) * 100
    for inst in get_instrument_master():
        try:
            master_strike = int(round(float(inst.get("strike", 0))))
        except (TypeError, ValueError):
            continue

        symbol = str(inst.get("symbol", ""))
        if (
            inst.get("exch_seg") == "NFO"
            and inst.get("name") == NIFTY50_SYMBOL
            and inst.get("instrumenttype") == "OPTIDX"
            and str(inst.get("expiry", "")).upper() == expiry_str
            and master_strike == expected_master_strike
            and symbol.upper().endswith(option_type)
        ):
            resolved = {
                "token": str(inst.get("token", "")),
                "trading_symbol": symbol,
                "exchange": "NFO",
                "lotsize": int(inst.get("lotsize", 0) or 0),
            }
            logger.info(
                "Resolved NIFTY option: symbol=%s token=%s exchange=NFO",
                resolved["trading_symbol"], resolved["token"]
            )
            return resolved

    # Fallback to Search Scrip if the public master is temporarily
    # unavailable. Weekly NIFTY symbols use DDMMMYY, not DDMMMYYYY.
    try:
        expiry_symbol = datetime.strptime(expiry_str, "%d%b%Y").strftime(
            "%d%b%y"
        ).upper()
    except ValueError:
        logger.error("Invalid Angel One expiry date: %s", expiry_str)
        return {}

    search_term = f"{NIFTY50_SYMBOL}{expiry_symbol}{int(strike)}{option_type}"
    for inst in search_instruments(search_term, exchange="NFO"):
        # searchScrip uses these names; the instrument master uses the
        # alternatives after ``or``.
        symbol = str(inst.get("tradingsymbol") or inst.get("symbol") or "")
        token = str(inst.get("symboltoken") or inst.get("token") or "")
        exchange = str(inst.get("exchange") or inst.get("exch_seg") or "NFO")
        if symbol.upper() == search_term and token:
            return {
                "token": token,
                "trading_symbol": symbol,
                "exchange": exchange,
                "lotsize": int(inst.get("lotsize", 0) or 0),
            }

    logger.warning("No instrument found for %s", search_term)
    return {}


def get_nifty_option_chain(
    expiry_str: str,
    call_strike: int,
    put_strike: int
) -> dict:
    """
    Fetch LTPs for both the chosen CALL and PUT strikes in one call.

    Args:
        expiry_str:   Expiry date in ``"DDMMMYYYY"`` format.
        call_strike:  CALL strike price.
        put_strike:   PUT strike price.

    Returns:
        Dictionary:
            ``{"call": {ltp, token, ...}, "put": {ltp, token, ...}}``
        Missing keys on failure.
    """
    call_info = resolve_option_token(expiry_str, call_strike, "CE")
    put_info = resolve_option_token(expiry_str, put_strike, "PE")
    resolved = [info for info in (call_info, put_info) if info]
    quotes = get_option_ltps(resolved)

    result = {}
    for key, info in (("call", call_info), ("put", put_info)):
        if not info:
            result[key] = None
            continue
        data = quotes.get(str(info["token"]), {}).copy()
        data.update(info)
        result[key] = data

    return result
