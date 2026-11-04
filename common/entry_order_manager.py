"""
Entry Order Manager for Automated Paired MARKET BUY Strategy Execution.

Handles paired CALL + PUT market entry order submission, funds preflight,
rapid polling verification, and atomic cancel-and-SELL rollback for weekly
NIFTY 50 options strategies:
  - Tuesday Track (5-Day Strategy): Strictly pinned to Upstox.
  - Thursday Track (3-Day Strategy): Strictly pinned to AngelOne.

Key Invariants:
1. Preflight both legs: Check broker funds, validate contracts, quantities, LTPs.
   If funds insufficient, place NEITHER order and mark SKIPPED_INSUFFICIENT_FUNDS.
2. Back-to-back MARKET BUY: Submit CALL and PUT orders back-to-back without
   waiting for the first leg to fill.
3. Atomic Rollback: The pair is successful ONLY when BOTH legs are fully filled.
   If either leg fails, is rejected, or partially fills, cancel still-open BUYs
   and immediately place MARKET SELL for any filled quantity.
4. Fill Price vs Reference LTP: Capture LTPs as reference anchors (call_ref_ltp,
   put_ref_ltp) and store actual execution prices as call_buy_price, put_buy_price.
5. No Redis Dependency for Entry: State and order IDs reside in cycle stores.
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from config import (
    ANGELONE_BASE_URL,
    NIFTY_LOT_SIZE,
    SQLITE_DIR,
    get_redis_client,
)

logger = logging.getLogger("entry_order_manager")

UPSTOX_BASE = os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com").rstrip("/")
ANGELONE_BASE = (os.getenv("ANGELONE_BASE_URL") or ANGELONE_BASE_URL).rstrip("/")

DEFAULT_BUY_BUFFER_PCT = float(os.getenv("BUY_BUFFER_PCT", "0.05"))
DEFAULT_ESTIMATED_CHARGES = float(os.getenv("BUY_ESTIMATED_CHARGES", "40.0"))
DEFAULT_TIMEOUT_SEC = float(os.getenv("BUY_VERIFY_TIMEOUT_SEC", "10.0"))
DEFAULT_POLL_INTERVAL_SEC = 0.5
MOCK_TOKEN = "MOCK_TOKEN"
ORDER_ALERT_OUTBOX_DB_PATH = os.getenv(
    "ORDER_ALERT_OUTBOX_DB_PATH",
    os.path.join(SQLITE_DIR, "pending_order_alerts.db"),
)

TERMINAL_ENTRY_ABORT_STATUSES = (
    "SKIPPED_INSUFFICIENT_FUNDS",
    "FAILED_ROLLED_BACK",
    "UNWIND_FAILED",
    "PREFLIGHT_FAILED",
    "AUTH_FAILED",
    "FAILED",
    "ABORTED",
)

# Status normalizations
STATUS_SYNONYMS = {
    "complete": "complete",
    "completed": "complete",
    "filled": "complete",
    "traded": "complete",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "open": "open",
    "pending": "open",
    "validation pending": "open",
    "trigger pending": "open",
    "put order req received": "open",
    "after market order req received": "open",
}


def normalize_status(status: Any) -> str:
    """Normalize broker order status string."""
    raw = str(status or "").strip().lower()
    return STATUS_SYNONYMS.get(raw, raw)


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


_UPSTOX_KEY_CACHE: Dict[Tuple[str, int, str], str] = {}


def resolve_upstox_option_key(expiry_str: str, strike: int, option_type: str, token: Optional[str] = None) -> str:
    """
    Resolve Upstox instrument_key for NIFTY option.
    Uses cached mapping or queries Upstox Option Chain API via common.angelone_client.
    """
    cache_key = (expiry_str.upper(), int(strike), option_type.upper())
    if cache_key in _UPSTOX_KEY_CACHE:
        return _UPSTOX_KEY_CACHE[cache_key]

    if token == MOCK_TOKEN or os.getenv("TEST_MODE") == "true" or os.getenv("MOCK_BROKER") == "true":
        key = f"NSE_FO|MOCK_{strike}_{option_type.upper()}"
        _UPSTOX_KEY_CACHE[cache_key] = key
        return key

    try:
        from common.angelone_client import _get_upstox_option_quotes
        quotes = _get_upstox_option_quotes(expiry_str, strike, strike)
        if option_type.upper() == "CE" and "call" in quotes:
            key = quotes["call"].get("instrument_key", "")
            if key:
                _UPSTOX_KEY_CACHE[cache_key] = key
                return key
        elif option_type.upper() == "PE" and "put" in quotes:
            key = quotes["put"].get("instrument_key", "")
            if key:
                _UPSTOX_KEY_CACHE[cache_key] = key
                return key
    except Exception as exc:
        logger.warning("Failed to resolve Upstox key via angelone_client: %s", exc)

    return ""


def _headers_upstox(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _headers_angelone(token: str) -> Dict[str, str]:
    if token == MOCK_TOKEN:
        return {
            "Authorization": f"Bearer {MOCK_TOKEN}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    api_key = os.environ.get("ANGELONE_API_KEY", "")
    return {
        "Authorization": f"Bearer {token}",
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
# Authentication & Access Tokens
# ---------------------------------------------------------------------------

def get_broker_token(broker: str, redis_client=None) -> str:
    """
    Retrieve access token for the specified broker.
    Tuesday strictly supports 'upstox'; Thursday strictly supports 'angelone'.
    """
    broker = (broker or "").lower().strip()
    if os.getenv("TEST_MODE") == "true" or os.getenv("MOCK_BROKER") == "true":
        return MOCK_TOKEN

    if broker == "upstox":
        candidates = ["upstox_access_token", "UPSTOX_ACCESS_TOKEN", "UPSTOX_TOKEN", "upstox_token"]
        r = redis_client or get_redis_client()
        if r:
            for k in candidates:
                try:
                    val = r.get(k)
                    if val:
                        return val
                except Exception:
                    pass
        for k in candidates:
            val = os.getenv(k)
            if val:
                return val
        if os.getenv("TEST_MODE") == "true" or os.getenv("MOCK_BROKER") == "true":
            return MOCK_TOKEN
        raise RuntimeError(f"No access token found for Upstox in Redis or environment.")

    elif broker in ("angelone", "angel"):
        r = redis_client or get_redis_client()
        if r:
            try:
                raw = r.get("angelone_jwt_feed")
                if raw:
                    data = json.loads(raw)
                    jwt = data.get("jwtToken") or data.get("token") or ""
                    if jwt.startswith("Bearer "):
                        jwt = jwt[7:]
                    if jwt:
                        return jwt
            except Exception:
                pass
        for env_var in ("ANGELONE_JWT_TOKEN", "ANGELONE_ACCESS_TOKEN", "ANGEL_JWT_TOKEN"):
            val = os.getenv(env_var)
            if val:
                if val.startswith("Bearer "):
                    val = val[7:]
                return val
        if os.getenv("TEST_MODE") == "true" or os.getenv("MOCK_BROKER") == "true":
            return MOCK_TOKEN
        raise RuntimeError("No access token found for AngelOne in Redis or environment.")

    else:
        raise ValueError(f"Unsupported broker '{broker}'. Must be 'upstox' or 'angelone'.")


# ---------------------------------------------------------------------------
# Broker Margin & Funds Preflight
# ---------------------------------------------------------------------------

def get_available_funds(broker: str, token: str) -> float:
    """
    Query available cash / trading margin for option BUY orders from the broker.
    """
    broker = (broker or "").lower().strip()
    if token == MOCK_TOKEN:
        mock_funds = os.getenv("MOCK_AVAILABLE_FUNDS")
        return float(mock_funds) if mock_funds is not None else 500000.0

    if broker == "upstox":
        url = f"{UPSTOX_BASE}/v2/user/get-funds-and-margin"
        resp = requests.get(url, headers=_headers_upstox(token), timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"Upstox funds check HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Upstox funds API error: {body.get('errors') or body}")
        equity = (body.get("data") or {}).get("equity") or {}
        funds = equity.get("available_margin") or equity.get("available_funds") or 0.0
        return float(funds)

    elif broker in ("angelone", "angel"):
        url = f"{ANGELONE_BASE}/rest/secure/angelbroking/user/v1/getRMS"
        resp = requests.get(url, headers=_headers_angelone(token), timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"AngelOne RMS funds check HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if body.get("status") is not True:
            raise RuntimeError(f"AngelOne RMS API error: {body.get('message') or body}")
        data = body.get("data") or {}
        funds = data.get("availablecash") or data.get("net") or data.get("availablemargin") or 0.0
        return float(funds)

    raise ValueError(f"Unsupported broker '{broker}' for funds check")


def preflight_entry(
    broker: str,
    token: str,
    call_contract: dict,
    put_contract: dict,
    quantity: int,
    call_ltp: float,
    put_ltp: float,
    buffer_pct: float = DEFAULT_BUY_BUFFER_PCT,
    estimated_charges: float = DEFAULT_ESTIMATED_CHARGES,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Validate option contracts, quantities, LTPs, and ensure broker funds are sufficient.
    Returns: (passed, reason, metrics_dict)
    """
    broker = (broker or "").lower().strip()
    lot_size = NIFTY_LOT_SIZE
    if quantity <= 0 or (quantity % lot_size != 0):
        return False, f"Order quantity {quantity} must be a positive whole-number multiple of NIFTY lot size ({lot_size})", {}

    if call_ltp <= 0 or put_ltp <= 0:
        return False, f"Invalid option reference LTPs: CALL={call_ltp}, PUT={put_ltp}", {}

    # Validate contract parameters
    if broker == "upstox":
        call_tok = call_contract.get("instrument_token") or call_contract.get("instrument_key") or call_contract.get("token")
        put_tok = put_contract.get("instrument_token") or put_contract.get("instrument_key") or put_contract.get("token")
        if not call_tok or not put_tok:
            return False, f"Upstox contract missing instrument_token: call={call_tok}, put={put_tok}", {}
        call_tok_str = str(call_tok)
        put_tok_str = str(put_tok)
        if not call_tok_str.startswith("NSE_FO|") or not put_tok_str.startswith("NSE_FO|"):
            return False, f"Invalid Upstox instrument key prefix: call={call_tok}, put={put_tok}", {}
        if "NIFTY_" in call_tok_str or "NIFTY_" in put_tok_str:
            return False, f"Unresolved placeholder Upstox instrument key: call={call_tok}, put={put_tok}", {}
    elif broker in ("angelone", "angel"):
        call_tok = call_contract.get("token") or call_contract.get("symboltoken")
        put_tok = put_contract.get("token") or put_contract.get("symboltoken")
        call_sym = call_contract.get("trading_symbol") or call_contract.get("tradingsymbol")
        put_sym = put_contract.get("trading_symbol") or put_contract.get("tradingsymbol")
        if not call_tok or not put_tok or not call_sym or not put_sym:
            return False, f"AngelOne contract missing token or symbol: call=({call_sym}, {call_tok}), put=({put_sym}, {put_tok})", {}
    else:
        return False, f"Unsupported broker '{broker}'", {}

    # Estimate required funds scaled by lot quantity
    lot_size = NIFTY_LOT_SIZE
    num_lots = max(1, (quantity + lot_size - 1) // lot_size)
    total_charges = estimated_charges * num_lots

    estimated_cost = (call_ltp + put_ltp) * quantity
    buffer_amount = estimated_cost * buffer_pct
    required_funds = estimated_cost + buffer_amount + total_charges

    try:
        available_funds = get_available_funds(broker, token)
    except Exception as exc:
        logger.error("[%s] Funds verification query failed: %s", broker, exc)
        return False, f"Failed to verify broker funds: {exc}", {"required_funds": required_funds}

    metrics = {
        "broker": broker,
        "quantity": quantity,
        "lot_size": lot_size,
        "num_lots": num_lots,
        "call_ltp": call_ltp,
        "put_ltp": put_ltp,
        "estimated_cost": estimated_cost,
        "buffer_pct": buffer_pct,
        "buffer_amount": buffer_amount,
        "estimated_charges": estimated_charges,
        "total_charges": total_charges,
        "required_funds": required_funds,
        "available_funds": available_funds,
    }

    if available_funds < required_funds:
        reason = (
            f"Insufficient funds in {broker}: available ₹{available_funds:.2f} < "
            f"required ₹{required_funds:.2f} (est. premium ₹{estimated_cost:.2f} + "
            f"{buffer_pct*100:.0f}% buffer ₹{buffer_amount:.2f} + ₹{total_charges:.2f} charges across {num_lots} lot(s))"
        )
        return False, reason, metrics

    logger.info(
        "[%s] Preflight passed: Available ₹%.2f >= Required ₹%.2f (est. premium ₹%.2f + buffer ₹%.2f + charges ₹%.2f)",
        broker, available_funds, required_funds, estimated_cost, buffer_amount, total_charges
    )
    return True, "Preflight checks passed", metrics


# ---------------------------------------------------------------------------
# Order Placement & Cancellation
# ---------------------------------------------------------------------------

def place_market_order(
    broker: str,
    token: str,
    contract: dict,
    quantity: int,
    transaction_type: str = "BUY",
    tag: str = "AUTO-BUY",
) -> Dict[str, Any]:
    """
    Submit a single MARKET order (BUY or SELL) to the target broker.
    """
    broker = (broker or "").lower().strip()
    transaction_type = transaction_type.upper()

    if token == MOCK_TOKEN:
        import uuid
        oid = f"MOCK-{transaction_type}-{uuid.uuid4().hex[:8].upper()}"
        return {
            "success": True,
            "order_id": oid,
            "status": "submitted",
            "broker": broker,
            "quantity": quantity,
            "transaction_type": transaction_type,
        }

    if broker == "upstox":
        url = f"{UPSTOX_BASE}/v2/order/place"
        tok = str(contract.get("instrument_token") or contract.get("instrument_key") or contract.get("token") or "")
        payload = {
            "quantity": int(quantity),
            "product": "D",
            "validity": "DAY",
            "price": 0,
            "tag": tag,
            "instrument_token": tok,
            "order_type": "MARKET",
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        resp = requests.post(url, headers=_headers_upstox(token), json=payload, timeout=10)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:300]}
        if 200 <= resp.status_code < 300 and isinstance(body, dict) and body.get("status") == "success":
            oid = str((body.get("data") or {}).get("order_id") or "")
            return {
                "success": True,
                "order_id": oid,
                "status": "submitted",
                "broker": "upstox",
                "quantity": quantity,
                "response": body,
            }
        err = body.get("errors") or body.get("message") or body
        return {
            "success": False,
            "order_id": None,
            "status": "rejected",
            "broker": "upstox",
            "error": str(err),
            "http_status": resp.status_code,
            "response": body,
        }

    elif broker in ("angelone", "angel"):
        url = f"{ANGELONE_BASE}/rest/secure/angelbroking/order/v1/placeOrder"
        sym = str(contract.get("trading_symbol") or contract.get("tradingsymbol") or "")
        tok = str(contract.get("token") or contract.get("symboltoken") or "")
        exch = str(contract.get("exchange") or "NFO").upper()
        payload = {
            "variety": "NORMAL",
            "tradingsymbol": sym,
            "symboltoken": tok,
            "transactiontype": transaction_type,
            "exchange": exch,
            "ordertype": "MARKET",
            "producttype": "CARRYFORWARD",
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(quantity),
        }
        resp = requests.post(url, headers=_headers_angelone(token), json=payload, timeout=10)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:300]}
        if 200 <= resp.status_code < 300 and isinstance(body, dict) and body.get("status") is True:
            oid = str((body.get("data") or {}).get("orderid") or "")
            return {
                "success": True,
                "order_id": oid,
                "status": "submitted",
                "broker": "angelone",
                "quantity": quantity,
                "response": body,
            }
        err = body.get("message") or body.get("errorcode") or body
        return {
            "success": False,
            "order_id": None,
            "status": "rejected",
            "broker": "angelone",
            "error": str(err),
            "http_status": resp.status_code,
            "response": body,
        }

    raise ValueError(f"Unsupported broker '{broker}'")


