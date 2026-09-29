#!/usr/bin/env python3
r"""
run_thursday_close.py - Post-Market 3-Day Strategy Closing Orchestrator.

Orchestrates the end-of-cycle closing sequence for the parallel 3-day Thursday track:
1. Validates whether today is the true Strategy Closing Day:
   - Normal week: Monday post-market (15:38 IST).
   - Monday Holiday: Tuesday post-market / morning (09:08 IST).
   - Skips cleanly when not the designated closing day.
2. Closes the cycle upfront in current_thursday_buy.json.
3. Dispatches performance reporting (HTML chart preview & Discord card via send_weekly_report.py --track thursday).
4. Synchronizes cycle dossier to MongoDB Atlas (derivative_strategies) with strategy_name='nifty50_thursday_3day_collector'.
5. Explicitly skips LLM skill generation (sentinel-hermes/skill_generator.py).

Crontab entries:
    # Monday post-market close (10:08 UTC = 15:38 IST)
    8 10 * * 1 cd ~/nifty50_strategies && .venv/bin/python scripts/run_thursday_close.py >> /home/ubuntu/logs/thursday_close_$(date +\%F).log 2>&1

    # Tuesday deferred close (for Monday exchange holidays; 03:38 UTC = 09:08 IST)
    38 3 * * 2 cd ~/nifty50_strategies && .venv/bin/python scripts/run_thursday_close.py >> /home/ubuntu/logs/thursday_close_$(date +\%F).log 2>&1
"""

