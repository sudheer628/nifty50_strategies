"""Generate and email the NIFTY50 Tuesday-Monday weekly strategy report.

Examples:
    python scripts/send_weekly_report.py --dry-run
    python scripts/send_weekly_report.py
    python scripts/send_weekly_report.py --db /path/to/weekly.db --dry-run
"""

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pytz
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from config import (
    SQLITE_DIR,
    SNAPSHOT_DIR,
    ACTIVE_SNAPSHOT_FILE,
    THURSDAY_SNAPSHOT_FILE,
    STRATEGY_NAME,
    STRATEGY_NAME_THURSDAY,
    NIFTY_LOT_SIZE,
    DISCORD_URL,
    DISCORD_EXIT_3DAY,
    DISCORD_EXIT_5DAY,
)


logger = logging.getLogger("nifty50_weekly_report")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.UTC
DB_NAME_PATTERN_WEEKLY = re.compile(
    r"nifty50_weekly_data_(?P<start>\d{8})_(?P<expiry>\d{8})\.db$"
)
DB_NAME_PATTERN_THURSDAY = re.compile(
    r"nifty50_thursday_data_(?P<start>\d{8})_(?P<expiry>\d{8})\.db$"
)
DB_NAME_PATTERN = DB_NAME_PATTERN_WEEKLY


def _parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _parse_timestamp(value) -> datetime:
    """Parse timestamp that can be either integer (Unix epoch) or ISO string."""
    if isinstance(value, (int, float)):
        # Unix timestamp
        dt = datetime.fromtimestamp(int(value), tz=UTC)
    else:
        # ISO string format
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = UTC.localize(parsed)
        dt = parsed
    return dt.astimezone(IST)


def find_weekly_db(report_date: date, explicit_path: Optional[str] = None, track: str = "weekly") -> Path:
    """Find the strategy DB whose cycle window covers report_date."""
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Strategy database not found: {path}")
        return path

    is_thu = track.lower() == "thursday"
    pattern = DB_NAME_PATTERN_THURSDAY if is_thu else DB_NAME_PATTERN_WEEKLY
    glob_str = "nifty50_thursday_data_*_*.db" if is_thu else "nifty50_weekly_data_*_*.db"

    candidates: List[Tuple[date, date, Path]] = []
    for path in Path(SQLITE_DIR).glob(glob_str):
        match = pattern.match(path.name)
        if not match:
            continue
        start = _parse_yyyymmdd(match.group("start"))
        expiry = _parse_yyyymmdd(match.group("expiry"))
        if start <= report_date <= expiry:
            candidates.append((start, expiry, path))

    if not candidates:
        raise FileNotFoundError(
            f"No {track} strategy database in {SQLITE_DIR} covers {report_date.isoformat()}"
        )
    return max(candidates, key=lambda item: item[0])[2]


def _format_leg_status(status_str: Optional[str]) -> str:
    """Format leg status for human-readable report display."""
    if not status_str:
        return "Open"
    s = str(status_str).upper()
    if "HARVEST" in s:
        return "House Money Harvested ✓"
    if "SALVAGE" in s:
        return "Salvage Stop Hit (Capital Rescued)"
    if "TRAILING" in s:
        return "Trailing Stop Hit"
    if "CLOSED" in s:
        return "Closed"
    if "ACTIVE" in s:
        return "Active"
    return str(status_str)


