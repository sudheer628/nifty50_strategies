"""
fsm_strategy.py - Decoupled Leg Finite State Machine (FSM) for Long Option Strategies.

Implements active, independent leg management for weekly NIFTY50 options:
1. States:
   - DUAL_LONG: Both Call and Put legs are active.
   - SOLO_CALL: Put leg closed/salvaged; Call leg active or trailing.
   - SOLO_PUT:  Call leg closed/harvested; Put leg active or trailing.
   - FLAT:      Both legs closed; cycle capital protected.

2. Alpha Exit Rules:
   - The "House Money" Harvest Rule:
     * In ITM mode (Long Gut): Triggers when a winning leg covers the initial
       total extrinsic time-value risk of the entire strategy (or leg gain >= +50%),
       locking profits and allowing the remaining leg to "free roll" with intrinsic protection.
     * In OTM mode: Triggers when leg gain >= initial total strangle cost.
   - Dynamic Trailing Stop: Tightens trailing stop as expiry nears (25% on Tue/Wed,
     18% on Thu/Fri, 10% on Mon morning, and 5% tight trail during Monday afternoon
     14:00+ IST theta cliff).
   - Put IV-Spike Acceleration: Tightens Put trailing stop (8% buffer) during sharp downside
     market breaks when IV expands rapidly, locking peak volatility gains before mean reversion.
   - Losing Leg Salvage Stop:
     * In ITM mode: Cuts losing leg if price drops >= 50% and |Delta| falls below 0.35
       (loss of ITM advantage), salvaging remaining intrinsic capital.
     * In OTM mode: Cuts decaying leg at <= -55% when Delta < 0.10 and premium >= ₹5.
"""

import math
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, List, Optional

try:
    import pytz
    IST_TZ = pytz.timezone("Asia/Kolkata")
except ImportError:
    from zoneinfo import ZoneInfo
    IST_TZ = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger("fsm_strategy")

try:
    from config import PUT_IV_SPIKE_THRESHOLD
except ImportError:
    import os
    PUT_IV_SPIKE_THRESHOLD = float(os.getenv("PUT_IV_SPIKE_THRESHOLD", "18.0"))

try:
    from common.pivot_calculator import get_pivot_zone
except ImportError:
    get_pivot_zone = None


