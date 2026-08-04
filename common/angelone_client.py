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
import logging
import requests

from config import (
    logger,
    get_redis_client,
    ANGELONE_BASE_URL,
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

def get_nifty_spot() -> dict:
    """
    Fetch current NIFTY50 spot market data.

    First searches for the NIFTY 50 index instrument via the search API to
    obtain the correct Angel One symbol token, then calls the LTP endpoint.

    Returns:
        Dictionary with keys:
            - ``ltp`` (float)   : last traded price
            - ``open`` (float)  : day open price (if available)
            - ``close`` (float) : previous close (if available)
            - ``high`` (float)  : day high (if available)
            - ``low`` (float)   : day low (if available)
        Returns empty dict on failure.
    """
    headers = build_headers()
    if not headers:
        return {}

    # NIFTY50 in Angel One is token=2, exch_seg=CDS (per OpenAPIScripMaster)
    url = f"{ANGELONE_BASE_URL}/rest/secure/angelbroking/order/v1/getLtp"
    payload = {
        "exchange": "NSE",
        "tradingsymbol": NIFTY50_TRADING_SYMBOL,
        "symboltoken": NIFTY50_TOKEN,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        logger.info(
            "NIFTY LTP HTTP %s  body=%s",
            resp.status_code,
            resp.text[:300] if resp.text else "(empty)"
        )

        if resp.status_code != 200 or not resp.text.strip():
            logger.error("NIFTY LTP call failed: status=%s", resp.status_code)
            return {}

        body = resp.json()

        if body.get("status") is False:
            logger.error("NIFTY LTP API error: %s", body.get("message", body))
            return {}

        data = body.get("data", {})
        if not data:
            logger.warning("NIFTY LTP returned no data: %s", body)
            return {}

        return {
            "ltp": float(data.get("ltp", 0) or 0),
            "open": float(data.get("open", 0) or 0),
            "close": float(data.get("close", 0) or 0),
            "high": float(data.get("high", 0) or 0),
            "low": float(data.get("low", 0) or 0),
        }
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.error("NIFTY spot fetch failed: %s", exc)
        return {}


def get_option_ltp(exchange: str, trading_symbol: str, token: str) -> dict:
    """
    Fetch LTP for a specific option contract.

    Args:
        exchange:        Exchange code, e.g. ``"NFO"``.
        trading_symbol:  Trading symbol, e.g. ``"NIFTY04AUG2424500CE"``.
        token:           Angel One symbol token for the contract.

    Returns:
        Dictionary with ``ltp`` (float) and raw data.  Empty dict on failure.
    """
    url = f"{ANGELONE_BASE_URL}/rest/secure/angelbroking/order/v1/getLtp"
    headers = build_headers()
    if not headers:
        return {}

    payload = {
        "exchange": exchange,
        "tradingsymbol": trading_symbol,
        "symboltoken": token,
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

        if body.get("status") is False:
            logger.error("Option LTP API error for %s: %s",
                         trading_symbol, body.get("message", body))
            return {}

        data = body.get("data", {})
        if not data:
            return {}

        return {
            "ltp": float(data.get("ltp", 0) or 0),
            "open": float(data.get("open", 0) or 0),
            "close": float(data.get("close", 0) or 0),
        }
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.error("Option LTP fetch failed for %s: %s", trading_symbol, exc)
        return {}


def search_instruments(search_text: str, exchange: str = "NFO") -> list:
    """
    Search for instrument tokens and metadata by name.

    Uses the Angel One ``searchScrip`` endpoint to look up option contracts
    or other instruments.

    Args:
        search_text:  Partial or full trading symbol / name to search.
        exchange:     Exchange, defaults to ``"NFO"`` for F&O.

    Returns:
        List of matching instrument dicts, each containing ``symbol``,
        ``token``, ``name``, ``expiry``, ``strike``, ``lotsize``, and
        ``instrumenttype`` fields.  Returns empty list on failure.
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
        body = resp.json()
        logger.debug("searchScrip '%s' HTTP %s: %d results",
                     search_text, resp.status_code,
                     len(body.get("data", [])))

        if resp.status_code != 200:
            logger.error("searchScrip HTTP %s", resp.status_code)
            return []

        if body.get("status") is False:
            logger.error("searchScrip API error: %s", body.get("message", ""))
            return []

        data = body.get("data", [])
        if not isinstance(data, list):
            return []

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
    # Build the expected trading symbol prefix
    symbol_prefix = f"{NIFTY50_SYMBOL}{expiry_str}{strike}"
    search_term = f"{symbol_prefix}{option_type}"

    results = search_instruments(search_term, exchange="NFO")
    if not results:
        logger.warning("No instrument found for %s", search_term)
        return {}

    # Pick the best match: exact trading symbol match
    for inst in results:
        ts = inst.get("symbol", "")
        if ts.upper().startswith(symbol_prefix.upper()):
            return {
                "token": inst.get("token", ""),
                "trading_symbol": inst.get("symbol", ""),
                "exchange": inst.get("exch_seg", "NFO"),
                "lotsize": int(inst.get("lotsize", 0) or 0),
            }

    logger.warning("No exact match for %s among %d results",
                   search_term, len(results))
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
    result = {}

    call_info = resolve_option_token(expiry_str, call_strike, "CE")
    if call_info:
        call_data = get_option_ltp(
            call_info["exchange"],
            call_info["trading_symbol"],
            call_info["token"]
        )
        call_data.update(call_info)
        result["call"] = call_data
    else:
        result["call"] = None

    put_info = resolve_option_token(expiry_str, put_strike, "PE")
    if put_info:
        put_data = get_option_ltp(
            put_info["exchange"],
            put_info["trading_symbol"],
            put_info["token"]
        )
        put_data.update(put_info)
        result["put"] = put_data
    else:
        result["put"] = None

    return result
