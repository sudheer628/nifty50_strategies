"""
common/broker_adapters.py - Unified Broker Adapters for Upstox & AngelOne.

Provides a polymorphic broker interface for:
1. Upstox (v2 Open API)
2. AngelOne (SmartAPI REST)

Supports:
- Token retrieval from Redis / environment
- Portfolio positions retrieval and normalization
- Order book retrieval and normalization
- Market LTP lookups
- SL-M / MARKET exit order placement and modification
- Order cancellation
- Product code normalization
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
import logging
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("broker_adapters")

MOCK_TOKEN = "MOCK_TOKEN"
TICK_SIZE = 0.05
REQUEST_TIMEOUT = 15

OPEN_ORDER_STATUSES = {
    "open", "pending", "modify_pending", "trigger_pending", "open_pending",
    "cancel_pending", "replace_pending", "validation_pending",
    "put_order_req_received", "after_market_order_req_received",
    "partially_filled", "partiallyfilled",
}
TERMINAL_CANCELLED_STATUSES = {"cancelled", "canceled", "rejected", "expired"}
TERMINAL_FILLED_STATUSES = {"complete", "completed", "filled"}
DERIVATIVE_EXCHANGES = ("NFO", "BFO", "NSE_FO", "BSE_FO")
DERIVATIVE_KEY_PREFIXES = ("NSE_FO|", "BSE_FO|")


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


def round_to_tick(price: float, tick: float = TICK_SIZE) -> float:
    """Round price to the nearest tick (default: 0.05)."""
    return round(math.floor(float(price) / tick + 0.5) * tick, 2)


def normalize_order_status(status: Any) -> str:
    raw = str(status or "").strip().lower()
    return raw.replace(" ", "_")


def is_derivative_position(pos: Dict[str, Any]) -> bool:
    exch = str(pos.get("exchange") or "").upper()
    token = str(pos.get("instrument_token") or pos.get("symboltoken") or "").upper()
    if exch in DERIVATIVE_EXCHANGES:
        return True
    return any(token.startswith(prefix) for prefix in DERIVATIVE_KEY_PREFIXES)


def _position_quantity(pos: Dict[str, Any]) -> float:
    for field in ("quantity", "net_quantity", "netqty", "netQty"):
        if field in pos and pos.get(field) not in (None, ""):
            return _to_float(pos.get(field))
    buy_q = _to_float(pos.get("cfbuyqty") or pos.get("buyqty") or pos.get("day_buy_quantity") or 0)
    sell_q = _to_float(pos.get("cfsellqty") or pos.get("sellqty") or 0)
    if buy_q != 0 or sell_q != 0:
        return buy_q - sell_q
    return 0.0


def parse_positions(raw: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Parse Upstox or AngelOne positions response into normalized records.
    Returns: (long_positions, short_positions)
    """
    if not isinstance(raw, dict):
        raise ValueError("Unexpected positions response: expected a JSON object")
    data = raw.get("data")
    if isinstance(data, list):
        source_positions = data
    elif isinstance(data, dict):
        if isinstance(data.get("net"), list):
            source_positions = data["net"]
        elif isinstance(data.get("positions"), list):
            source_positions = data["positions"]
        elif not data:
            source_positions = []
        else:
            raise ValueError(f"Unsupported positions response shape: data keys={list(data.keys())}")
    elif data is None or not data:
        source_positions = []
    else:
        raise ValueError("Unsupported positions payload")

    long_positions = []
    short_positions = []
    for p in source_positions:
        if isinstance(p, dict):
            norm_p = dict(p)
            tok = str(p.get("instrument_token") or p.get("symboltoken") or "").strip()
            sym = str(p.get("tradingsymbol") or p.get("trading_symbol") or "").strip().upper()
            prod = str(p.get("product") or p.get("producttype") or "CARRYFORWARD").strip().upper()
            qty = _position_quantity(p)
            avg_price = _to_float(
                p.get("average_price") or p.get("buy_price") or p.get("cfbuyavgprice") or p.get("buyavgprice") or 0.0
            )

            norm_p["instrument_token"] = tok
            norm_p["symboltoken"] = tok
            norm_p["tradingsymbol"] = sym
            norm_p["trading_symbol"] = sym
            norm_p["product"] = prod
            norm_p["producttype"] = prod
            norm_p["quantity"] = qty
            norm_p["net_quantity"] = qty
            norm_p["netqty"] = qty
            norm_p["buy_price"] = avg_price
            norm_p["average_price"] = avg_price

            if is_derivative_position(norm_p):
                if qty > 0:
                    long_positions.append(norm_p)
                elif qty < 0:
                    short_positions.append(norm_p)
    return long_positions, short_positions


