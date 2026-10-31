"""
Discord Webhook Alert Dispatcher for NIFTY 50 Opening Straddle Strategy.

Dispatches real-time session notifications:
  1. Entry Fill Alert (09:15:05 AM)
  2. Decoupling Action Alert (09:20 / 09:25 AM)
  3. Exit & Daily P&L Summary Alert
"""
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from dotenv import load_dotenv
import requests

from strategies.opening_straddle_models import (
    ExitReason,
    OrderAction,
    PreMarketIntelligence,
    SelectedLeg,
    StraddleExecutionMode,
    StraddleSessionRecord,
    TradeLeg,
    derive_bias_from_cms,
)

logger = logging.getLogger("nifty50_strategies.opening_straddle_alerts")

# Color palette for Discord embeds
COLOR_ENTRY = 0x3498DB       # Blue
COLOR_CALL_RUNNER = 0x2ECC71  # Green
COLOR_PUT_RUNNER = 0xE74C3C   # Red
COLOR_SCRATCH = 0x95A5A6      # Grey
COLOR_PROFIT = 0x2ECC71       # Green
COLOR_STOP_LOSS = 0xE74C3C    # Red
COLOR_TRAILING = 0xF39C12     # Amber / Gold


# Load .env once on module import
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.is_file():
    load_dotenv(dotenv_path=_env_path, override=False)
else:
    load_dotenv(override=False)


def resolve_straddle_discord_webhook() -> str:
    """
    Resolves the dedicated Discord webhook URL for opening straddle alerts.
    Strictly reads DISCORD_OPENING_STRADDLE from environment / .env.
    """
    webhook = os.getenv("DISCORD_OPENING_STRADDLE", "").strip()
    if not webhook:
        logger.debug("DISCORD_OPENING_STRADDLE is not configured.")
    return webhook


