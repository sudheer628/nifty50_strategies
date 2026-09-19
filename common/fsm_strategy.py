"""
fsm_strategy.py - Decoupled Leg Finite State Machine (FSM) for Long Option Strategies.

Implements active, independent leg management for weekly NIFTY50 options:
1. States:
   - DUAL_LONG: Both Call and Put legs are active.
   - SOLO_CALL: Put leg closed/salvaged; Call leg active or trailing.
   - SOLO_PUT:  Call leg closed/harvested; Put leg active or trailing.
   - FLAT:      Both legs closed; cycle capital protected.

2. Alpha Exit Rules:
   - The "House Money" Harvest Rule: When a winning leg reaches >= +100%
     (unrealized gain covers the initial cost of the entire strangle), profit is locked,
     leaving the remaining leg as a 100% risk-free "free roll".
   - Dynamic Trailing Stop: Tightens trailing stop as expiry nears (25% on Tue/Wed,
     18% on Thu/Fri, 10% on Mon) to prevent round-tripping peak gains.
   - Losing Leg Salvage Stop: Closes decaying leg at <= -55% when Delta < 0.10 and
     premium >= ₹15, preventing a complete 100% write-off.
"""

import math
import logging
from typing import Dict, Any, Tuple, List, Optional

logger = logging.getLogger("fsm_strategy")


def estimate_option_delta(
    spot: float,
    strike: float,
    days_to_expiry: float,
    is_call: bool,
    iv: float = 0.14
) -> float:
    """
    Estimate option delta using closed-form Black-Scholes formula.
    Uses Python's math.erf for standard normal CDF without external dependencies.
    """
    if days_to_expiry <= 0.01 or spot <= 0 or strike <= 0:
        if is_call:
            return 1.0 if spot > strike else 0.0
        else:
            return -1.0 if spot < strike else 0.0
    t = max(0.001, days_to_expiry / 365.0)
    vol = max(0.05, iv)
    try:
        d1 = (math.log(spot / strike) + (0.5 * vol * vol) * t) / (vol * math.sqrt(t))
        n_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        return round(n_d1 if is_call else (n_d1 - 1.0), 3)
    except Exception:
        return 0.5 if is_call else -0.5


def init_fsm_state(
    call_strike: int,
    put_strike: int,
    call_buy_price: float,
    put_buy_price: float,
    entry_ts: int,
    composite_score: float = 0.0,
) -> Dict[str, Any]:
    """
    Initializes a fresh Decoupled FSM strategy state on Tuesday cycle initiation.
    """
    call_buy = float(call_buy_price or 0.0)
    put_buy = float(put_buy_price or 0.0)
    total_cost = round(call_buy + put_buy, 2)

    return {
        "state": "DUAL_LONG",
        "composite_direction_score": round(float(composite_score or 0.0), 3),
        "total_initial_cost": total_cost,
        "free_roll_active": False,
        "call_leg": {
            "strike": int(call_strike),
            "buy_price": call_buy,
            "status": "ACTIVE",  # ACTIVE | HARVESTED_CLOSED | TRAILING_STOP_CLOSED | MANUAL_CLOSED
            "exit_price": None,
            "exit_ts": None,
            "exit_reason": "",
            "peak_ltp": call_buy,
            "trailing_stop": round(call_buy * 0.75, 2),
            "realized_pnl": 0.0,
        },
        "put_leg": {
            "strike": int(put_strike),
            "buy_price": put_buy,
            "status": "ACTIVE",  # ACTIVE | HARVESTED_CLOSED | SALVAGED_CLOSED | TRAILING_STOP_CLOSED
            "exit_price": None,
            "exit_ts": None,
            "exit_reason": "",
            "peak_ltp": put_buy,
            "trailing_stop": round(put_buy * 0.75, 2),
            "realized_pnl": 0.0,
        },
        "realized_pnl_pts": 0.0,
        "unrealized_pnl_pts": 0.0,
        "total_gainloss": 0.0,
        "roi_pct": 0.0,
    }