def _enrich_snapshot_with_exits(snapshot: Dict[str, Any], db_path: Path, track: Optional[str] = None) -> Dict[str, Any]:
    """Enrich cycle snapshot with high-water mark, Alpha FSM state, and AI exit metadata."""
    enriched = dict(snapshot)
    cycle_id = enriched.get("cycle_id")

    # 1. Attempt to load from active snapshot JSON or directory
    is_thu = (track and track.lower() == "thursday") or ("thursday" in db_path.name.lower())
    active_snap_file = Path(THURSDAY_SNAPSHOT_FILE if is_thu else ACTIVE_SNAPSHOT_FILE)

    snap_candidates = [active_snap_file]
    if SNAPSHOT_DIR:
        snap_dir = Path(SNAPSHOT_DIR)
        if snap_dir.is_dir():
            snap_candidates.extend(snap_dir.glob("current_*_buy.json"))
            if cycle_id:
                snap_candidates.extend(snap_dir.glob(f"*{cycle_id}*.json"))

    for sc in snap_candidates:
        if sc.is_file():
            try:
                data = json.loads(sc.read_text(encoding="utf-8"))
                file_cycle = data.get("cycle_id")
                if not cycle_id or not file_cycle or file_cycle == cycle_id:
                    if "peak_profit" in data and "peak_profit" not in enriched:
                        enriched["peak_profit"] = data["peak_profit"]
                    if "alpha_fsm" in data and "alpha_fsm" not in enriched:
                        enriched["alpha_fsm"] = data["alpha_fsm"]
                    break
            except Exception:
                pass

    # 2. Check sentinel-hermes predictions.db for dispatched exit alerts if available
    pred_db_candidates = [
        PROJECT_ROOT.parent / "sentinel-hermes" / "predictions.db",
        Path(os.path.expanduser("~/sentinel-hermes/predictions.db")),
    ]
    pred_db = next((p for p in pred_db_candidates if p.is_file()), None)
    if pred_db and cycle_id:
        try:
            conn_pred = sqlite3.connect(str(pred_db))
            conn_pred.row_factory = sqlite3.Row
            try:
                tbls = {r[0] for r in conn_pred.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if "dispatched_exit_alerts" in tbls:
                    alerts = [
                        dict(r) for r in conn_pred.execute(
                            "SELECT leg, action, dispatched_ts, leg_pnl_pct, total_pnl_pct "
                            "FROM dispatched_exit_alerts WHERE cycle_id = ? ORDER BY dispatched_ts ASC",
                            (cycle_id,),
                        ).fetchall()
                    ]
                    if alerts:
                        enriched["dispatched_exit_alerts"] = alerts

                if "ai_dispatched_exit_alerts" in tbls:
                    ai_alerts = [
                        dict(r) for r in conn_pred.execute(
                            "SELECT leg, action, matched_rule, confidence, dispatched_ts, leg_pnl_pct, total_pnl_pct, model_name "
                            "FROM ai_dispatched_exit_alerts WHERE cycle_id = ? ORDER BY dispatched_ts ASC",
                            (cycle_id,),
                        ).fetchall()
                    ]
                    if ai_alerts:
                        enriched["ai_dispatched_exit_alerts"] = ai_alerts
            finally:
                conn_pred.close()
        except Exception:
            pass

    return enriched


def load_report_data(db_path: Path, track: Optional[str] = None) -> Tuple[List[Dict], Dict]:
    """Load hourly rows and the cycle snapshot from a weekly or thursday database."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(strategy_hourly_data)")
        }
        gainloss_sql = (
            "gainloss"
            if "gainloss" in columns
            else "ROUND((call_ltp-call_buy_price)+(put_ltp-put_buy_price), 2)"
        )

        fsm_cols_to_check = [
            "fsm_state", "fsm_call_status", "fsm_put_status",
            "fsm_call_exit_price", "fsm_put_exit_price",
            "fsm_realized_pnl", "fsm_unrealized_pnl",
            "fsm_total_gainloss", "fsm_roi_pct"
        ]
        fsm_select_parts = [col for col in fsm_cols_to_check if col in columns]
        fsm_select_sql = (", " + ", ".join(fsm_select_parts)) if fsm_select_parts else ""

        is_thu = (track and track.lower() == "thursday") or ("thursday" in db_path.name.lower())
        target_strategy = STRATEGY_NAME_THURSDAY if is_thu else STRATEGY_NAME

        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT
                    id, collection_timestamp, expiry_date,
                    nifty_open, nifty_ltp, nifty_previous_close,
                    put_strike, put_ltp, put_buy_price,
                    call_strike, call_ltp, call_buy_price,
                    {gainloss_sql} AS gainloss, cycle_id{fsm_select_sql}
                FROM strategy_hourly_data
                WHERE strategy_name = ?
                ORDER BY collection_timestamp ASC
                """,
                (target_strategy,),
            )
        ]
        # Resilient fallback: if no rows match target_strategy, query all rows from this dedicated cycle DB
        if not rows:
            rows = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT
                        id, collection_timestamp, expiry_date,
                        nifty_open, nifty_ltp, nifty_previous_close,
                        put_strike, put_ltp, put_buy_price,
                        call_strike, call_ltp, call_buy_price,
                        {gainloss_sql} AS gainloss, cycle_id{fsm_select_sql}
                    FROM strategy_hourly_data
                    ORDER BY collection_timestamp ASC
                    """
                )
            ]

        snapshot_row = conn.execute(
            """
            SELECT * FROM strategy_buy_snapshots
            WHERE strategy_name = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (target_strategy,),
        ).fetchone()
        if not snapshot_row:
            snapshot_row = conn.execute(
                """
                SELECT * FROM strategy_buy_snapshots
                ORDER BY captured_at DESC
                LIMIT 1
                """
            ).fetchone()
        snapshot = dict(snapshot_row) if snapshot_row else {}

        timing_shadows = []
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='timing_shadow_entries'")
            if cur.fetchone():
                timing_shadows = [dict(r) for r in conn.execute("SELECT * FROM timing_shadow_entries ORDER BY id ASC").fetchall()]
        except Exception:
            pass
        snapshot["timing_shadows"] = timing_shadows
    finally:
        conn.close()

    if not rows:
        raise ValueError(f"No strategy rows found in {db_path}")
    for row in rows:
        row["timestamp_ist"] = _parse_timestamp(row["collection_timestamp"])

    snapshot = _enrich_snapshot_with_exits(snapshot, db_path, track=track)
    return rows, snapshot


