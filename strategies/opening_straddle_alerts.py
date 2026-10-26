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
    PreMarketIntelligence,
    SelectedLeg,
    StraddleSessionRecord,
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


def _ensure_dotenv_loaded():
    """Loads environment variables strictly from repository root .env."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv(override=False)


def resolve_straddle_discord_webhook() -> str:
    """
    Resolves the dedicated Discord webhook URL for opening straddle alerts.
    Strictly reads DISCORD_OPENING_STRADDLE from .env or environment to prevent
    spillover into general channels.
    """
    _ensure_dotenv_loaded()
    webhook = os.getenv("DISCORD_OPENING_STRADDLE", "").strip()
    if not webhook:
        logger.debug("DISCORD_OPENING_STRADDLE is not configured in .env.")
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
        "title": f"⚡ NIFTY 50 Opening Straddle Entered ({date_label})",
        "description": f"**ATM Strike:** `{atm_strike}` | **Total Debit:** `₹{total_debit_inr:,.2f}`",
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
            "text": "NIFTY 50 Opening Straddle Selector • Checkpoint 1 at 09:20:00 AM",
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
        title = f"🚀 [{checkpoint_name}] Bullish Decoupling: Retaining CALL Runner"
    elif selected_runner == SelectedLeg.PUT:
        color = COLOR_PUT_RUNNER
        title = f"🔻 [{checkpoint_name}] Bearish Decoupling: Retaining PUT Runner"
    else:
        color = COLOR_SCRATCH
        title = f"⚖️ [{checkpoint_name}] Market Indecisive: Flattening Both Legs (Scratch)"

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
            "text": "NIFTY 50 Opening Straddle Selector • Monitoring Runner",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return _post_webhook(webhook_url, {"embeds": [embed]})


def send_straddle_exit_summary(record: StraddleSessionRecord) -> bool:
    """Dispatches Session Exit & Daily P&L Summary Alert."""
    webhook_url = resolve_straddle_discord_webhook()
    if not webhook_url:
        return False

    if record.exit_reason == ExitReason.PROFIT_TARGET:
        color = COLOR_PROFIT
        emoji = "🏆"
    elif record.exit_reason == ExitReason.STOP_LOSS:
        color = COLOR_STOP_LOSS
        emoji = "🛑"
    elif record.exit_reason == ExitReason.TRAILING_STOP:
        color = COLOR_TRAILING
        emoji = "🔒"
    else:
        color = COLOR_SCRATCH
        emoji = "🏁"

    fields = [
        {
            "name": "💰 Net P&L",
            "value": f"**`₹{record.net_pnl_inr:+,.2f}`**",
            "inline": True,
        },
        {
            "name": "📊 Net Combined ROI",
            "value": f"**`{record.net_roi_pct:+.2f}%`**",
            "inline": True,
        },
        {
            "name": "📈 Peak Session ROI",
            "value": f"`{record.peak_roi_pct:+.2f}%`",
            "inline": True,
        },
        {
            "name": "🎯 Decision Time",
            "value": f"`{record.decision_time or 'N/A'}` ({record.selected_leg.value})",
            "inline": True,
        },
        {
            "name": "🚪 Exit Reason",
            "value": f"`{record.exit_reason.value}`",
            "inline": True,
        },
        {
            "name": "🧭 Pre-Market Prior",
            "value": f"CDS `{record.pre_market.cds:+.3f}` ({record.pre_market.bias.value})\nCoverage: `{record.pre_market.coverage * 100:.1f}%`",
            "inline": True,
        },
    ]

    if record.selected_leg in (SelectedLeg.CALL, SelectedLeg.PUT):
        closed_label = "PE (Put)" if record.selected_leg == SelectedLeg.CALL else "CE (Call)"
        runner_label = "CE (Call)" if record.selected_leg == SelectedLeg.CALL else "PE (Put)"
        closed_exit_val = f"₹{record.closed_leg_exit_price:.2f}" if record.closed_leg_exit_price is not None else "N/A"
        runner_exit_val = f"₹{record.runner_exit_price:.2f}" if record.runner_exit_price is not None else "N/A"

        fields.append({
            "name": f"🚪 Closed Leg [{closed_label}]",
            "value": f"Exit: `{closed_exit_val}`\nP&L: `₹{record.closed_leg_pnl_inr:+,.2f}`",
            "inline": True,
        })
        fields.append({
            "name": f"🏃 Runner Leg [{runner_label}]",
            "value": f"Exit: `{runner_exit_val}`\nP&L: `₹{record.runner_pnl_inr:+,.2f}`",
            "inline": True,
        })

    embed = {
        "title": f"{emoji} NIFTY 50 Opening Session Closed: {record.exit_reason.value}",
        "description": f"**Trade Date:** `{record.trade_date}` | **ATM Strike:** `{record.atm_strike}`",
        "color": color,
        "fields": fields,
        "footer": {
            "text": "NIFTY 50 Opening Straddle Selector • Daily Session Complete",
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