def fetch_option_surface_iv(
    call_strike: int,
    put_strike: int,
    sqlite_dir: str = "/home/ubuntu/sqlite",
    reference_ts: Optional[int] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Fetch implied volatility for Call and Put strikes from the most recent
    market_data option_chain_surface in signals_data_YYYY_MM.db (with VIX fallback).
    
    Returns:
        (call_iv, put_iv) as percentages (e.g. 18.5 for 18.5%), or (None, None).
    """
    import os
    import glob
    import sqlite3
    import json
    from datetime import datetime, timezone

    try:
        base_dir = sqlite_dir.replace("/strategies", "").replace("\\strategies", "").rstrip("/\\")
        if reference_ts:
            dt = datetime.fromtimestamp(reference_ts, tz=timezone.utc)
        else:
            dt = datetime.now(timezone.utc)
        yyyy_mm = dt.strftime("%Y_%m")
        db_path = os.path.join(base_dir, f"signals_data_{yyyy_mm}.db")
        
        if not os.path.exists(db_path):
            matches = sorted(glob.glob(os.path.join(base_dir, "signals_data_*.db")), reverse=True)
            if matches:
                db_path = matches[0]
            else:
                return None, None

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT vix, data FROM market_data WHERE pk = 'INDEX#NIFTY50' ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()

        if not row:
            return None, None

        sig_data = json.loads(row["data"]) if row["data"] else {}
        vix = float(row["vix"] or sig_data.get("vix") or 0.0)

        surface = sig_data.get("option_chain_surface", {})
        if isinstance(surface, str):
            try:
                surface = json.loads(surface)
            except Exception:
                surface = {}

        call_data = surface.get(str(int(call_strike)), {}).get("CALL", {})
        put_data = surface.get(str(int(put_strike)), {}).get("PUT", {})

        c_iv = call_data.get("implied_volatility") or call_data.get("iv")
        p_iv = put_data.get("implied_volatility") or put_data.get("iv")

        # Normalize to percentage format (e.g., 0.185 -> 18.5)
        if c_iv is not None:
            c_iv = float(c_iv) * 100.0 if float(c_iv) <= 1.5 else float(c_iv)
        elif vix > 0:
            c_iv = vix

        if p_iv is not None:
            p_iv = float(p_iv) * 100.0 if float(p_iv) <= 1.5 else float(p_iv)
        elif vix > 0:
            p_iv = vix

        return c_iv, p_iv
    except Exception as e:
        logger.debug(f"Failed to fetch option surface IV: {e}")
        return None, None


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
    moneyness: Optional[str] = None,
    call_intrinsic: float = 0.0,
    call_extrinsic: float = 0.0,
    put_intrinsic: float = 0.0,
    put_extrinsic: float = 0.0,
    spread_width: int = 0,
) -> Dict[str, Any]:
    """
    Initializes a fresh Decoupled FSM strategy state on cycle initiation.
    """
    call_buy = float(call_buy_price or 0.0)
    put_buy = float(put_buy_price or 0.0)
    total_cost = round(call_buy + put_buy, 2)

    # In ITM Long Gut, Put strike > Call strike (intrinsic floor W = Put - Call).
    # In OTM Long Strangle, Call strike > Put strike (floor W = 0).
    is_gut = int(put_strike) > int(call_strike)
    if moneyness is not None:
        eff_moneyness = str(moneyness).upper()
    else:
        eff_moneyness = "ITM" if is_gut else "OTM"

    spread_w = spread_width if spread_width > 0 else (max(0, int(put_strike) - int(call_strike)) if eff_moneyness == "ITM" else 0)

    # Calculate extrinsic capital at risk
    if eff_moneyness == "ITM" and spread_w > 0:
        if (call_extrinsic > 0 or put_extrinsic > 0):
            total_extrinsic_risk = round(float(call_extrinsic) + float(put_extrinsic), 2)
        elif total_cost > spread_w:
            total_extrinsic_risk = max(1.0, round(total_cost - spread_w, 2))
        else:
            total_extrinsic_risk = total_cost
    else:
        total_extrinsic_risk = total_cost

    c_ext = float(call_extrinsic) if call_extrinsic > 0 else (call_buy * 0.5 if eff_moneyness == "ITM" else call_buy)
    p_ext = float(put_extrinsic) if put_extrinsic > 0 else (put_buy * 0.5 if eff_moneyness == "ITM" else put_buy)

    return {
        "state": "DUAL_LONG",
        "moneyness": eff_moneyness,
        "spread_width": spread_w,
        "composite_direction_score": round(float(composite_score or 0.0), 3),
        "total_initial_cost": total_cost,
        "total_extrinsic_risk": total_extrinsic_risk,
        "free_roll_active": False,
        "call_leg": {
            "strike": int(call_strike),
            "buy_price": call_buy,
            "intrinsic_at_entry": round(float(call_intrinsic), 2),
            "extrinsic_at_entry": round(c_ext, 2),
            "status": "ACTIVE",  # ACTIVE | HARVESTED_CLOSED | TRAILING_STOP_CLOSED | SALVAGED_CLOSED | MANUAL_CLOSED
            "shadow_status": "ACTIVE",
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
            "intrinsic_at_entry": round(float(put_intrinsic), 2),
            "extrinsic_at_entry": round(p_ext, 2),
            "status": "ACTIVE",  # ACTIVE | HARVESTED_CLOSED | SALVAGED_CLOSED | TRAILING_STOP_CLOSED
            "shadow_status": "ACTIVE",
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
    put_iv: Optional[float] = None,
    call_iv: Optional[float] = None,
    nifty_spot: Optional[float] = None,
    daily_pivots: Optional[Dict[str, float]] = None,
    pivot_accel_enabled: bool = False,
    pivot_shadow_mode: bool = True,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Evaluates current option prices against FSM rules and transitions states if triggered.

    Returns:
        Tuple of (updated_fsm_state_dict, list_of_event_log_messages)
    """
    if not fsm_state:
        return {}, []

    import copy
    state = copy.deepcopy(fsm_state)
    events: List[str] = []

    c_leg = state.get("call_leg", {})
    p_leg = state.get("put_leg", {})
    total_cost = state.get("total_initial_cost", 0.0) or (c_leg.get("buy_price", 0.0) + p_leg.get("buy_price", 0.0))
    c_strike = int(c_leg.get("strike", 0))
    p_strike = int(p_leg.get("strike", 0))
    if state.get("moneyness"):
        moneyness = str(state["moneyness"]).upper()
    elif c_strike > 0 and p_strike > 0:
        moneyness = "ITM" if p_strike > c_strike else "OTM"
    else:
        moneyness = "OTM"
    total_extrinsic_risk = state.get("total_extrinsic_risk", total_cost) or total_cost

    # Check Monday afternoon expiry cliff (14:00+ IST)
    is_monday_cliff = False
    try:
        dt_ist = datetime.fromtimestamp(current_ts, tz=timezone.utc).astimezone(IST_TZ)
        if dt_ist.weekday() == 0 and dt_ist.hour >= 14:
            is_monday_cliff = True
    except Exception:
        if days_to_expiry <= 0.3:
            is_monday_cliff = True

    # Time-decaying trailing stop buffer delta(t)
    if is_monday_cliff:
        trail_buffer = 0.05  # Tight 5% trailing buffer during Monday afternoon theta cliff
    elif days_to_expiry > 5.0:
        trail_buffer = 0.25  # 25% buffer on Tue/Wed (DTE: 7, 6)
    elif days_to_expiry > 1.5:
        trail_buffer = 0.18  # 18% buffer on Thu/Fri (DTE: 5, 4)
    else:
        trail_buffer = 0.10  # 10% tight buffer on Expiry Eve/Monday morning (DTE: 1, 0)

    # Detect NIFTY floor pivot zone independently from leg profit eligibility
    market_zone = None
    if daily_pivots and nifty_spot is not None and get_pivot_zone is not None:
        market_zone = get_pivot_zone(nifty_spot, daily_pivots)

    # Variables for shadow tracking
    c_in_pivot_zone = False
    c_pivot_zone = market_zone
    shadow_c_trail_buffer = trail_buffer

    p_in_pivot_zone = False
    p_pivot_zone = market_zone
    shadow_p_trail_buffer = trail_buffer

    # ------------------------------------------------------------------
    # 1. Evaluate CALL LEG
    # ------------------------------------------------------------------
    if c_leg.get("status") == "ACTIVE" and current_call_ltp is not None:
        c_buy = c_leg["buy_price"]
        c_ltp = float(current_call_ltp)
        c_peak = max(c_leg.get("peak_ltp", c_buy) or c_buy, c_ltp)
        c_leg["peak_ltp"] = c_peak
        c_ext = c_leg.get("extrinsic_at_entry", c_buy)

        # Call IV-spike acceleration: if Call is in profit during volatility expansion
        call_iv_spike = False
        c_profit_qualified = (c_ltp >= (c_buy + max(25.0, c_ext * 0.30))) if moneyness == "ITM" else (c_ltp >= c_buy * 1.30)
        has_c_iv_expansion = (call_iv is not None and call_iv >= PUT_IV_SPIKE_THRESHOLD) or (call_delta is not None and abs(call_delta) >= 0.70)
        if c_profit_qualified and has_c_iv_expansion:
            call_iv_spike = True

        # Pivot zone acceleration qualification for Call (R1, R2 resistance zones when profit-qualified)
        if c_pivot_zone in ("R1", "R2") and c_profit_qualified:
            c_in_pivot_zone = True

        # Buffer precedence hierarchy (buffers can only tighten, never loosen):
        # Base buffer (trail_buffer) can be tightened by IV spike (8%) or Pivot acceleration (5%)
        effective_c_trail_buffer = min(
            trail_buffer,
            0.08 if call_iv_spike else 1.0,
            0.05 if (c_in_pivot_zone and pivot_accel_enabled) else 1.0,
        )

        shadow_c_trail_buffer = min(
            trail_buffer,
            0.08 if call_iv_spike else 1.0,
            0.05 if c_in_pivot_zone else 1.0,
        )

        # Update dynamic trailing stop once in profit
        # In ITM: arm once gain exceeds 30% of extrinsic risk or 15% of premium
        # In OTM: arm once gain >= 30% of buy price
        c_arm_threshold = (c_buy + max(25.0, c_ext * 0.30)) if moneyness == "ITM" else (c_buy * 1.30)
        if c_ltp >= c_arm_threshold:
            candidate_stop = round(c_peak * (1.0 - effective_c_trail_buffer), 2)
            c_leg["trailing_stop"] = max(c_leg.get("trailing_stop", 0.0), candidate_stop)

            # Persistent ratcheting shadow trailing stop (carried forward through cycle)
            candidate_shadow_stop = round(c_peak * (1.0 - shadow_c_trail_buffer), 2)
            c_leg["shadow_trailing_stop"] = max(c_leg.get("shadow_trailing_stop", 0.0), candidate_shadow_stop)
            if c_in_pivot_zone and c_pivot_zone:
                c_leg["shadow_pivot_zone"] = c_pivot_zone

        # Counterfactual Shadow Breach Evaluation:
        # Check if the persistent shadow trailing stop would have triggered an exit
        c_breach_arm = (c_buy + max(35.0, c_ext * 0.40)) if moneyness == "ITM" else (c_buy * 1.40)
        c_shadow_status = c_leg.get("shadow_status", "ACTIVE")
        if (
            c_shadow_status == "ACTIVE"
            and c_leg.get("shadow_pivot_zone") is not None
            and c_peak >= c_breach_arm
            and c_leg.get("shadow_trailing_stop", 0.0) > 0
        ):
            if c_ltp <= c_leg["shadow_trailing_stop"]:
                c_leg["shadow_status"] = "SHADOW_TRAILING_STOP_CLOSED"
                c_leg["shadow_exit_price"] = c_ltp
                c_leg["shadow_exit_ts"] = current_ts
                c_leg["shadow_exit_reason"] = (
                    f"SHADOW_TRAILING_STOP_HIT (Peak: ₹{c_peak:.2f}, Stop: ₹{c_leg['shadow_trailing_stop']:.2f}, "
                    f"PivotZone: {c_leg.get('shadow_pivot_zone')})"
                )
                c_leg["shadow_realized_pnl"] = round(c_ltp - c_buy, 2)

        # Rule 1A: House Money Harvest (covers extrinsic risk or entire strangle cost)
        c_gain = c_ltp - c_buy
        is_c_harvest = False
        if moneyness == "ITM":
            if total_extrinsic_risk > 0 and (c_gain >= total_extrinsic_risk or (c_gain >= c_buy * 0.50 and c_gain >= total_extrinsic_risk * 0.75)):
                is_c_harvest = True
        else:
            if total_cost > 0 and c_gain >= total_cost:
                is_c_harvest = True

        if is_c_harvest:
            c_leg["status"] = "HARVESTED_CLOSED"
            c_leg["exit_price"] = c_ltp
            c_leg["exit_ts"] = current_ts
            c_leg["exit_reason"] = f"HOUSE_MONEY_HARVEST (Gain: +{round(c_gain, 1)} pts >= Extrinsic Risk {total_extrinsic_risk} pts)"
            c_leg["realized_pnl"] = round(c_gain, 2)
            if c_leg.get("shadow_status") == "ACTIVE":
                c_leg["shadow_status"] = "HARVESTED_CLOSED"
                c_leg["shadow_exit_price"] = c_ltp
                c_leg["shadow_exit_ts"] = current_ts
                c_leg["shadow_exit_reason"] = c_leg["exit_reason"]
                c_leg["shadow_realized_pnl"] = round(c_gain, 2)
            state["free_roll_active"] = True
            events.append(f"🎉 CALL HARVEST: Closed at ₹{c_ltp:.2f} (+{round(c_gain/c_buy*100, 1)}%). House Money secured! Put is now a 100% Free Roll.")

        # Rule 1B: Trailing Stop Breach (protects peak gains from round-tripping)
        if not is_c_harvest and c_peak >= c_breach_arm and c_ltp <= c_leg.get("trailing_stop", 0.0):
            c_leg["status"] = "TRAILING_STOP_CLOSED"
            c_leg["exit_price"] = c_ltp
            c_leg["exit_ts"] = current_ts
            if c_in_pivot_zone and pivot_accel_enabled:
                c_leg["exit_reason"] = f"TRAILING_STOP_HIT (Peak: ₹{c_peak:.2f}, Stop: ₹{c_leg['trailing_stop']:.2f}, PivotZone: {c_pivot_zone}, Buffer: {int(effective_c_trail_buffer*100)}%)"
            else:
                c_leg["exit_reason"] = f"TRAILING_STOP_HIT (Peak: ₹{c_peak:.2f}, Stop: ₹{c_leg['trailing_stop']:.2f})"
            c_leg["realized_pnl"] = round(c_ltp - c_buy, 2)
            events.append(f"🛡️ CALL TRAILING STOP: Closed at ₹{c_ltp:.2f} to lock peak profits (Peak: ₹{c_peak:.2f}).")

        # Rule 1C: Losing Leg Salvage Stop on Call
        is_c_salvage = False
        if moneyness == "ITM":
            if (c_ltp <= c_buy * 0.50) and (call_delta is not None and abs(call_delta) < 0.35):
                is_c_salvage = True
        else:
            if (c_ltp <= c_buy * 0.45) and (call_delta is not None and abs(call_delta) < 0.10) and (c_ltp >= 5.0):
                is_c_salvage = True

        if not is_c_harvest and c_leg.get("status") == "ACTIVE" and is_c_salvage:
            c_leg["status"] = "SALVAGED_CLOSED"
            c_leg["exit_price"] = c_ltp
            c_leg["exit_ts"] = current_ts
            c_leg["exit_reason"] = f"SALVAGE_STOP ({round((c_ltp-c_buy)/c_buy*100, 1)}% decay & Delta={call_delta:.2f})"
            c_leg["realized_pnl"] = round(c_ltp - c_buy, 2)
            if c_leg.get("shadow_status") == "ACTIVE":
                c_leg["shadow_status"] = "SALVAGED_CLOSED"
                c_leg["shadow_exit_price"] = c_ltp
                c_leg["shadow_exit_ts"] = current_ts
                c_leg["shadow_exit_reason"] = c_leg["exit_reason"]
                c_leg["shadow_realized_pnl"] = round(c_ltp - c_buy, 2)
            events.append(f"⚠️ CALL SALVAGED: Closed at ₹{c_ltp:.2f} to salvage capital (Loss: {round((c_ltp-c_buy)/c_buy*100, 1)}%).")

    # ------------------------------------------------------------------
    # 2. Evaluate PUT LEG
    # ------------------------------------------------------------------
    if p_leg.get("status") == "ACTIVE" and current_put_ltp is not None:
        p_buy = p_leg["buy_price"]
        p_ltp = float(current_put_ltp)
        p_peak = max(p_leg.get("peak_ltp", p_buy) or p_buy, p_ltp)
        p_leg["peak_ltp"] = p_peak
        p_ext = p_leg.get("extrinsic_at_entry", p_buy)

        # Put IV-spike acceleration: if Put is in profit during volatility expansion
        put_iv_spike = False
        p_profit_qualified = (p_ltp >= (p_buy + max(25.0, p_ext * 0.30))) if moneyness == "ITM" else (p_ltp >= p_buy * 1.30)
        has_iv_expansion = (put_iv is not None and put_iv >= PUT_IV_SPIKE_THRESHOLD) or (put_delta is not None and abs(put_delta) >= 0.70)
        if p_profit_qualified and has_iv_expansion:
            put_iv_spike = True

        # Pivot zone acceleration qualification for Put (S1, S2 support zones when profit-qualified)
        if p_pivot_zone in ("S1", "S2") and p_profit_qualified:
            p_in_pivot_zone = True

        # Buffer precedence hierarchy (buffers can only tighten, never loosen):
        effective_p_trail_buffer = min(
            trail_buffer,
            0.08 if put_iv_spike else 1.0,
            0.05 if (p_in_pivot_zone and pivot_accel_enabled) else 1.0,
        )

        shadow_p_trail_buffer = min(
            trail_buffer,
            0.08 if put_iv_spike else 1.0,
            0.05 if p_in_pivot_zone else 1.0,
        )

        # Update dynamic trailing stop once in profit
        p_arm_threshold = (p_buy + max(25.0, p_ext * 0.30)) if moneyness == "ITM" else (p_buy * 1.30)
        if p_ltp >= p_arm_threshold:
            candidate_stop = round(p_peak * (1.0 - effective_p_trail_buffer), 2)
            p_leg["trailing_stop"] = max(p_leg.get("trailing_stop", 0.0), candidate_stop)

            # Persistent ratcheting shadow trailing stop (carried forward through cycle)
            candidate_shadow_stop = round(p_peak * (1.0 - shadow_p_trail_buffer), 2)
            p_leg["shadow_trailing_stop"] = max(p_leg.get("shadow_trailing_stop", 0.0), candidate_shadow_stop)
            if p_in_pivot_zone and p_pivot_zone:
                p_leg["shadow_pivot_zone"] = p_pivot_zone

        # Counterfactual Shadow Breach Evaluation:
        # Check if the persistent shadow trailing stop would have triggered an exit
        p_breach_arm = (p_buy + max(35.0, p_ext * 0.40)) if moneyness == "ITM" else (p_buy * 1.40)
        p_shadow_status = p_leg.get("shadow_status", "ACTIVE")
        if (
            p_shadow_status == "ACTIVE"
            and p_leg.get("shadow_pivot_zone") is not None
            and p_peak >= p_breach_arm
            and p_leg.get("shadow_trailing_stop", 0.0) > 0
        ):
            if p_ltp <= p_leg["shadow_trailing_stop"]:
                p_leg["shadow_status"] = "SHADOW_TRAILING_STOP_CLOSED"
                p_leg["shadow_exit_price"] = p_ltp
                p_leg["shadow_exit_ts"] = current_ts
                p_leg["shadow_exit_reason"] = (
                    f"SHADOW_TRAILING_STOP_HIT (Peak: ₹{p_peak:.2f}, Stop: ₹{p_leg['shadow_trailing_stop']:.2f}, "
                    f"PivotZone: {p_leg.get('shadow_pivot_zone')})"
                )
                p_leg["shadow_realized_pnl"] = round(p_ltp - p_buy, 2)

        # Rule 2A: House Money Harvest (Unrealized gain covers extrinsic risk or total cost)
        p_gain = p_ltp - p_buy
        is_p_harvest = False
        if moneyness == "ITM":
            if total_extrinsic_risk > 0 and (p_gain >= total_extrinsic_risk or (p_gain >= p_buy * 0.50 and p_gain >= total_extrinsic_risk * 0.75)):
                is_p_harvest = True
        else:
            if total_cost > 0 and p_gain >= total_cost:
                is_p_harvest = True

        if is_p_harvest:
            p_leg["status"] = "HARVESTED_CLOSED"
            p_leg["exit_price"] = p_ltp
            p_leg["exit_ts"] = current_ts
            p_leg["exit_reason"] = f"HOUSE_MONEY_HARVEST (Gain: +{round(p_gain, 1)} pts >= Extrinsic Risk {total_extrinsic_risk} pts)"
            p_leg["realized_pnl"] = round(p_gain, 2)
            if p_leg.get("shadow_status") == "ACTIVE":
                p_leg["shadow_status"] = "HARVESTED_CLOSED"
                p_leg["shadow_exit_price"] = p_ltp
                p_leg["shadow_exit_ts"] = current_ts
                p_leg["shadow_exit_reason"] = p_leg["exit_reason"]
                p_leg["shadow_realized_pnl"] = round(p_gain, 2)
            state["free_roll_active"] = True
            events.append(f"🎉 PUT HARVEST: Closed at ₹{p_ltp:.2f} (+{round(p_gain/p_buy*100, 1)}%). House Money secured! Call is now a 100% Free Roll.")

        # Rule 2B: Trailing Stop Breach (protects peak gains from round-tripping)
        if not is_p_harvest and p_peak >= p_breach_arm and p_ltp <= p_leg.get("trailing_stop", 0.0):
            p_leg["status"] = "TRAILING_STOP_CLOSED"
            p_leg["exit_price"] = p_ltp
            p_leg["exit_ts"] = current_ts
            if p_in_pivot_zone and pivot_accel_enabled:
                p_leg["exit_reason"] = f"TRAILING_STOP_HIT (Peak: ₹{p_peak:.2f}, Stop: ₹{p_leg['trailing_stop']:.2f}, PivotZone: {p_pivot_zone}, Buffer: {int(effective_p_trail_buffer*100)}%)"
            else:
                p_leg["exit_reason"] = f"TRAILING_STOP_HIT (Peak: ₹{p_peak:.2f}, Stop: ₹{p_leg['trailing_stop']:.2f})"
            p_leg["realized_pnl"] = round(p_ltp - p_buy, 2)
            events.append(f"🛡️ PUT TRAILING STOP: Closed at ₹{p_ltp:.2f} to lock peak profits (Peak: ₹{p_peak:.2f}).")

        # Rule 2C: Losing Leg Salvage Stop on Put
        is_p_salvage = False
        if moneyness == "ITM":
            if (p_ltp <= p_buy * 0.50) and (put_delta is not None and abs(put_delta) < 0.35):
                is_p_salvage = True
        else:
            if (p_ltp <= p_buy * 0.45) and (put_delta is not None and abs(put_delta) < 0.10) and (p_ltp >= 5.0):
                is_p_salvage = True

        if not is_p_harvest and p_leg.get("status") == "ACTIVE" and is_p_salvage:
            p_leg["status"] = "SALVAGED_CLOSED"
            p_leg["exit_price"] = p_ltp
            p_leg["exit_ts"] = current_ts
            p_leg["exit_reason"] = f"SALVAGE_STOP ({round((p_ltp-p_buy)/p_buy*100, 1)}% decay & Delta={put_delta:.2f})"
            p_leg["realized_pnl"] = round(p_ltp - p_buy, 2)
            if p_leg.get("shadow_status") == "ACTIVE":
                p_leg["shadow_status"] = "SALVAGED_CLOSED"
                p_leg["shadow_exit_price"] = p_ltp
                p_leg["shadow_exit_ts"] = current_ts
                p_leg["shadow_exit_reason"] = p_leg["exit_reason"]
                p_leg["shadow_realized_pnl"] = round(p_ltp - p_buy, 2)
            events.append(f"⚠️ PUT SALVAGED: Closed at ₹{p_ltp:.2f} to salvage capital (Loss: {round((p_ltp-p_buy)/p_buy*100, 1)}%).")

    # ------------------------------------------------------------------
    # Shadow Telemetry Recording
    # ------------------------------------------------------------------
    if pivot_shadow_mode and (daily_pivots is not None or nifty_spot is not None):
        current_zone = market_zone
        current_leg = "CALL" if market_zone in ("R1", "R2") else ("PUT" if market_zone in ("S1", "S2") else None)
        in_proximity = market_zone is not None
        leg_qualified_for_accel = bool(c_in_pivot_zone or p_in_pivot_zone)

        # Retained / armed zones from earlier touches in this cycle
        c_armed_zone = c_leg.get("shadow_pivot_zone")
        p_armed_zone = p_leg.get("shadow_pivot_zone")
        armed_zone = c_armed_zone or p_armed_zone
        armed_leg = "CALL" if c_armed_zone else ("PUT" if p_armed_zone else None)

        live_stop = c_leg.get("trailing_stop", 0.0) if market_zone in ("R1", "R2") else (p_leg.get("trailing_stop", 0.0) if market_zone in ("S1", "S2") else 0.0)
        shadow_stop = c_leg.get("shadow_trailing_stop", 0.0) if market_zone in ("R1", "R2") else (p_leg.get("shadow_trailing_stop", 0.0) if market_zone in ("S1", "S2") else 0.0)
        delta_pts = round(shadow_stop - live_stop, 2) if (live_stop > 0 and shadow_stop > 0) else 0.0

        c_would_exit = bool(c_leg.get("shadow_status") == "SHADOW_TRAILING_STOP_CLOSED")
        p_would_exit = bool(p_leg.get("shadow_status") == "SHADOW_TRAILING_STOP_CLOSED")
        would_exit = c_would_exit or p_would_exit

        try:
            eval_time_str = datetime.fromtimestamp(current_ts, tz=timezone.utc).astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST") if current_ts else ""
        except Exception:
            eval_time_str = ""

        # Maintain persistent events list in pivot_shadow
        prev_shadow = state.get("pivot_shadow") or {}
        shadow_events = list(prev_shadow.get("events") or [])

        if (
            c_leg.get("shadow_status") == "SHADOW_TRAILING_STOP_CLOSED"
            and c_leg.get("shadow_pivot_zone") is not None
            and not any(e.get("leg") == "CALL" for e in shadow_events)
        ):
            shadow_events.append({
                "leg": "CALL",
                "status": "SHADOW_TRAILING_STOP_CLOSED",
                "exit_price": c_leg.get("shadow_exit_price"),
                "exit_ts": c_leg.get("shadow_exit_ts"),
                "exit_reason": c_leg.get("shadow_exit_reason"),
                "realized_pnl": c_leg.get("shadow_realized_pnl"),
                "peak_ltp": c_leg.get("peak_ltp"),
                "trailing_stop": c_leg.get("shadow_trailing_stop"),
                "live_trailing_stop": c_leg.get("trailing_stop"),
                "spot": nifty_spot,
                "zone": c_leg.get("shadow_pivot_zone"),
                "live_fsm_status": c_leg.get("status"),
            })

        if (
            p_leg.get("shadow_status") == "SHADOW_TRAILING_STOP_CLOSED"
            and p_leg.get("shadow_pivot_zone") is not None
            and not any(e.get("leg") == "PUT" for e in shadow_events)
        ):
            shadow_events.append({
                "leg": "PUT",
                "status": "SHADOW_TRAILING_STOP_CLOSED",
                "exit_price": p_leg.get("shadow_exit_price"),
                "exit_ts": p_leg.get("shadow_exit_ts"),
                "exit_reason": p_leg.get("shadow_exit_reason"),
                "realized_pnl": p_leg.get("shadow_realized_pnl"),
                "peak_ltp": p_leg.get("peak_ltp"),
                "trailing_stop": p_leg.get("shadow_trailing_stop"),
                "live_trailing_stop": p_leg.get("trailing_stop"),
                "spot": nifty_spot,
                "zone": p_leg.get("shadow_pivot_zone"),
                "live_fsm_status": p_leg.get("status"),
            })

        # Open legs holding an armed shadow stop
        c_shadow_open = (c_leg.get("status") == "ACTIVE" and c_leg.get("shadow_status") == "ACTIVE")
        p_shadow_open = (p_leg.get("status") == "ACTIVE" and p_leg.get("shadow_status") == "ACTIVE")

        retained_stop_active = bool(
            (c_shadow_open and c_armed_zone is not None) or
            (p_shadow_open and p_armed_zone is not None)
        )

        # Counterfactual terminal exit status (historical completion)
        c_shadow_closed = (c_leg.get("shadow_status") == "SHADOW_TRAILING_STOP_CLOSED")
        p_shadow_closed = (p_leg.get("shadow_status") == "SHADOW_TRAILING_STOP_CLOSED")
        counterfactual_terminal = bool(c_shadow_closed or p_shadow_closed)
        if c_shadow_closed and p_shadow_closed:
            counterfactual_terminal_leg = "BOTH"
        elif c_shadow_closed:
            counterfactual_terminal_leg = "CALL"
        elif p_shadow_closed:
            counterfactual_terminal_leg = "PUT"
        else:
            counterfactual_terminal_leg = None

        # has_shadow_activity is True when spot is currently in proximity, or holding an active retained stop on an OPEN leg
        has_shadow_activity = bool(in_proximity or retained_stop_active)

        state["pivot_shadow"] = {
            "active": has_shadow_activity,
            "in_proximity": in_proximity,
            "retained_stop_active": retained_stop_active,
            "counterfactual_terminal": counterfactual_terminal,
            "counterfactual_terminal_leg": counterfactual_terminal_leg,
            "leg_qualified_for_accel": leg_qualified_for_accel,
            "leg": current_leg,
            "zone": current_zone,
            "armed_zone": armed_zone,
            "armed_leg": armed_leg,
            "evaluated_at": eval_time_str,
            "evaluated_ts": current_ts,
            "spot": nifty_spot,
            "live_trail_stop": live_stop,
            "shadow_trail_stop": shadow_stop,
            "delta_points": delta_pts,
            "would_have_exited": counterfactual_terminal,
            "call_shadow_stop": c_leg.get("shadow_trailing_stop", 0.0),
            "put_shadow_stop": p_leg.get("shadow_trailing_stop", 0.0),
            "call_shadow_status": c_leg.get("shadow_status", "ACTIVE"),
            "put_shadow_status": p_leg.get("shadow_status", "ACTIVE"),
            "call_shadow_pnl": c_leg.get("shadow_realized_pnl"),
            "put_shadow_pnl": p_leg.get("shadow_realized_pnl"),
            "events": shadow_events,
            "daily_pivots": daily_pivots,
        }

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
        c_pnl = c_leg.get("realized_pnl")
        if c_pnl is not None:
            realized_pts += float(c_pnl)
    if not p_active:
        p_pnl = p_leg.get("realized_pnl")
        if p_pnl is not None:
            realized_pts += float(p_pnl)

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