def calculate_summary(rows: Sequence[Dict], snapshot: Dict, db_path: Path) -> Dict:
    """Calculate headline metrics for the HTML report and Discord alerts."""
    first = rows[0]
    latest = rows[-1]
    gains = [float(row["gainloss"]) for row in rows if row["gainloss"] is not None]
    is_thu = "thursday" in db_path.name.lower()
    pattern = DB_NAME_PATTERN_THURSDAY if is_thu else DB_NAME_PATTERN_WEEKLY
    match = pattern.match(db_path.name)
    start_str = snapshot.get("week_start_date") or (
        match.group("start") if match else ""
    )
    expiry_str = snapshot.get("expiry_date") or latest["expiry_date"]
    start_date = _parse_yyyymmdd(start_str) if start_str else None
    expiry_date = _parse_yyyymmdd(str(expiry_str))
    nifty_start = float(first["nifty_ltp"])
    nifty_latest = float(latest["nifty_ltp"])

    call_buy = float(snapshot.get("call_buy_price") or latest.get("call_buy_price") or 0.0)
    put_buy = float(snapshot.get("put_buy_price") or latest.get("put_buy_price") or 0.0)
    total_cost = round(call_buy + put_buy, 2)
    passive_gainloss = gains[-1] if gains else None
    best_gainloss = max(gains) if gains else None
    worst_gainloss = min(gains) if gains else None

    # -------------------------------------------------------------------------
    # Single Source of Truth Resolution: Systematic Strategy vs Passive Hold
    # -------------------------------------------------------------------------
    alpha_snapshot = snapshot.get("alpha_fsm") or {}
    fsm_state = str(latest.get("fsm_state") or alpha_snapshot.get("state") or "DUAL_LONG").upper()
    fsm_call_status = str(latest.get("fsm_call_status") or alpha_snapshot.get("call_leg", {}).get("status") or "ACTIVE").upper()
    fsm_put_status = str(latest.get("fsm_put_status") or alpha_snapshot.get("put_leg", {}).get("status") or "ACTIVE").upper()
    fsm_call_exit = latest.get("fsm_call_exit_price") or alpha_snapshot.get("call_leg", {}).get("exit_price")
    fsm_put_exit = latest.get("fsm_put_exit_price") or alpha_snapshot.get("put_leg", {}).get("exit_price")
    fsm_realized_pnl = latest.get("fsm_realized_pnl") if latest.get("fsm_realized_pnl") is not None else alpha_snapshot.get("realized_pnl_pts")
    fsm_unrealized_pnl = latest.get("fsm_unrealized_pnl") if latest.get("fsm_unrealized_pnl") is not None else alpha_snapshot.get("unrealized_pnl_pts")
    fsm_total_gainloss = latest.get("fsm_total_gainloss") if latest.get("fsm_total_gainloss") is not None else alpha_snapshot.get("total_gainloss")
    fsm_roi_pct = latest.get("fsm_roi_pct") if latest.get("fsm_roi_pct") is not None else alpha_snapshot.get("roi_pct")

    is_fsm_flat = (fsm_state == "FLAT") or ("CLOSED" in fsm_call_status and "CLOSED" in fsm_put_status)
    is_fsm_partial = not is_fsm_flat and ("CLOSED" in fsm_call_status or "CLOSED" in fsm_put_status)

    peak_profit = snapshot.get("peak_profit") or {}
    peak_pts = float(peak_profit.get("peak_pnl_pts") if peak_profit.get("peak_pnl_pts") is not None else (best_gainloss or 0.0))
    peak_pct = float(peak_profit.get("peak_pnl_pct") if peak_profit.get("peak_pnl_pct") is not None else (((peak_pts / total_cost) * 100.0) if total_cost > 0 else 0.0))
    is_peak_target_hit = peak_pct >= 20.0 and peak_pts > 0

    ai_dispatched = snapshot.get("ai_dispatched_exit_alerts") or []

    # Hierarchy:
    # 1. Terminal Alpha FSM (FLAT): Both legs closed systematically; 100% of profit is locked.
    # 2. Partial Alpha FSM: One leg closed by rule, remaining leg settled at expiry close.
    # 3. Third AI Exit: Dispatched alert signal noted.
    # 4. Passive Hold to Expiry: Default baseline if no systematic exits triggered.
    if is_fsm_flat and fsm_total_gainloss is not None:
        realized_gainloss = round(float(fsm_total_gainloss), 2)
        exit_mode = "ALPHA_FSM"
        exit_label = "Alpha FSM (FLAT - Profit Locked ✓)"
        alpha_secured = True
    elif is_fsm_partial and fsm_total_gainloss is not None:
        realized_gainloss = round(float(fsm_total_gainloss), 2)
        exit_mode = "ALPHA_FSM_PARTIAL"
        exit_label = f"Alpha FSM ({fsm_state} - Partial Exit)"
        alpha_secured = False
    else:
        realized_gainloss = passive_gainloss
        exit_mode = "HOLD_TO_EXPIRY"
        exit_label = "Hold to Expiry (Passive Close)"
        alpha_secured = False

    realized_inr = round((realized_gainloss or 0.0) * NIFTY_LOT_SIZE, 2)
    passive_inr = round((passive_gainloss or 0.0) * NIFTY_LOT_SIZE, 2) if passive_gainloss is not None else 0.0
    alpha_saved_pts = round((realized_gainloss or 0.0) - (passive_gainloss or 0.0), 2) if passive_gainloss is not None else 0.0
    alpha_saved_inr = round(alpha_saved_pts * NIFTY_LOT_SIZE, 2)

    return {
        "start_date": start_date,
        "expiry_date": expiry_date,
        "row_count": len(rows),
        "first_timestamp": first["timestamp_ist"],
        "latest_timestamp": latest["timestamp_ist"],
        "put_strike": int(snapshot.get("put_strike") or latest["put_strike"]),
        "call_strike": int(snapshot.get("call_strike") or latest["call_strike"]),
        "put_buy_price": put_buy,
        "call_buy_price": call_buy,
        "total_cost": total_cost,
        "nifty_start": nifty_start,
        "nifty_latest": nifty_latest,
        "nifty_change": round(nifty_latest - nifty_start, 2),
        "realized_gainloss": realized_gainloss,
        "realized_inr": realized_inr,
        "exit_mode": exit_mode,
        "exit_label": exit_label,
        "alpha_secured": alpha_secured,
        "is_fsm_flat": is_fsm_flat,
        "is_fsm_partial": is_fsm_partial,
        "fsm_state": fsm_state,
        "fsm_call_status": fsm_call_status,
        "fsm_put_status": fsm_put_status,
        "fsm_call_exit_price": fsm_call_exit,
        "fsm_put_exit_price": fsm_put_exit,
        "fsm_realized_pnl": fsm_realized_pnl,
        "fsm_unrealized_pnl": fsm_unrealized_pnl,
        "fsm_roi_pct": fsm_roi_pct,
        "passive_gainloss": passive_gainloss,
        "passive_inr": passive_inr,
        "alpha_saved_pts": alpha_saved_pts,
        "alpha_saved_inr": alpha_saved_inr,
        "peak_profit": peak_profit,
        "peak_pnl_pts": peak_pts,
        "peak_pnl_pct": peak_pct,
        "is_peak_target_hit": is_peak_target_hit,
        "latest_gainloss": realized_gainloss,  # Backward compatible headline
        "passive_hold_gainloss": passive_gainloss,
        "best_gainloss": best_gainloss,
        "worst_gainloss": worst_gainloss,
        "cycle_id": snapshot.get("cycle_id") or latest["cycle_id"],
        "selection_mode": snapshot.get("selection_mode") or "STATIC_RULE",
        "selection_rationale": snapshot.get("selection_rationale") or "",
        "static_call_strike": snapshot.get("static_call_strike"),
        "static_put_strike": snapshot.get("static_put_strike"),
        "timing_shadows": snapshot.get("timing_shadows", []),
        "db_name": db_path.name,
    }


