"""Generate and email the NIFTY50 Tuesday-Monday weekly strategy report.

Examples:
    python scripts/send_weekly_report.py --dry-run
    python scripts/send_weekly_report.py
    python scripts/send_weekly_report.py --db /path/to/weekly.db --dry-run
"""

import argparse
import html
import logging
import os
import re
import smtplib
import sqlite3
import sys
from datetime import date, datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

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

from config import SQLITE_DIR, STRATEGY_NAME


logger = logging.getLogger("nifty50_weekly_report")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.UTC
DB_NAME_PATTERN = re.compile(
    r"nifty50_weekly_data_(?P<start>\d{8})_(?P<expiry>\d{8})\.db$"
)


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


def find_weekly_db(report_date: date, explicit_path: Optional[str] = None) -> Path:
    """Find the weekly DB whose Tuesday-expiry window covers report_date."""
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Weekly database not found: {path}")
        return path

    candidates: List[Tuple[date, date, Path]] = []
    for path in Path(SQLITE_DIR).glob("nifty50_weekly_data_*_*.db"):
        match = DB_NAME_PATTERN.match(path.name)
        if not match:
            continue
        start = _parse_yyyymmdd(match.group("start"))
        expiry = _parse_yyyymmdd(match.group("expiry"))
        if start <= report_date <= expiry:
            candidates.append((start, expiry, path))

    if not candidates:
        raise FileNotFoundError(
            f"No weekly database in {SQLITE_DIR} covers {report_date.isoformat()}"
        )
    return max(candidates, key=lambda item: item[0])[2]


def load_report_data(db_path: Path) -> Tuple[List[Dict], Dict]:
    """Load hourly rows and the cycle snapshot from a weekly database."""
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
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT
                    id, collection_timestamp, expiry_date,
                    nifty_open, nifty_ltp, nifty_previous_close,
                    put_strike, put_ltp, put_buy_price,
                    call_strike, call_ltp, call_buy_price,
                    {gainloss_sql} AS gainloss, cycle_id
                FROM strategy_hourly_data
                WHERE strategy_name = ?
                ORDER BY collection_timestamp ASC
                """,
                (STRATEGY_NAME,),
            )
        ]
        snapshot_row = conn.execute(
            """
            SELECT * FROM strategy_buy_snapshots
            WHERE strategy_name = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (STRATEGY_NAME,),
        ).fetchone()
        snapshot = dict(snapshot_row) if snapshot_row else {}
    finally:
        conn.close()

    if not rows:
        raise ValueError(f"No strategy rows found in {db_path}")
    for row in rows:
        row["timestamp_ist"] = _parse_timestamp(row["collection_timestamp"])
    return rows, snapshot