def order_filled_quantity(order: Dict[str, Any]) -> int:
    filled = _to_float(order.get("filled_quantity") or order.get("filledshares") or 0)
    if filled <= 0:
        total = _to_float(order.get("quantity") or 0)
        pending = order.get("pending_quantity") or order.get("unfilledshares")
        if pending not in (None, ""):
            filled = max(0.0, total - _to_float(pending))
    return int(max(0.0, filled))


def order_pending_quantity(order: Dict[str, Any]) -> int:
    for field in ("pending_quantity", "unfilledshares", "quantity_remaining", "open_quantity"):
        if field in order and order.get(field) not in (None, ""):
            return int(max(0.0, _to_float(order.get(field))))
    total = _to_float(order.get("quantity") or 0)
    return int(max(0.0, total - order_filled_quantity(order)))


def parse_order_book(raw: Any) -> List[Dict[str, Any]]:
    """Parse Upstox or AngelOne order book response into normalized order dicts."""
    if isinstance(raw, list):
        raw_orders = [o for o in raw if isinstance(o, dict)]
    elif isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list):
            raw_orders = [o for o in data if isinstance(o, dict)]
        elif data is None or data == []:
            raw_orders = []
        else:
            raise ValueError(f"Unexpected order book response shape: data is {type(data).__name__}, expected list")
    elif raw is None:
        raw_orders = []
    else:
        raise ValueError(f"Unexpected order book response shape: {type(raw).__name__}, expected dict or list")

    normalized = []
    for o in raw_orders:
        norm_o = dict(o)
        oid = str(o.get("order_id") or o.get("orderid") or "").strip()
        tok = str(o.get("instrument_token") or o.get("symboltoken") or "").strip()
        sym = str(o.get("trading_symbol") or o.get("tradingsymbol") or "").strip().upper()
        status = str(o.get("status") or o.get("orderstatus") or "").strip()
        ttype = str(o.get("transaction_type") or o.get("transactiontype") or "").strip().upper()
        otype = str(o.get("order_type") or o.get("ordertype") or "").strip().upper()
        ptype = str(o.get("product") or o.get("producttype") or "CARRYFORWARD").strip().upper()

        qty = _to_float(o.get("quantity") or 0)
        filled = _to_float(o.get("filled_quantity") or o.get("filledshares") or 0)
        unfilled = _to_float(o.get("pending_quantity") or o.get("unfilledshares") or max(0, qty - filled))
        trig = _to_float(o.get("trigger_price") or o.get("triggerprice") or 0.0)
        tag = str(o.get("tag") or o.get("order_tag") or "").strip()

        norm_o["order_id"] = oid
        norm_o["orderid"] = oid
        norm_o["instrument_token"] = tok
        norm_o["symboltoken"] = tok
        norm_o["trading_symbol"] = sym
        norm_o["tradingsymbol"] = sym
        norm_o["status"] = status
        norm_o["orderstatus"] = status
        norm_o["transaction_type"] = ttype
        norm_o["transactiontype"] = ttype
        norm_o["order_type"] = otype
        norm_o["ordertype"] = otype
        norm_o["product"] = ptype
        norm_o["producttype"] = ptype
        norm_o["quantity"] = qty
        norm_o["filled_quantity"] = filled
        norm_o["pending_quantity"] = unfilled
        norm_o["trigger_price"] = trig
        norm_o["triggerprice"] = trig
        norm_o["tag"] = tag
        normalized.append(norm_o)
    return normalized


def order_matches_position(order: Dict[str, Any], position: Dict[str, Any]) -> bool:
    order_token = str(order.get("instrument_token") or order.get("symboltoken") or "").strip().upper()
    pos_token = str(position.get("instrument_token") or position.get("symboltoken") or "").strip().upper()
    order_symbol = str(order.get("trading_symbol") or order.get("tradingsymbol") or "").strip().upper()
    pos_symbol = str(position.get("tradingsymbol") or position.get("trading_symbol") or "").strip().upper()
    return bool((order_token and pos_token and order_token == pos_token) or
                (order_symbol and pos_symbol and order_symbol == pos_symbol))