def create_chart(rows: Sequence[Dict], output_path: Path) -> None:
    """Render NIFTY LTP and combined option gain/loss to a PNG."""
    times = [row["timestamp_ist"] for row in rows]
    nifty = [float(row["nifty_ltp"]) for row in rows]
    gains = [
        float(row["gainloss"]) if row["gainloss"] is not None else float("nan")
        for row in rows
    ]

    plt.style.use("seaborn-v0_8-whitegrid")
    fig, (ax_nifty, ax_gain) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [2, 1]}
    )
    fig.patch.set_facecolor("#f5f7fb")
    ax_nifty.plot(times, nifty, color="#2563eb", linewidth=2.4, marker="o", markersize=3)
    ax_nifty.fill_between(times, nifty, min(nifty), color="#bfdbfe", alpha=0.35)
    ax_nifty.set_title("NIFTY 50 — Tuesday to Monday", loc="left", weight="bold")
    ax_nifty.set_ylabel("Index level")

    colors = ["#16a34a" if value >= 0 else "#dc2626" for value in gains]
    ax_gain.bar(times, gains, width=0.025, color=colors, alpha=0.85)
    ax_gain.axhline(0, color="#64748b", linewidth=1)
    ax_gain.set_title("Combined CALL + PUT Gain/Loss", loc="left", weight="bold")
    ax_gain.set_ylabel("Points")
    ax_gain.xaxis.set_major_formatter(mdates.DateFormatter("%a\n%d %b\n%H:%M", tz=IST))
    ax_gain.tick_params(axis="x", labelsize=8)
    fig.tight_layout(pad=2)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def _number(value: Optional[float], decimals: int = 2) -> str:
    return "—" if value is None else f"{float(value):,.{decimals}f}"


def _gain_style(value: Optional[float]) -> Tuple[str, str]:
    if value is None:
        return "#64748b", "—"
    color = "#15803d" if value >= 0 else "#b91c1c"
    return color, f"{value:+,.2f}"