def calculate_summary(rows: Sequence[Dict], snapshot: Dict, db_path: Path) -> Dict:
    """Calculate headline metrics for the HTML report."""
    first = rows[0]
    latest = rows[-1]
    gains = [float(row["gainloss"]) for row in rows if row["gainloss"] is not None]
    match = DB_NAME_PATTERN.match(db_path.name)
    start_str = snapshot.get("week_start_date") or (
        match.group("start") if match else ""
    )
    expiry_str = snapshot.get("expiry_date") or latest["expiry_date"]
    start_date = _parse_yyyymmdd(start_str) if start_str else None
    expiry_date = _parse_yyyymmdd(str(expiry_str))
    nifty_start = float(first["nifty_ltp"])
    nifty_latest = float(latest["nifty_ltp"])
    return {
        "start_date": start_date,
        "expiry_date": expiry_date,
        "row_count": len(rows),
        "first_timestamp": first["timestamp_ist"],
        "latest_timestamp": latest["timestamp_ist"],
        "put_strike": int(snapshot.get("put_strike") or latest["put_strike"]),
        "call_strike": int(snapshot.get("call_strike") or latest["call_strike"]),
        "put_buy_price": float(
            snapshot.get("put_buy_price") or latest["put_buy_price"]
        ),
        "call_buy_price": float(
            snapshot.get("call_buy_price") or latest["call_buy_price"]
        ),
        "nifty_start": nifty_start,
        "nifty_latest": nifty_latest,
        "nifty_change": round(nifty_latest - nifty_start, 2),
        "latest_gainloss": gains[-1] if gains else None,
        "best_gainloss": max(gains) if gains else None,
        "worst_gainloss": min(gains) if gains else None,
        "cycle_id": snapshot.get("cycle_id") or latest["cycle_id"],
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
      <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:15px;"><div style="font-size:12px;color:#64748b;">LATEST GAIN/LOSS</div><div style="font-size:25px;font-weight:700;color:{latest_color};">{latest_gain} pts</div></td>
      <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:15px;"><div style="font-size:12px;color:#64748b;">NIFTY LATEST</div><div style="font-size:25px;font-weight:700;">{_number(summary['nifty_latest'])}</div><div style="color:{nifty_color};">{summary['nifty_change']:+,.2f} pts</div></td>
      <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:15px;"><div style="font-size:12px;color:#64748b;">RANGE</div><div style="font-size:17px;font-weight:700;color:#15803d;">Best {_number(summary['best_gainloss'])}</div><div style="font-size:14px;color:#b91c1c;">Worst {_number(summary['worst_gainloss'])}</div></td>
    </tr></table>

    <div style="margin:16px 8px;padding:16px;background:#eff6ff;border-left:4px solid #2563eb;border-radius:8px;">
      <strong>Cycle setup:</strong> PUT {summary['put_strike']} @ {_number(summary['put_buy_price'])} &nbsp;·&nbsp;
      CALL {summary['call_strike']} @ {_number(summary['call_buy_price'])}<br>
      <span style="font-size:12px;color:#475569;">Cycle {html.escape(str(summary['cycle_id']))} · {summary['row_count']} observations · Last update {summary['latest_timestamp'].strftime('%d %b %Y %H:%M IST')}</span>
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
    """Send the weekly report via Gmail-compatible SMTP SSL."""
    sender = os.getenv("EMAIL_SENDER", "").strip()
    password = os.getenv("EMAIL_APP_PASSWORD", "").strip()
    recipients = [
        item.strip() for item in os.getenv("EMAIL_RECIPIENT", "").split(",")
        if item.strip()
    ]
    server_name = os.getenv("EMAIL_SMTP_SERVER", "smtp.gmail.com")
    server_port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
    if not sender or not password or not recipients:
        raise ValueError(
            "Set EMAIL_SENDER, EMAIL_APP_PASSWORD, and EMAIL_RECIPIENT in .env"
        )

    message = MIMEMultipart("related")
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    alternatives = MIMEMultipart("alternative")
    alternatives.attach(MIMEText(plain_body, "plain", "utf-8"))
    alternatives.attach(MIMEText(html_body, "html", "utf-8"))
    message.attach(alternatives)
    with chart_path.open("rb") as chart_file:
        chart = MIMEImage(chart_file.read(), _subtype="png")
    chart.add_header("Content-ID", "<nifty-chart>")
    chart.add_header("Content-Disposition", "inline", filename=chart_path.name)
    message.attach(chart)

    with smtplib.SMTP_SSL(server_name, server_port, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.sendmail(sender, recipients, message.as_string())
    logger.info("Weekly report sent to %s", ", ".join(recipients))


def main() -> int:
    parser = argparse.ArgumentParser(description="Send NIFTY50 weekly report")
    parser.add_argument("--dry-run", action="store_true", help="Generate preview only")
    parser.add_argument("--db", help="Explicit weekly SQLite database path")
    parser.add_argument("--report-date", help="Date used to discover DB (YYYY-MM-DD)")
    parser.add_argument(
        "--output-dir",
        default=os.getenv("WEEKLY_REPORT_DIR", str(Path(SQLITE_DIR) / "reports")),
        help="Directory for archived HTML and chart output",
    )
    args = parser.parse_args()

    try:
        report_date = (
            date.fromisoformat(args.report_date)
            if args.report_date
            else datetime.now(IST).date()
        )
        db_path = find_weekly_db(report_date, args.db)
        logger.info("Using weekly database: %s", db_path)
        rows, snapshot = load_report_data(db_path)
        summary = calculate_summary(rows, snapshot, db_path)

        output_dir = Path(args.output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = summary["start_date"].strftime("%Y%m%d") if summary["start_date"] else report_date.strftime("%Y%m%d")
        chart_path = output_dir / f"nifty50_weekly_{stamp}.png"
        preview_path = output_dir / f"nifty50_weekly_{stamp}.html"
        create_chart(rows, chart_path)
        preview_path.write_text(
            render_html(rows, summary, chart_path.name), encoding="utf-8"
        )
        logger.info("Report preview written: %s", preview_path)

        if args.dry_run:
            logger.info("Dry run complete; email not sent")
            return 0

        latest_gain = _gain_style(summary["latest_gainloss"])[1]
        subject = (
            f"NIFTY50 Weekly Report | {summary['expiry_date'].strftime('%d %b %Y')} "
            f"| G/L {latest_gain} pts"
        )
        send_email(
            subject,
            render_plain_text(summary),
            render_html(rows, summary, "cid:nifty-chart"),
            chart_path,
        )
        return 0
    except Exception as exc:
        logger.error("Weekly report failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