import os
import sys
import argparse
import logging
import subprocess
from datetime import date, datetime
from typing import Optional, List
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import THURSDAY_SNAPSHOT_FILE, SQLITE_DIR, STRATEGY_NAME_THURSDAY
from common.calendar_utils import (
    check_nse_holiday,
    is_strategy_closing_day,
    get_strategy_cycle_role,
)
from common.storage import (
    load_active_snapshot,
    mark_active_cycle_closed,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_thursday_close")


def run_command(cmd: List[str], cwd: str, dry_run: bool = False) -> bool:
    """Executes a command subprocess with real-time logging."""
    cmd_str = " ".join(cmd)
    logger.info(f"Running: {cmd_str} (in {cwd})")

    if dry_run:
        logger.info(f"[DRY-RUN] Would execute: {cmd_str}")
        return True

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.stdout:
            for line in proc.stdout.strip().splitlines():
                logger.info(f"  [stdout] {line}")
        if proc.stderr:
            for line in proc.stderr.strip().splitlines():
                logger.warning(f"  [stderr] {line}")

        if proc.returncode != 0:
            logger.error(f"Command failed with exit code {proc.returncode}: {cmd_str}")
            return False

        logger.info(f"Command completed successfully: {cmd_str}")
        return True

    except Exception as e:
        logger.error(f"Failed to execute command '{cmd_str}': {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run post-market closing sequence for 3-day Thursday strategy."
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Date to evaluate in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--morning-holiday-check",
        action="store_true",
        help="Early morning check on Monday: if today is an NSE holiday, close the strategy immediately",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass holiday calendar and force execution of closing sequence",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution steps without modifying state or dispatching alerts",
    )
    args = parser.parse_args()

    # Determine target date
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            logger.error(f"Invalid date format '{args.date}'. Expected YYYY-MM-DD.")
            return 1
    else:
        target_date = date.today()

    day_name = target_date.strftime("%A")
    role = get_strategy_cycle_role(target_date)

    # -----------------------------------------------------------------
    # Morning Holiday Mode Check
    # -----------------------------------------------------------------
    if args.morning_holiday_check:
        if target_date.weekday() == 0:
            if not check_nse_holiday(target_date):
                logger.info(f"Monday ({target_date}) is a normal trading day. Thursday strategy will close post-market at 15:38 IST.")
                return 0
            logger.info(f"🚨 Monday ({target_date}) is an NSE holiday! Executing early morning close for Thursday track...")
            args.force = True
        elif target_date.weekday() == 1:
            prev_monday = target_date - timedelta(days=1)
            if check_nse_holiday(prev_monday):
                logger.info(f"🚨 Monday ({prev_monday}) was an NSE holiday! Executing deferred Tuesday close for Thursday track...")
                args.force = True
            else:
                logger.info(f"Monday ({prev_monday}) was a normal trading day. Skipping deferred Tuesday close.")
                return 0
        else:
            logger.info(f"Morning holiday check is only applicable on Mondays/Tuesdays (today is {day_name}). Skipping.")
            return 0

    logger.info(f"[Thursday Track] Evaluating strategy close schedule for: {target_date} ({day_name})")

    # Check if the active Thursday cycle is already marked closed (idempotent protection)
    snapshot = load_active_snapshot(filepath=THURSDAY_SNAPSHOT_FILE)
    if not snapshot:
        logger.info("No active Thursday snapshot found at %s. Skipping close.", THURSDAY_SNAPSHOT_FILE)
        return 0

    if snapshot.get("status") == "closed" and not args.force:
        logger.info(f"Thursday strategy cycle {snapshot.get('cycle_id')} is already marked CLOSED. Skipping close.")
        return 0

    # Validation: Is today the active Strategy Closing Day?
    if not args.force:
        if not is_strategy_closing_day(target_date):
            if target_date.weekday() == 0 and check_nse_holiday(target_date):
                logger.info(
                    f"Monday ({target_date}) was an NSE holiday (closed in morning). Skipping post-market close."
                )
                return 0
            logger.info(
                f"Today ({target_date}) is not an active Strategy Closing Day for Thursday track. Skipping cleanly."
            )
            return 0

    mode_label = "EARLY MORNING HOLIDAY" if args.morning_holiday_check else "POST-MARKET"
    logger.info("=" * 65)
    logger.info(f"🚀 INITIATING {mode_label} THURSDAY STRATEGY CLOSE: {target_date}")
    logger.info("=" * 65)

    python_bin = sys.executable
    success = True

    # 1. Mark active Thursday cycle as closed upfront
    if not args.dry_run:
        try:
            mark_active_cycle_closed(filepath=THURSDAY_SNAPSHOT_FILE)
            logger.info("Marked Thursday strategy cycle as CLOSED in snapshot.")
        except Exception as e:
            logger.warning(f"Could not update cycle status in snapshot: {e}")

    # 2. Step 1: Send Thursday Strategy Performance Report
    logger.info("▶ Step 1/2: Generating and emailing Thursday performance report...")
    report_script = os.path.join(PROJECT_ROOT, "scripts", "send_weekly_report.py")
    if os.path.exists(report_script):
        report_cmd = [
            python_bin,
            report_script,
            "--track",
            "thursday",
            "--report-date",
            target_date.strftime("%Y-%m-%d"),
        ]
        if args.dry_run:
            report_cmd.append("--dry-run")
        step1_ok = run_command(
            report_cmd,
            cwd=PROJECT_ROOT,
            dry_run=args.dry_run,
        )
        if not step1_ok:
            success = False
    else:
        logger.warning(f"Script not found: {report_script}")

    # 3. Step 2: Push cycle dossier to MongoDB Atlas for Portal UI visibility
    logger.info("▶ Step 2/2: Synchronizing Thursday cycle dossier to MongoDB Atlas...")
    sync_script = os.path.join(PROJECT_ROOT, "scripts", "sync_to_mongodb.py")
    if os.path.exists(sync_script):
        # Locate latest thursday db
        from scripts.send_weekly_report import find_weekly_db
        try:
            db_path = find_weekly_db(target_date, track="thursday")
            sync_cmd = [python_bin, sync_script, "--db", str(db_path), "--track", "thursday"]
            if args.dry_run:
                logger.info("[DRY-RUN] Would sync %s to MongoDB Atlas", db_path)
            else:
                step2_ok = run_command(sync_cmd, cwd=PROJECT_ROOT, dry_run=args.dry_run)
                if not step2_ok:
                    logger.warning("MongoDB Atlas sync failed non-critically.")
        except Exception as sync_err:
            logger.warning(f"Could not locate Thursday DB to sync to MongoDB: {sync_err}")
    else:
        logger.warning(f"Sync script not found: {sync_script}")

    # NOTE: LLM skill generation (sentinel-hermes/skill_generator.py) is intentionally skipped.
    logger.info("ℹ️ Skill generation skipped for 3-day Thursday track as designed.")

    logger.info("=" * 65)
    if success:
        logger.info(f"✅ THURSDAY STRATEGY CLOSE PIPELINE FINISHED SUCCESSFULLY: {target_date}")
    else:
        logger.error(f"❌ THURSDAY STRATEGY CLOSE PIPELINE COMPLETED WITH ERRORS: {target_date}")
    logger.info("=" * 65)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