def render_html(rows: Sequence[Dict], summary: Dict, chart_src: str) -> str:
    """Build a Gmail-compatible HTML report using inline CSS."""
    latest_color, latest_gain = _gain_style(summary["latest_gainloss"])
    nifty_color = "#15803d" if summary["nifty_change"] >= 0 else "#b91c1c"
    table_rows = []
    for row in reversed(rows):
        gain_color, gain_text = _gain_style(row["gainloss"])
        table_rows.append(
            f"""
            <tr>
              <td style="padding:9px;border-bottom:1px solid #e2e8f0;white-space:nowrap;">{row['timestamp_ist'].strftime('%a %d %b, %H:%M')}</td>
              <td style="padding:9px;border-bottom:1px solid #e2e8f0;text-align:right;">{_number(row['nifty_ltp'])}</td>
              <td style="padding:9px;border-bottom:1px solid #e2e8f0;text-align:right;">{_number(row['call_ltp'])}</td>
              <td style="padding:9px;border-bottom:1px solid #e2e8f0;text-align:right;">{_number(row['put_ltp'])}</td>
              <td style="padding:9px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:700;color:{gain_color};">{gain_text}</td>
            </tr>"""
        )

    period = (
        f"{summary['start_date'].strftime('%d %b %Y')} – "
        f"{summary['latest_timestamp'].strftime('%d %b %Y')}"
        if summary["start_date"]
        else summary["latest_timestamp"].strftime("%d %b %Y")
    )

    mode_badge = summary.get("selection_mode", "STATIC_RULE")
    badge_bg = "#2563eb" if "AI" in mode_badge else "#64748b"
    rationale_html = ""
    if summary.get("selection_rationale"):
        rationale_html = f"""<div style="margin-top:8px;font-size:12px;color:#1e40af;line-height:1.4;">
          <strong>AI Selection Rationale:</strong> {html.escape(summary['selection_rationale'])}
        </div>"""

    benchmark_info = ""
    if summary.get("static_call_strike") and summary.get("static_put_strike"):
        benchmark_info = f" &nbsp;·&nbsp; <span style='color:#64748b;'>Static Benchmark: PUT {summary['static_put_strike']} / CALL {summary['static_call_strike']}</span>"

    timing_html = ""
    timing_shadows = summary.get("timing_shadows", [])
    if timing_shadows:
        var_map = {s.get("variant"): s for s in timing_shadows}
        v0 = var_map.get("V0_baseline_0931")
        v1 = var_map.get("V1_delayed_1000")
        v2 = var_map.get("V2_staggered_c0931_p1000")
        v3 = var_map.get("V3_staggered_p0931_c1000")
        v4 = var_map.get("V4_trend_stagger")
        items = []
        if v0:
            items.append(f"<li><strong>V0 Baseline (09:31)</strong>: ₹{v0['combined_entry_price']:.1f}</li>")
        if v1 and v0:
            diff = round(v0['combined_entry_price'] - v1['combined_entry_price'], 2)
            pct = round(diff / v0['combined_entry_price'] * 100, 1) if v0['combined_entry_price'] else 0
            items.append(f"<li><strong>V1 Delayed (10:00)</strong>: ₹{v1['combined_entry_price']:.1f} ({diff:+.1f} pts / {pct:+.1f}%)</li>")
        if v2 and v0:
            diff2 = round(v0['combined_entry_price'] - v2['combined_entry_price'], 2)
            items.append(f"<li><strong>V2 Staggered (Call 9:30, Put 10:00)</strong>: ₹{v2['combined_entry_price']:.1f} ({diff2:+.1f} pts)</li>")
        if v3 and v0:
            diff3 = round(v0['combined_entry_price'] - v3['combined_entry_price'], 2)
            items.append(f"<li><strong>V3 Staggered (Put 9:30, Call 10:00)</strong>: ₹{v3['combined_entry_price']:.1f} ({diff3:+.1f} pts)</li>")
        if v4 and v0:
            diff4 = round(v0['combined_entry_price'] - v4['combined_entry_price'], 2)
            items.append(f"<li><strong>V4 Trend Stagger</strong>: ₹{v4['combined_entry_price']:.1f} ({diff4:+.1f} pts)</li>")
        if items:
            timing_html = f"""<div style="margin-top:10px;padding-top:8px;border-top:1px dashed #cbd5e1;font-size:12px;color:#334155;">
              <strong>⏱️ Entry Timing Benchmark:</strong>
              <ul style="margin:4px 0 0 16px;padding:0;">{''.join(items)}</ul>
            </div>"""

    if summary.get("exit_mode") == "ALPHA_FSM":
        pnl_card_title = "STRATEGY LOCKED P&L"
    elif summary.get("exit_mode") == "ALPHA_FSM_PARTIAL":
        pnl_card_title = "STRATEGY NET P&L (PARTIAL EXIT)"
    else:
        pnl_card_title = "STRATEGY FINAL P&L (CYCLE CLOSE)"

    return f"""<!doctype html>
<html><body style="margin:0;background:#eef2f7;font-family:Arial,sans-serif;color:#0f172a;">
<div style="max-width:900px;margin:0 auto;padding:24px 12px;">
  <div style="background:linear-gradient(135deg,#0f172a,#1d4ed8);color:white;padding:28px;border-radius:16px 16px 0 0;">
    <div style="font-size:12px;letter-spacing:1.5px;text-transform:uppercase;color:#bfdbfe;">NIFTY50 Weekly Option Strategy</div>
    <h1 style="margin:8px 0 4px;font-size:28px;">Tuesday–Monday Report</h1>
    <div style="color:#dbeafe;">{period} · Expiry {summary['expiry_date'].strftime('%d %b %Y')}</div>
  </div>
  <div style="background:white;padding:22px;border-radius:0 0 16px 16px;box-shadow:0 8px 30px rgba(15,23,42,.08);">
    <table role="presentation" style="width:100%;border-collapse:separate;border-spacing:8px;"><tr>
      <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:15px;">
        <div style="font-size:12px;color:#64748b;">{pnl_card_title}</div>
        <div style="font-size:25px;font-weight:700;color:{latest_color};">{latest_gain} pts</div>
        <div style="font-size:13px;font-weight:600;color:{latest_color};">₹{summary.get('realized_inr', 0):+,.0f}</div>
        <div style="font-size:11px;color:#2563eb;font-weight:700;margin-top:2px;">{html.escape(str(summary.get('exit_label', '')))}</div>
      </td>
      <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:15px;">
        <div style="font-size:12px;color:#64748b;">NIFTY LATEST</div>
        <div style="font-size:25px;font-weight:700;">{_number(summary['nifty_latest'])}</div>
        <div style="color:{nifty_color};">{summary['nifty_change']:+,.2f} pts</div>
      </td>
      <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:15px;">
        <div style="font-size:12px;color:#64748b;">WATERMARK RANGE</div>
        <div style="font-size:17px;font-weight:700;color:#15803d;">Peak {_number(summary['best_gainloss'])}</div>
        <div style="font-size:14px;color:#b91c1c;">Drawdown {_number(summary['worst_gainloss'])}</div>
        {f'<div style="font-size:11px;color:#4f46e5;font-weight:600;margin-top:4px;">🎯 20% Target Alert: +{summary["peak_pnl_pts"]:.2f} pts (+{summary["peak_pnl_pct"]:.1f}%)</div>' if summary.get("is_peak_target_hit") else ''}
      </td>
    </tr></table>

    {f"""<div style="margin:10px 8px;padding:12px;background:#f8fafc;border:1px solid #cbd5e1;border-radius:8px;font-size:12px;color:#334155;">
      <strong>📉 Counterfactual: Passive Unhedged Hold (No Exit):</strong> {_number(summary.get('passive_gainloss'))} pts (₹{summary.get('passive_inr', 0):+,.0f}) &nbsp;·&nbsp;
      <strong style="color:#15803d;">🛡️ {'Alpha Secured by FSM' if summary.get('is_fsm_flat') else 'Net FSM Advantage vs Passive Hold'}:</strong> {summary.get('alpha_saved_pts', 0):+,.2f} pts (+₹{summary.get('alpha_saved_inr', 0):+,.0f})
    </div>""" if summary.get('exit_mode') != 'HOLD_TO_EXPIRY' and summary.get('passive_gainloss') is not None else ""}

    <div style="margin:16px 8px;padding:16px;background:#eff6ff;border-left:4px solid #2563eb;border-radius:8px;">
      <div style="margin-bottom:6px;">
        <span style="background:{badge_bg};color:white;padding:3px 8px;border-radius:4px;font-size:11px;font-weight:700;letter-spacing:0.5px;">{html.escape(mode_badge)}</span>
      </div>
      <strong>Cycle setup:</strong> PUT {summary['put_strike']} @ {_number(summary['put_buy_price'])} &nbsp;·&nbsp;
      CALL {summary['call_strike']} @ {_number(summary['call_buy_price'])}{benchmark_info}<br>
      <span style="font-size:12px;color:#475569;">Cycle {html.escape(str(summary['cycle_id']))} · {summary['row_count']} observations · Last update {summary['latest_timestamp'].strftime('%d %b %Y %H:%M IST')}</span>
      {f'''<div style="margin-top:6px;font-size:12px;color:#1e3a8a;">
        <strong>FSM Leg Execution:</strong> CALL {summary['call_strike']} @ {f"₹{summary['fsm_call_exit_price']:.2f}" if summary.get('fsm_call_exit_price') else "Open"} ({html.escape(_format_leg_status(summary.get('fsm_call_status')))}) &nbsp;·&nbsp;
        PUT {summary['put_strike']} @ {f"₹{summary['fsm_put_exit_price']:.2f}" if summary.get('fsm_put_exit_price') else "Open"} ({html.escape(_format_leg_status(summary.get('fsm_put_status')))})
      </div>''' if summary.get('fsm_call_status') or summary.get('fsm_put_status') else ''}
      {rationale_html}
      {timing_html}
    </div>

    <img src="{html.escape(chart_src)}" alt="NIFTY50 and gain/loss chart" style="display:block;width:100%;max-width:840px;margin:22px auto;border:1px solid #e2e8f0;border-radius:12px;">

    <h2 style="font-size:19px;margin:26px 8px 10px;">Hourly gain/loss table</h2>
    <div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead><tr style="background:#0f172a;color:white;">
        <th style="padding:10px;text-align:left;">Time (IST)</th><th style="padding:10px;text-align:right;">NIFTY</th>
        <th style="padding:10px;text-align:right;">CALL {summary['call_strike']}</th><th style="padding:10px;text-align:right;">PUT {summary['put_strike']}</th>
        <th style="padding:10px;text-align:right;">Gain/Loss</th>
      </tr></thead><tbody>{''.join(table_rows)}</tbody>
    </table></div>
    <div style="margin-top:22px;padding-top:14px;border-top:1px solid #e2e8f0;font-size:11px;color:#64748b;">
      Gain/loss is expressed in combined option premium points and is not multiplied by lot size. Source: {html.escape(summary['db_name'])}.
    </div>
  </div>
</div></body></html>"""


