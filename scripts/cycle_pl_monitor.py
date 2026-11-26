#!/usr/bin/env python3
"""
Read-only P&L monitor for one strategy cycle (Tuesday OR Thursday).

Usage:
    .venv/bin/python scripts/cycle_pl_monitor.py --thursday
    .venv/bin/python scripts/cycle_pl_monitor.py --tuesday --watch 60
    .venv/bin/python scripts/cycle_pl_monitor.py --thursday --json

Data sources (in order of trust):
  * BUY leg identity and buy price  -> local DB (strategy_buy_snapshots), selected by track tag
  * Open / sold status and quantity -> broker positions and order book (live)
  * Sold price for legs sold today  -> broker order book (SELL orders, filled qty, avg price)
  * Current LTP of open legs        -> broker quote API (live)
  * Sold price for legs sold on an earlier day -> local DB (strategy_hourly_data FSM exit price).
    If that is also missing, the script exits with code 2 ("insufficient data").

SAFETY: this script never writes to the DB or Redis, and never places, modifies or
cancels orders. It only calls fetch_positions, fetch_order_book and fetch_ltp.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SQLITE_DIR = os.environ.get("SQLITE_STRATEGIES_DIR", "/home/ubuntu/sqlite/strategies")
IST = timezone(timedelta(hours=5, minutes=30))

TRACKS = {
    "tuesday": {
        "strategy_name": "nifty50_weekly_option_collector",
        "buy_tag": "TUE-BUY",
        "db_glob": "nifty50_weekly_data_*_*.db",
    },
    "thursday": {
        "strategy_name": "nifty50_thursday_3day_collector",
        "buy_tag": "THU-BUY",
        "db_glob": "nifty50_thursday_data_*_*.db",
    },
}

LEG_TYPES = (("CALL", "CE", "call"), ("PUT", "PE", "put"))
PRICE_MISMATCH_TOLERANCE = 0.05
SELL_STATUSES = ("complete", "filled", "partially filled", "partially_filled")


class InsufficientData(Exception):
    """Raised when a leg's P&L cannot be computed from broker or local DB data."""


# ---------------------------------------------------------------------------
# Local DB (read-only)
# ---------------------------------------------------------------------------

