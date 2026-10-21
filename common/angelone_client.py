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
import re
import tempfile
import time
from datetime import datetime
from functools import lru_cache
import requests
from typing import Any, Dict, List, Optional, Tuple

from config import (
    logger,
    get_redis_client,
    ANGELONE_BASE_URL,
    ANGELONE_INSTRUMENT_MASTER_URL,
    NIFTY50_TOKEN,
    NIFTY50_SYMBOL,
    NIFTY50_TRADING_SYMBOL,
)

try:
    from common.upstox_feed_reader import read_quote, FeedUnavailableError
except ImportError:
    from upstox_feed_reader import read_quote, FeedUnavailableError

_NIFTY50_FEED_KEY = "NSE_INDEX|Nifty 50"


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


def _get_nifty_spot_angel() -> dict:
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
            "source": "angelone",
        }
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.error("NIFTY spot fetch failed: %s", exc)
        return {}


def _get_nifty_spot_from_feed(max_age_sec: Optional[int] = None) -> dict:
    """
    Fetch current NIFTY50 spot from local Upstox websocket feed cache.

    Requires valid ltp > 0, open > 0, and close > 0; otherwise raises
    FeedUnavailableError to trigger the Angel One fallback.
    """
    if max_age_sec is None:
        try:
            max_age_sec = int(os.environ.get("UPSTOX_SPOT_FEED_MAX_AGE_SEC", "30"))
        except (ValueError, TypeError):
            max_age_sec = 30
    quote = read_quote(_NIFTY50_FEED_KEY, max_age_sec=max_age_sec)

    ltp = float(quote.get("ltp") or 0.0)
    open_p = float(quote.get("open") or 0.0)
    close_p = float(quote.get("close") or quote.get("cp") or 0.0)
    if close_p <= 0.0 and quote.get("change_pct") is not None:
        try:
            chg_pct = float(quote.get("change_pct"))
            if 1.0 + chg_pct / 100.0 != 0:
                close_p = round(ltp / (1.0 + chg_pct / 100.0), 2)
        except (ValueError, TypeError, ZeroDivisionError):
            pass

    high_p = float(quote.get("high") or 0.0)
    low_p = float(quote.get("low") or 0.0)

    if ltp <= 0 or open_p <= 0 or close_p <= 0:
        raise FeedUnavailableError(
            f"Incomplete quote fields from feed (ltp={ltp}, open={open_p}, close={close_p})"
        )

    # NIFTY +/-5% previous close drift against live spot is impossible (circuit limit is 10%)
    if abs(close_p - ltp) / ltp > 0.05:
        raise FeedUnavailableError(
            f"Implausible prev close {close_p} vs ltp {ltp} (>5% drift)"
        )

    return {
        "ltp": ltp,
        "open": open_p,
        "close": close_p,
        "high": high_p,
        "low": low_p,
        "source": "upstox_feed",
    }


def get_nifty_spot() -> dict:
    """
    Fetch current NIFTY50 spot market data.

    Attempts to read from local Upstox websocket feed cache first
    (/tmp/upstox_live_quotes.json, max_age 30s).
    Falls back to Angel One SmartAPI Market Data quote on any feed error.

    Returns:
        Dictionary with keys:
            - ``ltp`` (float)   : last traded price
            - ``open`` (float)  : day open price
            - ``close`` (float) : previous close
            - ``high`` (float)  : day high
            - ``low`` (float)   : day low
            - ``source`` (str)  : 'upstox_feed' or 'angelone'
        Returns empty dict on total failure.
    """
    try:
        spot = _get_nifty_spot_from_feed()
        logger.info(
            "NIFTY spot fetched from Upstox feed: LTP=%.2f  Open=%.2f  Close=%.2f",
            spot["ltp"], spot["open"], spot["close"]
        )
        return spot
    except FeedUnavailableError as exc:
        logger.warning("Upstox feed unavailable for NIFTY spot (%s); falling back to Angel One", exc)
    except Exception as exc:
        logger.warning("Unexpected error reading Upstox feed (%s); falling back to Angel One", exc)

    return _get_nifty_spot_angel()