def render_plain_text(summary: Dict) -> str:
    latest = _gain_style(summary["latest_gainloss"])[1]
    return (
        "NIFTY50 Weekly Option Strategy Report\n"
        f"Expiry: {summary['expiry_date'].isoformat()}\n"
        f"PUT {summary['put_strike']} buy {summary['put_buy_price']:.2f}\n"
        f"CALL {summary['call_strike']} buy {summary['call_buy_price']:.2f}\n"
        f"Latest NIFTY: {summary['nifty_latest']:.2f}\n"
        f"Latest combined gain/loss: {latest} points\n"
        f"Rows: {summary['row_count']}\n"
    )


def send_email(subject: str, plain_body: str, html_body: str, chart_path: Path) -> None:
    """
    Deprecated: Email notifications have been retired in favor of DISCORD_WATCHDOG.
    Logs notification and returns.
    """
    logger.info("Email notifications are disabled. Weekly report cards route to DISCORD_URL.")


def resolve_discord_url(track: Optional[str] = None) -> str:
    """Resolve track-specific Discord webhook URL from os.environ, config, or candidate .env files.

    Track routing priority:
      No track (None / watchdog):     DISCORD_URL
      3-Day Track (thursday / 3-day): DISCORD_EXIT_3DAY -> DISCORD_URL_3DAY -> candidate .env -> DISCORD_URL
      5-Day Track (weekly / 5-day):   DISCORD_EXIT_5DAY -> DISCORD_URL_5DAY -> candidate .env -> DISCORD_URL
    Candidate .env files are scanned for the track-specific keys only if not defined in os.environ,
    and before falling back to DISCORD_URL.
    """
    home = os.path.expanduser("~")
    base_dir = Path(__file__).resolve().parents[1]
    candidate_paths = [
        base_dir / ".env",
        Path(home) / ".env",
        Path(home) / "nifty50_strategies" / ".env",
        Path(home) / "sentinel-hermes" / ".env",
        Path(home) / "market_signal_agent" / ".env",
        Path(home) / "news-analyzer-for-market-sentiment" / ".env",
        Path(home) / "mcx_signal_features" / ".env",
        Path(home) / "nifty_signal_features" / ".env",
    ]

    t = str(track or "").strip().lower()
    if not t:
        # Default / watchdog channel: resolve DISCORD_URL
        if "DISCORD_URL" in os.environ:
            return os.environ["DISCORD_URL"].strip()
        for p in candidate_paths:
            if p.exists():
                try:
                    with p.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("DISCORD_URL="):
                                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                                if val:
                                    return val
                except Exception:
                    pass
        return os.getenv("DISCORD_URL", "").strip() or DISCORD_URL

    is_3day = "thu" in t or "3" in t
    track_keys = ["DISCORD_EXIT_3DAY", "DISCORD_URL_3DAY"] if is_3day else ["DISCORD_EXIT_5DAY", "DISCORD_URL_5DAY"]

    # 1. Check if track keys are present in os.environ (highest precedence)
    for k in track_keys:
        if k in os.environ:
            val = os.environ[k].strip()
            if val:
                return val

    # 2. Check candidate .env files only if track keys are NOT defined in os.environ
    track_keys_in_env = any(k in os.environ for k in track_keys)
    if not track_keys_in_env:
        for k in track_keys:
            for p in candidate_paths:
                if p.exists():
                    try:
                        with p.open("r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line.startswith(f"{k}="):
                                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                                    if val:
                                        return val
                    except Exception:
                        pass

    # 3. Fall back to general DISCORD_URL in os.environ
    if "DISCORD_URL" in os.environ:
        return os.environ["DISCORD_URL"].strip()

    # 4. Fall back to DISCORD_URL in candidate .env files
    for p in candidate_paths:
        if p.exists():
            try:
                with p.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("DISCORD_URL="):
                            val = line.split("=", 1)[1].strip().strip('"').strip("'")
                            if val:
                                return val
            except Exception:
                pass

    return os.getenv("DISCORD_URL", "").strip() or DISCORD_URL


# Backwards compatibility alias
resolve_discord_watchdog_url = resolve_discord_url


def send_discord_report(
    content: str, embed: Optional[Dict[str, Any]] = None, track: str = "weekly"
) -> bool:
    """
    Send a report notification to the track-specific Discord channel (fallback DISCORD_URL).
    Uses standard library urllib (zero external pip dependencies) and is wrapped
    in try/except so webhook issues never crash the script.
    """
    webhook_url = resolve_discord_url(track=track)
    if not webhook_url:
        logger.warning(
            "⚠️ [DISCORD_URL] Webhook URL not set for track '%s' in environment or .env; alert dropped: %s",
            track,
            content[:120],
        )
        return False

    safe_content = content[:1900] + ("..." if len(content) > 1900 else "")
    payload: Dict[str, Any] = {"content": safe_content}

    if embed:
        if "fields" in embed:
            for field in embed["fields"]:
                if "value" in field and len(str(field["value"])) > 1020:
                    field["value"] = str(field["value"])[:1020] + "..."
        payload["embeds"] = [embed]

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Nifty50WeeklyReport/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            success = 200 <= resp.status < 300
            if success:
                logger.info("✓ Discord weekly report dispatched successfully")
            return success
    except Exception as exc:
        logger.warning("Discord notification delivery failed: %s", exc)
        return False


def send_discord_watchdog(
    content: str, embed: Optional[Dict[str, Any]] = None, track: str = "weekly"
) -> bool:
    """Send an operational notification to Discord (alias for backward compatibility)."""
    return send_discord_report(content, embed, track=track)


def send_weekly_report_discord(summary: Dict[str, Any], track: str = "weekly") -> bool:
    """Send NIFTY50 weekly strangle close report card to track-specific Discord channel."""
    cycle_id = summary.get("cycle_id", "UNKNOWN")
    realized_gain = summary.get("realized_gainloss", summary.get("latest_gainloss"))
    passive_gain = summary.get("passive_gainloss")
    best_gain = summary.get("best_gainloss")
    worst_gain = summary.get("worst_gainloss")
    exit_label = summary.get("exit_label", "Exit Engine")
    exit_mode = summary.get("exit_mode", "HOLD_TO_EXPIRY")
    call_strike = summary.get("call_strike", "N/A")
    put_strike = summary.get("put_strike", "N/A")
    call_buy = summary.get("call_buy_price", 0.0)
    put_buy = summary.get("put_buy_price", 0.0)
    nifty_start = summary.get("nifty_start", 0.0)
    nifty_latest = summary.get("nifty_latest", 0.0)
    nifty_change = summary.get("nifty_change", 0.0)
    start_date = summary.get("start_date")
    expiry_date = summary.get("expiry_date")
    selection_mode = summary.get("selection_mode", "STATIC_RULE")

    track_resolved = summary.get("track") or track or "weekly"
    track_label = "3-Day Thursday Strategy" if str(track_resolved).lower() == "thursday" else "5-Day Tuesday Strategy"
    gain_str = f"{realized_gain:+.2f} pts" if realized_gain is not None else "N/A"
    inr_gain = f"₹{realized_gain * NIFTY_LOT_SIZE:+,.0f}" if realized_gain is not None else "N/A"
    is_profitable = (realized_gain or 0.0) >= 0
    color = 0x16a34a if is_profitable else 0xdc2626
    status_icon = "🟢" if is_profitable else "🔴"
    status_text = "PROFITABLE" if is_profitable else "LOSS"

    period_str = (
        f"{start_date.strftime('%d %b %Y') if start_date else 'N/A'} → "
        f"{expiry_date.strftime('%d %b %Y') if expiry_date else 'N/A'}"
    )

    # Field 1: Primary Headline P&L Card (Distinguishing FSM-locked from partial or expiry hold)
    if exit_mode == "ALPHA_FSM":
        headline_pnl_label = "Locked P&L"
        pnl_field_name = "📊 Strategy P&L (Alpha FSM Locked ✓)"
        pnl_field_val = (
            f"FSM Locked Gain: **{gain_str}**\n"
            f"Estimated INR P&L: **{inr_gain}** (1 lot / {NIFTY_LOT_SIZE} qty)\n"
            f"Execution State: **FLAT (Capital Rescued & Harvested)**"
        )
    elif exit_mode == "ALPHA_FSM_PARTIAL":
        headline_pnl_label = "Net Cycle P&L"
        fsm_realized_str = f"{summary.get('fsm_realized_pnl', 0.0):+.2f} pts"
        fsm_unreal_str = f"{summary.get('fsm_unrealized_pnl', 0.0):+.2f} pts"
        pnl_field_name = "📊 Strategy P&L (Partial FSM Exit)"
        pnl_field_val = (
            f"Net Cycle Outcome: **{gain_str}** ({inr_gain})\n"
            f"Closed Leg Realized: **{fsm_realized_str}**\n"
            f"Remaining Leg Mark (Cycle Close): **{fsm_unreal_str}**"
        )
    else:
        headline_pnl_label = "Final P&L"
        pnl_field_name = "📊 Strategy Final P&L (Hold to Expiry)"
        pnl_field_val = (
            f"Combined Gain/Loss: **{gain_str}**\n"
            f"Estimated INR P&L: **{inr_gain}** (1 lot / {NIFTY_LOT_SIZE} qty)\n"
            f"Execution: **Passive Expiry Settlement**"
        )

    headline = (
        f"📈 **[NIFTY50 {track_label} Close]** Cycle `{cycle_id}` • "
        f"{headline_pnl_label}: **{gain_str} ({inr_gain})** {status_icon}"
    )

    target_alert_note = ""
    if summary.get("is_peak_target_hit"):
        target_alert_note = f"\n🎯 20% Target Alert: `+{summary.get('peak_pnl_pts', 0):.2f} pts` (+{summary.get('peak_pnl_pct', 0):.1f}%)"

    fields = [
        {
            "name": pnl_field_name,
            "value": pnl_field_val,
            "inline": True,
        },
        {
            "name": "🌊 Watermark Range",
            "value": (
                f"Peak Profit: `+{best_gain:+.2f} pts` (₹{best_gain * NIFTY_LOT_SIZE:+,.0f})\n"
                f"Max Drawdown: `{worst_gain:+.2f} pts`{target_alert_note}"
                if best_gain is not None and worst_gain is not None
                else "N/A"
            ),
            "inline": True,
        },
    ]

    fsm_c_status = summary.get("fsm_call_status")
    fsm_p_status = summary.get("fsm_put_status")
    fsm_c_exit = summary.get("fsm_call_exit_price")
    fsm_p_exit = summary.get("fsm_put_exit_price")
    if (fsm_c_status and fsm_c_status != "ACTIVE") or (fsm_p_status and fsm_p_status != "ACTIVE") or str(exit_mode).startswith("ALPHA_FSM"):
        c_p = f"₹{fsm_c_exit:.2f}" if fsm_c_exit else "Open"
        p_p = f"₹{fsm_p_exit:.2f}" if fsm_p_exit else "Open"
        fields.append({
            "name": "⚡ FSM Leg Execution Breakdown",
            "value": (
                f"**CALL {call_strike} CE**: {c_p} ({_format_leg_status(fsm_c_status)})\n"
                f"**PUT {put_strike} PE**: {p_p} ({_format_leg_status(fsm_p_status)})"
            ),
            "inline": False,
        })

    if passive_gain is not None and summary.get("exit_mode") != "HOLD_TO_EXPIRY":
        pass_inr = summary.get("passive_inr", passive_gain * NIFTY_LOT_SIZE)
        alpha_pts = summary.get("alpha_saved_pts", 0.0)
        alpha_inr = summary.get("alpha_saved_inr", 0.0)
        alpha_sign = "+" if alpha_pts >= 0 else ""
        alpha_label = "Alpha Secured by FSM" if summary.get("is_fsm_flat") else "Net FSM Advantage vs Passive Hold"
        fields.append({
            "name": "📉 Counterfactual: Passive Hold Decay",
            "value": (
                f"Monday Close (No Exit): **{passive_gain:+.2f} pts** (₹{pass_inr:+,.0f})\n"
                f"{alpha_label}: **{alpha_sign}{alpha_pts:+.2f} pts** (₹{alpha_inr:+,.0f}) 🛡️"
            ),
            "inline": False,
        })

    fields.extend([
        {
            "name": "🎯 Strangle Positioning",
            "value": (
                f"**CALL {call_strike} CE** (Entry: ₹{call_buy:.2f})\n"
                f"**PUT {put_strike} PE** (Entry: ₹{put_buy:.2f})"
            ),
            "inline": False,
        },
        {
            "name": "📍 Underlying NIFTY Index",
            "value": (
                f"Open: **{nifty_start:.1f}** → Close: **{nifty_latest:.1f}** "
                f"({nifty_change:+.1f} pts)"
            ),
            "inline": True,
        },
        {
            "name": "🗓️ Execution Window",
            "value": f"{period_str}\nRecorded Points: **{summary.get('row_count', 0)}**",
            "inline": True,
        },
    ])

    if summary.get("selection_rationale"):
        fields.append({
            "name": f"🧠 Strategy Mode: `{selection_mode}`",
            "value": str(summary["selection_rationale"])[:1000],
            "inline": False,
        })

    timing_shadows = summary.get("timing_shadows", [])
    if timing_shadows:
        var_map = {s.get("variant"): s for s in timing_shadows}
        v0 = var_map.get("V0_baseline_0931")
        v1 = var_map.get("V1_delayed_1000")
        v2 = var_map.get("V2_staggered_c0931_p1000")
        v3 = var_map.get("V3_staggered_p0931_c1000")
        v4 = var_map.get("V4_trend_stagger")

        lines = []
        if v0:
            lines.append(f"• **V0 Baseline (09:31)**: ₹{v0['combined_entry_price']:.1f}")
        if v1 and v0:
            diff = round(v0['combined_entry_price'] - v1['combined_entry_price'], 2)
            pct = round(diff / v0['combined_entry_price'] * 100, 1) if v0['combined_entry_price'] else 0
            lines.append(f"• **V1 Delayed (10:00)**: ₹{v1['combined_entry_price']:.1f} ({diff:+.1f} pts / {pct:+.1f}%)")
        if v2 and v0:
            diff2 = round(v0['combined_entry_price'] - v2['combined_entry_price'], 2)
            lines.append(f"• **V2 Call 9:30 / Put 10:00**: ₹{v2['combined_entry_price']:.1f} ({diff2:+.1f} pts)")
        if v3 and v0:
            diff3 = round(v0['combined_entry_price'] - v3['combined_entry_price'], 2)
            lines.append(f"• **V3 Put 9:30 / Call 10:00**: ₹{v3['combined_entry_price']:.1f} ({diff3:+.1f} pts)")
        if v4 and v0:
            diff4 = round(v0['combined_entry_price'] - v4['combined_entry_price'], 2)
            lines.append(f"• **V4 Trend Stagger**: ₹{v4['combined_entry_price']:.1f} ({diff4:+.1f} pts)")

        if lines:
            fields.append({
                "name": "⏱️ Entry Timing Benchmark (Counterfactuals)",
                "value": "\n".join(lines),
                "inline": False,
            })

    embed = {
        "title": f"📈 NIFTY50 {track_label} Lifecycle Report [{cycle_id}]",
        "color": color,
        "description": f"{track_label} cycle performance closed. Status: **{status_text}** ({exit_label})",
        "fields": fields,
        "footer": {
            "text": f"NIFTY50 {track_label} Automated Strategy • Lifecycle Report"
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return send_discord_watchdog(headline, embed, track=track)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send NIFTY50 strategy report")
    parser.add_argument("--dry-run", action="store_true", help="Generate preview only")
    parser.add_argument("--db", help="Explicit SQLite database path")
    parser.add_argument(
        "--track",
        choices=["weekly", "thursday"],
        default="weekly",
        help="Strategy track to report on (default: weekly)",
    )
    parser.add_argument(
        "--report-date",
        "--date",
        dest="report_date",
        help="Date used to discover DB (YYYY-MM-DD or YYYYMMDD)",
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("WEEKLY_REPORT_DIR", str(Path(SQLITE_DIR) / "reports")),
        help="Directory for archived HTML and chart output",
    )
    args = parser.parse_args()

    try:
        if args.report_date:
            clean_date = args.report_date.replace("-", "")
            report_date = datetime.strptime(clean_date, "%Y%m%d").date()
        else:
            report_date = datetime.now(IST).date()
        db_path = find_weekly_db(report_date, args.db, track=args.track)
        logger.info("Using strategy database: %s", db_path)
        try:
            rows, snapshot = load_report_data(db_path, track=args.track)
        except ValueError as val_err:
            logger.warning("Strategy reporting skipped cleanly: %s", val_err)
            return 0
        summary = calculate_summary(rows, snapshot, db_path)

        output_dir = Path(args.output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = summary["start_date"].strftime("%Y%m%d") if summary["start_date"] else report_date.strftime("%Y%m%d")
        prefix = "nifty50_thursday" if args.track.lower() == "thursday" else "nifty50_weekly"
        chart_path = output_dir / f"{prefix}_{stamp}.png"
        preview_path = output_dir / f"{prefix}_{stamp}.html"
        create_chart(rows, chart_path)
        preview_path.write_text(
            render_html(rows, summary, chart_path.name), encoding="utf-8"
        )
        logger.info("Report preview written: %s", preview_path)

        if args.dry_run:
            logger.info("Dry run complete; report card not sent")
            return 0

        # Dispatch to Discord channel
        try:
            logger.info("Dispatching %s report card to Discord...", args.track)
            success = send_weekly_report_discord(summary, track=args.track)
            if success:
                logger.info("✓ Discord report dispatched successfully")
            else:
                logger.warning("⚠ Discord delivery skipped or webhook not set")
        except Exception as discord_err:
            logger.warning("Discord delivery failed: %s", discord_err)

        return 0
    except Exception as exc:
        logger.error("Report failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