def evaluate_fsm_tick(
    fsm_state: Dict[str, Any],
    current_call_ltp: Optional[float],
    current_put_ltp: Optional[float],
    current_ts: int,
    days_to_expiry: float = 3.0,
    call_delta: Optional[float] = None,
    put_delta: Optional[float] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Evaluates current option prices against FSM rules and transitions states if triggered.

    Returns:
        Tuple of (updated_fsm_state_dict, list_of_event_log_messages)
    """
    if not fsm_state:
        return {}, []

    state = dict(fsm_state)
    events: List[str] = []

    c_leg = state.get("call_leg", {})
    p_leg = state.get("put_leg", {})
    total_cost = state.get("total_initial_cost", 0.0) or (c_leg.get("buy_price", 0.0) + p_leg.get("buy_price", 0.0))

    # Time-decaying trailing stop buffer delta(t)
    if days_to_expiry > 5.0:
        trail_buffer = 0.25  # 25% buffer on Tue/Wed (DTE: 7, 6)
    elif days_to_expiry > 1.5:
        trail_buffer = 0.18  # 18% buffer on Thu/Fri (DTE: 5, 4)
    else:
        trail_buffer = 0.10  # 10% tight buffer on Expiry Eve/Monday (DTE: 1, 0)

    # ------------------------------------------------------------------
    # 1. Evaluate CALL LEG
    # ------------------------------------------------------------------
    if c_leg.get("status") == "ACTIVE" and current_call_ltp is not None:
        c_buy = c_leg["buy_price"]
        c_ltp = float(current_call_ltp)
        c_peak = max(c_leg.get("peak_ltp", c_buy) or c_buy, c_ltp)
        c_leg["peak_ltp"] = c_peak

        # Update dynamic trailing stop once in profit (>= +30% gain)
        if c_ltp >= c_buy * 1.30:
            candidate_stop = round(c_peak * (1.0 - trail_buffer), 2)
            c_leg["trailing_stop"] = max(c_leg.get("trailing_stop", 0.0), candidate_stop)

        # Rule 1A: House Money Harvest (Unrealized gain covers entire initial strangle cost)
        c_gain = c_ltp - c_buy
        if c_gain >= total_cost and total_cost > 0:
            c_leg["status"] = "HARVESTED_CLOSED"
            c_leg["exit_price"] = c_ltp
            c_leg["exit_ts"] = current_ts
            c_leg["exit_reason"] = f"HOUSE_MONEY_HARVEST (Gain: +{round(c_gain, 1)} pts >= Total Cost {total_cost} pts)"
            c_leg["realized_pnl"] = round(c_gain, 2)
            state["free_roll_active"] = True
            events.append(f"🎉 CALL HARVEST: Closed at ₹{c_ltp:.2f} (+{round(c_gain/c_buy*100, 1)}%). House Money secured! Put is now a 100% Free Roll.")

        # Rule 1B: Trailing Stop Breach (protects peak gains from round-tripping)
        elif c_peak >= c_buy * 1.40 and c_ltp <= c_leg.get("trailing_stop", 0.0):
            c_leg["status"] = "TRAILING_STOP_CLOSED"
            c_leg["exit_price"] = c_ltp
            c_leg["exit_ts"] = current_ts
            c_leg["exit_reason"] = f"TRAILING_STOP_HIT (Peak: ₹{c_peak:.2f}, Stop: ₹{c_leg['trailing_stop']:.2f})"
            c_leg["realized_pnl"] = round(c_ltp - c_buy, 2)
            events.append(f"🛡️ CALL TRAILING STOP: Closed at ₹{c_ltp:.2f} to lock peak profits (Peak: ₹{c_peak:.2f}).")

        # Rule 1C: Losing Leg Salvage Stop on Call (if market crashed)
        elif (c_ltp <= c_buy * 0.45) and (call_delta is not None and abs(call_delta) < 0.10) and (c_ltp >= 5.0):
            c_leg["status"] = "SALVAGED_CLOSED"
            c_leg["exit_price"] = c_ltp
            c_leg["exit_ts"] = current_ts
            c_leg["exit_reason"] = f"SALVAGE_STOP (-55% decay & Delta={call_delta:.2f})"
            c_leg["realized_pnl"] = round(c_ltp - c_buy, 2)
            events.append(f"⚠️ CALL SALVAGED: Closed at ₹{c_ltp:.2f} to salvage capital (Loss: {round((c_ltp-c_buy)/c_buy*100, 1)}%).")

    # ------------------------------------------------------------------
    # 2. Evaluate PUT LEG
    # ------------------------------------------------------------------
    if p_leg.get("status") == "ACTIVE" and current_put_ltp is not None:
        p_buy = p_leg["buy_price"]
        p_ltp = float(current_put_ltp)
        p_peak = max(p_leg.get("peak_ltp", p_buy) or p_buy, p_ltp)
        p_leg["peak_ltp"] = p_peak

        # Update dynamic trailing stop once in profit (>= +30% gain)
        if p_ltp >= p_buy * 1.30:
            candidate_stop = round(p_peak * (1.0 - trail_buffer), 2)
            p_leg["trailing_stop"] = max(p_leg.get("trailing_stop", 0.0), candidate_stop)

        # Rule 2A: House Money Harvest (Unrealized gain covers entire initial strangle cost)
        p_gain = p_ltp - p_buy
        if p_gain >= total_cost and total_cost > 0:
            p_leg["status"] = "HARVESTED_CLOSED"
            p_leg["exit_price"] = p_ltp
            p_leg["exit_ts"] = current_ts
            p_leg["exit_reason"] = f"HOUSE_MONEY_HARVEST (Gain: +{round(p_gain, 1)} pts >= Total Cost {total_cost} pts)"
            p_leg["realized_pnl"] = round(p_gain, 2)
            state["free_roll_active"] = True
            events.append(f"🎉 PUT HARVEST: Closed at ₹{p_ltp:.2f} (+{round(p_gain/p_buy*100, 1)}%). House Money secured! Call is now a 100% Free Roll.")

        # Rule 2B: Trailing Stop Breach (protects peak gains from round-tripping)
        elif p_peak >= p_buy * 1.40 and p_ltp <= p_leg.get("trailing_stop", 0.0):
            p_leg["status"] = "TRAILING_STOP_CLOSED"
            p_leg["exit_price"] = p_ltp
            p_leg["exit_ts"] = current_ts
            p_leg["exit_reason"] = f"TRAILING_STOP_HIT (Peak: ₹{p_peak:.2f}, Stop: ₹{p_leg['trailing_stop']:.2f})"
            p_leg["realized_pnl"] = round(p_ltp - p_buy, 2)
            events.append(f"🛡️ PUT TRAILING STOP: Closed at ₹{p_ltp:.2f} to lock peak profits (Peak: ₹{p_peak:.2f}).")

        # Rule 2C: Losing Leg Salvage Stop on Put (if market rallied)
        elif (p_ltp <= p_buy * 0.45) and (put_delta is not None and abs(put_delta) < 0.10) and (p_ltp >= 5.0):
            p_leg["status"] = "SALVAGED_CLOSED"
            p_leg["exit_price"] = p_ltp
            p_leg["exit_ts"] = current_ts
            p_leg["exit_reason"] = f"SALVAGE_STOP (-55% decay & Delta={put_delta:.2f})"
            p_leg["realized_pnl"] = round(p_ltp - p_buy, 2)
            events.append(f"⚠️ PUT SALVAGED: Closed at ₹{p_ltp:.2f} to salvage capital (Loss: {round((p_ltp-p_buy)/p_buy*100, 1)}%).")

    # ------------------------------------------------------------------
    # 3. Derive Overall Machine State & P&L
    # ------------------------------------------------------------------
    c_active = (c_leg.get("status") == "ACTIVE")
    p_active = (p_leg.get("status") == "ACTIVE")

    if c_active and p_active:
        state["state"] = "DUAL_LONG"
    elif c_active and not p_active:
        state["state"] = "SOLO_CALL"
    elif not c_active and p_active:
        state["state"] = "SOLO_PUT"
    else:
        state["state"] = "FLAT"

    # Realized P&L from closed legs
    realized_pts = 0.0
    if not c_active:
        realized_pts += c_leg.get("realized_pnl", 0.0)
    if not p_active:
        realized_pts += p_leg.get("realized_pnl", 0.0)

    # Unrealized P&L from still-active legs
    unrealized_pts = 0.0
    if c_active and current_call_ltp is not None:
        unrealized_pts += (float(current_call_ltp) - c_leg["buy_price"])
    if p_active and current_put_ltp is not None:
        unrealized_pts += (float(current_put_ltp) - p_leg["buy_price"])

    total_pts = round(realized_pts + unrealized_pts, 2)
    roi_pct = round((total_pts / total_cost * 100.0), 2) if total_cost > 0 else 0.0

    state["realized_pnl_pts"] = round(realized_pts, 2)
    state["unrealized_pnl_pts"] = round(unrealized_pts, 2)
    state["total_gainloss"] = total_pts
    state["roi_pct"] = roi_pct
    state["call_leg"] = c_leg
    state["put_leg"] = p_leg

    return state, events
