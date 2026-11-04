#!/usr/bin/env python3
"""
Record a manual (broker app) square-off of one strategy leg so the automation respects it.

What it does (never calls any broker API):
  1. Marks the leg MANUAL_CLOSED in the cycle's Alpha FSM state (current_week_buy.json /
     current_thursday_buy.json) with your fill price -> FSM stops evaluating the leg, no further
     Alpha exit signal / Discord exit alert for it, realized P&L uses your fill.
  2. Records the exited quantity in Redis (fno:{broker}:exited:{cycle}:{expiry}:{strike}:{type})
     so, on a contract shared with the other track on the same broker, the other track's exit
     manager knows this lot is gone and can still exit its own lot.
  3. Marks the leg's Alpha exit as triggered in active_strategies.json (no later publish) and
     retires any active exit authorization for that leg (unless a managed order is still tracked,
     in which case the exit manager cleans up / cancels the orphan order itself).

Usage:
  .venv/bin/python scripts/mark_manual_exit.py --track tuesday --leg PUT --price 152.40 \
      --time "2026-11-05 10:12" --dry-run
  .venv/bin/python scripts/mark_manual_exit.py --track tuesday --leg PUT --price 152.40 --time "2026-11-05 10:12"
"""
import argparse
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import config  # noqa: E402
from common.fno_keys import (  # noqa: E402
    EXITED_QTY_TTL_SEC,
    build_exited_qty_key,
    build_leg_enabled_key,
    normalize_expiry,
    parse_enabled_key,
)
from common.fsm_strategy import evaluate_fsm_tick  # noqa: E402
from common.storage import load_active_snapshot, update_active_fsm_state  # noqa: E402
from common import unified_strategy_store as uss  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

TRACKS = {
    "tuesday": {"track": "5day_strategy", "cycle": "tue", "gate": "ORDERS_LIVE_TUE", "alias": "fno:orders_live:tuesday"},
    "thursday": {"track": "3day_strategy", "cycle": "thu", "gate": "ORDERS_LIVE_THU", "alias": "fno:orders_live:thursday"},
}


def _ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _parse_ist(value: Optional[str]) -> int:
    if not value:
        return int(datetime.now(tz=timezone.utc).timestamp())
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=IST).timestamp())
        except ValueError:
            continue
    raise ValueError(f"--time must be 'YYYY-MM-DD HH:MM[:SS]' in IST (got {value!r})")