def send_straddle_entry_alert(
    atm_strike: int,
    call_entry: float,
    put_entry: float,
    total_debit_inr: float,
    intel: PreMarketIntelligence,
    trade_date: Optional[str] = None,
) -> bool:
    """Dispatches 09:15:05 AM Opening Straddle Entry Alert."""
    webhook_url = resolve_straddle_discord_webhook()
    if not webhook_url:
        logger.debug("Discord alert skipped: Webhook URL not configured.")
        return False

    date_label = trade_date or datetime.now().strftime("%Y-%m-%d")
    embed = {
        "title": f"⚡ [PAPER TRADING] NIFTY 50 Opening Straddle Entered ({date_label})",
        "description": f"**[Paper Trading • Virtual Position]**\n**ATM Strike:** `{atm_strike}` | **Total Debit:** `₹{total_debit_inr:,.2f}`",
        "color": COLOR_ENTRY,
        "fields": [
            {
                "name": "📞 Call Entry (C0)",
                "value": f"`₹{call_entry:.2f}`",
                "inline": True,
            },
            {
                "name": "📉 Put Entry (P0)",
                "value": f"`₹{put_entry:.2f}`",
                "inline": True,
            },
            {
                "name": "🛡️ Volatility Regime",
                "value": f"`{intel.vix_regime.value}` (VIX: {intel.vix:.2f})",
                "inline": True,
            },
            {
                "name": "🧭 Pre-Market Prior",
                "value": f"**CDS:** `{intel.cds:+.3f}`\n**Bias:** `{intel.bias.value}`",
                "inline": True,
            },
            {
                "name": "📊 Data Coverage",
                "value": f"`{intel.coverage * 100:.1f}%` ({intel.confidence.value})",
                "inline": True,
            },
            {
                "name": "⚡ Contrarian Filter",
                "value": "`ELIGIBLE`" if intel.is_contrarian_filter_eligible else "`INELIGIBLE`",
                "inline": True,
            },
        ],
        "footer": {
            "text": "NIFTY 50 Opening Straddle • Paper Trading • Checkpoint 1 at 09:20:00 AM",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return _post_webhook(webhook_url, {"embeds": [embed]})


def send_single_leg_entry_alert(
    leg: TradeLeg,
    action: OrderAction,
    strike: int,
    entry_price: float,
    capital_deployed_inr: float,
    margin_inr: float,
    intel: PreMarketIntelligence,
    trade_date: Optional[str] = None,
    final_cms: Optional[float] = None,
) -> bool:
    """Dispatches 09:16:05 AM Single-Leg Directional Entry Alert."""
    webhook_url = resolve_straddle_discord_webhook()
    if not webhook_url:
        logger.debug("Discord alert skipped: Webhook URL not configured.")
        return False

    date_label = trade_date or datetime.now().strftime("%Y-%m-%d")
    color = COLOR_CALL_RUNNER if action == OrderAction.BUY else COLOR_TRAILING
    leg_type_icon = "📞 CALL" if leg == TradeLeg.CALL else "📉 PUT"
    action_type = "BUY" if action == OrderAction.BUY else "SELL"

    vix_str = f"{intel.vix:.2f}" if intel.vix is not None else "N/A"
    regime_str = intel.vix_regime.value if hasattr(intel.vix_regime, "value") else str(intel.vix_regime)
    cms_val = (
        final_cms
        if final_cms is not None
        else (intel.composite_morning_score if intel.composite_morning_score is not None else intel.cds)
    )

    active_bias = derive_bias_from_cms(cms_val) if cms_val is not None else intel.bias
    bias_label = (
        f"`{active_bias.value}`"
        if active_bias == intel.bias
        else f"`{active_bias.value}` *(Premarket: {intel.bias.value})*"
    )

    embed = {
        "title": f"⚡ [PAPER TRADING] NIFTY 50 Single-Leg Directional Scalp Entered ({date_label})",
        "description": (
            f"**[Paper Trading • Virtual Scalp Position]**\n"
            f"**Order:** `{action_type} {leg.value} {strike}` @ `₹{entry_price:.2f}`\n"
            f"**Capital / Margin:** `₹{capital_deployed_inr:,.2f}`"
        ),
        "color": color,
        "fields": [
            {
                "name": f"{leg_type_icon} Strike",
                "value": f"`{strike}`",
                "inline": True,
            },
            {
                "name": "💵 Entry Price",
                "value": f"`₹{entry_price:.2f}`",
                "inline": True,
            },
            {
                "name": "🛡️ VIX Status",
                "value": f"`{regime_str}` (VIX: {vix_str})",
                "inline": True,
            },
            {
                "name": "🧭 Composite Score (CMS)",
                "value": f"`{cms_val:+.3f}`\n**Bias:** {bias_label}",
                "inline": True,
            },
            {
                "name": "📊 Data Coverage",
                "value": f"`{intel.coverage * 100:.1f}%` ({intel.confidence.value})",
                "inline": True,
            },
            {
                "name": "🎯 Target Exit",
                "value": "+8.0 pts net / +10% (BUY) / +12% (SELL)\nHard Stop: 09:55 AM IST",
                "inline": True,
            },
        ],
        "footer": {
            "text": "NIFTY 50 Directional Scalper • Paper Trading • Active Monitoring",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return _post_webhook(webhook_url, {"embeds": [embed]})


def send_skipped_session_alert(
    skip_reason: str,
    exit_reason: ExitReason,
    intel: PreMarketIntelligence,
    trade_date: Optional[str] = None,
    final_cms: Optional[float] = None,
) -> bool:
    """Dispatches alert when session is skipped due to High VIX, Missing VIX, Neutral Chop, or Insufficient Coverage."""
    webhook_url = resolve_straddle_discord_webhook()
    if not webhook_url:
        return False

    date_label = trade_date or datetime.now().strftime("%Y-%m-%d")
    vix_str = f"{intel.vix:.2f}" if intel.vix is not None else "N/A"
    regime_str = intel.vix_regime.value if hasattr(intel.vix_regime, "value") else str(intel.vix_regime)
    cms_val = (
        final_cms
        if final_cms is not None
        else (intel.composite_morning_score if intel.composite_morning_score is not None else intel.cds)
    )

    is_warning = exit_reason in (ExitReason.MISSING_VIX_SKIP, ExitReason.INSUFFICIENT_COVERAGE_SKIP)
    icon = "⚠️" if is_warning else "🛡️"

    embed = {
        "title": f"{icon} [PAPER TRADING] Morning Strategy Skipped: {exit_reason.value} ({date_label})",
        "description": f"**[Paper Trading • Virtual Tracking]**\n**Reason:** `{skip_reason}`\nZero trades placed, zero capital risked.",
        "color": COLOR_SCRATCH,
        "fields": [
            {
                "name": "🛡️ Volatility Regime",
                "value": f"`{regime_str}` (VIX: {vix_str})",
                "inline": True,
            },
            {
                "name": "🧭 Directional Score (CMS)",
                "value": f"**CMS:** `{cms_val:+.3f}`\n**Coverage:** `{intel.coverage * 100:.1f}%`",
                "inline": True,
            },
            {
                "name": "🚪 Action Taken",
                "value": f"`{exit_reason.value}`",
                "inline": True,
            },
        ],
        "footer": {
            "text": "NIFTY 50 Directional Scalper • Paper Trading • Day Skipped Cleanly",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return _post_webhook(webhook_url, {"embeds": [embed]})


def send_decoupling_alert(
    checkpoint_name: str,
    selected_runner: SelectedLeg,
    closed_leg: SelectedLeg,
    closed_exit_price: float,
    closed_pnl_inr: float,
    closed_pnl_pct: float,
    live_roi_pct: float,
) -> bool:
    """Dispatches 09:20:00 or 09:25:00 Decoupling Action Alert."""
    webhook_url = resolve_straddle_discord_webhook()
    if not webhook_url:
        return False

    if selected_runner == SelectedLeg.CALL:
        color = COLOR_CALL_RUNNER
        title = f"🚀 [PAPER TRADING] [{checkpoint_name}] Bullish Decoupling: Retaining CALL Runner"
    elif selected_runner == SelectedLeg.PUT:
        color = COLOR_PUT_RUNNER
        title = f"🔻 [PAPER TRADING] [{checkpoint_name}] Bearish Decoupling: Retaining PUT Runner"
    else:
        color = COLOR_SCRATCH
        title = f"⚖️ [PAPER TRADING] [{checkpoint_name}] Market Indecisive: Flattening Both Legs (Scratch)"

    embed = {
        "title": title,
        "color": color,
        "fields": [
            {
                "name": "🏃 Retained Runner",
                "value": f"`{selected_runner.value}`",
                "inline": True,
            },
            {
                "name": "🚪 Closed Leg",
                "value": f"`{closed_leg.value}` @ `₹{closed_exit_price:.2f}`",
                "inline": True,
            },
            {
                "name": "💵 Closed Leg P&L",
                "value": f"`₹{closed_pnl_inr:+,.2f}` (`{closed_pnl_pct:+.2f}%`)",
                "inline": True,
            },
            {
                "name": "📈 Live Net ROI",
                "value": f"`{live_roi_pct:+.2f}%`",
                "inline": True,
            },
        ],
        "footer": {
            "text": "NIFTY 50 Opening Straddle • Paper Trading • Monitoring Runner",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return _post_webhook(webhook_url, {"embeds": [embed]})


def send_straddle_exit_summary(record: StraddleSessionRecord) -> bool:
    """Dispatches Session Exit & Daily P&L Summary Alert."""
    webhook_url = resolve_straddle_discord_webhook()
    if not webhook_url:
        return False

    is_single_leg = (
        getattr(record, "execution_mode", None) == StraddleExecutionMode.SINGLE_LEG_DIRECTIONAL.value
        or (hasattr(record, "order_action") and record.order_action is not None and getattr(record.order_action, "value", str(record.order_action)) in ("BUY", "SELL"))
    )

    if is_single_leg:
        # Single-leg scalp exit formatting
        act_str = record.order_action.value if hasattr(record.order_action, "value") else str(record.order_action or "TRADE")
        leg_str = record.trade_leg.value if hasattr(record.trade_leg, "value") else str(record.trade_leg or "LEG")
        stk_str = str(record.executed_strike or record.atm_strike or "")
        p0_str = f"₹{record.entry_price:.2f}" if record.entry_price is not None else "N/A"
        pe_str = f"₹{record.exit_price:.2f}" if record.exit_price is not None else "N/A"

        # Determine points captured
        if record.entry_price is not None and record.exit_price is not None:
            if act_str == "BUY":
                pts = record.exit_price - record.entry_price
            else:
                pts = record.entry_price - record.exit_price
            pts_str = f"{pts:+.2f} pts"
        else:
            pts_str = "N/A"

        pnl_sign = "+" if record.net_pnl_inr >= 0 else ""
        net_pnl_str = f"₹{pnl_sign}{record.net_pnl_inr:,.2f}"
        net_roi_str = f"{record.net_roi_pct:+.2f}%"
        prem_pnl_str = f"{record.premium_pnl_pct:+.2f}%"

        if record.exit_reason in (ExitReason.MIN_PROFIT_TARGET, ExitReason.PROFIT_TARGET):
            color = COLOR_PROFIT
            emoji = "🏆"
            reason_label = "QUICK SCALP PROFIT TARGET"
        elif record.exit_reason == ExitReason.TRAILING_STOP:
            color = COLOR_TRAILING
            emoji = "🔒"
            reason_label = "TRAILING STOP (PROFIT RATCHETED)"
        elif record.exit_reason == ExitReason.STOP_LOSS:
            color = COLOR_STOP_LOSS
            emoji = "🛑"
            reason_label = "STOP LOSS TRIGGERED"
        elif record.exit_reason == ExitReason.HARD_TIME_STOP:
            color = COLOR_ENTRY
            emoji = "⏰"
            reason_label = "09:55 AM HARD TIME STOP"
        else:
            color = COLOR_SCRATCH
            emoji = "🛡️"
            reason_label = record.exit_reason.value if hasattr(record.exit_reason, "value") else str(record.exit_reason)

        cms_val = (
            record.pre_market.composite_morning_score
            if record.pre_market.composite_morning_score is not None
            else record.pre_market.cds
        )
        active_bias = getattr(record, "final_bias", None) or getattr(record.pre_market, "final_bias", None) or derive_bias_from_cms(cms_val)
        premarket_bias = record.pre_market.bias
        bias_tag = (
            f"`{active_bias.value}`"
            if active_bias == premarket_bias
            else f"`{active_bias.value}` *(Prior: {premarket_bias.value})*"
        )

        fields = [
            {
                "name": "💰 Net Realized P&L",
                "value": f"**`{net_pnl_str}`** (`{net_roi_str}`)",
                "inline": False,
            },
            {
                "name": "🎯 Contract Executed",
                "value": f"`{act_str} {leg_str} {stk_str}`",
                "inline": True,
            },
            {
                "name": "📊 Price Movement",
                "value": f"P0: `{p0_str}` ➔ Exit: `{pe_str}`\n(`{pts_str}` | `{prem_pnl_str}`)",
                "inline": True,
            },
            {
                "name": "🚪 Exit Reason",
                "value": f"`{reason_label}`",
                "inline": True,
            },
            {
                "name": "🧭 Entry CMS",
                "value": f"`{cms_val:+.3f}` ({bias_tag})",
                "inline": True,
            },
            {
                "name": "⏱️ Execution Window",
                "value": "09:16:05 ➔ 09:55:00 AM IST",
                "inline": True,
            },
        ]

        embed = {
            "title": f"{emoji} [PAPER TRADING] Directional Scalp Closed: {record.exit_reason.value}",
            "description": (
                f"**[Paper Trading • Virtual Scalp Position]**\n"
                f"**Trade Date:** `{record.trade_date}` | **Position:** `{act_str} {leg_str} {stk_str}`\n"
                f"**Net Result:** **`{net_pnl_str}`** (`{net_roi_str}` on capital)"
            ),
            "color": color,
            "fields": fields,
            "footer": {
                "text": "NIFTY 50 Directional Scalper • Paper Trading • Session Closed",
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return _post_webhook(webhook_url, {"embeds": [embed]})

    if record.exit_reason == ExitReason.PROFIT_TARGET:
        color = COLOR_PROFIT
        emoji = "🏆"
        reason_label = "PROFIT TARGET REACHED"
    elif record.exit_reason == ExitReason.STOP_LOSS:
        color = COLOR_STOP_LOSS
        emoji = "🛑"
        reason_label = "STOP LOSS TRIGGERED"
    elif record.exit_reason == ExitReason.TRAILING_STOP:
        color = COLOR_TRAILING
        emoji = "🔒"
        reason_label = "TRAILING STOP (PROFIT PROTECTED)"
    elif record.exit_reason == ExitReason.HARD_TIME_STOP:
        color = COLOR_ENTRY
        emoji = "⏰"
        reason_label = "09:55 AM HARD TIME STOP"
    else:
        color = COLOR_SCRATCH
        emoji = "⚖️"
        reason_label = "SCRATCH EXIT (FLATTENED)"

    atm = record.atm_strike
    call_exit_val = None
    call_pnl_val = 0.0
    put_exit_val = None
    put_pnl_val = 0.0

    if record.selected_leg == SelectedLeg.CALL:
        call_exit_val = f"₹{record.runner_exit_price:.2f}" if record.runner_exit_price is not None else "N/A"
        call_pnl_val = record.runner_pnl_inr
        put_exit_val = f"₹{record.closed_leg_exit_price:.2f}" if record.closed_leg_exit_price is not None else "N/A"
        put_pnl_val = record.closed_leg_pnl_inr
    elif record.selected_leg == SelectedLeg.PUT:
        call_exit_val = f"₹{record.closed_leg_exit_price:.2f}" if record.closed_leg_exit_price is not None else "N/A"
        call_pnl_val = record.closed_leg_pnl_inr
        put_exit_val = f"₹{record.runner_exit_price:.2f}" if record.runner_exit_price is not None else "N/A"
        put_pnl_val = record.runner_pnl_inr
    else:
        call_exit_val = f"₹{record.closed_leg_exit_price:.2f}" if record.closed_leg_exit_price is not None else "N/A"
        call_pnl_val = record.closed_leg_pnl_inr
        put_exit_val = f"₹{record.runner_exit_price:.2f}" if record.runner_exit_price is not None else "N/A"
        put_pnl_val = record.runner_pnl_inr

    call_pnl_str = f"₹{call_pnl_val:+,.2f}"
    put_pnl_str = f"₹{put_pnl_val:+,.2f}"

    net_sign = "+" if record.net_pnl_inr >= 0 else ""
    net_pnl_str = f"₹{net_sign}{record.net_pnl_inr:,.2f}"
    net_roi_str = f"{record.net_roi_pct:+.2f}%"

    fields = [
        {
            "name": "💰 Net Total P&L",
            "value": f"**`{net_pnl_str}`** (`{net_roi_str}`)",
            "inline": False,
        },
        {
            "name": f"📞 CALL Leg (CE {atm})",
            "value": f"Exit: `{call_exit_val}` ➔ **P&L: `{call_pnl_str}`**",
            "inline": True,
        },
        {
            "name": f"📉 PUT Leg (PE {atm})",
            "value": f"Exit: `{put_exit_val}` ➔ **P&L: `{put_pnl_str}`**",
            "inline": True,
        },
        {
            "name": "🚪 Exit Reason",
            "value": f"`{reason_label}`",
            "inline": True,
        },
        {
            "name": "⏱️ Decision / Exit Time",
            "value": f"`{record.decision_time or '09:20:00'}`",
            "inline": True,
        },
    ]

    embed = {
        "title": f"{emoji} [PAPER TRADING] NIFTY 50 Opening Session Closed: {record.exit_reason.value}",
        "description": (
            f"**[Paper Trading • Virtual Execution]**\n"
            f"**Trade Date:** `{record.trade_date}` | **ATM Strike:** `{atm}`\n"
            f"**Result:** `{call_pnl_str}` (CE) + `{put_pnl_str}` (PE) = **`{net_pnl_str}`**"
        ),
        "color": color,
        "fields": fields,
        "footer": {
            "text": "NIFTY 50 Opening Straddle • Paper Trading • Daily Session Complete",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return _post_webhook(webhook_url, {"embeds": [embed]})


def send_daily_session_report_from_db(
    db_path: Optional[str] = None,
    trade_date: Optional[str] = None,
    force: bool = False,
) -> bool:
    """
    Reads the latest session record from opening_straddle.db and sends a complete Discord report.
    Guarantees exactly ONE report per day: skips if report was already sent earlier today unless force=True.
    """
    import sqlite3
    from strategies.opening_straddle_models import (
        ConfidenceLevel,
        BiasClassification,
        VolatilityRegime,
    )

    webhook_url = resolve_straddle_discord_webhook()
    if not webhook_url:
        logger.warning("Discord report skipped: DISCORD_OPENING_STRADDLE not configured in .env.")
        return False

    default_db = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "opening_straddle.db"
    )
    target_db = db_path or os.getenv("OPENING_STRADDLE_DB", default_db)
    if not os.path.isfile(target_db):
        logger.warning("Opening straddle database not found at: %s", target_db)
        return False

    try:
        with sqlite3.connect(target_db) as conn:
            conn.row_factory = sqlite3.Row
            if trade_date:
                row = conn.execute(
                    "SELECT * FROM opening_straddle_sessions WHERE trade_date = ? ORDER BY id DESC LIMIT 1",
                    (trade_date,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM opening_straddle_sessions ORDER BY id DESC LIMIT 1"
                ).fetchone()

        if not row:
            logger.warning("No session records found in %s", target_db)
            return False

        # Deduplication check: prevent sending more than 1 report per day
        has_sent = bool(row["report_sent"]) if "report_sent" in row.keys() and row["report_sent"] is not None else False
        if not force and has_sent:
            logger.info("Daily report for %s was already sent earlier today. Skipping duplicate.", row["trade_date"])
            return True

        confidence_val = ConfidenceLevel.LOW
        for c in ConfidenceLevel:
            if c.value == row["cds_confidence"]:
                confidence_val = c
                break

        bias_val = BiasClassification.NEUTRAL
        for b in BiasClassification:
            if b.value == row["bias_classification"]:
                bias_val = b
                break

        intel = PreMarketIntelligence(
            cds=float(row["composite_directional_score"] or 0.0),
            coverage=float(row["cds_coverage"] or 0.0),
            confidence=confidence_val,
            bias=bias_val,
            components={},
            vix=float(row["vix_open"] or 0.0),
            vix_regime=VolatilityRegime.NORMAL,
            timestamp_utc="",
        )

        selected_leg_val = SelectedLeg.NONE
        for leg in SelectedLeg:
            if leg.value == row["selected_leg"]:
                selected_leg_val = leg
                break

        exit_reason_val = ExitReason.NONE
        for er in ExitReason:
            if er.value == row["exit_reason"]:
                exit_reason_val = er
                break

        order_action_val = None
        if "order_action" in row.keys() and row["order_action"]:
            for oa in OrderAction:
                if oa.value == row["order_action"]:
                    order_action_val = oa
                    break

        trade_leg_val = TradeLeg.NONE
        if "trade_leg" in row.keys() and row["trade_leg"]:
            for tl in TradeLeg:
                if tl.value == row["trade_leg"]:
                    trade_leg_val = tl
                    break

        record = StraddleSessionRecord(
            trade_date=str(row["trade_date"]),
            expiry_date=str(row["expiry_date"]),
            atm_strike=int(row["atm_strike"] or 0),
            pre_market=intel,
            call_entry_price=float(row["call_entry_price"] or 0.0),
            put_entry_price=float(row["put_entry_price"] or 0.0),
            total_debit_inr=float(row["total_debit_inr"] or 0.0),
            selected_leg=selected_leg_val,
            decision_time=row["decision_time"],
            closed_leg_exit_price=float(row["closed_leg_exit_price"]) if row["closed_leg_exit_price"] is not None else None,
            closed_leg_pnl_inr=float(row["closed_leg_pnl_inr"] or 0.0),
            runner_exit_price=float(row["runner_exit_price"]) if row["runner_exit_price"] is not None else None,
            runner_pnl_inr=float(row["runner_pnl_inr"] or 0.0),
            net_pnl_inr=float(row["net_pnl_inr"] or 0.0),
            net_roi_pct=float(row["net_roi_pct"] or 0.0),
            peak_roi_pct=float(row["peak_roi_pct"] or 0.0),
            spot_confirmed_ticks=int(row["spot_confirmed_ticks"] or 0),
            exit_reason=exit_reason_val,
            status=str(row["status"] or "COMPLETED"),
            execution_mode=str(row["execution_mode"] or "BUY_STRADDLE") if "execution_mode" in row.keys() else "BUY_STRADDLE",
            order_action=order_action_val,
            trade_leg=trade_leg_val,
            executed_strike=int(row["executed_strike"]) if ("executed_strike" in row.keys() and row["executed_strike"] is not None) else None,
            entry_price=float(row["entry_price"]) if ("entry_price" in row.keys() and row["entry_price"] is not None) else None,
            exit_price=float(row["exit_price"]) if ("exit_price" in row.keys() and row["exit_price"] is not None) else None,
            margin_inr=float(row["margin_inr"] or 0.0) if "margin_inr" in row.keys() else 0.0,
            premium_pnl_pct=float(row["premium_pnl_pct"] or 0.0) if "premium_pnl_pct" in row.keys() else 0.0,
            capital_deployed_inr=float(row["capital_deployed_inr"] or 0.0) if "capital_deployed_inr" in row.keys() else 0.0,
        )

        success = send_straddle_exit_summary(record)
        if success:
            with sqlite3.connect(target_db) as conn:
                try:
                    conn.execute(
                        "UPDATE opening_straddle_sessions SET report_sent = 1 WHERE trade_date = ?",
                        (str(row["trade_date"]),),
                    )
                    conn.commit()
                except Exception:
                    pass
        return success
    except Exception as exc:
        logger.error("Failed to generate Discord report from DB: %s", exc)
        return False


def _post_webhook(url: str, payload: Dict[str, Any]) -> bool:
    """Sends payload to Discord webhook safely."""
    try:
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code in (200, 204):
            logger.info("Dispatched Discord alert successfully.")
            return True
        else:
            logger.warning("Discord webhook returned HTTP %d: %s", resp.status_code, resp.text[:120])
            return False
    except Exception as exc:
        logger.warning("Failed to dispatch Discord webhook: %s", exc)
        return False