def cancel_broker_order(
    broker: str,
    token: str,
    order_id: str,
    variety: str = "NORMAL",
) -> Dict[str, Any]:
    """
    Cancel an open / pending order at the broker.
    """
    broker = (broker or "").lower().strip()
    if token == MOCK_TOKEN:
        return {"action": "cancel", "order_id": order_id, "success": True, "http_status": 200}

    if broker == "upstox":
        url = f"{UPSTOX_BASE}/v2/order/cancel"
        resp = requests.delete(url, headers=_headers_upstox(token), params={"order_id": order_id}, timeout=10)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:300]}
        success = 200 <= resp.status_code < 300 and isinstance(body, dict) and body.get("status") == "success"
        return {"action": "cancel", "order_id": order_id, "success": success, "http_status": resp.status_code, "response": body}

    elif broker in ("angelone", "angel"):
        url = f"{ANGELONE_BASE}/rest/secure/angelbroking/order/v1/cancelOrder"
        payload = {"variety": variety, "orderid": str(order_id)}
        resp = requests.post(url, headers=_headers_angelone(token), json=payload, timeout=10)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:300]}
        success = 200 <= resp.status_code < 300 and isinstance(body, dict) and body.get("status") is True
        return {"action": "cancel", "order_id": order_id, "success": success, "http_status": resp.status_code, "response": body}

    raise ValueError(f"Unsupported broker '{broker}'")