def mark_manual_exit(
    track_name: str,
    leg: str,
    price: float,
    exit_ts: int,
    qty: Optional[int] = None,
    snapshot_file: Optional[str] = None,
    active_file: Optional[str] = None,
    redis_client: Optional[Any] = None,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    t = TRACKS[track_name.lower()]
    norm, cyc = t["track"], t["cycle"]
    leg = leg.upper()
    if leg not in ("CALL", "PUT"):
        raise ValueError("--leg must be CALL or PUT")
    if price is None or float(price) <= 0:
        raise ValueError("--price must be the positive fill price of your manual SELL")
    snapshot_file = snapshot_file or (config.ACTIVE_SNAPSHOT_FILE if norm == "5day_strategy" else config.THURSDAY_SNAPSHOT_FILE)
    active_file = active_file or uss.ACTIVE_STRATEGIES_JSON

    snap = load_active_snapshot(filepath=snapshot_file)
    if not snap:
        raise RuntimeError(f"No active snapshot at {snapshot_file}")
    if str(snap.get("status") or "").lower() == "closed":
        raise RuntimeError(f"Cycle {snap.get('cycle_id')} is already closed")
    fsm = snap.get("alpha_fsm") or {}
    leg_key = "call_leg" if leg == "CALL" else "put_leg"
    fsm_leg = fsm.get(leg_key) or {}
    if not fsm_leg:
        raise RuntimeError(f"Cycle {snap.get('cycle_id')} has no Alpha FSM {leg} leg")
    if fsm_leg.get("status") != "ACTIVE" and not force:
        raise RuntimeError(
            f"{leg} leg is already {fsm_leg.get('status')} (exit ₹{fsm_leg.get('exit_price')}). Use --force to overwrite."
        )

    opt = "CE" if leg == "CALL" else "PE"
    strike = int(snap["call_strike"] if leg == "CALL" else snap["put_strike"])
    if int(fsm_leg.get("strike") or strike) != strike:
        raise RuntimeError(
            f"Snapshot {leg} strike {strike} differs from Alpha FSM leg strike {fsm_leg.get('strike')}; repair state first."
        )
    expiry_iso = normalize_expiry(snap.get("expiry_date"))

    active = uss.load_active_strategies(filepath=active_file)
    record = active.get(norm) or {}
    same_cycle = record.get("cycle_id") == snap.get("cycle_id")
    broker = str(
        snap.get("broker")
        or (record.get("broker") if same_cycle else None)
        or config.get_broker_for_track(norm)
    ).lower().strip()
    exit_qty = int(qty) if qty else uss.resolve_cycle_entry_qty(norm, record if same_cycle else {}, snap)

    # 1. Alpha FSM: close the leg at the user's fill and recompute state / P&L
    new_fsm = copy.deepcopy(fsm)
    buy = float(fsm_leg.get("buy_price") or 0.0)
    new_leg = new_fsm[leg_key]
    new_leg.update({
        "status": "MANUAL_CLOSED",
        "exit_price": round(float(price), 2),
        "exit_ts": int(exit_ts),
        "exit_reason": f"MANUAL_SELL by user at ₹{float(price):.2f} ({_ist(exit_ts)}), qty {exit_qty}",
        "realized_pnl": round(float(price) - buy, 2),
    })
    new_fsm, _ = evaluate_fsm_tick(new_fsm, None, None, int(exit_ts))

    # 2. Redis plan
    exited_key = build_exited_qty_key(cyc, expiry_iso, strike, opt, broker)
    canonical_enabled = build_leg_enabled_key(cyc, expiry_iso, strike, opt, broker=broker)
    plan: Dict[str, Any] = {
        "track": norm, "cycle_id": snap.get("cycle_id"), "leg": leg, "contract": f"{strike} {opt} exp {expiry_iso}",
        "broker": broker, "qty": exit_qty, "fill_price": round(float(price), 2), "exit_time": _ist(exit_ts),
        "realized_pnl_pts": new_leg["realized_pnl"],
        "fsm_state": f"{fsm.get('state')} -> {new_fsm.get('state')}",
        "redis_exited_key": exited_key, "dry_run": dry_run,
    }

    r = redis_client
    if r is None and not dry_run:
        r = config.get_redis_client()
    leg_enabled_keys = []
    tracked_order_ids = []
    if r is not None:
        try:
            for k in r.scan_iter(f"fno:{broker}:enabled:{cyc}:*"):
                parsed = parse_enabled_key(k, broker=broker, cycle=cyc)
                if parsed and parsed.get("strike") == strike and parsed.get("option_type") == opt \
                        and (not parsed.get("expiry") or parsed.get("expiry") == expiry_iso):
                    leg_enabled_keys.append(k)
            for suffix in ("order_id", "market_order_id"):
                for k in r.scan_iter(f"fno:{broker}:trail:{cyc}:*:{strike}:{opt}:*:{suffix}"):
                    if r.get(k):
                        tracked_order_ids.append(k)
        except Exception as exc:
            plan["redis_warning"] = f"Redis scan failed: {exc}"
    plan["authorization_keys_found"] = leg_enabled_keys
    plan["managed_order_keys_found"] = tracked_order_ids
    retire_auth = bool(leg_enabled_keys) and not tracked_order_ids
    plan["retire_authorization"] = retire_auth
    if tracked_order_ids:
        plan["note"] = ("A managed exit order is still tracked for this leg; authorization kept so the exit manager "
                        "cancels the orphan order and retires the leg itself.")

    if dry_run:
        return plan

    # 3. Writes
    if r is not None:
        prev = 0
        try:
            prev = int(float(r.get(exited_key) or 0))
        except (TypeError, ValueError):
            prev = 0
        r.set(exited_key, str(max(prev, exit_qty)), ex=EXITED_QTY_TTL_SEC)
        if retire_auth:
            for k in leg_enabled_keys:
                r.delete(k)
            if canonical_enabled not in leg_enabled_keys:
                r.delete(canonical_enabled)
            remaining = [k for k in r.scan_iter(f"fno:{broker}:enabled:{cyc}:*") if r.get(k)]
            if not remaining:
                r.set(t["gate"], "0")
                r.set(t["alias"], "0")
                plan["gate_reset"] = t["gate"]
    else:
        plan["redis_warning"] = "Redis unavailable: exited qty NOT recorded; re-run when Redis is reachable."

    if not update_active_fsm_state(new_fsm, filepath=snapshot_file):
        raise RuntimeError(f"Failed to write Alpha FSM state to {snapshot_file}")

    if same_cycle:
        with uss.strategy_lock(active_file):
            active = uss.load_active_strategies(filepath=active_file)
            rec = active.get(norm) or {}
            alpha = rec.setdefault("exit_signals", {}).setdefault("alpha_fsm", {})
            alpha[f"{leg.lower()}_triggered"] = True
            alpha[f"{leg.lower()}_status"] = "MANUAL_CLOSED"
            rec.setdefault("manual_exits", []).append({
                "leg": leg, "strike": strike, "option_type": opt, "qty": exit_qty,
                "price": round(float(price), 2), "timestamp": int(exit_ts), "time_ist": _ist(exit_ts),
            })
            rec["updated_ts"] = int(datetime.now(tz=timezone.utc).timestamp())
            active[norm] = rec
            uss.save_active_strategies(active, filepath=active_file)
        try:
            uss.sync_active_cycles_to_mongodb(active)
        except Exception as exc:
            plan["mongo_warning"] = str(exc)
    else:
        plan["unified_store_warning"] = "active_strategies.json holds a different cycle; not updated."
    return plan


def main() -> int:
    p = argparse.ArgumentParser(description="Record a manual square-off of one strategy leg (no broker calls).")
    p.add_argument("--track", required=True, choices=sorted(TRACKS))
    p.add_argument("--leg", required=True, choices=["CALL", "PUT", "call", "put"])
    p.add_argument("--price", required=True, type=float, help="Fill price of your manual SELL")
    p.add_argument("--time", default=None, help="Fill time in IST 'YYYY-MM-DD HH:MM' (default: now)")
    p.add_argument("--qty", type=int, default=None, help="Quantity sold (default: the cycle's entry quantity)")
    p.add_argument("--dry-run", action="store_true", help="Show the plan without writing anything")
    p.add_argument("--force", action="store_true", help="Overwrite a leg that is already closed")
    args = p.parse_args()
    try:
        plan = mark_manual_exit(args.track, args.leg, args.price, _parse_ist(args.time), qty=args.qty,
                                dry_run=args.dry_run, force=args.force)
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, indent=2, ensure_ascii=False))
        return 1
    print(json.dumps({"success": True, **plan}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