def _ro_connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def find_latest_cycle(track: str, cycle_id: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Return (db_path, buy_snapshot_row) for the newest FILLED cycle of the track."""
    spec = TRACKS[track]
    best: Optional[Tuple[int, str, Dict[str, Any]]] = None
    for db_path in glob.glob(os.path.join(SQLITE_DIR, spec["db_glob"])):
        try:
            conn = _ro_connect(db_path)
        except sqlite3.Error:
            continue
        try:
            conn.row_factory = sqlite3.Row
            if cycle_id:
                rows = conn.execute(
                    "SELECT * FROM strategy_buy_snapshots WHERE strategy_name = ? AND cycle_id = ?",
                    (spec["strategy_name"], cycle_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM strategy_buy_snapshots WHERE strategy_name = ? AND entry_status = 'FILLED'",
                    (spec["strategy_name"],),
                ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for row in rows:
            row = dict(row)
            key = int(row.get("captured_at") or 0)
            if best is None or key > best[0]:
                best = (key, db_path, row)
    if best is None:
        raise InsufficientData(f"No {track} cycle found in {SQLITE_DIR} (cycle_id={cycle_id or 'latest'})")
    return best[1], best[2]


def load_prior_sold_price(db_path: str, cycle_id: str, leg: str) -> Optional[float]:
    """Latest FSM exit price recorded for a leg in the local DB (used only for earlier-day sells)."""
    col = "fsm_call_exit_price" if leg == "CALL" else "fsm_put_exit_price"
    conn = _ro_connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {col} FROM strategy_hourly_data WHERE cycle_id = ? AND {col} IS NOT NULL "
            f"ORDER BY id DESC LIMIT 1",
            (cycle_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if not row or row[0] in (None, 0):
        return None
    return float(row[0])


# ---------------------------------------------------------------------------
# Broker (read-only)
# ---------------------------------------------------------------------------

def _as_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    return []


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _order_sell_fills(order: Dict[str, Any]) -> Tuple[int, float]:
    """(filled_qty, avg_price) for a SELL order; (0, 0.0) if nothing filled."""
    filled = int(_f(order.get("filled_quantity")))
    avg = _f(order.get("average_price"))
    if filled <= 0 or avg <= 0:
        return 0, 0.0
    return filled, avg


def _contract_matches(item: Dict[str, Any], strike: int, opt_type: str, expiry_yyyymmdd: str) -> bool:
    """Exact contract identity using the repo matcher; ambiguous (None) counts as no match."""
    from common.broker_adapters import contract_match_status

    return contract_match_status(
        item,
        target_strike=strike,
        target_opt_type=opt_type,
        target_expiry=expiry_yyyymmdd,
        target_underlying="NIFTY",
    ) is True


class BrokerView:
    """Snapshot of broker state for one run. Only read calls are made."""

    def __init__(self, broker: str):
        from common.broker_adapters import get_broker_adapter
        from common.entry_order_manager import get_broker_token

        self.broker = broker
        self.adapter = get_broker_adapter(broker)
        self.token = get_broker_token(broker)
        self.positions = _as_list(self.adapter.fetch_positions(self.token))
        self.orders = self.adapter.fetch_order_book(self.token)

    def order_by_id(self, order_id: str) -> Optional[Dict[str, Any]]:
        for o in self.orders:
            if str(o.get("order_id") or "") == str(order_id):
                return o
        return None

    def position_for(self, instrument_token: str = "", strike: int = 0, opt_type: str = "", expiry: str = "") -> Optional[Dict[str, Any]]:
        for p in self.positions:
            if instrument_token and str(p.get("instrument_token") or "") == instrument_token:
                return p
        if strike and opt_type and expiry:
            for p in self.positions:
                if _contract_matches(p, strike, opt_type, expiry):
                    return p
        return None

    def sells_today(self, instrument_token: str, symbol: str) -> List[Tuple[int, float, str]]:
        """Filled SELL orders for this contract today: [(qty, avg_price, order_id)]."""
        out = []
        for o in self.orders:
            if str(o.get("transaction_type") or "").upper() != "SELL":
                continue
            same = (instrument_token and str(o.get("instrument_token") or "") == instrument_token) or (
                symbol and str(o.get("trading_symbol") or "").upper() == symbol.upper()
            )
            if not same:
                continue
            qty, avg = _order_sell_fills(o)
            if qty > 0:
                out.append((qty, avg, str(o.get("order_id"))))
        return out

    def ltp(self, instrument_token: str, symbol: str) -> Optional[float]:
        return self.adapter.fetch_ltp(self.token, instrument_token, symbol)


# ---------------------------------------------------------------------------
# P&L math (pure; unit-tested)
# ---------------------------------------------------------------------------

def value_leg(
    buy_qty: int,
    buy_price: float,
    open_qty: int,
    ltp: Optional[float],
    sells_today: List[Tuple[int, float, str]],
    prior_sold_price: Optional[float],
) -> Dict[str, Any]:
    """
    P&L for one leg, measured from the BUY price.

      realised   = sum over sells of (sell_price - buy_price) * qty
                   (sells today from broker; any quantity sold on an earlier day at the DB price)
      unrealised = open_qty * (ltp - buy_price)
    """
    if buy_qty <= 0:
        raise InsufficientData("BUY quantity is unknown or zero")
    sold_today_qty = sum(q for q, _, _ in sells_today)
    prior_qty = buy_qty - open_qty - sold_today_qty
    if open_qty < 0 or prior_qty < 0:
        raise InsufficientData(
            f"Quantity mismatch: buy={buy_qty} open={open_qty} sold_today={sold_today_qty}"
        )

    realised = 0.0
    for q, avg, _ in sells_today:
        realised += (avg - buy_price) * q

    if prior_qty > 0:
        if prior_sold_price is None:
            raise InsufficientData(
                f"{prior_qty} qty SOLD on an earlier day: sell price not known from broker or local DB"
            )
        realised += (prior_sold_price - buy_price) * prior_qty

    unrealised = 0.0
    if open_qty > 0:
        if ltp is None or ltp <= 0:
            raise InsufficientData("Open leg has no live LTP from broker")
        unrealised = (ltp - buy_price) * open_qty

    return {
        "buy_qty": buy_qty,
        "buy_price": buy_price,
        "open_qty": open_qty,
        "ltp": ltp,
        "sold_today_qty": sold_today_qty,
        "sold_today_avg": (sum(q * a for q, a, _ in sells_today) / sold_today_qty) if sold_today_qty else None,
        "prior_sold_qty": prior_qty,
        "prior_sold_price": prior_sold_price,
        "realised": round(realised, 2),
        "unrealised": round(unrealised, 2),
        "leg_pl": round(realised + unrealised, 2),
        "cost": round(buy_price * buy_qty, 2),
    }


# ---------------------------------------------------------------------------
# Cycle evaluation
# ---------------------------------------------------------------------------

def _resolve_leg(broker: BrokerView, leg_name: str, opt_type: str, db_row: Dict[str, Any], expiry: str,
                 buy_tag: str, warnings: List[str], snapshot_entry_qty: Optional[int]) -> Dict[str, Any]:
    prefix = "call" if leg_name == "CALL" else "put"
    order_id = str(db_row.get(f"{prefix}_order_id") or "")
    strike = int(db_row.get(f"{prefix}_strike") or 0)
    db_buy_price = _f(db_row.get(f"{prefix}_buy_price"))
    if not order_id or strike <= 0 or db_buy_price <= 0:
        raise InsufficientData(f"{leg_name}: incomplete BUY record in DB (order_id/strike/buy_price)")

    buy_order = broker.order_by_id(order_id)
    instrument_token = ""
    symbol = ""
    buy_qty = 0
    if buy_order is not None:
        instrument_token = str(buy_order.get("instrument_token") or "")
        symbol = str(buy_order.get("trading_symbol") or "")
        buy_qty = int(_f(buy_order.get("filled_quantity")))
        if str(buy_order.get("status") or "").lower() != "complete":
            warnings.append(f"{leg_name}: BUY order {order_id} status is {buy_order.get('status')!r}")
        if buy_tag and str(buy_order.get("tag") or "") != buy_tag:
            warnings.append(f"{leg_name}: BUY order {order_id} tag {buy_order.get('tag')!r} != {buy_tag!r}")
        broker_avg = _f(buy_order.get("average_price"))
        if broker_avg > 0 and abs(broker_avg - db_buy_price) > PRICE_MISMATCH_TOLERANCE:
            warnings.append(
                f"{leg_name}: DB buy price {db_buy_price} differs from broker avg {broker_avg} (DB used)"
            )
    else:
        warnings.append(f"{leg_name}: BUY order {order_id} not in today's order book (earlier-day entry)")

    pos = broker.position_for(instrument_token=instrument_token, strike=strike, opt_type=opt_type, expiry=expiry)
    if pos is not None:
        instrument_token = instrument_token or str(pos.get("instrument_token") or "")
        symbol = symbol or str(pos.get("trading_symbol") or pos.get("tradingsymbol") or "")
    open_qty = int(_f(pos.get("quantity"))) if pos is not None else 0

    qty_source = "broker order book" if buy_order is not None else None
    if buy_qty <= 0 and pos is not None:
        buy_qty = int(_f(pos.get("overnight_buy_quantity")) + _f(pos.get("day_buy_quantity")))
        qty_source = "broker positions"
    if buy_qty <= 0 and snapshot_entry_qty:
        # Broker has no record of an earlier-day BUY (order book is today-only); use the
        # cycle's local snapshot entry_qty. This is labelled in the output as a fallback.
        buy_qty = int(snapshot_entry_qty)
        qty_source = "local snapshot entry_qty (fallback)"
        warnings.append(f"{leg_name}: BUY quantity taken from local snapshot ({buy_qty}); broker has no record of BUY {order_id}")

    return {
        "leg": leg_name,
        "strike": strike,
        "opt_type": opt_type,
        "order_id": order_id,
        "instrument_token": instrument_token,
        "symbol": symbol,
        "buy_price": db_buy_price,
        "open_qty": open_qty,
        "buy_qty": buy_qty,
        "qty_source": qty_source,
    }


def _snapshot_entry_qty(track: str, cycle_id: str) -> Optional[int]:
    """entry_qty of the active local snapshot if it is the same cycle (fallback only)."""
    fname = "current_week_buy.json" if track == "tuesday" else "current_thursday_buy.json"
    try:
        with open(os.path.join(SQLITE_DIR, fname), encoding="utf-8") as fh:
            snap = json.load(fh)
    except (OSError, ValueError):
        return None
    if str(snap.get("cycle_id") or "") != cycle_id:
        return None
    qty = int(_f(snap.get("entry_qty")))
    return qty if qty > 0 else None


def _leg_status(open_qty: int, sold_qty: int) -> str:
    if open_qty > 0 and sold_qty > 0:
        return "PARTIAL"
    if open_qty > 0:
        return "OPEN"
    return "SOLD"


def evaluate_cycle(track: str, broker_name: Optional[str] = None, cycle_id: Optional[str] = None,
                   broker_view: Optional["BrokerView"] = None) -> Dict[str, Any]:
    if track not in TRACKS:
        raise InsufficientData(f"Unknown track {track!r}")
    spec = TRACKS[track]
    db_path, row = find_latest_cycle(track, cycle_id)
    broker_name = (broker_name or row.get("broker") or "upstox").lower()
    expiry = str(row["expiry_date"])
    cycle = str(row["cycle_id"])
    warnings: List[str] = []

    broker = broker_view or BrokerView(broker_name)
    legs_out: Dict[str, Any] = {}
    total_pl = 0.0
    total_cost = 0.0

    snapshot_qty = _snapshot_entry_qty(track, cycle)
    for leg_name, opt_type, _ in LEG_TYPES:
        spec_leg = _resolve_leg(broker, leg_name, opt_type, row, expiry, spec["buy_tag"], warnings, snapshot_qty)
        sells = broker.sells_today(spec_leg["instrument_token"], spec_leg["symbol"])
        ltp = None
        if spec_leg["open_qty"] > 0:
            ltp = broker.ltp(spec_leg["instrument_token"], spec_leg["symbol"])
        prior_price = None
        sold_today = sum(q for q, _, _ in sells)
        if spec_leg["buy_qty"] - spec_leg["open_qty"] - sold_today > 0:
            prior_price = load_prior_sold_price(db_path, cycle, leg_name)
        val = value_leg(spec_leg["buy_qty"], spec_leg["buy_price"], spec_leg["open_qty"], ltp, sells, prior_price)
        val.update({
            "leg": leg_name,
            "strike": spec_leg["strike"],
            "opt_type": opt_type,
            "symbol": spec_leg["symbol"],
            "order_id": spec_leg["order_id"],
            "instrument_token": spec_leg["instrument_token"],
            "qty_source": spec_leg["qty_source"],
            "status": _leg_status(spec_leg["open_qty"], sold_today + val["prior_sold_qty"]),
            "sold_source": (
                "broker order book" if sold_today else ("local DB" if prior_price is not None and val["prior_sold_qty"] > 0 else None)
            ),
        })
        legs_out[leg_name] = val
        total_pl += val["leg_pl"]
        total_cost += val["cost"]

    return {
        "track": track,
        "cycle_id": cycle,
        "expiry": expiry,
        "broker": broker_name,
        "db_path": db_path,
        "timestamp_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "legs": legs_out,
        "total_pl": round(total_pl, 2),
        "total_cost": round(total_cost, 2),
        "total_pl_pct": round(total_pl / total_cost * 100, 2) if total_cost else 0.0,
        "currency_note": "All P&L values are INR = (price - BUY price) x quantity",
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def render(result: Dict[str, Any]) -> str:
    lines = [
        f"{result['timestamp_ist']}  track={result['track']}  cycle={result['cycle_id']}  "
        f"expiry={result['expiry']}  broker={result['broker']}",
        f"{'LEG':<6}{'CONTRACT':<22}{'QTY':>5}{'BUY':>10}{'OPEN':>6}{'LTP':>10}{'SOLD(qty@px)':>20}"
        f"{'REALISED':>12}{'UNREAL':>12}{'LEG P&L':>12}  STATUS",
    ]
    for name in ("CALL", "PUT"):
        l = result["legs"][name]
        sold = "-"
        if l["sold_today_qty"]:
            sold = f"{l['sold_today_qty']}@{l['sold_today_avg']:.2f}"
        if l["prior_sold_qty"] > 0:
            sold += f" +{l['prior_sold_qty']}@{l['prior_sold_price']:.2f}(DB)"
        ltp = f"{l['ltp']:.2f}" if l["ltp"] is not None else "-"
        contract = l["symbol"] or f"{l['strike']} {l['opt_type']}"
        lines.append(
            f"{name:<6}{contract[:21]:<22}{l['buy_qty']:>5}{l['buy_price']:>10.2f}"
            f"{l['open_qty']:>6}{ltp:>10}{sold:>20}{l['realised']:>12.2f}{l['unrealised']:>12.2f}"
            f"{l['leg_pl']:>12.2f}  {l['status']}"
        )
    lines.append(
        f"TOTAL P&L: INR {result['total_pl']:+,.2f}   "
        f"P&L %: {result['total_pl_pct']:+.2f}% of BUY cost INR {result['total_cost']:,.2f}"
    )
    for w in result["warnings"]:
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Combined profit-exit (ACTS ON THE BROKER ONLY WITH --execute)
# ---------------------------------------------------------------------------

PROFIT_EXIT_TAG = "PT20-SQ"
PROFIT_EXIT_REASON = "PROFIT_TARGET_20PCT_SQUAREOFF"
MARKET_OPEN_IST = (9, 15)
MARKET_CLOSE_IST = (15, 30)
FILL_POLL_SECONDS = 2.0
FILL_POLL_ATTEMPTS = 15
TRACK_AUTO_STRATEGY = {"tuesday": "5day_strategy", "thursday": "3day_strategy"}


def _is_market_open_ist(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(IST)
    hm = (now.hour, now.minute)
    return now.weekday() < 5 and MARKET_OPEN_IST <= hm <= MARKET_CLOSE_IST


def open_cycle_orders(broker: "BrokerView", leg_tokens: set, leg_symbols: set) -> List[Dict[str, Any]]:
    """Open broker orders that belong to this cycle's legs. Other orders are ignored."""
    from common.broker_adapters import OPEN_ORDER_STATUSES, normalize_order_status

    found = []
    for o in broker.orders:
        if normalize_order_status(o.get("status")) not in OPEN_ORDER_STATUSES:
            continue
        tok = str(o.get("instrument_token") or "")
        sym = str(o.get("trading_symbol") or "").upper()
        if (tok and tok in leg_tokens) or (sym and sym in leg_symbols):
            found.append({k: o.get(k) for k in ("order_id", "status", "transaction_type", "trading_symbol", "quantity", "tag")})
    return found


def plan_profit_exit(result: Dict[str, Any], threshold_pct: float) -> Dict[str, Any]:
    """Decide what to sell. Combined P&L % is measured over both legs of the cycle."""
    legs = result["legs"]
    active = [l for l in legs.values() if l["open_qty"] > 0]
    combined_pct = result["total_pl_pct"]
    if not active:
        return {"action": "none", "reason": "no active leg at broker"}
    if combined_pct < threshold_pct:
        return {"action": "none", "reason": f"combined P&L {combined_pct:+.2f}% below threshold {threshold_pct:.2f}%"}
    # Higher leg P&L first; a single active leg is sold regardless of its own sign.
    ordered = sorted(active, key=lambda l: l["leg_pl"], reverse=True)
    return {
        "action": "square_off",
        "combined_pl_pct": combined_pct,
        "sequence": [
            {"leg": l["leg"], "symbol": l["symbol"], "strike": l["strike"], "opt_type": l["opt_type"],
             "qty": l["open_qty"], "ltp": l["ltp"], "leg_pl": l["leg_pl"], "instrument_token": l["instrument_token"]}
            for l in ordered
        ],
    }


def _place_market_sell(broker: "BrokerView", leg: Dict[str, Any], qty: int) -> Dict[str, Any]:
    """The only place that submits an order. Called only from run_profit_exit when execute=True."""
    pos = broker.position_for(instrument_token=leg["instrument_token"]) or {}
    position = {"instrument_token": leg["instrument_token"], "tradingsymbol": leg["symbol"]}
    product = broker.adapter.get_exit_product(pos) if pos else None
    return broker.adapter.place_exit_order_market(
        broker.token, position, qty, product=product or "D", tag=PROFIT_EXIT_TAG,
    )


def _wait_for_fill(broker: "BrokerView", order_id: str) -> Dict[str, Any]:
    from common.broker_adapters import TERMINAL_CANCELLED_STATUSES, TERMINAL_FILLED_STATUSES, normalize_order_status

    for _ in range(FILL_POLL_ATTEMPTS):
        broker.orders = broker.adapter.fetch_order_book(broker.token)
        o = broker.order_by_id(order_id)
        if o is not None:
            st = normalize_order_status(o.get("status"))
            if st in TERMINAL_FILLED_STATUSES:
                return {"state": "filled", "filled_qty": int(_f(o.get("filled_quantity"))),
                        "avg_price": _f(o.get("average_price"))}
            if st in TERMINAL_CANCELLED_STATUSES:
                return {"state": st, "filled_qty": int(_f(o.get("filled_quantity"))),
                        "avg_price": _f(o.get("average_price"))}
        time.sleep(FILL_POLL_SECONDS)
    return {"state": "timeout", "filled_qty": 0, "avg_price": 0.0}


def _fresh_open_qty(broker: "BrokerView", instrument_token: str) -> int:
    """Re-read the broker position right before selling: the leg must still be open."""
    broker.positions = _as_list(broker.adapter.fetch_positions(broker.token))
    pos = broker.position_for(instrument_token=instrument_token)
    return int(_f(pos.get("quantity"))) if pos is not None else 0


def _expiry_iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _market_holiday_flag() -> Optional[str]:
    """Redis is_market_holiday flag ('yes'/'no'). None if Redis is unreadable."""
    from config import get_redis_client

    try:
        r = get_redis_client()
        val = r.get("is_market_holiday") if r else None
    except Exception:
        return None
    if isinstance(val, bytes):
        val = val.decode("utf-8", "replace")
    return str(val).strip().lower() if val is not None else None


def _cancel_and_confirm(view: "BrokerView", orders: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Cancel the cycle's open orders and wait until each one is confirmed cancelled."""
    from common.broker_adapters import OPEN_ORDER_STATUSES, TERMINAL_CANCELLED_STATUSES, normalize_order_status

    requests_out = []
    for o in orders:
        oid = str(o["order_id"])
        resp = view.adapter.cancel_order(view.token, oid)
        requests_out.append({"order_id": oid, "request_success": bool(resp.get("success"))})

    pending = {str(o["order_id"]) for o in orders}
    final: Dict[str, str] = {}
    for _ in range(FILL_POLL_ATTEMPTS):
        view.orders = view.adapter.fetch_order_book(view.token)
        for oid in list(pending):
            found = view.order_by_id(oid)
            st = normalize_order_status((found or {}).get("status"))
            if st in TERMINAL_CANCELLED_STATUSES:
                final[oid] = "cancelled"
                pending.discard(oid)
            elif st and st not in OPEN_ORDER_STATUSES:
                final[oid] = st  # e.g. filled meanwhile: must NOT be sold again
                pending.discard(oid)
        if not pending:
            break
        time.sleep(FILL_POLL_SECONDS)
    for oid in pending:
        final[oid] = "unconfirmed"
    return {"requests": requests_out, "final": final,
            "confirmed": all(v == "cancelled" for v in final.values())}


def run_profit_exit(track: str, threshold_pct: float = 20.0, execute: bool = False,
                    broker_name: Optional[str] = None, cycle_id: Optional[str] = None,
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(IST)

    # 0. Exchange holiday: stop before any broker call for the day.
    if _market_holiday_flag() == "yes":
        return {"action": "skipped_market_holiday", "track": track,
                "note": "Redis is_market_holiday=yes; no broker calls made"}

    db_path, row = find_latest_cycle(track, cycle_id)
    broker_name = (broker_name or row.get("broker") or "upstox").lower()
    view = BrokerView(broker_name)
    result = evaluate_cycle(track, broker_name=broker_name, cycle_id=cycle_id, broker_view=view)

    # 1. Orders on this cycle's legs (anything else at the broker is ignored).
    tokens = {l["instrument_token"] for l in result["legs"].values() if l["instrument_token"]}
    symbols = {str(l["symbol"]).upper() for l in result["legs"].values() if l["symbol"]}
    open_orders = open_cycle_orders(view, tokens, symbols)

    # 2. Threshold on combined P&L over both legs.
    plan = plan_profit_exit(result, threshold_pct)
    out: Dict[str, Any] = {
        "cycle_id": result["cycle_id"], "track": track, "threshold_pct": threshold_pct,
        "combined_pl_pct": result["total_pl_pct"], "plan": plan,
        "open_cycle_orders": open_orders,
        "mode": "execute" if execute else "dry_run",
    }
    if plan["action"] != "square_off":
        return out  # below threshold: open orders are reported only, never touched
    if not execute:
        out["note"] = "dry-run: nothing sent. Re-run with --execute to cancel open orders and square off."
        if open_orders:
            out["would_cancel"] = [o["order_id"] for o in open_orders]
        return out
    if not _is_market_open_ist(now):
        out["action"] = "refused_market_closed"
        return out

    # 3. Threshold met: cancel this cycle's open orders and confirm before any SELL.
    if open_orders:
        cancel = _cancel_and_confirm(view, open_orders)
        out["cancel"] = cancel
        if not cancel["confirmed"]:
            out["action"] = "aborted_open_orders_not_cancelled"
            return out

    # 4. Sequential square-off: higher-P&L leg first; second leg only after the first is confirmed filled.
    expiry_iso = _expiry_iso(result["expiry"])
    fills = []
    for step in plan["sequence"]:
        qty_now = _fresh_open_qty(view, step["instrument_token"])
        if qty_now <= 0:
            fills.append({"leg": step["leg"], "skipped": "not active at broker"})
            continue
        resp = _place_market_sell(view, step, qty_now)
        if not resp.get("success"):
            fills.append({"leg": step["leg"], "error": "order rejected", "response": resp})
            break
        fill = _wait_for_fill(view, resp.get("order_id"))
        fill.update({"leg": step["leg"], "order_id": resp.get("order_id"), "qty_requested": qty_now})
        fills.append(fill)
        if fill["state"] != "filled" or fill["filled_qty"] != qty_now:
            break  # do not start the second leg until the first is confirmed

        # 5. Record the fill so the FSM stops issuing exits for this leg.
        from scripts.auto_record_manual_exit import auto_record_manual_exit

        try:
            fill["db_record"] = auto_record_manual_exit(
                track=TRACK_AUTO_STRATEGY[track], broker=broker_name, strike=int(step["strike"]),
                option_type=step["opt_type"], expiry_iso=expiry_iso, sell_price=fill["avg_price"],
                sell_ts=int(now.timestamp()), sell_qty=fill["filled_qty"], dry_run=False,
                exit_reason_override=PROFIT_EXIT_REASON, status_override="PROFIT_TARGET_CLOSED",
            )
        except Exception as exc:  # the SELL already filled: report it, do not hide it
            fill["db_record_error"] = f"{type(exc).__name__}: {exc}"
            break
    out["action"] = "square_off_executed"
    out["fills"] = fills
    return out


def _run_once(args) -> int:
    track = "tuesday" if args.tuesday else "thursday"
    as_json = not args.table
    try:
        result = evaluate_cycle(track, broker_name=args.broker, cycle_id=args.cycle_id)
    except InsufficientData as exc:
        if as_json:
            print(json.dumps({"status": "insufficient_data", "track": track, "message": str(exc)}, indent=2))
        else:
            print(f"INSUFFICIENT DATA ({track}): {exc}")
        return 2
    except Exception as exc:  # broker/network/config errors
        if as_json:
            print(json.dumps({"status": "error", "track": track, "message": str(exc)}, indent=2))
        else:
            print(f"ERROR ({track}): {type(exc).__name__}: {exc}")
        return 1
    if as_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render(result))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only P&L monitor for one Tuesday or Thursday cycle.")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--tuesday", action="store_true", help="Monitor the Tuesday (TUE-BUY) cycle")
    grp.add_argument("--thursday", action="store_true", help="Monitor the Thursday (THU-BUY) cycle")
    parser.add_argument("--broker", default=None, help="Override broker (default: broker recorded in DB)")
    parser.add_argument("--cycle-id", default=None, help="Specific cycle_id (default: latest FILLED cycle)")
    parser.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                        help="Repeat every N seconds until interrupted (e.g. 60)")
    parser.add_argument("--table", action="store_true", help="Print a human-readable table instead of JSON")
    parser.add_argument("--profit-exit", action="store_true",
                        help="Check the combined profit threshold and square off. Dry-run unless --execute.")
    parser.add_argument("--threshold", type=float, default=20.0, help="Combined P&L %% threshold (default 20)")
    parser.add_argument("--execute", action="store_true",
                        help="With --profit-exit: actually send MARKET SELL orders and record fills.")
    args = parser.parse_args(argv)

    if args.profit_exit:
        track = "tuesday" if args.tuesday else "thursday"
        try:
            res = run_profit_exit(track, threshold_pct=args.threshold, execute=args.execute,
                                  broker_name=args.broker, cycle_id=args.cycle_id)
        except InsufficientData as exc:
            print(json.dumps({"status": "insufficient_data", "track": track, "message": str(exc)}, indent=2))
            return 2
        except Exception as exc:
            print(json.dumps({"status": "error", "track": track, "message": f"{type(exc).__name__}: {exc}"}, indent=2))
            return 1
        print(json.dumps(res, indent=2, default=str))
        # 3 = a square-off was attempted but a leg was not confirmed filled (needs review)
        failed = [f for f in res.get("fills", []) if "skipped" not in f and (f.get("state") != "filled" or "error" in f or "db_record_error" in f)]
        return 3 if failed or str(res.get("action", "")).startswith("aborted") else 0

    if args.watch <= 0:
        return _run_once(args)
    try:
        while True:
            _run_once(args)
            print("-" * 60, flush=True)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