# ---------------------------------------------------------------------------
# Abstract Broker Adapter Interface
# ---------------------------------------------------------------------------

class BrokerAdapter(ABC):
    name: str

    @abstractmethod
    def get_access_token(self, redis_client: Optional[Any] = None) -> str:
        """Acquire authentication/access token from Redis or environment."""
        pass

    @abstractmethod
    def fetch_positions(self, token: str) -> Dict[str, Any]:
        """Fetch raw positions payload from broker API."""
        pass

    @abstractmethod
    def fetch_order_book(self, token: str) -> List[Dict[str, Any]]:
        """Fetch and normalize order book from broker API."""
        pass

    @abstractmethod
    def fetch_ltp(
        self,
        token: str,
        instrument_token: str,
        trading_symbol: str = "",
        exchange: str = "NFO",
    ) -> Optional[float]:
        """Fetch latest traded price (LTP) from broker quote API."""
        pass

    @abstractmethod
    def cancel_order(
        self,
        token: str,
        order_id: str,
        variety: str = "STOPLOSS",
    ) -> Dict[str, Any]:
        """Cancel an open order."""
        pass

    @abstractmethod
    def place_exit_order_slm(
        self,
        token: str,
        position: Dict[str, Any],
        target_qty: int,
        trigger_price: float,
        product: Optional[str] = None,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        """Place a STOPLOSS_MARKET (SL-M) sell order."""
        pass

    @abstractmethod
    def place_exit_order_market(
        self,
        token: str,
        position: Dict[str, Any],
        target_qty: int,
        product: Optional[str] = None,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        """Place an immediate MARKET sell order."""
        pass

    @abstractmethod
    def modify_exit_order_slm(
        self,
        token: str,
        order: Dict[str, Any],
        target_qty: int,
        trigger_price: float,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        """Modify an existing SL-M order trigger price and quantity."""
        pass

    @abstractmethod
    def get_exit_product(
        self,
        position: Dict[str, Any],
        cli_product: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve valid product code for this broker."""
        pass


# ---------------------------------------------------------------------------
# Upstox Adapter Implementation
# ---------------------------------------------------------------------------

class UpstoxAdapter(BrokerAdapter):
    name = "upstox"

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com")).rstrip("/")

    def _headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def get_access_token(self, redis_client: Optional[Any] = None) -> str:
        candidates = ["upstox_access_token", "UPSTOX_ACCESS_TOKEN", "UPSTOX_TOKEN", "upstox_token"]
        r = redis_client
        if r is None:
            try:
                from config import get_redis_client
                r = get_redis_client()
            except Exception as exc:
                logger.debug("Failed reading Upstox token from Redis: %s", exc)
        if r is not None:
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

        raise RuntimeError(f"No access token found for broker '{self.name}'. Checked Redis and .env for {candidates}")

    def fetch_positions(self, token: str) -> Dict[str, Any]:
        url = f"{self.base_url}/v2/portfolio/short-term-positions"
        resp = requests.get(url, headers=self._headers(token), timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"Positions HTTP {resp.status_code}: {resp.text[:400]}")
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Positions API error: {body.get('errors') or body}")
        return body

    def fetch_order_book(self, token: str) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/v2/order/retrieve-all"
        resp = requests.get(url, headers=self._headers(token), timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"Order book HTTP {resp.status_code}: {resp.text[:400]}")
        body = resp.json()
        if isinstance(body, dict) and body.get("status") not in (None, "success"):
            raise RuntimeError(f"Order book API error: {body.get('errors') or body}")
        return parse_order_book(body)

    def fetch_ltp(
        self,
        token: str,
        instrument_token: str,
        trading_symbol: str = "",
        exchange: str = "NFO",
    ) -> Optional[float]:
        if token == MOCK_TOKEN or not instrument_token:
            return None
        url = f"{self.base_url}/v2/market-quote/quotes"
        params = {"instrument_key": instrument_token}
        resp = requests.get(url, headers=self._headers(token), params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.error("Upstox Quote HTTP %s: %s", resp.status_code, resp.text[:300])
            return None
        data = (resp.json() or {}).get("data")
        if not isinstance(data, dict):
            return None
        # Upstox keys the response by "<segment>:<trading symbol>" (e.g. "NSE_FO:NIFTY26N0924200CE"),
        # not by the requested instrument key ("NSE_FO|39971"). Match on the entry's own
        # instrument_token field first, then fall back to key-based lookups.
        wanted = {instrument_token, instrument_token.replace(":", "|"), instrument_token.replace("|", ":")}
        candidates = [e for e in data.values() if isinstance(e, dict) and str(e.get("instrument_token") or "") in wanted]
        for key in list(wanted) + ([f"{instrument_token.split('|')[0].split(':')[0]}:{trading_symbol.upper()}"] if trading_symbol else []):
            entry = data.get(key)
            if isinstance(entry, dict) and entry not in candidates:
                candidates.append(entry)
        for entry in candidates:
            for candidate in ("last_price", "ltp"):
                if candidate in entry:
                    val = _to_float(entry.get(candidate))
                    if val > 0:
                        return val
        if data:
            logger.warning("Upstox quote for %s returned keys %s with no matching instrument_token", instrument_token, list(data.keys())[:5])
        return None

    def cancel_order(
        self,
        token: str,
        order_id: str,
        variety: str = "STOPLOSS",
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/v2/order/cancel"
        params = {"order_id": order_id}
        resp = requests.delete(url, headers=self._headers(token), params=params, timeout=REQUEST_TIMEOUT)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": getattr(resp, "text", "")[:300]}
        success = (200 <= getattr(resp, "status_code", 0) < 300 and isinstance(body, dict) and body.get("status") == "success")
        return {
            "action": "cancel",
            "order_id": order_id,
            "success": success,
            "http_status": getattr(resp, "status_code", None),
            "response": body,
        }

    def place_exit_order_slm(
        self,
        token: str,
        position: Dict[str, Any],
        target_qty: int,
        trigger_price: float,
        product: Optional[str] = None,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        trigger_price = round_to_tick(trigger_price)
        prod = product or self.get_exit_product(position) or "D"
        tok = position.get("instrument_token") or position.get("symboltoken")
        payload = {
            "quantity": int(target_qty),
            "product": prod,
            "validity": "DAY",
            "price": 0,
            "tag": tag,
            "instrument_token": tok,
            "order_type": "SL-M",
            "transaction_type": "SELL",
            "disclosed_quantity": 0,
            "trigger_price": trigger_price,
            "is_amo": False,
        }
        url = f"{self.base_url}/v2/order/place"
        resp = requests.post(url, headers=self._headers(token), json=payload, timeout=REQUEST_TIMEOUT)
        return self._order_response("place", resp, payload)

    def place_exit_order_market(
        self,
        token: str,
        position: Dict[str, Any],
        target_qty: int,
        product: Optional[str] = None,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        prod = product or self.get_exit_product(position) or "D"
        tok = position.get("instrument_token") or position.get("symboltoken")
        payload = {
            "quantity": int(target_qty),
            "product": prod,
            "validity": "DAY",
            "price": 0,
            "tag": tag,
            "instrument_token": tok,
            "order_type": "MARKET",
            "transaction_type": "SELL",
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        url = f"{self.base_url}/v2/order/place"
        resp = requests.post(url, headers=self._headers(token), json=payload, timeout=REQUEST_TIMEOUT)
        return self._order_response("place_market", resp, payload)

    def modify_exit_order_slm(
        self,
        token: str,
        order: Dict[str, Any],
        target_qty: int,
        trigger_price: float,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        trigger_price = round_to_tick(trigger_price)
        total_qty = order_filled_quantity(order) + target_qty
        oid = str(order.get("order_id") or order.get("orderid") or "")
        payload = {
            "order_id": oid,
            "quantity": int(total_qty),
            "validity": "DAY",
            "price": 0,
            "order_type": "SL-M",
            "disclosed_quantity": 0,
            "trigger_price": trigger_price,
            "tag": tag,
        }
        url = f"{self.base_url}/v2/order/modify"
        resp = requests.put(url, headers=self._headers(token), json=payload, timeout=REQUEST_TIMEOUT)
        return self._order_response("modify", resp, payload)

    def get_exit_product(
        self,
        position: Dict[str, Any],
        cli_product: Optional[str] = None,
    ) -> Optional[str]:
        raw = str(position.get("product") or position.get("producttype") or "").upper().strip()
        product_map = {
            "I": "I", "MIS": "I", "INTRADAY": "I",
            "D": "D", "NRML": "D", "CNC": "D", "CARRYFORWARD": "D",
        }
        if raw in product_map:
            return product_map[raw]
        if cli_product:
            cli = str(cli_product).upper().strip()
            if cli in {"I", "D"}:
                return cli
        return "D"

    def _order_response(self, action: str, resp, payload: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"action": action, "request": payload}
        try:
            data = resp.json()
            result["response"] = data
            result["http_status"] = resp.status_code
            if resp.status_code in (200, 201) and data.get("status") == "success":
                result["success"] = True
                result["order_id"] = (data.get("data") or {}).get("order_id")
            else:
                result["success"] = False
                result["error"] = data.get("errors") or data.get("message") or f"HTTP {resp.status_code}"
        except Exception as exc:
            result["success"] = False
            result["http_status"] = getattr(resp, "status_code", None)
            result["error"] = f"Failed to parse broker response: {exc}"
            result["response"] = {"raw": getattr(resp, "text", "")[:300]}
        return result


# ---------------------------------------------------------------------------
# AngelOne Adapter Implementation
# ---------------------------------------------------------------------------

class AngelOneAdapter(BrokerAdapter):
    name = "angelone"

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or os.getenv("ANGELONE_BASE_URL", "https://apiconnect.angelone.in")).rstrip("/")

    def _headers(self, token: str) -> Dict[str, str]:
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

    def get_access_token(self, redis_client: Optional[Any] = None) -> str:
        r = redis_client
        if r is None:
            try:
                from config import get_redis_client
                r = get_redis_client()
            except Exception as exc:
                logger.debug("Failed reading AngelOne token from Redis: %s", exc)

        if r is not None:
            try:
                raw = r.get("angelone_jwt_feed")
                if raw:
                    try:
                        data = json.loads(raw)
                        jwt = data.get("jwtToken") or data.get("token") or ""
                    except Exception:
                        jwt = str(raw).strip()
                    if jwt.startswith("Bearer "):
                        jwt = jwt[7:]
                    if jwt:
                        return jwt
            except Exception as exc:
                logger.debug("Failed to read angelone_jwt_feed from Redis: %s", exc)

        for env_var in ("ANGELONE_JWT_TOKEN", "ANGELONE_ACCESS_TOKEN", "ANGEL_JWT_TOKEN"):
            val = os.getenv(env_var)
            if val:
                if val.startswith("Bearer "):
                    val = val[7:]
                return val

        candidates = ["angelone_jwt_feed (Redis)", "ANGELONE_JWT_TOKEN (.env)", "ANGELONE_ACCESS_TOKEN (.env)"]
        raise RuntimeError(f"No access token found for broker '{self.name}'. Checked {candidates}")

    def fetch_positions(self, token: str) -> Dict[str, Any]:
        url = f"{self.base_url}/rest/secure/angelbroking/order/v1/getPosition"
        resp = requests.get(url, headers=self._headers(token), timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"Positions HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if isinstance(body, dict) and body.get("status") is False:
            raise RuntimeError(f"Positions API error: {body.get('message') or body.get('errorcode') or body}")
        return body

    def fetch_order_book(self, token: str) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/rest/secure/angelbroking/order/v1/getOrderBook"
        resp = requests.get(url, headers=self._headers(token), timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"Order book HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if isinstance(body, dict) and body.get("status") is False:
            raise RuntimeError(f"Order book API error: {body.get('message') or body.get('errorcode') or body}")
        return parse_order_book(body)

    def fetch_ltp(
        self,
        token: str,
        instrument_token: str,
        trading_symbol: str = "",
        exchange: str = "NFO",
    ) -> Optional[float]:
        if token == MOCK_TOKEN or not instrument_token:
            return None
        url = f"{self.base_url}/rest/secure/angelbroking/market/v1/quote/"
        payload = {
            "mode": "LTP",
            "exchangeTokens": {
                exchange: [str(instrument_token)]
            }
        }
        try:
            resp = requests.post(url, headers=self._headers(token), json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200 or not resp.text.strip():
                logger.error("AngelOne Quote HTTP %d: %s", resp.status_code, resp.text[:300])
                return None
            body = resp.json()
            if body.get("status") is not True:
                logger.error("AngelOne Quote API error: %s", body.get("message") or body)
                return None
            fetched = (body.get("data") or {}).get("fetched", [])
            if fetched and isinstance(fetched, list):
                ltp_val = _to_float(fetched[0].get("ltp"))
                if ltp_val > 0:
                    return ltp_val
        except Exception as exc:
            logger.error("AngelOne fetch_ltp exception for token %s: %s", instrument_token, exc)
        return None

    def cancel_order(
        self,
        token: str,
        order_id: str,
        variety: str = "STOPLOSS",
    ) -> Dict[str, Any]:
        if token == MOCK_TOKEN:
            return {"action": "cancel", "order_id": order_id, "success": True, "http_status": 200, "response": {"status": True}}
        url = f"{self.base_url}/rest/secure/angelbroking/order/v1/cancelOrder"
        payload = {
            "variety": variety,
            "orderid": str(order_id),
        }
        resp = requests.post(url, headers=self._headers(token), json=payload, timeout=REQUEST_TIMEOUT)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": getattr(resp, "text", "")[:300]}
        success = (200 <= getattr(resp, "status_code", 0) < 300 and isinstance(body, dict) and body.get("status") is True)
        return {
            "action": "cancel",
            "order_id": order_id,
            "success": success,
            "http_status": getattr(resp, "status_code", None),
            "response": body,
        }

    def place_exit_order_slm(
        self,
        token: str,
        position: Dict[str, Any],
        target_qty: int,
        trigger_price: float,
        product: Optional[str] = None,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        trigger_price = round_to_tick(trigger_price)
        sym = position.get("tradingsymbol") or position.get("trading_symbol")
        tok = str(position.get("symboltoken") or position.get("instrument_token") or "")
        exch = str(position.get("exchange") or "NFO").upper()
        prod = product or self.get_exit_product(position) or "CARRYFORWARD"

        payload = {
            "variety": "STOPLOSS",
            "tradingsymbol": sym,
            "symboltoken": tok,
            "transactiontype": "SELL",
            "exchange": exch,
            "ordertype": "STOPLOSS_MARKET",
            "producttype": prod,
            "duration": "DAY",
            "price": "0",
            "triggerprice": f"{trigger_price:.2f}",
            "quantity": str(int(target_qty)),
        }
        if token == MOCK_TOKEN:
            mock_oid = f"mock_{int(time.time()*1000)}"
            return {
                "action": "place",
                "request": payload,
                "success": True,
                "order_id": mock_oid,
                "response": {"status": True, "data": {"orderid": mock_oid}},
            }
        url = f"{self.base_url}/rest/secure/angelbroking/order/v1/placeOrder"
        resp = requests.post(url, headers=self._headers(token), json=payload, timeout=REQUEST_TIMEOUT)
        return self._order_response("place", resp, payload)

    def place_exit_order_market(
        self,
        token: str,
        position: Dict[str, Any],
        target_qty: int,
        product: Optional[str] = None,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        sym = position.get("tradingsymbol") or position.get("trading_symbol")
        tok = str(position.get("symboltoken") or position.get("instrument_token") or "")
        exch = str(position.get("exchange") or "NFO").upper()
        prod = product or self.get_exit_product(position) or "CARRYFORWARD"

        payload = {
            "variety": "NORMAL",
            "tradingsymbol": sym,
            "symboltoken": tok,
            "transactiontype": "SELL",
            "exchange": exch,
            "ordertype": "MARKET",
            "producttype": prod,
            "duration": "DAY",
            "price": "0",
            "triggerprice": "0",
            "quantity": str(int(target_qty)),
        }
        if token == MOCK_TOKEN:
            mock_oid = f"mock_{int(time.time()*1000)}"
            return {
                "action": "place_market",
                "request": payload,
                "success": True,
                "order_id": mock_oid,
                "response": {"status": True, "data": {"orderid": mock_oid}},
            }
        url = f"{self.base_url}/rest/secure/angelbroking/order/v1/placeOrder"
        resp = requests.post(url, headers=self._headers(token), json=payload, timeout=REQUEST_TIMEOUT)
        return self._order_response("place_market", resp, payload)

    def modify_exit_order_slm(
        self,
        token: str,
        order: Dict[str, Any],
        target_qty: int,
        trigger_price: float,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        trigger_price = round_to_tick(trigger_price)
        total_qty = order_filled_quantity(order) + target_qty
        oid = str(order.get("order_id") or order.get("orderid") or "")
        sym = order.get("trading_symbol") or order.get("tradingsymbol")
        tok = str(order.get("instrument_token") or order.get("symboltoken") or "")
        exch = str(order.get("exchange") or "NFO").upper()
        prod = self.get_exit_product(order) or "CARRYFORWARD"

        payload = {
            "variety": "STOPLOSS",
            "orderid": oid,
            "ordertype": "STOPLOSS_MARKET",
            "producttype": prod,
            "duration": "DAY",
            "price": "0",
            "triggerprice": f"{trigger_price:.2f}",
            "quantity": str(int(total_qty)),
            "tradingsymbol": sym,
            "symboltoken": tok,
            "exchange": exch,
        }
        if token == MOCK_TOKEN:
            return {
                "action": "modify",
                "request": payload,
                "success": True,
                "order_id": oid,
                "response": {"status": True, "data": {"orderid": oid}},
            }
        url = f"{self.base_url}/rest/secure/angelbroking/order/v1/modifyOrder"
        resp = requests.post(url, headers=self._headers(token), json=payload, timeout=REQUEST_TIMEOUT)
        return self._order_response("modify", resp, payload)

    def get_exit_product(
        self,
        position: Dict[str, Any],
        cli_product: Optional[str] = None,
    ) -> Optional[str]:
        raw = str(position.get("product") or position.get("producttype") or "").upper().strip()
        if raw in ("CARRYFORWARD", "NRML", "CNC", "D"):
            return "CARRYFORWARD"
        if raw in ("INTRADAY", "MIS", "I"):
            return "INTRADAY"
        if cli_product:
            cli = str(cli_product).upper().strip()
            if cli in ("CARRYFORWARD", "INTRADAY"):
                return cli
        return "CARRYFORWARD"

    def _order_response(self, action: str, resp, payload: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"action": action, "request": payload}
        try:
            data = resp.json()
            result["response"] = data
            result["http_status"] = resp.status_code
            if resp.status_code in (200, 201) and isinstance(data, dict) and data.get("status") is True:
                result["success"] = True
                result["order_id"] = (data.get("data") or {}).get("orderid") or (data.get("data") or {}).get("order_id")
            else:
                result["success"] = False
                result["error"] = data.get("message") or data.get("errorcode") or f"HTTP {resp.status_code}"
        except Exception as exc:
            result["success"] = False
            result["http_status"] = getattr(resp, "status_code", None)
            result["error"] = f"Failed to parse broker response: {exc}"
            result["response"] = {"raw": getattr(resp, "text", "")[:300]}
        return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_ADAPTER_REGISTRY: Dict[str, BrokerAdapter] = {}


def get_broker_adapter(broker: str) -> BrokerAdapter:
    """Return a BrokerAdapter instance for the specified broker name."""
    b = str(broker or "").lower().strip()
    if b in ("angelone", "angel"):
        b = "angelone"
    elif b == "upstox":
        b = "upstox"
    elif b in ("kotak", "kotakneo", "kotak_neo", "neo"):
        b = "kotak"
    else:
        raise ValueError(f"Unsupported broker '{broker}'. Must be 'upstox', 'angelone', or 'kotak'.")

    if b not in _ADAPTER_REGISTRY:
        if b == "upstox":
            _ADAPTER_REGISTRY[b] = UpstoxAdapter()
        elif b == "angelone":
            _ADAPTER_REGISTRY[b] = AngelOneAdapter()
        elif b == "kotak":
            from common.kotak_neo_adapter import KotakNeoAdapter
            _ADAPTER_REGISTRY[b] = KotakNeoAdapter()
    return _ADAPTER_REGISTRY[b]
