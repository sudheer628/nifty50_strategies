"""
Peak-Profit Monitoring and Alerting Engine for NIFTY Weekly Option Strangle.

Monitors combined strangle profit across 15/30-minute collector runs after Tuesday
entry order locking. Triggers real-time Discord notifications when new profit peaks
are achieved, filtered by a minimum 20% profit threshold and anti-spam hysteresis.

Includes an explicit on_new_peak(event) execution seam for future live broker
order integration (Angel One / Upstox).
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional
import pytz
import requests

from config import (
    NIFTY_LOT_SIZE,
    PEAK_ALERT_MIN_PROFIT_PCT,
    PEAK_ALERT_HYSTERESIS_PCT,
    PEAK_ALERT_HYSTERESIS_PTS,
    PEAK_ALERT_ENABLED,
    DISCORD_URL,
)
from common.storage import update_active_peak_state

logger = logging.getLogger("nifty50_strategies.profit_monitor")
tz_ist = pytz.timezone("Asia/Kolkata")


def _get_ist_date_str(ts: Optional[int] = None) -> str:
    """Return YYYY-MM-DD formatted date in IST."""
    if ts:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz_ist)
    else:
        dt = datetime.now(tz_ist)
    return dt.strftime("%Y-%m-%d")


def calculate_strangle_pnl(
    call_buy: float,
    call_ltp: float,
    put_buy: float,
    put_ltp: float,
    lot_size: int = NIFTY_LOT_SIZE,
) -> Dict[str, float]:
    """
    Calculate combined and leg-level strangle P&L metrics.

    Returns dict with:
        call_pnl_pts, put_pnl_pts, combined_pnl_pts,
        combined_pnl_pct, combined_pnl_inr
    """
    call_pnl_pts = round(call_ltp - call_buy, 2)
    put_pnl_pts = round(put_ltp - put_buy, 2)
    combined_pnl_pts = round(call_pnl_pts + put_pnl_pts, 2)

    total_cost = call_buy + put_buy
    if total_cost > 0:
        combined_pnl_pct = round((combined_pnl_pts / total_cost) * 100.0, 2)
    else:
        combined_pnl_pct = 0.0

    combined_pnl_inr = round(combined_pnl_pts * lot_size, 2)

    return {
        "call_pnl_pts": call_pnl_pts,
        "put_pnl_pts": put_pnl_pts,
        "combined_pnl_pts": combined_pnl_pts,
        "combined_pnl_pct": combined_pnl_pct,
        "combined_pnl_inr": combined_pnl_inr,
    }


def evaluate_peak_profit(
    snapshot: Dict[str, Any],
    nifty_ltp: float,
    call_strike: int,
    call_ltp: float,
    call_buy: Optional[float],
    put_strike: int,
    put_ltp: float,
    put_buy: Optional[float],
    is_first_tick: bool = False,
    dry_run: bool = False,
    current_ts: Optional[int] = None,
    fsm_state: Optional[str] = None,
    fsm_total_gainloss: Optional[float] = None,
) -> Tuple[Dict[str, Any], bool, str]:
    """
    Evaluate current strangle profit against the cycle's high-water mark.

    Rules:
    1. Tracking only starts once Tuesday buy prices are locked (call_buy and put_buy not None).
    2. Closed cycles are skipped.
    3. First tick establishes baseline; zero alerts sent.
    4. Profit must reach >= PEAK_ALERT_MIN_PROFIT_PCT (default 20.0%) to qualify for Discord alert.
    5. Anti-spam hysteresis: Subsequent peak alerts require at least +PEAK_ALERT_HYSTERESIS_PCT (+5%)
       or +PEAK_ALERT_HYSTERESIS_PTS (+10 pts) expansion over previous notified peak.
    6. Rollback on dispatch failure: If Discord delivery fails, last_notified_* is rolled back
       so the peak can be retried on the subsequent tick.

    Returns:
        (peak_state, alert_triggered, reason_message)
    """
    if snapshot.get("status") == "closed":
        return snapshot.get("peak_profit", {}), False, "Cycle is closed; peak profit evaluation skipped"

    if call_buy is None or put_buy is None:
        return snapshot.get("peak_profit", {}), False, "Buy prices not locked yet"

    if current_ts is None:
        current_ts = int(datetime.now(timezone.utc).timestamp())

    today_ist = _get_ist_date_str(current_ts)
    pnl = calculate_strangle_pnl(call_buy, call_ltp, put_buy, put_ltp)
    combined_pnl_pts = pnl["combined_pnl_pts"]
    combined_pnl_pct = pnl["combined_pnl_pct"]
    combined_pnl_inr = pnl["combined_pnl_inr"]

    peak_state = snapshot.get("peak_profit")
    cycle_id = snapshot.get("cycle_id")

    # Baseline initialization on first run or when peak_profit not yet recorded
    if not peak_state:
        peak_state = {
            "cycle_id": cycle_id,
            "peak_pnl_pts": combined_pnl_pts,
            "peak_pnl_pct": combined_pnl_pct,
            "peak_pnl_inr": combined_pnl_inr,
            "peak_timestamp": current_ts,
            "last_notified_pts": None,
            "last_notified_pct": None,
            "last_notified_timestamp": None,
            "alerts_sent_today": 0,
            "last_alert_date": today_ist,
        }
        update_active_peak_state(peak_state)
        return peak_state, False, "Baseline established at cycle order lock"

    if cycle_id and "cycle_id" not in peak_state:
        peak_state["cycle_id"] = cycle_id

    # Reset daily alert counter if day rolled over in IST
    if peak_state.get("last_alert_date") != today_ist:
        peak_state["alerts_sent_today"] = 0
        peak_state["last_alert_date"] = today_ist
        update_active_peak_state(peak_state)

    if is_first_tick:
        # First tick of cycle lock establishes initial baseline
        peak_state["peak_pnl_pts"] = combined_pnl_pts
        peak_state["peak_pnl_pct"] = combined_pnl_pct
        peak_state["peak_pnl_inr"] = combined_pnl_inr
        peak_state["peak_timestamp"] = current_ts
        update_active_peak_state(peak_state)
        return peak_state, False, "Baseline updated at initial cycle tick"

    # Check if a new profit peak has been reached or an unnotified qualifying peak is being retried
    previous_peak_pts = peak_state.get("peak_pnl_pts", -999999.0)
    last_notified_pts = peak_state.get("last_notified_pts")
    last_notified_pct = peak_state.get("last_notified_pct")

    is_higher_peak = combined_pnl_pts > previous_peak_pts
    is_unnotified_peak = (last_notified_pts is None) or (combined_pnl_pts > last_notified_pts)
    is_retry_at_peak = (
        combined_pnl_pts >= previous_peak_pts
        and is_unnotified_peak
        and combined_pnl_pct >= PEAK_ALERT_MIN_PROFIT_PCT
    )

    if is_higher_peak:
        peak_state["peak_pnl_pts"] = combined_pnl_pts
        peak_state["peak_pnl_pct"] = combined_pnl_pct
        peak_state["peak_pnl_inr"] = combined_pnl_inr
        peak_state["peak_timestamp"] = current_ts

    # Determine alert eligibility
    if not is_higher_peak and not is_retry_at_peak:
        return peak_state, False, f"Current profit ({combined_pnl_pts:.2f} pts) did not exceed peak ({previous_peak_pts:.2f} pts)"

    # Feature flag check
    if not PEAK_ALERT_ENABLED:
        update_active_peak_state(peak_state)
        return peak_state, False, "Peak profit alert disabled in configuration"

    # Validation: Profit must be >= 20.0%
    if combined_pnl_pct < PEAK_ALERT_MIN_PROFIT_PCT:
        update_active_peak_state(peak_state)
        return (
            peak_state,
            False,
            f"New peak (+{combined_pnl_pct:.1f}% / +{combined_pnl_pts:.1f} pts) is below minimum {PEAK_ALERT_MIN_PROFIT_PCT:.1f}% threshold",
        )

    alerts_today = peak_state.get("alerts_sent_today", 0)

    # Hysteresis check against last notified peak
    last_notified_pts = peak_state.get("last_notified_pts")
    last_notified_pct = peak_state.get("last_notified_pct")

    if last_notified_pts is not None and last_notified_pct is not None:
        pts_diff = combined_pnl_pts - last_notified_pts
        pct_diff = combined_pnl_pct - last_notified_pct

        if pts_diff < PEAK_ALERT_HYSTERESIS_PTS and pct_diff < PEAK_ALERT_HYSTERESIS_PCT:
            update_active_peak_state(peak_state)
            return (
                peak_state,
                False,
                f"Peak expansion (+{pct_diff:.1f}% / +{pts_diff:.1f} pts) below hysteresis (+{PEAK_ALERT_HYSTERESIS_PCT:.1f}% / +{PEAK_ALERT_HYSTERESIS_PTS:.1f} pts)",
            )

    # All criteria satisfied: Qualifies for alert!
    prev_notified_pts = peak_state.get("last_notified_pts")
    prev_notified_pct = peak_state.get("last_notified_pct")
    prev_notified_ts = peak_state.get("last_notified_timestamp")
    prev_alerts_today = peak_state.get("alerts_sent_today", 0)

    peak_state["last_notified_pts"] = combined_pnl_pts
    peak_state["last_notified_pct"] = combined_pnl_pct
    peak_state["last_notified_timestamp"] = current_ts
    peak_state["alerts_sent_today"] = alerts_today + 1
    peak_state["last_alert_date"] = today_ist

    update_active_peak_state(peak_state)

    event = {
        "cycle_id": snapshot.get("cycle_id", "UNKNOWN"),
        "week_start_date": snapshot.get("week_start_date", ""),
        "expiry_date": snapshot.get("expiry_date", ""),
        "timestamp": current_ts,
        "nifty_ltp": nifty_ltp,
        "call_strike": call_strike,
        "call_buy": call_buy,
        "call_ltp": call_ltp,
        "call_pnl_pts": pnl["call_pnl_pts"],
        "put_strike": put_strike,
        "put_buy": put_buy,
        "put_ltp": put_ltp,
        "put_pnl_pts": pnl["put_pnl_pts"],
        "combined_pnl_pts": combined_pnl_pts,
        "combined_pnl_pct": combined_pnl_pct,
        "combined_pnl_inr": combined_pnl_inr,
        "lot_size": NIFTY_LOT_SIZE,
        "alerts_sent_today": peak_state["alerts_sent_today"],
        "fsm_state": fsm_state,
        "fsm_total_gainloss": fsm_total_gainloss,
        "dry_run": dry_run,
    }

    dispatch_ok = on_new_peak(event)

    if not dispatch_ok and not dry_run:
        # Roll back notification state so this peak or next higher peak will be retried on next tick
        peak_state["last_notified_pts"] = prev_notified_pts
        peak_state["last_notified_pct"] = prev_notified_pct
        peak_state["last_notified_timestamp"] = prev_notified_ts
        peak_state["alerts_sent_today"] = prev_alerts_today
        update_active_peak_state(peak_state)
        return (
            peak_state,
            False,
            f"Peak qualified (+{combined_pnl_pct:.1f}% / +{combined_pnl_pts:.1f} pts) but alert delivery failed; state rolled back to retry next tick",
        )

    return peak_state, True, f"Qualifying peak alert triggered (+{combined_pnl_pct:.1f}% / +{combined_pnl_pts:.1f} pts)"


def on_new_peak(event: Dict[str, Any]) -> bool:
    """
    Dispatch hook triggered whenever a new peak profit passes all validation filters.

    Phase 1 (Active): Generates and dispatches styled Discord webhook notification.
    Phase 2 (Future Seam): Connect live broker order placement (Angel One / Upstox)
                           when ready to automate live futures/options trading.

    Returns:
        bool: True if alert dispatch succeeded (or in dry_run), False on failure.
    """
    fsm_info = f" | FSM: {event.get('fsm_state')} ({event.get('fsm_total_gainloss', 0.0):+.2f} pts)" if event.get("fsm_state") else ""
    logger.info(
        "🚀 [PEAK PROFIT TRIGGER] New Peak: +%.2f%% (+%.2f pts / ₹%.2f) on cycle %s (Alert #%d today%s)",
        event["combined_pnl_pct"],
        event["combined_pnl_pts"],
        event["combined_pnl_inr"],
        event["cycle_id"],
        event["alerts_sent_today"],
        fsm_info,
    )

    # 1. Dispatch Discord Alert
    dispatch_success = True
    if not event.get("dry_run"):
        dispatch_success = send_peak_alert_discord(event)
    else:
        logger.info("  [DRY-RUN] Peak alert Discord dispatch skipped.")

    # 2. Future Live Broker Hook Seam:
    # if dispatch_success and config.LIVE_EXECUTION_ENABLED:
    #     broker_execute_order(event)

    return dispatch_success


def send_peak_alert_discord(event: Dict[str, Any]) -> bool:
    """
    Send formatted peak-profit alert to Discord channel via Webhook.

    Webhook URL is fetched from DISCORD_URL environment variable or config.
    Wrapped with robust error isolation so collection/monitor runs never crash on webhook issues.
    """
    webhook_url = os.getenv("DISCORD_URL", "").strip() or DISCORD_URL
    if not webhook_url:
        logger.warning(
            "Peak profit alert Discord notification skipped: DISCORD_URL not set in environment or config."
        )
        return False

    time_ist = (
        datetime.fromtimestamp(event["timestamp"], tz=timezone.utc)
        .astimezone(tz_ist)
        .strftime("%Y-%m-%d %H:%M:%S IST")
    )

    call_pnl_sign = "+" if event.get("call_pnl_pts", 0.0) >= 0 else ""
    put_pnl_sign = "+" if event.get("put_pnl_pts", 0.0) >= 0 else ""
    comb_pnl_sign = "+" if event.get("combined_pnl_pts", 0.0) >= 0 else ""

    headline = (
        f"🎯 **NIFTY Strangle NEW PEAK PROFIT: {comb_pnl_sign}{event['combined_pnl_pct']:.1f}% "
        f"({comb_pnl_sign}{event['combined_pnl_pts']:.1f} pts / ₹{event['combined_pnl_inr']:,.0f})** | NIFTY: {event['nifty_ltp']:.2f}"
    )

    fsm_str = f" • FSM: `{event['fsm_state']}`" if event.get("fsm_state") else ""

    call_strike = event.get("call_strike", "N/A")
    call_buy = event.get("call_buy")
    call_ltp = event.get("call_ltp")
    call_buy_str = f"₹{call_buy:.2f}" if call_buy is not None else "N/A"
    call_ltp_str = f"₹{call_ltp:.2f}" if call_ltp is not None else "N/A"

    put_strike = event.get("put_strike", "N/A")
    put_buy = event.get("put_buy")
    put_ltp = event.get("put_ltp")
    put_buy_str = f"₹{put_buy:.2f}" if put_buy is not None else "N/A"
    put_ltp_str = f"₹{put_ltp:.2f}" if put_ltp is not None else "N/A"

    embed = {
        "title": "🎯 NIFTY Weekly Strangle High-Water Mark",
        "description": f"New peak profit achieved for active cycle `{event.get('cycle_id', 'UNKNOWN')}`{fsm_str}",
        "color": 0x10B981,  # Emerald green
        "fields": [
            {
                "name": "📊 Combined Peak Profit",
                "value": (
                    f"**{comb_pnl_sign}{event['combined_pnl_pct']:.2f}%**\n"
                    f"{comb_pnl_sign}{event['combined_pnl_pts']:.2f} pts  |  **+₹{event['combined_pnl_inr']:,.2f}** ({event.get('lot_size', NIFTY_LOT_SIZE)} Qty)"
                ),
                "inline": False,
            },
            {
                "name": f"🟢 CALL {call_strike}",
                "value": (
                    f"Buy: {call_buy_str}\n"
                    f"LTP: {call_ltp_str}\n"
                    f"Gain: `{call_pnl_sign}{event.get('call_pnl_pts', 0.0):.2f} pts`"
                ),
                "inline": True,
            },
            {
                "name": f"🔴 PUT {put_strike}",
                "value": (
                    f"Buy: {put_buy_str}\n"
                    f"LTP: {put_ltp_str}\n"
                    f"Gain: `{put_pnl_sign}{event.get('put_pnl_pts', 0.0):.2f} pts`"
                ),
                "inline": True,
            },
            {
                "name": "📍 Market Context",
                "value": (
                    f"NIFTY Spot: **{event['nifty_ltp']:.2f}**\n"
                    f"Expiry: `{event.get('expiry_date', 'N/A')}`\n"
                    f"Cycle Start: `{event.get('week_start_date', 'N/A')}`"
                ),
                "inline": False,
            },
            {
                "name": "📌 Alert Frequency & Hysteresis",
                "value": (
                    f"Alert #{event.get('alerts_sent_today', 1)} today.\n"
                    f"Next alert requires a +{PEAK_ALERT_HYSTERESIS_PCT:.1f}% or +{PEAK_ALERT_HYSTERESIS_PTS:.1f} pts expansion above this peak."
                ),
                "inline": False,
            },
        ],
        "footer": {
            "text": f"NIFTY 50 Strategy Automated Monitor • Time: {time_ist}"
        },
        "timestamp": datetime.fromtimestamp(event["timestamp"], tz=timezone.utc).isoformat(),
    }

    payload = {
        "content": headline,
        "embeds": [embed],
    }

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if 200 <= response.status_code < 300:
            logger.info(
                "✅ Peak-profit alert Discord notification successfully dispatched (HTTP %d)",
                response.status_code,
            )
            return True
        else:
            logger.error(
                "❌ Failed to send Discord peak-profit alert: HTTP %d - %s",
                response.status_code,
                response.text[:200] if response.text else "",
            )
            return False
    except Exception as exc:
        logger.error("❌ Failed to send peak-profit alert Discord notification: %s", exc)
        return False