def _get_upstox_option_quotes(expiry_str: str, call_strike: int, put_strike: int) -> dict:
    """
    Fetch option quotes from Upstox Option Chain API as a resilient secondary fallback.

    Args:
        expiry_str:   Angel One formatted expiry date, e.g. "11AUG2026" or "27OCT2026".
        call_strike:  Call strike price, e.g. 24700.
        put_strike:   Put strike price, e.g. 24500.

    Returns:
        Dictionary with 'call' and/or 'put' dicts containing 'ltp' and 'source': 'upstox_api'.
        Empty dict on total failure or missing token.
    """
    r = get_redis_client()
    if not r:
        return {}

    try:
        token = r.get("upstox_access_token")
        if not token:
            logger.debug("Upstox option fallback: upstox_access_token missing in Redis")
            return {}
    except Exception as exc:
        logger.debug("Upstox option fallback: Redis get failed: %s", exc)
        return {}

    try:
        expiry_iso = datetime.strptime(expiry_str.upper(), "%d%b%Y").strftime("%Y-%m-%d")
    except ValueError:
        logger.warning("Upstox option fallback: invalid expiry format '%s'", expiry_str)
        return {}

    url = "https://api.upstox.com/v2/option/chain"
    params = {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "expiry_date": expiry_iso,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning(
                "Upstox option chain HTTP %s: %s",
                resp.status_code, resp.text[:200] if resp.text else "(empty)"
            )
            return {}

        body = resp.json()
        if body.get("status") != "success":
            logger.warning("Upstox option chain returned non-success: %s", body)
            return {}

        out = {}
        for item in body.get("data", []):
            try:
                strike = int(round(float(item.get("strike_price") or 0)))
            except (ValueError, TypeError):
                continue

            if strike == call_strike:
                call_opts = item.get("call_options") or {}
                mkt = call_opts.get("market_data") or {}
                ltp = mkt.get("ltp")
                if ltp is not None and float(ltp) > 0:
                    out["call"] = {
                        "ltp": float(ltp),
                        "instrument_key": str(call_opts.get("instrument_key") or ""),
                        "source": "upstox_api",
                    }

            if strike == put_strike:
                put_opts = item.get("put_options") or {}
                mkt = put_opts.get("market_data") or {}
                ltp = mkt.get("ltp")
                if ltp is not None and float(ltp) > 0:
                    out["put"] = {
                        "ltp": float(ltp),
                        "instrument_key": str(put_opts.get("instrument_key") or ""),
                        "source": "upstox_api",
                    }

        return out
    except Exception as exc:
        logger.warning("Upstox option chain request failed: %s", exc)
        return {}


def _get_single_option_ltp_from_upstox(trading_symbol: str) -> dict:
    """Fallback: fetch single option LTP from Upstox Option Chain if Angel One fails."""
    match = re.match(
        r"^NIFTY(\d{2}[A-Z]{3})(\d{2})(\d{4,6})(CE|PE)$", trading_symbol.upper()
    ) or re.match(
        r"^NIFTY(\d{2}[A-Z]{3})(\d{4})(\d{4,6})(CE|PE)$", trading_symbol.upper()
    )
    if not match:
        return {}
    day_mon, yr, strike_str, opt_type = match.groups()
    if len(yr) == 2:
        yr = f"20{yr}"
    exp_full = f"{day_mon}{yr}"
    try:
        strike_val = int(strike_str)
        up_quotes = _get_upstox_option_quotes(
            exp_full,
            strike_val if opt_type == "CE" else 0,
            strike_val if opt_type == "PE" else 0,
        )
        quote = up_quotes.get("call" if opt_type == "CE" else "put")
        if quote and float(quote.get("ltp") or 0) > 0:
            logger.warning(
                "Option LTP for %s recovered via Upstox API fallback: %.2f",
                trading_symbol, quote["ltp"]
            )
            return {"ltp": float(quote["ltp"]), "source": "upstox_api"}
    except Exception as exc:
        logger.debug("Upstox single option fallback failed for %s: %s", trading_symbol, exc)
    return {}


def get_option_ltp(exchange: str, trading_symbol: str, token: str) -> dict:
    """
    Fetch LTP for a specific option contract via the Market Data API.

    Uses LTP mode for efficiency (only need the last traded price).
    Falls back to Upstox API if Angel One fails.

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
        return _get_single_option_ltp_from_upstox(trading_symbol)

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
            logger.warning("Option LTP HTTP %s for %s; trying Upstox fallback", resp.status_code, trading_symbol)
            return _get_single_option_ltp_from_upstox(trading_symbol)

        body = resp.json()

        if body.get("status") is not True:
            logger.warning("Option LTP API error for %s: %s; trying Upstox fallback",
                           trading_symbol, body.get("message", body))
            return _get_single_option_ltp_from_upstox(trading_symbol)

        fetched = (body.get("data") or {}).get("fetched", [])
        if not fetched:
            unfetched = (body.get("data") or {}).get("unfetched", [])
            if unfetched:
                logger.warning("Option LTP unfetched for %s: %s; trying Upstox fallback",
                               trading_symbol, unfetched[0])
            return _get_single_option_ltp_from_upstox(trading_symbol)

        item = fetched[0]
        ltp_val = float(item.get("ltp", 0) or 0)
        if ltp_val > 0:
            return {
                "ltp": ltp_val,
            }
        return _get_single_option_ltp_from_upstox(trading_symbol)
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Option LTP fetch failed for %s (%s); trying Upstox fallback", trading_symbol, exc)
        return _get_single_option_ltp_from_upstox(trading_symbol)


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


_CACHE_DIR = os.environ.get("CACHE_DIR") or tempfile.gettempdir()
_MASTER_DISK_CACHE_PATH = os.path.join(_CACHE_DIR, "angelone_scrip_master.json")
_TOKEN_DISK_CACHE_PATH = os.path.join(_CACHE_DIR, "angelone_resolved_tokens.json")

_INSTRUMENT_MASTER_RETRIES = int(os.environ.get("ANGELONE_MASTER_RETRIES", "1"))
_INSTRUMENT_MASTER_BACKOFF = (2,)
_INSTRUMENT_MASTER_TIMEOUT = int(os.environ.get("ANGELONE_MASTER_TIMEOUT_SEC", "8"))
_MASTER_FAILURE_COOLDOWN_SEC = int(os.environ.get("ANGELONE_MASTER_COOLDOWN_SEC", "300"))

_LAST_MASTER_FAILURE_TIME = 0.0
_RESOLVED_OPTION_TOKENS: Dict[Tuple[str, int, str], dict] = {}


def clear_token_cache() -> None:
    """Clear in-memory and disk token cache (primarily for tests and weekly resets)."""
    global _LAST_MASTER_FAILURE_TIME
    _RESOLVED_OPTION_TOKENS.clear()
    _LAST_MASTER_FAILURE_TIME = 0.0
    try:
        _fetch_instrument_master.cache_clear()
    except Exception:
        pass
    if os.path.exists(_TOKEN_DISK_CACHE_PATH):
        try:
            os.remove(_TOKEN_DISK_CACHE_PATH)
        except OSError:
            pass


def _load_token_cache_from_disk() -> dict:
    if not os.path.exists(_TOKEN_DISK_CACHE_PATH):
        return {}
    try:
        if time.time() - os.path.getmtime(_TOKEN_DISK_CACHE_PATH) > 7 * 86400:
            return {}
        with open(_TOKEN_DISK_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_token_to_disk(str_key: str, data: dict) -> None:
    try:
        existing = _load_token_cache_from_disk()
        existing[str_key] = data
        with open(_TOKEN_DISK_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f)
    except Exception:
        pass


def _load_master_from_disk() -> list:
    if not os.path.exists(_MASTER_DISK_CACHE_PATH):
        return []
    try:
        if time.time() - os.path.getmtime(_MASTER_DISK_CACHE_PATH) > 86400:
            return []
        with open(_MASTER_DISK_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list) and len(data) > 0:
                logger.info("Loaded %d instruments from local disk cache (%s)",
                            len(data), _MASTER_DISK_CACHE_PATH)
                return data
    except Exception as exc:
        logger.debug("Failed reading master disk cache: %s", exc)
    return []


def _save_master_to_disk(instruments: list) -> None:
    try:
        with open(_MASTER_DISK_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(instruments, f)
    except Exception as exc:
        logger.debug("Failed writing master disk cache: %s", exc)


@lru_cache(maxsize=1)
def _fetch_instrument_master() -> list:
    """Internal: download instrument master with fast-path retry and cooldown.

    Raises on failure so lru_cache does NOT cache empty results.
    """
    global _LAST_MASTER_FAILURE_TIME

    # If recent failure cooldown is active, return disk cache if valid, otherwise raise
    if _LAST_MASTER_FAILURE_TIME > 0 and (time.time() - _LAST_MASTER_FAILURE_TIME < _MASTER_FAILURE_COOLDOWN_SEC):
        cached = _load_master_from_disk()
        if cached:
            return cached
        raise requests.RequestException(
            f"Instrument master download in failure cooldown ({int(_MASTER_FAILURE_COOLDOWN_SEC - (time.time() - _LAST_MASTER_FAILURE_TIME))}s remaining)"
        )

    last_exc = None
    for attempt in range(_INSTRUMENT_MASTER_RETRIES + 1):
        try:
            resp = requests.get(
                ANGELONE_INSTRUMENT_MASTER_URL,
                timeout=_INSTRUMENT_MASTER_TIMEOUT
            )
            resp.raise_for_status()
            instruments = resp.json()
            if not isinstance(instruments, list):
                raise ValueError("Angel One instrument master is not a list")
            logger.info("Loaded %d instruments from Angel One master (attempt %d)",
                        len(instruments), attempt + 1)
            _save_master_to_disk(instruments)
            _LAST_MASTER_FAILURE_TIME = 0.0
            return instruments
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
            if attempt < _INSTRUMENT_MASTER_RETRIES:
                wait = _INSTRUMENT_MASTER_BACKOFF[min(attempt, len(_INSTRUMENT_MASTER_BACKOFF) - 1)]
                logger.warning(
                    "Instrument master attempt %d/%d failed: %s — retrying in %ds",
                    attempt + 1, _INSTRUMENT_MASTER_RETRIES + 1, exc, wait
                )
                time.sleep(wait)

    _LAST_MASTER_FAILURE_TIME = time.time()
    # If network attempts failed, attempt recovery from local disk cache
    cached = _load_master_from_disk()
    if cached:
        return cached

    # All retries exhausted — raise so lru_cache does not cache the failure
    raise last_exc  # type: ignore[misc]


def get_instrument_master() -> list:
    """Download and cache Angel One's current instrument catalogue.

    Retries with fast timeout and exponential backoff.
    Returns an empty list on total failure (callers unchanged).
    """
    try:
        return _fetch_instrument_master()
    except Exception as exc:
        logger.warning(
            "Angel One instrument master download unavailable (%s); falling back to searchScrip API",
            exc
        )
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
    Caches resolved tokens in memory and on disk.

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

    cache_key = (expiry_str, int(strike), option_type)
    if cache_key in _RESOLVED_OPTION_TOKENS:
        return _RESOLVED_OPTION_TOKENS[cache_key].copy()

    str_key = f"{expiry_str}_{int(strike)}_{option_type}"
    disk_cache = _load_token_cache_from_disk()
    if str_key in disk_cache:
        token_info = disk_cache[str_key]
        _RESOLVED_OPTION_TOKENS[cache_key] = token_info
        return token_info.copy()

    # Step 1: In the instrument master, equity/index option strikes are represented
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
            _RESOLVED_OPTION_TOKENS[cache_key] = resolved
            _save_token_to_disk(str_key, resolved)
            return resolved

    # Step 2: Fallback to Search Scrip if the public master is temporarily
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
            resolved = {
                "token": token,
                "trading_symbol": symbol,
                "exchange": exchange,
                "lotsize": int(inst.get("lotsize", 0) or 0),
            }
            logger.info(
                "Resolved NIFTY option via searchScrip: symbol=%s token=%s exchange=%s",
                symbol, token, exchange
            )
            _RESOLVED_OPTION_TOKENS[cache_key] = resolved
            _save_token_to_disk(str_key, resolved)
            return resolved

    # Only log ERROR when BOTH master and searchScrip fail
    logger.error(
        "Failed to resolve NIFTY option token for %s (expiry=%s, strike=%s, type=%s) across master and searchScrip",
        search_term, expiry_str, strike, option_type
    )
    return {}


def get_nifty_option_chain(
    expiry_str: str,
    call_strike: int,
    put_strike: int
) -> dict:
    """
    Fetch LTPs for both the chosen CALL and PUT strikes in one call.

    Falls back to Upstox Option Chain API if Angel One quotes return empty or zero.

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

    # Check if Angel One quotes were empty or missing LTPs; fallback to Upstox API
    call_ltp = float((result.get("call") or {}).get("ltp") or 0.0)
    put_ltp = float((result.get("put") or {}).get("ltp") or 0.0)

    if call_ltp <= 0.0 or put_ltp <= 0.0:
        upstox_quotes = _get_upstox_option_quotes(expiry_str, call_strike, put_strike)
        if call_ltp <= 0.0 and "call" in upstox_quotes:
            if not result.get("call"):
                result["call"] = {
                    "trading_symbol": f"NIFTY{expiry_str}{call_strike}CE",
                    "exchange": "NFO",
                    "token": upstox_quotes["call"].get("instrument_key", ""),
                    "lotsize": 65,
                }
            result["call"]["ltp"] = upstox_quotes["call"]["ltp"]
            result["call"]["source"] = "upstox_api"
            logger.warning(
                "Angel One Call LTP unavailable; recovered Call LTP=%.2f from Upstox API",
                result["call"]["ltp"]
            )
        if put_ltp <= 0.0 and "put" in upstox_quotes:
            if not result.get("put"):
                result["put"] = {
                    "trading_symbol": f"NIFTY{expiry_str}{put_strike}PE",
                    "exchange": "NFO",
                    "token": upstox_quotes["put"].get("instrument_key", ""),
                    "lotsize": 65,
                }
            result["put"]["ltp"] = upstox_quotes["put"]["ltp"]
            result["put"]["source"] = "upstox_api"
            logger.warning(
                "Angel One Put LTP unavailable; recovered Put LTP=%.2f from Upstox API",
                result["put"]["ltp"]
            )

    return result