# ---------------------------------------------------------------------------
# Order Book Retrieval & Reconciliation
# ---------------------------------------------------------------------------

def fetch_normalized_orders(broker: str, token: str) -> List[Dict[str, Any]]:
    """
    Fetch order book and return normalized list of orders across brokers.
    """
    broker = (broker or "").lower().strip()
    if token == MOCK_TOKEN:
        return []

    if broker == "upstox":
        url = f"{UPSTOX_BASE}/v2/order/retrieve-all"
        resp = requests.get(url, headers=_headers_upstox(token), timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"Upstox order book HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        raw_list = body.get("data") or []
        normalized = []
        for o in raw_list:
            oid = str(o.get("order_id") or "")
            st = normalize_status(o.get("status"))
            fq = int(_to_float(o.get("filled_quantity", 0)))
            tq = int(_to_float(o.get("quantity", 0)))
            avg_p = _to_float(o.get("average_price") or o.get("price"))
            normalized.append({
                "order_id": oid,
                "status": st,
                "raw_status": str(o.get("status")),
                "filled_quantity": fq,
                "quantity": tq,
                "average_price": avg_p,
                "transaction_type": str(o.get("transaction_type") or "").upper(),
                "instrument_token": str(o.get("instrument_token") or ""),
                "symbol": str(o.get("trading_symbol") or o.get("tradingsymbol") or ""),
                "raw": o,
            })
        return normalized

    elif broker in ("angelone", "angel"):
        url = f"{ANGELONE_BASE}/rest/secure/angelbroking/order/v1/getOrderBook"
        resp = requests.get(url, headers=_headers_angelone(token), timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"AngelOne order book HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        raw_list = body.get("data") or []
        normalized = []
        for o in raw_list:
            oid = str(o.get("orderid") or "")
            st = normalize_status(o.get("status"))
            fq = int(_to_float(o.get("filledshares") or o.get("filled_quantity", 0)))
            tq = int(_to_float(o.get("quantity", 0)))
            avg_p = _to_float(o.get("averageprice") or o.get("price"))
            normalized.append({
                "order_id": oid,
                "status": st,
                "raw_status": str(o.get("status")),
                "filled_quantity": fq,
                "quantity": tq,
                "average_price": avg_p,
                "transaction_type": str(o.get("transactiontype") or "").upper(),
                "symboltoken": str(o.get("symboltoken") or ""),
                "symbol": str(o.get("tradingsymbol") or ""),
                "raw": o,
            })
        return normalized

    raise ValueError(f"Unsupported broker '{broker}'")


def poll_order_fills(
    broker: str,
    token: str,
    order_ids: List[str],
    target_qty: int,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SEC,
    order_book_provider: Optional[Any] = None,
) -> Tuple[bool, Dict[str, dict]]:
    """
    Poll broker order book until all order IDs reach terminal filled status,
    a terminal rejection occurs, or timeout is reached.
    """
    deadline = time.time() + timeout_sec
    orders_map: Dict[str, dict] = {}

    while time.time() < deadline:
        try:
            if order_book_provider:
                orders = order_book_provider()
            else:
                orders = fetch_normalized_orders(broker, token)
        except Exception as exc:
            logger.warning("[%s] Order book fetch failed during verification: %s", broker, exc)
            time.sleep(poll_interval)
            continue

        orders_by_id = {o["order_id"]: o for o in orders if o.get("order_id")}
        for oid in order_ids:
            if oid in orders_by_id:
                orders_map[oid] = orders_by_id[oid]

        # Check if all orders reached complete
        all_filled = True
        has_terminal_failure = False

        for oid in order_ids:
            ord_info = orders_map.get(oid)
            if not ord_info:
                all_filled = False
                continue
            status = ord_info.get("status")
            filled_qty = ord_info.get("filled_quantity", 0)

            if status in ("rejected", "cancelled"):
                has_terminal_failure = True
                all_filled = False
                break
            elif status == "complete" and filled_qty >= target_qty:
                continue
            else:
                all_filled = False

        if all_filled:
            return True, orders_map

        if has_terminal_failure:
            logger.warning("[%s] One or more orders rejected/cancelled during polling", broker)
            return False, orders_map

        time.sleep(poll_interval)

    # Polling timed out
    logger.warning("[%s] Order fill verification timed out after %.1fs", broker, timeout_sec)
    return False, orders_map


# ---------------------------------------------------------------------------
# Position Verification & Flatness Checks
# ---------------------------------------------------------------------------

def fetch_broker_positions(broker: str, token: str) -> List[Dict[str, Any]]:
    """
    Fetch raw/normalized position records from the designated broker.
    """
    broker = (broker or "").lower().strip()
    if token == MOCK_TOKEN:
        return []

    if broker == "upstox":
        url = f"{UPSTOX_BASE}/v2/portfolio/short-term-positions"
        resp = requests.get(url, headers=_headers_upstox(token), timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"Upstox positions HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Upstox positions API error: {body.get('errors') or body}")
        return body.get("data") or []

    elif broker in ("angelone", "angel"):
        url = f"{ANGELONE_BASE}/rest/secure/angelbroking/order/v1/getPosition"
        resp = requests.get(url, headers=_headers_angelone(token), timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"AngelOne positions HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if body.get("status") is False:
            raise RuntimeError(f"AngelOne positions API error: {body.get('message') or body}")
        return body.get("data") or []

    raise ValueError(f"Unsupported broker '{broker}' for positions check")


def verify_positions_flat(
    broker: str,
    positions: List[Dict[str, Any]],
    call_contract: dict,
    put_contract: dict,
    baseline_call_qty: int = 0,
    baseline_put_qty: int = 0,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Verify that net positions for both CALL and PUT contracts match expected baseline (strictly 0 by default).
    Any positive (residual long) or negative (short) net exposure relative to baseline is strictly NOT flat.
    Returns (is_flat, details_dict).
    """
    call_tokens = {
        str(call_contract.get("instrument_token") or ""),
        str(call_contract.get("instrument_key") or ""),
        str(call_contract.get("token") or ""),
        str(call_contract.get("symboltoken") or ""),
    } - {"", "None"}
    put_tokens = {
        str(put_contract.get("instrument_token") or ""),
        str(put_contract.get("instrument_key") or ""),
        str(put_contract.get("token") or ""),
        str(put_contract.get("symboltoken") or ""),
    } - {"", "None"}

    call_symbols = {
        str(call_contract.get("trading_symbol") or "").upper(),
        str(call_contract.get("tradingsymbol") or "").upper(),
    } - {"", "NONE"}
    put_symbols = {
        str(put_contract.get("trading_symbol") or "").upper(),
        str(put_contract.get("tradingsymbol") or "").upper(),
    } - {"", "NONE"}

    call_net = 0
    put_net = 0

    for pos in positions:
        pos_token = str(pos.get("instrument_token") or pos.get("instrument_key") or pos.get("symboltoken") or pos.get("token") or "")
        pos_sym = str(pos.get("trading_symbol") or pos.get("tradingsymbol") or "").upper()

        qty = _to_float(
            pos.get("net_quantity") if pos.get("net_quantity") is not None
            else pos.get("netqty") if pos.get("netqty") is not None
            else pos.get("quantity") if pos.get("quantity") is not None
            else 0
        )

        is_call = (pos_token in call_tokens) or (bool(pos_sym) and pos_sym in call_symbols)
        is_put = (pos_token in put_tokens) or (bool(pos_sym) and pos_sym in put_symbols)

        if is_call:
            call_net += int(qty)
        if is_put:
            put_net += int(qty)

    is_flat = (call_net == baseline_call_qty and put_net == baseline_put_qty)
    return is_flat, {
        "call_net": call_net,
        "put_net": put_net,
        "baseline_call_qty": baseline_call_qty,
        "baseline_put_qty": baseline_put_qty,
        "is_flat": is_flat,
    }


# ---------------------------------------------------------------------------
# Emergency Unwind Failure Alerting
# ---------------------------------------------------------------------------

def dispatch_unwind_failure_alert(
    broker: str,
    track: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Dispatch critical Discord alert when paired entry fails AND unwinding recovery SELLs fail.
    Requires immediate operator manual intervention.
    """
    unwind_data = details or {}
    logger.critical("[%s] EMERGENCY: Dispatching unwind failure alert! Details: %s", broker, unwind_data)
    try:
        from common.profit_monitor import resolve_discord_peak_url
        webhook_url = resolve_discord_peak_url(track=track)
    except Exception:
        webhook_url = os.getenv("DISCORD_URL", "").strip()

    if not webhook_url:
        logger.error("[%s] Cannot send unwind failure alert: No Discord webhook URL configured", broker)
        return False

    time_ist = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    headline = f"🚨 **[CRITICAL ALERT: UNWIND ROLLBACK FAILED — {broker.upper()}]** Immediate Manual Intervention Required!"
    embed = {
        "title": f"⚠️ Unwind Rollback Incomplete on {broker.upper()}",
        "color": 0xFF0000,
        "description": "A paired MARKET BUY order experienced partial execution, but the recovery MARKET SELL orders **FAILED** to fully square off the position. You may have an unhedged open long position!",
        "fields": [
            {
                "name": "Broker & Track",
                "value": f"**Broker:** `{broker}`\n**Track:** `{track or 'Unknown'}`",
                "inline": True,
            },
            {
                "name": "Affected Quantities",
                "value": f"CALL Filled: `{unwind_data.get('call_filled', 0)}`\nPUT Filled: `{unwind_data.get('put_filled', 0)}`",
                "inline": True,
            },
            {
                "name": "Rollback Status",
                "value": f"Unwind Successful: `False`\nReason: `{unwind_data.get('error', 'Recovery SELL orders failed or incomplete')}`",
                "inline": False,
            },
            {
                "name": "Sell Order Details",
                "value": json.dumps(unwind_data.get("sell_results", []), indent=2)[:900],
                "inline": False,
            },
        ],
        "footer": {"text": f"Antigravity Paired Order Safety System • {time_ist}"},
    }
    if unwind_data.get("buy_failure_reason"):
        embed["fields"].append({
            "name": "Original BUY Failure Details",
            "value": f"```{str(unwind_data['buy_failure_reason'])[:400]}```",
            "inline": False,
        })
    payload = {"content": headline, "embeds": [embed]}
    try:
        resp = requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        return 200 <= resp.status_code < 300
    except Exception as exc:
        logger.error("[%s] Failed to send unwind failure webhook: %s", broker, exc)
        return False


def _alert_outbox_track(track: Optional[str], broker: Optional[str] = None) -> str:
    """Normalize broker/track labels to the collector's outbox partition."""
    value = str(track or "").lower()
    broker_value = str(broker or "").lower()
    if "thu" in value or "3" in value or broker_value in ("angelone", "angel"):
        return "3day_strategy"
    return "5day_strategy"


def _connect_order_alert_outbox() -> sqlite3.Connection:
    db_path = os.getenv("ORDER_ALERT_OUTBOX_DB_PATH", ORDER_ALERT_OUTBOX_DB_PATH)
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_order_alerts (
            alert_id TEXT PRIMARY KEY,
            track TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_ts INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            lease_until INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return conn


def _enqueue_order_alert(alert_id: str, track: str, alert_type: str, payload: Dict[str, Any]) -> str:
    """Persist the alert before attempting Discord delivery; return queued/exists/error."""
    conn = None
    try:
        conn = _connect_order_alert_outbox()
        with conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO pending_order_alerts
                    (alert_id, track, alert_type, payload, created_ts, lease_until)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    track,
                    alert_type,
                    json.dumps(payload, default=str),
                    int(time.time()),
                    int(time.time()) + 60,
                ),
            )
            return "queued" if cursor.rowcount else "exists"
    except Exception as exc:
        logger.error("Failed to persist pending order alert %s: %s", alert_id, exc)
        return "error"
    finally:
        if conn is not None:
            conn.close()


def _finish_pending_order_alert(alert_id: str, delivered: bool, error: str = "") -> None:
    conn = None
    try:
        conn = _connect_order_alert_outbox()
        with conn:
            if delivered:
                conn.execute("DELETE FROM pending_order_alerts WHERE alert_id = ?", (alert_id,))
            else:
                conn.execute(
                    """
                    UPDATE pending_order_alerts
                    SET attempts = attempts + 1, last_error = ?, lease_until = 0
                    WHERE alert_id = ?
                    """,
                    (error[:500], alert_id),
                )
    except Exception as exc:
        logger.error("Failed to update pending order alert %s: %s", alert_id, exc)
    finally:
        if conn is not None:
            conn.close()


def dispatch_unwind_failure_alert_with_retry(
    broker: str,
    track: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> bool:
    """Queue and immediately attempt an emergency unwind alert, retaining failures for retry."""
    alert_id = f"unwind-{uuid.uuid4().hex}"
    payload = {"broker": broker, "track": track, "details": details or {}}
    queued = _enqueue_order_alert(
        alert_id,
        _alert_outbox_track(track, broker),
        "unwind_failure",
        payload,
    )
    delivered = dispatch_unwind_failure_alert(broker=broker, track=track, details=details)
    if queued == "queued":
        _finish_pending_order_alert(alert_id, delivered, "Discord delivery failed" if not delivered else "")
    return delivered


def dispatch_order_failure_with_retry(
    notification_id: str,
    **alert_kwargs: Any,
) -> bool:
    """Queue and immediately attempt an order-failure alert without replaying any order."""
    alert_id = str(notification_id)
    track = _alert_outbox_track(alert_kwargs.get("track"), alert_kwargs.get("broker"))
    queued = _enqueue_order_alert(alert_id, track, "order_failure", alert_kwargs)
    if queued == "exists":
        logger.info("Order failure alert %s remains pending; deferring to outbox retry", alert_id)
        return False

    delivered = dispatch_order_failure_discord(**alert_kwargs)
    if queued == "queued":
        _finish_pending_order_alert(alert_id, delivered, "Discord delivery failed" if not delivered else "")
    return delivered


def retry_pending_order_alerts(track: Optional[str] = None, limit: int = 20) -> int:
    """Retry queued Discord notifications only; this function never submits broker orders."""
    now = int(time.time())
    claimed: List[Tuple[str, str, str]] = []
    conn = None
    try:
        conn = _connect_order_alert_outbox()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if track:
                rows = conn.execute(
                    """
                    SELECT alert_id, alert_type, payload FROM pending_order_alerts
                    WHERE track = ? AND lease_until <= ?
                    ORDER BY created_ts LIMIT ?
                    """,
                    (_alert_outbox_track(track), now, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT alert_id, alert_type, payload FROM pending_order_alerts
                    WHERE lease_until <= ? ORDER BY created_ts LIMIT ?
                    """,
                    (now, max(1, int(limit))),
                ).fetchall()
            for alert_id, alert_type, payload in rows:
                conn.execute(
                    "UPDATE pending_order_alerts SET lease_until = ? WHERE alert_id = ?",
                    (now + 60, alert_id),
                )
                claimed.append((alert_id, alert_type, payload))
    except Exception as exc:
        logger.error("Failed to read pending order alert outbox: %s", exc)
        return 0
    finally:
        if conn is not None:
            conn.close()

    delivered_count = 0
    for alert_id, alert_type, raw_payload in claimed:
        try:
            payload = json.loads(raw_payload)
            if alert_type == "unwind_failure":
                delivered = dispatch_unwind_failure_alert(**payload)
            elif alert_type == "order_failure":
                delivered = dispatch_order_failure_discord(**payload)
            else:
                logger.error("Unknown pending order alert type %r for %s", alert_type, alert_id)
                _finish_pending_order_alert(alert_id, False, "Unknown alert type")
                continue
            _finish_pending_order_alert(
                alert_id,
                delivered,
                "Discord delivery failed" if not delivered else "",
            )
            delivered_count += int(bool(delivered))
        except Exception as exc:
            logger.exception("Failed retrying pending order alert %s: %s", alert_id, exc)
            _finish_pending_order_alert(alert_id, False, str(exc))
    return delivered_count


def dispatch_order_failure_discord(
    broker: str,
    action: str,
    track: Optional[str] = None,
    symbol: Optional[str] = None,
    quantity: int = 0,
    error_reason: str = "",
    details: Optional[Dict[str, Any]] = None,
    redis_client: Optional[Any] = None,
    cooldown_seconds: int = 600,
) -> bool:
    """
    Dispatch critical Discord alert for order execution failures (BUY, SELL_MARKET, SELL_SLM).
    Includes Redis anti-spam cooldown protection to prevent repeating alerts on 1-min cron loops.

    Args:
        broker: 'upstox' or 'angelone'
        action: 'PAIRED_BUY', 'SELL_MARKET', 'SELL_SLM_PLACE', 'SELL_SLM_MODIFY'
        track: '5day_strategy' / 'tuesday' or '3day_strategy' / 'thursday'
        symbol: Trading symbol or strike description
        quantity: Order quantity in contracts
        error_reason: Detailed error message or status from broker
        details: Optional dict with raw response or metrics
        redis_client: Redis connection to enforce cooldown (if provided)
        cooldown_seconds: Cooldown TTL in seconds (default: 600s = 10 mins)

    Returns:
        bool: True if alert was sent, False if suppressed by cooldown or failed
    """
    broker_clean = (broker or "unknown").lower().strip()
    track_clean = (track or "unknown").lower().strip()
    symbol_clean = (symbol or "all").replace(" ", "_").strip()
    action_clean = (action or "order").upper().strip()

    # 1. Resolve webhook URL first before claiming any cooldown
    try:
        from common.profit_monitor import resolve_discord_peak_url
        webhook_url = resolve_discord_peak_url(track=track)
    except Exception:
        webhook_url = os.getenv("DISCORD_URL", "").strip()

    if not webhook_url:
        logger.error("[%s] Cannot send order failure alert: No Discord webhook URL configured", broker_clean)
        return False

    # 2. Check if active cooldown exists in Redis
    cooldown_key = None
    if redis_client is not None:
        cooldown_key = f"fno:alert:fail:{broker_clean}:{track_clean}:{symbol_clean}:{action_clean}"
        try:
            if redis_client.get(cooldown_key):
                logger.info(
                    "[%s] Order failure alert suppressed by active anti-spam cooldown (%ds): %s",
                    broker_clean, cooldown_seconds, cooldown_key
                )
                return False
        except Exception as exc:
            logger.warning("[%s] Redis cooldown check failed: %s; proceeding to alert", broker_clean, exc)

    time_ist = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    action_titles = {
        "PAIRED_BUY": "PAIRED MARKET BUY ORDER REJECTED",
        "SELL_MARKET": "EXIT MARKET SELL ORDER REJECTED",
        "SELL_MARKET_TERMINAL_REJECTED": "IN-FLIGHT MARKET EXIT TERMINATED WITHOUT FILL",
        "SELL_SLM_PLACE": "TRAILING SL-M ORDER PLACEMENT REJECTED",
        "SELL_SLM_MODIFY": "TRAILING SL-M RATCHET MODIFICATION FAILED",
        "SELL_SLM_TERMINAL_REJECTED": "IN-FLIGHT SL-M PROTECTION TERMINATED WITHOUT FILL",
        "SELL_SLM_CANCEL_FAILED": "IMMEDIATE EXIT BLOCKED: SL-M CANCELLATION FAILED",
        "IMMEDIATE_EXIT_BLOCKED": "IMMEDIATE EXIT BLOCKED: UNCONFIRMED SL-M STATE",
    }
    title_text = action_titles.get(action_clean, f"{action_clean} ORDER FAILED")

    is_terminal_alert = action_clean in ("SELL_MARKET_TERMINAL_REJECTED", "SELL_SLM_TERMINAL_REJECTED")
    terminal_status = ""
    if is_terminal_alert:
        terminal_status = str((details or {}).get("broker_status") or "").strip().lower()
        if terminal_status == "canceled":
            terminal_status = "cancelled"
        order_label = "MARKET EXIT" if action_clean == "SELL_MARKET_TERMINAL_REJECTED" else "SL-M PROTECTION"
        status_titles = {
            "rejected": f"IN-FLIGHT {order_label} REJECTED BY BROKER",
            "cancelled": f"IN-FLIGHT {order_label} CANCELLED WITHOUT FILL",
            "expired": f"IN-FLIGHT {order_label} EXPIRED WITHOUT FILL",
        }
        title_text = status_titles.get(terminal_status, f"IN-FLIGHT {order_label} TERMINATED WITHOUT FILL")

    if is_terminal_alert and terminal_status == "rejected":
        headline = f"🚨 **[BROKER ORDER REJECTED — {broker_clean.upper()}]** Immediate Manual Review Required!"
    elif is_terminal_alert:
        headline = f"🚨 **[BROKER ORDER TERMINATED WITHOUT FILL — {broker_clean.upper()}]** Immediate Manual Review Required!"
    else:
        headline = f"🚨 **[BROKER ORDER REJECTION — {broker_clean.upper()}]** Immediate Manual Review Required!"
    embed = {
        "title": f"⚠️ {title_text} on {broker_clean.upper()}",
        "color": 0xFF0000 if ("MARKET" in action_clean or "BUY" in action_clean or "CANCEL" in action_clean or "BLOCKED" in action_clean) else 0xF59E0B,
        "description": (
            f"An automated order management action **{terminal_status.upper()} without fill**.\n"
            if is_terminal_alert and terminal_status and terminal_status != "rejected"
            else "An automated order management action **FAILED** at the broker level.\n"
        ) + f"**Action:** `{action_clean}`\n" + (
            f"**Broker Status:** `{terminal_status}`\n" if is_terminal_alert else ""
        ) + f"**Anti-Spam Shield:** Throttled for {cooldown_seconds // 60} minutes once delivered.",
        "fields": [
            {
                "name": "Broker & Strategy",
                "value": f"**Broker:** `{broker_clean.upper()}`\n**Track:** `{track or 'Unknown'}`",
                "inline": True,
            },
            {
                "name": "Contract & Quantity",
                "value": f"**Symbol:** `{symbol or 'N/A'}`\n**Quantity:** `{quantity}`",
                "inline": True,
            },
            {
                "name": "Failure Reason",
                "value": f"```{error_reason[:400]}```",
                "inline": False,
            },
        ],
        "footer": {"text": f"Antigravity Order Safety System • {time_ist}"},
    }
    if details:
        details_str = json.dumps(details, default=str, indent=2)[:500]
        embed["fields"].append({
            "name": "Diagnostic Payload",
            "value": f"```{details_str}```",
            "inline": False,
        })

    payload = {"content": headline, "embeds": [embed]}
    try:
        resp = requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        success = (200 <= resp.status_code < 300)
        if success:
            # Claim cooldown only AFTER successful delivery
            if redis_client is not None and cooldown_key:
                try:
                    redis_client.set(cooldown_key, "1", ex=cooldown_seconds)
                except Exception as exc:
                    logger.warning("[%s] Failed to set Redis cooldown key: %s", broker_clean, exc)
            return True
        else:
            logger.error("[%s] Discord webhook returned status %s: %s", broker_clean, resp.status_code, resp.text[:200])
            return False
    except Exception as exc:
        logger.error("[%s] Failed to send order failure webhook: %s", broker_clean, exc)
        return False


def clear_order_failure_cooldown(
    redis_client: Any,
    broker: str,
    track: Optional[str] = None,
    symbol: Optional[str] = None,
    action: Optional[str] = None,
) -> None:
    """Clear failure alert cooldown in Redis when position closes or recovers."""
    if not redis_client:
        return
    try:
        b = (broker or "*").lower()
        t = (track or "*").lower()
        s = (symbol or "*").replace(" ", "_")
        a = (action or "*").upper()
        pattern = f"fno:alert:fail:{b}:{t}:{s}:{a}"
        keys = list(redis_client.scan_iter(pattern, count=50))
        if keys:
            redis_client.delete(*keys)
            logger.info("Cleared %d order failure alert cooldown key(s) matching %s", len(keys), pattern)
    except Exception as exc:
        logger.warning("Error clearing order failure cooldown keys: %s", exc)


# ---------------------------------------------------------------------------
# Atomic Rollback (Cancel open BUY + MARKET SELL filled)
# ---------------------------------------------------------------------------

def rollback_partial_entry(
    broker: str,
    token: str,
    call_contract: dict,
    put_contract: dict,
    target_qty: int,
    call_order_id: Optional[str] = None,
    put_order_id: Optional[str] = None,
    call_order: Optional[dict] = None,
    put_order: Optional[dict] = None,
    tag: str = "AUTO-BUY-ROLLBACK",
    order_book_provider: Optional[Any] = None,
    position_provider: Optional[Any] = None,
    unwind_timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    track: Optional[str] = None,
    baseline_call_qty: int = 0,
    baseline_put_qty: int = 0,
    buy_failure_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Rollback an incomplete paired entry:
    1. Immediately cancel any still-open BUY order quantity on either leg.
    2. Poll order book until BUY orders reach terminal status (complete, cancelled, rejected).
    3. Ascertain exact filled quantities from terminal orders.
    4. For any quantity already filled on either leg, submit immediate MARKET SELL.
    5. Verify recovery MARKET SELL fills via order book polling.
    6. Verify that broker positions are flat for both legs.
    7. Dispatch critical Discord alert if unwinding is incomplete, BUY cancel times out, or position remains open.
    """
    broker = (broker or "").lower().strip()
    logger.critical(
        "[%s] INITIATING ATOMIC ROLLBACK: CALL OID=%s (filled=%s), PUT OID=%s (filled=%s)",
        broker, call_order_id, (call_order or {}).get("filled_quantity", 0),
        put_order_id, (put_order or {}).get("filled_quantity", 0)
    )

    cancel_results = []
    sell_results = []

    # Step 1: Cancel open BUY orders
    for oid, ord_data in ((call_order_id, call_order), (put_order_id, put_order)):
        if not oid:
            continue
        status = normalize_status((ord_data or {}).get("status", "unknown"))
        if status not in ("complete", "rejected", "cancelled"):
            try:
                res = cancel_broker_order(broker, token, oid, variety="NORMAL")
                cancel_results.append(res)
                logger.info("[%s] Cancel request for BUY %s result: %s", broker, oid, res.get("success"))
            except Exception as exc:
                logger.error("[%s] Failed to cancel open BUY %s: %s", broker, oid, exc)
                cancel_results.append({"order_id": oid, "success": False, "error": str(exc)})

    # Step 2: Poll order book until BOTH BUY orders reach terminal status
    fresh_call = call_order or {}
    fresh_put = put_order or {}
    terminal_statuses = {"complete", "rejected", "cancelled"}
    poll_start = time.monotonic()
    buy_cancellation_confirmed = True

    while True:
        try:
            if order_book_provider:
                fresh_orders = order_book_provider() if callable(order_book_provider) else order_book_provider
            else:
                fresh_orders = fetch_normalized_orders(broker, token)
            for o in fresh_orders:
                if call_order_id and o.get("order_id") == call_order_id:
                    fresh_call = o
                if put_order_id and o.get("order_id") == put_order_id:
                    fresh_put = o
        except Exception as exc:
            logger.warning("[%s] Failed to re-query order book during rollback: %s", broker, exc)

        call_st = normalize_status(fresh_call.get("status")) if call_order_id else "cancelled"
        put_st = normalize_status(fresh_put.get("status")) if put_order_id else "cancelled"

        if (not call_order_id or call_st in terminal_statuses) and (not put_order_id or put_st in terminal_statuses):
            break

        if (time.monotonic() - poll_start) >= unwind_timeout_sec:
            logger.critical(
                "[%s] Timeout waiting for BUY order cancellation to become terminal: CALL=%s (%s), PUT=%s (%s)",
                broker, call_order_id, call_st, put_order_id, put_st
            )
            buy_cancellation_confirmed = False
            break

        time.sleep(DEFAULT_POLL_INTERVAL_SEC)

    call_filled = int(fresh_call.get("filled_quantity", 0))
    put_filled = int(fresh_put.get("filled_quantity", 0))

    # Step 3: Unwind filled quantities with MARKET SELL
    if call_filled > 0:
        logger.warning("[%s] Unwinding CALL filled quantity %d via MARKET SELL", broker, call_filled)
        try:
            sell_res = place_market_order(
                broker=broker,
                token=token,
                contract=call_contract,
                quantity=call_filled,
                transaction_type="SELL",
                tag=tag,
            )
            sell_results.append({"leg": "CALL", "quantity": call_filled, **sell_res})
        except Exception as exc:
            logger.critical("[%s] FAILED to submit unwind SELL for CALL (%d qty): %s", broker, call_filled, exc)
            sell_results.append({"leg": "CALL", "success": False, "error": str(exc), "quantity": call_filled})

    if put_filled > 0:
        logger.warning("[%s] Unwinding PUT filled quantity %d via MARKET SELL", broker, put_filled)
        try:
            sell_res = place_market_order(
                broker=broker,
                token=token,
                contract=put_contract,
                quantity=put_filled,
                transaction_type="SELL",
                tag=tag,
            )
            sell_results.append({"leg": "PUT", "quantity": put_filled, **sell_res})
        except Exception as exc:
            logger.critical("[%s] FAILED to submit unwind SELL for PUT (%d qty): %s", broker, put_filled, exc)
            sell_results.append({"leg": "PUT", "success": False, "error": str(exc), "quantity": put_filled})

    # Step 4: Verify recovery SELL fills via polling
    sells_confirmed = True
    sell_order_ids = [s.get("order_id") for s in sell_results if s.get("order_id") and s.get("success")]
    unwind_orders_map = {}

    # If any leg needed unwinding but its sell order failed submission:
    if (call_filled > 0 and not any(s.get("leg") == "CALL" and s.get("success") for s in sell_results)) or \
       (put_filled > 0 and not any(s.get("leg") == "PUT" and s.get("success") for s in sell_results)):
        sells_confirmed = False

    if sell_order_ids and sells_confirmed:
        logger.info("[%s] Polling recovery SELL fills for order IDs: %s", broker, sell_order_ids)
        _, unwind_orders_map = poll_order_fills(
            broker=broker,
            token=token,
            order_ids=sell_order_ids,
            target_qty=0,
            timeout_sec=unwind_timeout_sec,
            poll_interval=DEFAULT_POLL_INTERVAL_SEC,
            order_book_provider=order_book_provider,
        )
        for s in sell_results:
            oid = s.get("order_id")
            if not oid:
                sells_confirmed = False
                continue
            poll_info = unwind_orders_map.get(oid, {})
            s["poll_info"] = poll_info
            status = poll_info.get("status")
            filled_qty = int(poll_info.get("filled_quantity", 0))
            required_unwind_qty = int(s.get("quantity", 0))
            if status != "complete" or filled_qty < required_unwind_qty:
                logger.critical(
                    "[%s] Recovery SELL order %s NOT complete: status=%s, filled=%d/%d",
                    broker, oid, status, filled_qty, required_unwind_qty
                )
                sells_confirmed = False

    # Step 5: Verify broker position flatness
    positions_confirmed_flat = True
    pos_details = {}
    try:
        if position_provider:
            raw_positions = position_provider() if callable(position_provider) else position_provider
        else:
            raw_positions = fetch_broker_positions(broker, token)
        positions_confirmed_flat, pos_details = verify_positions_flat(
            broker=broker,
            positions=raw_positions,
            call_contract=call_contract,
            put_contract=put_contract,
            baseline_call_qty=baseline_call_qty,
            baseline_put_qty=baseline_put_qty,
        )
        if not positions_confirmed_flat:
            logger.critical(
                "[%s] Broker position verification failed: Non-zero or short exposure detected! Details: %s",
                broker, pos_details
            )
    except Exception as exc:
        logger.critical("[%s] Exception querying broker positions during rollback verification: %s", broker, exc)
        positions_confirmed_flat = False
        pos_details = {"error": str(exc)}

    # Step 6: Determine overall unwind success
    unwind_successful = bool(
        buy_cancellation_confirmed
        and sells_confirmed
        and positions_confirmed_flat
    )

    if not unwind_successful:
        logger.critical("[%s] Rollback unwind INCOMPLETE or FAILED! Triggering emergency alert.", broker)
        fail_reasons = []
        if not buy_cancellation_confirmed:
            fail_reasons.append("BUY order cancellation not confirmed terminal")
        if not sells_confirmed:
            fail_reasons.append("One or more recovery SELL orders failed to fill completely")
        if not positions_confirmed_flat:
            fail_reasons.append(f"Residual broker positions not flat: {pos_details}")
        combined_reason = "; ".join(fail_reasons) or "Unwind rollback failed"

        dispatch_unwind_failure_alert_with_retry(
            broker=broker,
            track=track,
            details={
                "call_filled": call_filled,
                "put_filled": put_filled,
                "sell_results": sell_results,
                "unwind_orders_map": unwind_orders_map,
                "buy_cancellation_confirmed": buy_cancellation_confirmed,
                "sells_confirmed": sells_confirmed,
                "positions_flat": positions_confirmed_flat,
                "position_details": pos_details,
                "error": combined_reason,
                "buy_failure_reason": buy_failure_reason,
            },
        )

    return {
        "broker": broker,
        "call_order_id": call_order_id,
        "put_order_id": put_order_id,
        "call_filled": call_filled,
        "put_filled": put_filled,
        "cancel_results": cancel_results,
        "sell_results": sell_results,
        "unwind_orders_map": unwind_orders_map,
        "positions_flat": positions_confirmed_flat,
        "position_details": pos_details,
        "unwind_successful": unwind_successful,
    }


# ---------------------------------------------------------------------------
# Paired Market Entry Orchestrator
# ---------------------------------------------------------------------------

def execute_paired_market_buy(
    broker: str,
    call_contract: dict,
    put_contract: dict,
    quantity: int,
    call_ltp: float,
    put_ltp: float,
    buffer_pct: float = DEFAULT_BUY_BUFFER_PCT,
    estimated_charges: float = DEFAULT_ESTIMATED_CHARGES,
    tag: str = "AUTO-BUY",
    token: Optional[str] = None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    order_book_provider: Optional[Any] = None,
    position_provider: Optional[Any] = None,
    baseline_call_qty: int = 0,
    baseline_put_qty: int = 0,
    snapshot_baseline: bool = False,
) -> Dict[str, Any]:
    """
    Orchestrate full paired MARKET BUY execution:
    1. Validate broker routing.
    2. Preflight funds and contracts (skip if insufficient).
    3. Submit back-to-back MARKET BUY orders.
    4. Rapid verification loop.
    5. Atomic rollback if either leg fails.
    """
    broker = (broker or "").lower().strip()
    if broker not in ("upstox", "angelone", "angel"):
        raise ValueError(f"Unsupported broker '{broker}'. Must be 'upstox' or 'angelone'.")

    # 1. Acquire Token
    try:
        active_token = token or get_broker_token(broker)
    except Exception as exc:
        logger.error("[%s] Failed to get broker token: %s", broker, exc)
        return {
            "success": False,
            "status": "AUTH_FAILED",
            "broker": broker,
            "error": str(exc),
            "message": f"Authentication token retrieval failed: {exc}",
        }

    # 2. Preflight Validation
    passed, reason, metrics = preflight_entry(
        broker=broker,
        token=active_token,
        call_contract=call_contract,
        put_contract=put_contract,
        quantity=quantity,
        call_ltp=call_ltp,
        put_ltp=put_ltp,
        buffer_pct=buffer_pct,
        estimated_charges=estimated_charges,
    )
    if not passed:
        logger.warning("[%s] Paired BUY preflight rejected: %s", broker, reason)
        return {
            "success": False,
            "status": "SKIPPED_INSUFFICIENT_FUNDS" if "Insufficient funds" in reason else "PREFLIGHT_FAILED",
            "broker": broker,
            "quantity": quantity,
            "call_ref_ltp": call_ltp,
            "put_ref_ltp": put_ltp,
            "metrics": metrics,
            "error": reason,
            "message": f"Preflight rejected: {reason}",
        }

    # 2b. Pre-entry position baseline: when both strategy tracks share a broker account,
    # the same contract may already be held by the other track. Rollback flatness must be
    # verified relative to that pre-existing quantity, not against zero.
    if snapshot_baseline:
        try:
            if position_provider:
                pre_positions = position_provider() if callable(position_provider) else position_provider
            else:
                pre_positions = fetch_broker_positions(broker, active_token)
            _, pre_details = verify_positions_flat(
                broker=broker,
                positions=pre_positions,
                call_contract=call_contract,
                put_contract=put_contract,
            )
            baseline_call_qty = int(pre_details.get("call_net", 0))
            baseline_put_qty = int(pre_details.get("put_net", 0))
            if baseline_call_qty or baseline_put_qty:
                logger.info(
                    "[%s] Pre-entry baseline: CALL net=%d, PUT net=%d already held in account (other strategy track)",
                    broker, baseline_call_qty, baseline_put_qty,
                )
        except Exception as exc:
            logger.warning("[%s] Could not snapshot pre-entry positions for rollback baseline: %s", broker, exc)

    # 3. Submit Paired Orders Back-to-Back
    logger.info(
        "[%s] Submitting back-to-back MARKET BUY orders for %d qty: CALL @ ref ₹%.2f, PUT @ ref ₹%.2f",
        broker, quantity, call_ltp, put_ltp
    )

    call_res = {}
    put_res = {}
    try:
        call_res = place_market_order(
            broker=broker,
            token=active_token,
            contract=call_contract,
            quantity=quantity,
            transaction_type="BUY",
            tag=tag,
        )
    except Exception as exc:
        logger.error("[%s] Exception submitting CALL market buy: %s", broker, exc)
        call_res = {"success": False, "error": str(exc)}

    try:
        put_res = place_market_order(
            broker=broker,
            token=active_token,
            contract=put_contract,
            quantity=quantity,
            transaction_type="BUY",
            tag=tag,
        )
    except Exception as exc:
        logger.error("[%s] Exception submitting PUT market buy: %s", broker, exc)
        put_res = {"success": False, "error": str(exc)}

    call_oid = call_res.get("order_id")
    put_oid = put_res.get("order_id")

    # If either order failed to submit entirely, rollback whatever was submitted
    if not call_res.get("success") or not put_res.get("success") or not call_oid or not put_oid:
        logger.critical(
            "[%s] Paired submission failed: CALL submission=%s (OID=%s), PUT submission=%s (OID=%s)",
            broker, call_res.get("success"), call_oid, put_res.get("success"), put_oid
        )
        rollback_data = rollback_partial_entry(
            broker=broker,
            token=active_token,
            call_contract=call_contract,
            put_contract=put_contract,
            target_qty=quantity,
            call_order_id=call_oid,
            put_order_id=put_oid,
            order_book_provider=order_book_provider,
            position_provider=position_provider,
            track=tag,
            baseline_call_qty=baseline_call_qty,
            baseline_put_qty=baseline_put_qty,
            buy_failure_reason=f"Submission error: CALL={call_res.get('error')}, PUT={put_res.get('error')}",
        )
        all_unwound = rollback_data.get("unwind_successful", False)
        status = "FAILED_ROLLED_BACK" if all_unwound else "UNWIND_FAILED"
        err_msg = f"Submission error: CALL={call_res.get('error')}, PUT={put_res.get('error')}"
        return {
            "success": False,
            "status": status,
            "broker": broker,
            "quantity": quantity,
            "call_ref_ltp": call_ltp,
            "put_ref_ltp": put_ltp,
            "call_order_id": call_oid,
            "put_order_id": put_oid,
            "rollback": rollback_data,
            "error": err_msg,
            "message": err_msg,
        }

    # 4. Rapid Verification Loop (Wait up to timeout_sec for both to complete)
    logger.info("[%s] Verifying fills for CALL OID=%s and PUT OID=%s...", broker, call_oid, put_oid)
    all_filled, orders_map = poll_order_fills(
        broker=broker,
        token=active_token,
        order_ids=[call_oid, put_oid],
        target_qty=quantity,
        timeout_sec=timeout_sec,
        poll_interval=DEFAULT_POLL_INTERVAL_SEC,
        order_book_provider=order_book_provider,
    )

    call_ord = orders_map.get(call_oid, {})
    put_ord = orders_map.get(put_oid, {})

    if all_filled:
        # Both legs fully confirmed filled
        call_buy_price = _to_float(call_ord.get("average_price"))
        put_buy_price = _to_float(put_ord.get("average_price"))

        if call_buy_price <= 0 or put_buy_price <= 0:
            logger.critical(
                "[%s] Verified filled orders have invalid average_price: CALL=%s (avg=%.2f), PUT=%s (avg=%.2f). "
                "Refusing to substitute reference LTP. Initiating rollback.",
                broker, call_oid, call_buy_price, put_oid, put_buy_price
            )
            rollback_data = rollback_partial_entry(
                broker=broker,
                token=active_token,
                call_contract=call_contract,
                put_contract=put_contract,
                target_qty=quantity,
                call_order_id=call_oid,
                put_order_id=put_oid,
                call_order=call_ord,
                put_order=put_ord,
                order_book_provider=order_book_provider,
                position_provider=position_provider,
                track=tag,
                baseline_call_qty=baseline_call_qty,
                baseline_put_qty=baseline_put_qty,
                buy_failure_reason=f"Zero or missing average_price: CALL={call_buy_price}, PUT={put_buy_price}",
            )
            all_unwound = rollback_data.get("unwind_successful", False)
            status = "FAILED_ROLLED_BACK" if all_unwound else "UNWIND_FAILED"
            fail_msg = f"Zero or missing average_price on confirmed fill: CALL={call_buy_price}, PUT={put_buy_price}"
            return {
                "success": False,
                "status": status,
                "broker": broker,
                "quantity": quantity,
                "call_ref_ltp": call_ltp,
                "put_ref_ltp": put_ltp,
                "call_order_id": call_oid,
                "put_order_id": put_oid,
                "call_order": call_ord,
                "put_order": put_ord,
                "rollback": rollback_data,
                "error": fail_msg,
                "message": fail_msg,
            }

        logger.info(
            "[%s] PAIRED MARKET BUY SUCCESS: CALL=%s @ ₹%.2f, PUT=%s @ ₹%.2f (Qty: %d)",
            broker, call_oid, call_buy_price, put_oid, put_buy_price, quantity
        )
        return {
            "success": True,
            "status": "FILLED",
            "broker": broker,
            "quantity": quantity,
            "call_order_id": call_oid,
            "put_order_id": put_oid,
            "call_buy_price": call_buy_price,
            "put_buy_price": put_buy_price,
            "call_ref_ltp": call_ltp,
            "put_ref_ltp": put_ltp,
            "metrics": metrics,
            "call_order": call_ord,
            "put_order": put_ord,
            "message": f"Paired MARKET BUY executed and filled: CALL ₹{call_buy_price:.2f}, PUT ₹{put_buy_price:.2f}",
        }

    # 5. Incomplete Fill / Failure -> Trigger Atomic Rollback
    logger.critical(
        "[%s] Paired fill verification FAILED: CALL status=%s (filled=%s/%d), PUT status=%s (filled=%s/%d)",
        broker, call_ord.get("status"), call_ord.get("filled_quantity", 0), quantity,
        put_ord.get("status"), put_ord.get("filled_quantity", 0), quantity
    )
    rollback_data = rollback_partial_entry(
        broker=broker,
        token=active_token,
        call_contract=call_contract,
        put_contract=put_contract,
        target_qty=quantity,
        call_order_id=call_oid,
        put_order_id=put_oid,
        call_order=call_ord,
        put_order=put_ord,
        order_book_provider=order_book_provider,
        position_provider=position_provider,
        track=tag,
        baseline_call_qty=baseline_call_qty,
        baseline_put_qty=baseline_put_qty,
        buy_failure_reason=f"Verification failed: CALL status={call_ord.get('status')} ({call_ord.get('filled_quantity', 0)}/{quantity}), PUT status={put_ord.get('status')} ({put_ord.get('filled_quantity', 0)}/{quantity})",
    )
    all_unwound = rollback_data.get("unwind_successful", False)
    status = "FAILED_ROLLED_BACK" if all_unwound else "UNWIND_FAILED"

    fail_reason = (
        f"Fill verification failed: CALL status={call_ord.get('status')} "
        f"({call_ord.get('filled_quantity', 0)}/{quantity}), "
        f"PUT status={put_ord.get('status')} ({put_ord.get('filled_quantity', 0)}/{quantity})"
    )
    return {
        "success": False,
        "status": status,
        "broker": broker,
        "quantity": quantity,
        "call_ref_ltp": call_ltp,
        "put_ref_ltp": put_ltp,
        "call_order_id": call_oid,
        "put_order_id": put_oid,
        "call_order": call_ord,
        "put_order": put_ord,
        "rollback": rollback_data,
        "error": fail_reason,
        "message": fail_reason,
    }
