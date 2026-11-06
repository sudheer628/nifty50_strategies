"""
common/kotak_neo_adapter.py - Kotak Neo Trade API v2 Adapter.

Provides a polymorphic broker interface for Kotak Neo Trade API v2:
- Reads credentials & session tokens from Redis Cloud (key: 'kotak_access_token')
  populated daily by generate_trading_keys/kotak_generate_session.py.
- Normalizes positions and order books into the standard strategy schema.
- Fetches live LTP quotes via Kotak's neosymbol quotes API.
- Places, modifies, and cancels SL-M and MARKET exit orders with form-urlencoded
  payloads complying with Kotak Neo Trade API v2 specifications.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

from common.broker_adapters import (
    BrokerAdapter,
    MOCK_TOKEN,
    REQUEST_TIMEOUT,
    _to_float,
    order_filled_quantity,
    parse_order_book,
    round_to_tick,
)

logger = logging.getLogger("kotak_neo_adapter")

DEFAULT_KOTAK_BASE_URL = "https://gw-napi.kotaksecurities.com"
DEFAULT_KOTAK_REDIS_KEY = "kotak_access_token"
NEO_FIN_KEY = "neotradeapi"

# NSE Weekly Option Month Code Mapping: Jan-Sep -> 1-9, Oct -> O, Nov -> N, Dec -> D
MONTH_CODE_MAP: Dict[int, str] = {
    1: "1",
    2: "2",
    3: "3",
    4: "4",
    5: "5",
    6: "6",
    7: "7",
    8: "8",
    9: "9",
    10: "O",
    11: "N",
    12: "D",
}

_KOTAK_CONTRACT_CACHE: Dict[Tuple[str, int, str], Dict[str, Any]] = {}


def parse_expiry_to_date(expiry_val: Any) -> Optional[date]:
    """Parse various expiry date representations into a datetime.date object."""
    if isinstance(expiry_val, datetime):
        return expiry_val.date()
    if isinstance(expiry_val, date):
        return expiry_val
    if not expiry_val:
        return None
    raw = str(expiry_val).strip()
    # ISO YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    # Compact YYYYMMDD
    if re.match(r"^\d{8}$", raw):
        try:
            return datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            pass
    # DDMMMYYYY (e.g. 06NOV2026)
    if re.match(r"^\d{1,2}[A-Za-z]{3}\d{4}$", raw):
        try:
            return datetime.strptime(raw, "%d%b%Y").date()
        except ValueError:
            pass
    # DD-Mon-YYYY or other common formats
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d%b%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def is_monthly_expiry(expiry_date: date) -> bool:
    """
    Check if expiry date is the monthly expiry for that month.
    In NSE F&O, the monthly expiry is the last contract expiry in that calendar month.
    If adding 7 days rolls into the subsequent month, there are no further weekly expiries.
    """
    return (expiry_date + timedelta(days=7)).month != expiry_date.month


def format_kotak_trading_symbol(
    expiry: Union[date, datetime, str],
    strike: int,
    option_type: str,
    is_monthly: Optional[bool] = None,
    underlying: str = "NIFTY",
) -> str:
    """
    Format NSE option trading symbol for Kotak Neo API.

    Rules per NSE / Kotak Neo specifications:
      - Weekly Expiry: UNDERLYING + YY + M + DD + STRIKE + CE/PE
        * YY: 2-digit year (e.g., '26')
        * M: 1-9 (Jan-Sep), 'O' (Oct), 'N' (Nov), 'D' (Dec)
        * DD: 2-digit date (e.g., '06')
        * Example: NIFTY26N0624500CE
      - Monthly Expiry: UNDERLYING + YY + MMM + STRIKE + CE/PE
        * YY: 2-digit year (e.g., '26')
        * MMM: 3-letter month uppercase (e.g., 'NOV')
        * Example: NIFTY26NOV24500CE
    """
    d = parse_expiry_to_date(expiry)
    if not d:
        raise ValueError(f"Unable to parse expiry date '{expiry}' for Kotak Neo symbol")

    yy = d.strftime("%y")
    strike_int = int(round(float(strike)))
    opt = option_type.upper().strip()
    if opt not in ("CE", "PE"):
        raise ValueError(f"Invalid option type '{option_type}'. Expected 'CE' or 'PE'.")

    monthly = is_monthly if is_monthly is not None else is_monthly_expiry(d)
    if monthly:
        mmm = d.strftime("%b").upper()
        return f"{underlying}{yy}{mmm}{strike_int}{opt}"
    else:
        m_code = MONTH_CODE_MAP.get(d.month)
        if not m_code:
            raise ValueError(f"Invalid month {d.month} in expiry date {d}")
        dd = d.strftime("%d")
        return f"{underlying}{yy}{m_code}{dd}{strike_int}{opt}"


def resolve_kotak_option_contract(
    expiry_str: str,
    strike: int,
    option_type: str,
    token: Optional[str] = None,
    is_monthly: Optional[bool] = None,
    underlying: str = "NIFTY",
) -> Dict[str, Any]:
    """
    Resolve Kotak Neo option contract details for paired MARKET BUY orders.
    Returns normalized contract dictionary compatible with entry_order_manager.
    """
    cache_key = (str(expiry_str).upper(), int(strike), option_type.upper())
    if cache_key in _KOTAK_CONTRACT_CACHE:
        return _KOTAK_CONTRACT_CACHE[cache_key].copy()

    trading_symbol = format_kotak_trading_symbol(
        expiry=expiry_str,
        strike=strike,
        option_type=option_type,
        is_monthly=is_monthly,
        underlying=underlying,
    )

    is_mock = (
        token == MOCK_TOKEN
        or os.getenv("TEST_MODE") == "true"
        or os.getenv("MOCK_BROKER") == "true"
    )
    tok = f"MOCK_KOTAK_{strike}_{option_type.upper()}" if is_mock else str(token or "")

    contract = {
        "token": tok,
        "instrument_token": tok,
        "symboltoken": tok,
        "trading_symbol": trading_symbol,
        "tradingsymbol": trading_symbol,
        "strike": int(strike),
        "option_type": option_type.upper(),
        "exchange": "NSE_FO",
        "product": "NRML",
        "lotsize": int(os.getenv("NIFTY_LOT_SIZE", "65")),
    }
    _KOTAK_CONTRACT_CACHE[cache_key] = contract
    return contract.copy()


class KotakNeoAdapter(BrokerAdapter):
    name = "kotak"

    def __init__(
        self,
        base_url: Optional[str] = None,
        sid: Optional[str] = None,
        consumer_key: Optional[str] = None,
    ):
        self.base_url = (
            base_url or os.getenv("KOTAK_BASE_URL", DEFAULT_KOTAK_BASE_URL)
        ).rstrip("/")
        self.sid = sid or os.getenv("KOTAK_SID", "")
        self.consumer_key = consumer_key or os.getenv(
            "KOTAK_CONSUMER_KEY", os.getenv("KOTAK_API_KEY", "")
        )

    def _headers(
        self,
        token: str,
        content_type: str = "application/x-www-form-urlencoded",
    ) -> Dict[str, str]:
        if token == MOCK_TOKEN:
            return {
                "Authorization": f"Bearer {MOCK_TOKEN}",
                "Auth": MOCK_TOKEN,
                "neo-fin-key": NEO_FIN_KEY,
                "accept": "application/json",
                "Content-Type": content_type,
            }

        headers: Dict[str, str] = {
            "Auth": token,
            "neo-fin-key": NEO_FIN_KEY,
            "accept": "application/json",
            "Content-Type": content_type,
        }
        if self.sid:
            headers["Sid"] = self.sid

        # Pass consumer key if available, else standard Bearer token
        if self.consumer_key:
            headers["Authorization"] = self.consumer_key
        elif token.startswith("Bearer "):
            headers["Authorization"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"

        return headers

    def get_access_token(self, redis_client: Optional[Any] = None) -> str:
        """
        Acquire authentication/access token from Redis or environment.

        Kotak session generator stores a JSON payload in Redis key 'kotak_access_token':
        {"token": "<trade_token>", "sid": "<edit_sid>", "base_url": "<base_url>"}
        """
        r = redis_client
        if r is None:
            try:
                from config import get_redis_client

                r = get_redis_client()
            except Exception as exc:
                logger.debug("Failed acquiring Redis client for Kotak Neo: %s", exc)

        redis_key = os.getenv("KOTAK_REDIS_KEY", DEFAULT_KOTAK_REDIS_KEY)

        if r is not None:
            try:
                raw = r.get(redis_key)
                if raw:
                    raw_str = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                    raw_str = raw_str.strip()
                    if raw_str.startswith("{") and raw_str.endswith("}"):
                        try:
                            data = json.loads(raw_str)
                            token = (
                                data.get("token")
                                or data.get("access_token")
                                or data.get("Auth")
                                or ""
                            )
                            if data.get("sid") or data.get("Sid"):
                                self.sid = data.get("sid") or data.get("Sid")
                            if data.get("base_url") or data.get("baseUrl"):
                                self.base_url = (
                                    data.get("base_url") or data.get("baseUrl")
                                ).rstrip("/")
                            if token:
                                return str(token).strip()
                        except Exception as parse_exc:
                            logger.warning(
                                "Failed parsing Kotak JSON session from Redis: %s", parse_exc
                            )
                    else:
                        return raw_str
            except Exception as exc:
                logger.debug("Failed reading Kotak session from Redis (%s): %s", redis_key, exc)

        # Fallback to environment variables
        for env_var in ("KOTAK_ACCESS_TOKEN", "KOTAK_TOKEN"):
            val = os.getenv(env_var)
            if val:
                val = val.strip()
                if val.startswith("{") and val.endswith("}"):
                    try:
                        data = json.loads(val)
                        tok = data.get("token") or data.get("access_token") or ""
                        if data.get("sid"):
                            self.sid = data.get("sid")
                        if data.get("base_url"):
                            self.base_url = data.get("base_url").rstrip("/")
                        if tok:
                            return str(tok).strip()
                    except Exception:
                        pass
                return val

        candidates = [
            f"{redis_key} (Redis)",
            "KOTAK_ACCESS_TOKEN (.env)",
            "KOTAK_TOKEN (.env)",
        ]
        raise RuntimeError(f"No access token found for broker '{self.name}'. Checked {candidates}")

    def fetch_positions(self, token: str) -> Dict[str, Any]:
        """Fetch and normalize raw positions payload from Kotak Neo API."""
        if token == MOCK_TOKEN:
            return {"stat": "Ok", "stCode": 200, "data": []}

        url = f"{self.base_url}/quick/user/positions"
        headers = self._headers(token, content_type="application/json")
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            # If 404, fallback to alternative endpoint path
            if resp.status_code == 404:
                alt_url = f"{self.base_url}/Orders/2.0/quick/user/positions"
                resp = requests.get(alt_url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Kotak positions network error: {e}")

        if resp.status_code != 200:
            raise RuntimeError(f"Kotak positions HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            body = resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to parse Kotak positions JSON: {e}")

        # Kotak returns stCode 5203 / No Data when empty
        if body.get("stCode") in (5203, "5203") or (body.get("errMsg") or "").strip().lower() == "no data":
            return {"stat": "Ok", "stCode": 200, "data": []}

        if (
            body.get("stat")
            and body.get("stat") not in ("Ok", "OK", "success", "SUCCESS")
            and body.get("stCode") not in (200, "200", None)
        ):
            raise RuntimeError(f"Kotak positions API error: {body}")

        positions = body.get("data", body.get("positions", []))
        if not isinstance(positions, list):
            positions = []

        normalized_positions = []
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            norm_p = dict(pos)
            tok = str(pos.get("tok") or pos.get("instrument_token") or pos.get("symboltoken") or "").strip()
            sym = str(pos.get("trdSym") or pos.get("tradingsymbol") or pos.get("trading_symbol") or "").strip().upper()
            prod = str(pos.get("prod") or pos.get("pc") or pos.get("product") or "NRML").strip().upper()

            # Derive net quantity
            fl_buy = _to_float(pos.get("flBuyQty") or pos.get("buyqty") or pos.get("buyQty") or 0)
            fl_sell = _to_float(pos.get("flSellQty") or pos.get("sellqty") or pos.get("sellQty") or 0)
            if fl_buy != 0 or fl_sell != 0:
                qty = fl_buy - fl_sell
            else:
                qty = _to_float(pos.get("qty") or pos.get("quantity") or pos.get("net_quantity") or 0)

            buy_avg = _to_float(
                pos.get("buyAvgPrc")
                or pos.get("buy_price")
                or pos.get("average_price")
                or pos.get("cfbuyavgprice")
                or 0.0
            )
            ex_seg = str(pos.get("exSeg") or pos.get("exchange") or "nse_fo").strip().upper()

            norm_p["instrument_token"] = tok
            norm_p["symboltoken"] = tok
            norm_p["tradingsymbol"] = sym
            norm_p["trading_symbol"] = sym
            norm_p["product"] = prod
            norm_p["producttype"] = prod
            norm_p["quantity"] = qty
            norm_p["net_quantity"] = qty
            norm_p["netqty"] = qty
            norm_p["buy_price"] = buy_avg
            norm_p["average_price"] = buy_avg
            norm_p["exchange"] = "NSE_FO" if ex_seg in ("NSE_FO", "NFO") else ex_seg

            normalized_positions.append(norm_p)

        return {"stat": "Ok", "stCode": 200, "data": normalized_positions}

    def fetch_order_book(self, token: str) -> List[Dict[str, Any]]:
        """Fetch and normalize order book from Kotak Neo API."""
        if token == MOCK_TOKEN:
            return []

        url = f"{self.base_url}/quick/user/orders"
        headers = self._headers(token, content_type="application/json")
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Kotak order book network error: {e}")

        if resp.status_code != 200:
            raise RuntimeError(f"Kotak order book HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            body = resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to parse Kotak order book JSON: {e}")

        if body.get("stCode") in (5203, "5203") or (body.get("errMsg") or "").strip().lower() == "no data":
            return []

        if (
            body.get("stat")
            and body.get("stat") not in ("Ok", "OK", "success", "SUCCESS")
            and body.get("stCode") not in (200, "200", None)
        ):
            raise RuntimeError(f"Kotak order book API error: {body}")

        raw_orders = body.get("data", body.get("orders", []))
        if not isinstance(raw_orders, list):
            raw_orders = []

        normalized_orders = []
        for o in raw_orders:
            if not isinstance(o, dict):
                continue
            norm_o = dict(o)
            oid = str(
                o.get("nOrdNo")
                or o.get("orderNo")
                or o.get("order_id")
                or o.get("orderid")
                or ""
            ).strip()
            tok = str(
                o.get("tok")
                or o.get("instrument_token")
                or o.get("symboltoken")
                or ""
            ).strip()
            sym = str(
                o.get("trdSym")
                or o.get("trading_symbol")
                or o.get("tradingsymbol")
                or ""
            ).strip().upper()

            # Status normalization
            raw_st = str(o.get("ordSt") or o.get("status") or o.get("orderstatus") or "").strip().lower()
            if raw_st in ("trig_pending", "trigger pending", "trigger_pending"):
                status = "trigger_pending"
            elif raw_st in ("open", "pending", "open_pending"):
                status = "open"
            elif raw_st in ("complete", "completed", "filled"):
                status = "complete"
            elif raw_st in ("can", "cancelled", "canceled"):
                status = "cancelled"
            elif raw_st in ("rej", "rejected"):
                status = "rejected"
            else:
                status = raw_st.replace(" ", "_")

            # Transaction type normalization
            raw_tt = str(o.get("trnsTp") or o.get("transaction_type") or "").strip().upper()
            ttype = "SELL" if raw_tt in ("S", "SELL") else ("BUY" if raw_tt in ("B", "BUY") else raw_tt)

            # Order type normalization
            raw_ot = str(o.get("ordType") or o.get("pt") or o.get("order_type") or "").strip().upper()
            if raw_ot in ("SL-M", "SL-MKT", "STOPLOSS_MARKET"):
                otype = "STOPLOSS_MARKET"
            elif raw_ot in ("MKT", "MARKET"):
                otype = "MARKET"
            elif raw_ot in ("L", "LMT", "LIMIT"):
                otype = "LIMIT"
            else:
                otype = raw_ot

            prod = str(o.get("prod") or o.get("pc") or o.get("product") or "NRML").strip().upper()
            qty = _to_float(o.get("qty") or o.get("quantity") or 0)
            fld = _to_float(o.get("fldQty") or o.get("filled_quantity") or 0)
            unfld = _to_float(o.get("unfldQty") or o.get("pending_quantity") or max(0, qty - fld))
            trig = _to_float(o.get("trgPrc") or o.get("tp") or o.get("trigger_price") or 0.0)
            tag = str(o.get("ig") or o.get("tag") or "").strip()
            avg_p = _to_float(
                o.get("avgPrc")
                or o.get("avgPrice")
                or o.get("flPrc")
                or o.get("average_price")
                or o.get("prc")
                or o.get("price")
                or 0.0
            )

            norm_o["order_id"] = oid
            norm_o["orderid"] = oid
            norm_o["instrument_token"] = tok
            norm_o["symboltoken"] = tok
            norm_o["trading_symbol"] = sym
            norm_o["tradingsymbol"] = sym
            norm_o["symbol"] = sym
            norm_o["status"] = status
            norm_o["orderstatus"] = status
            norm_o["transaction_type"] = ttype
            norm_o["transactiontype"] = ttype
            norm_o["order_type"] = otype
            norm_o["ordertype"] = otype
            norm_o["product"] = prod
            norm_o["producttype"] = prod
            norm_o["quantity"] = qty
            norm_o["filled_quantity"] = fld
            norm_o["pending_quantity"] = unfld
            norm_o["average_price"] = avg_p
            norm_o["trigger_price"] = trig
            norm_o["triggerprice"] = trig
            norm_o["tag"] = tag

            normalized_orders.append(norm_o)

        return parse_order_book(normalized_orders)

    def get_available_funds(self, token: str) -> float:
        """
        Query available cash / trading margin for option BUY orders via Kotak Neo API.
        Endpoint: POST /quick/user/limits
        """
        if token == MOCK_TOKEN:
            mock_funds = os.getenv("MOCK_AVAILABLE_FUNDS")
            return float(mock_funds) if mock_funds is not None else 500000.0

        url = f"{self.base_url}/quick/user/limits"
        payload = {"seg": "ALL", "exch": "ALL", "prod": "ALL"}
        headers = self._headers(token, content_type="application/x-www-form-urlencoded")
        try:
            resp = requests.post(url, headers=headers, data=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                raise RuntimeError(f"Kotak limits HTTP {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
            if (
                body.get("stat")
                and body.get("stat") not in ("Ok", "OK", "success", "SUCCESS")
                and body.get("stCode") not in (200, "200", None)
            ):
                raise RuntimeError(f"Kotak limits API error: {body}")

            data = body.get("data") or body.get("result") or body
            if isinstance(data, list) and data:
                data = data[0]

            funds = (
                data.get("Net")
                or data.get("availableMargin")
                or data.get("availableCash")
                or data.get("cashBalance")
                or data.get("collateralVal")
                or 0.0
            )
            return float(_to_float(funds))
        except Exception as exc:
            logger.error("Kotak get_available_funds failed: %s", exc)
            raise

    def place_market_order(
        self,
        token: str,
        contract: Dict[str, Any],
        quantity: int,
        transaction_type: str = "BUY",
        tag: str = "AUTO-BUY",
    ) -> Dict[str, Any]:
        """Place an immediate MARKET order (BUY or SELL) via Kotak Neo API."""
        sym = (
            contract.get("trading_symbol")
            or contract.get("tradingsymbol")
            or contract.get("trdSym")
        )
        prod = contract.get("product") or contract.get("producttype") or "NRML"
        ttype = "B" if transaction_type.upper() in ("B", "BUY") else "S"

        payload = {
            "am": "NO",
            "dq": "0",
            "es": "nse_fo",
            "mp": "0",
            "pc": prod,
            "pr": "0",
            "pt": "MKT",
            "qt": str(int(quantity)),
            "rt": "DAY",
            "tp": "0",
            "ts": str(sym),
            "tt": ttype,
            "ig": str(tag),
            "os": NEO_FIN_KEY,
        }

        action_name = "place_market_buy" if ttype == "B" else "place_market_sell"
        if token == MOCK_TOKEN:
            mock_oid = f"mock_kotak_{int(time.time() * 1000)}"
            return {
                "action": action_name,
                "request": payload,
                "success": True,
                "order_id": mock_oid,
                "response": {"stat": "Ok", "stCode": 200, "result": {"nOrdNo": mock_oid}},
            }

        url = f"{self.base_url}/quick/order/rule/ms/place"
        resp = requests.post(url, headers=self._headers(token), data=payload, timeout=REQUEST_TIMEOUT)
        return self._order_response(action_name, resp, payload)

    def fetch_ltp(
        self,
        token: str,
        instrument_token: str,
        trading_symbol: str = "",
        exchange: str = "NFO",
    ) -> Optional[float]:
        """Fetch latest traded price (LTP) from Kotak Neo quotes API."""
        if token == MOCK_TOKEN or not instrument_token:
            return None

        # Map exchange to Kotak Neo segment code
        ex_upper = str(exchange or "").upper()
        segment = "nse_fo" if ex_upper in ("NFO", "NSE_FO", "FO") else "nse_cm"

        url = f"{self.base_url}/script-details/1.0/quotes/neosymbol/{segment}|{instrument_token}/all"
        headers = self._headers(token, content_type="application/json")
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200 or not resp.text.strip():
                logger.debug("Kotak Quote HTTP %d: %s", resp.status_code, resp.text[:300])
                return None
            body = resp.json()
            ltp_val = None
            if isinstance(body, dict):
                ltp_val = body.get("ltp") or body.get("last_traded_price")
                if ltp_val is None and isinstance(body.get("data"), dict):
                    ltp_val = body["data"].get("ltp") or body["data"].get("last_traded_price")
                elif ltp_val is None and isinstance(body.get("data"), list) and body["data"]:
                    ltp_val = body["data"][0].get("ltp") or body["data"][0].get("last_traded_price")
            elif isinstance(body, list) and body:
                ltp_val = body[0].get("ltp") or body[0].get("last_traded_price")

            if ltp_val is not None:
                val = _to_float(ltp_val)
                if val > 0:
                    return val
        except Exception as exc:
            logger.debug("Kotak fetch_ltp exception for token %s: %s", instrument_token, exc)

        return None

    def place_exit_order_slm(
        self,
        token: str,
        position: Dict[str, Any],
        target_qty: int,
        trigger_price: float,
        product: Optional[str] = None,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        """Place a STOPLOSS_MARKET (SL-M) sell order via Kotak Neo API."""
        trigger_price = round_to_tick(trigger_price)
        sym = (
            position.get("tradingsymbol")
            or position.get("trading_symbol")
            or position.get("trdSym")
        )
        prod = product or self.get_exit_product(position) or "NRML"

        payload = {
            "am": "NO",
            "dq": "0",
            "es": "nse_fo",
            "mp": "0",
            "pc": prod,
            "pr": "0",
            "pt": "SL-M",
            "qt": str(int(target_qty)),
            "rt": "DAY",
            "tp": f"{trigger_price:.2f}",
            "ts": str(sym),
            "tt": "S",
            "ig": str(tag),
            "os": NEO_FIN_KEY,
        }

        if token == MOCK_TOKEN:
            mock_oid = f"mock_kotak_{int(time.time() * 1000)}"
            return {
                "action": "place",
                "request": payload,
                "success": True,
                "order_id": mock_oid,
                "response": {"stat": "Ok", "stCode": 200, "result": {"nOrdNo": mock_oid}},
            }

        url = f"{self.base_url}/quick/order/rule/ms/place"
        resp = requests.post(url, headers=self._headers(token), data=payload, timeout=REQUEST_TIMEOUT)
        return self._order_response("place", resp, payload)

    def place_exit_order_market(
        self,
        token: str,
        position: Dict[str, Any],
        target_qty: int,
        product: Optional[str] = None,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        """Place an immediate MARKET sell order via Kotak Neo API."""
        sym = (
            position.get("tradingsymbol")
            or position.get("trading_symbol")
            or position.get("trdSym")
        )
        prod = product or self.get_exit_product(position) or "NRML"

        payload = {
            "am": "NO",
            "dq": "0",
            "es": "nse_fo",
            "mp": "0",
            "pc": prod,
            "pr": "0",
            "pt": "MKT",
            "qt": str(int(target_qty)),
            "rt": "DAY",
            "tp": "0",
            "ts": str(sym),
            "tt": "S",
            "ig": str(tag),
            "os": NEO_FIN_KEY,
        }

        if token == MOCK_TOKEN:
            mock_oid = f"mock_kotak_{int(time.time() * 1000)}"
            return {
                "action": "place_market",
                "request": payload,
                "success": True,
                "order_id": mock_oid,
                "response": {"stat": "Ok", "stCode": 200, "result": {"nOrdNo": mock_oid}},
            }

        url = f"{self.base_url}/quick/order/rule/ms/place"
        resp = requests.post(url, headers=self._headers(token), data=payload, timeout=REQUEST_TIMEOUT)
        return self._order_response("place_market", resp, payload)

    def modify_exit_order_slm(
        self,
        token: str,
        order: Dict[str, Any],
        target_qty: int,
        trigger_price: float,
        tag: str = "AUTO-EXIT",
    ) -> Dict[str, Any]:
        """Modify an existing SL-M order trigger price and quantity."""
        trigger_price = round_to_tick(trigger_price)
        total_qty = order_filled_quantity(order) + target_qty
        oid = str(
            order.get("order_id")
            or order.get("orderid")
            or order.get("nOrdNo")
            or order.get("no")
            or ""
        )

        payload = {
            "no": oid,
            "pr": "0",
            "pt": "SL-M",
            "qt": str(int(total_qty)),
            "tp": f"{trigger_price:.2f}",
            "vd": "DAY",
            "am": "NO",
            "mp": "0",
            "dq": "0",
            "os": NEO_FIN_KEY,
        }

        if token == MOCK_TOKEN:
            return {
                "action": "modify",
                "request": payload,
                "success": True,
                "order_id": oid,
                "response": {"stat": "Ok", "stCode": 200, "result": {"nOrdNo": oid}},
            }

        url = f"{self.base_url}/quick/order/vr/modify"
        resp = requests.post(url, headers=self._headers(token), data=payload, timeout=REQUEST_TIMEOUT)
        return self._order_response("modify", resp, payload)

    def cancel_order(
        self,
        token: str,
        order_id: str,
        variety: str = "STOPLOSS",
    ) -> Dict[str, Any]:
        """Cancel an open order."""
        payload = {
            "on": str(order_id),
            "am": "NO",
        }

        if token == MOCK_TOKEN:
            return {
                "action": "cancel",
                "order_id": str(order_id),
                "success": True,
                "http_status": 200,
                "response": {"stat": "Ok", "stCode": 200},
            }

        url = f"{self.base_url}/quick/order/cancel"
        resp = requests.post(url, headers=self._headers(token), data=payload, timeout=REQUEST_TIMEOUT)
        result: Dict[str, Any] = {"action": "cancel", "order_id": str(order_id)}
        try:
            body = resp.json()
            result["response"] = body
            result["http_status"] = resp.status_code
            success = (
                resp.status_code in (200, 201)
                and isinstance(body, dict)
                and (
                    body.get("stat") in ("Ok", "OK", "success", "SUCCESS")
                    or body.get("stCode") in (200, "200")
                )
            )
            result["success"] = success
            if not success:
                result["error"] = body.get("errMsg") or body.get("message") or f"HTTP {resp.status_code}"
        except Exception as exc:
            result["success"] = False
            result["http_status"] = getattr(resp, "status_code", None)
            result["error"] = f"Failed to parse cancel response: {exc}"
            result["response"] = {"raw": getattr(resp, "text", "")[:300]}
        return result

    def get_exit_product(
        self,
        position: Dict[str, Any],
        cli_product: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve valid product code for Kotak Neo ('NRML' or 'MIS')."""
        raw = str(position.get("product") or position.get("producttype") or position.get("prod") or "").upper().strip()
        if raw in ("CARRYFORWARD", "NRML", "CNC", "D"):
            return "NRML"
        if raw in ("INTRADAY", "MIS", "I"):
            return "MIS"
        if cli_product:
            cli = str(cli_product).upper().strip()
            if cli in ("NRML", "MIS"):
                return cli
            if cli in ("CARRYFORWARD", "D", "CNC"):
                return "NRML"
            if cli in ("INTRADAY", "I"):
                return "MIS"
        return "NRML"

    def _order_response(self, action: str, resp, payload: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"action": action, "request": payload}
        try:
            data = resp.json()
            result["response"] = data
            result["http_status"] = resp.status_code
            is_stat_ok = data.get("stat") in ("Ok", "OK", "success", "SUCCESS")
            is_st_code_ok = data.get("stCode") in (200, "200")
            if resp.status_code in (200, 201) and (is_stat_ok or is_st_code_ok):
                result["success"] = True
                order_id = (
                    (data.get("result") or {}).get("nOrdNo")
                    or (data.get("result") or {}).get("orderId")
                    or (data.get("data") or {}).get("nOrdNo")
                    or data.get("nOrdNo")
                    or (data.get("data") or {}).get("order_id")
                )
                result["order_id"] = str(order_id) if order_id else None
            else:
                result["success"] = False
                result["error"] = data.get("errMsg") or data.get("message") or f"HTTP {resp.status_code}"
        except Exception as exc:
            result["success"] = False
            result["http_status"] = getattr(resp, "status_code", None)
            result["error"] = f"Failed to parse broker response: {exc}"
            result["response"] = {"raw": getattr(resp, "text", "")[:300]}
        return result
