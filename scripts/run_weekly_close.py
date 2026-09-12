#!/usr/bin/env python3
r"""
run_weekly_close.py - Holiday-Aware Weekly Strategy Closing Orchestrator.

Orchestrates the complete post-market weekly closing sequence for NIFTY 50:
1. Validates whether today is the true Strategy Closing Day:
   - Normal week: Monday post-market (15:35 IST).
   - Monday Holiday: Tuesday post-market (15:35 IST).
   - Skips silently when not the designated closing day.
2. Runs the end-of-cycle pipeline in sequence:
   - Step 1: scripts/send_weekly_report.py (PDF/HTML weekly performance report)
   - Step 2: scripts/compare_ai_vs_static_benchmark.py (AI vs Static comparison)
   - Step 3: sentinel-hermes/run_weekly_merge.sh (rebuilds merged SQLite database)
   - Step 4: sentinel-hermes/skill_generator.py (synthesizes weekly skill and syncs to MongoDB Atlas)

Scheduled in crontab on both Monday and Tuesday at 15:35 IST (10:05 UTC):
    35 10 * * 1,2 cd ~/nifty50_strategies && .venv/bin/python scripts/run_weekly_close.py >> /home/ubuntu/logs/weekly_close_$(date +\%F).log 2>&1

Usage:
    python scripts/run_weekly_close.py
    python scripts/run_weekly_close.py --dry-run
    python scripts/run_weekly_close.py --force
    python scripts/run_weekly_close.py --date 2026-09-15
"""

import os
import sys
import argparse
import logging
import subprocess
from datetime import date, datetime
from typing import Optional, List

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

from common.calendar_utils import (
    check_nse_holiday,
    is_strategy_closing_day,
    get_strategy_cycle_role,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_weekly_close")


def locate_sentinel_hermes_dir() -> Optional[str]:
    """Finds the sentinel-hermes project root directory."""
    env_dir = os.environ.get("SENTINEL_HERMES_DIR", "").strip()
    if env_dir and os.path.isdir(env_dir):
        return os.path.abspath(env_dir)

    # Sibling directory check (standard repo setup)
    sibling_dir = os.path.join(os.path.dirname(PROJECT_ROOT), "sentinel-hermes")
    if os.path.isdir(sibling_dir):
        return os.path.abspath(sibling_dir)

    # Home directory check (GCP / Ubuntu VM setup)
    home_dir = os.path.expanduser("~/sentinel-hermes")
    if os.path.isdir(home_dir):
        return os.path.abspath(home_dir)

    return None


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
        description="Run weekly post-market strategy closing sequence."
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Date to evaluate in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass holiday calendar and force execution of closing sequence",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution steps without invoking actual scripts",
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

    logger.info(f"Evaluating strategy close schedule for: {target_date} ({day_name})")
    logger.info(f"Calculated Strategy Lifecycle Role: {role}")

    # Validation: Is today the active Strategy Closing Day?
    if not args.force:
        if not is_strategy_closing_day(target_date):
            if target_date.weekday() == 0 and check_nse_holiday(target_date):
                logger.info(
                    f"Monday ({target_date}) is an NSE holiday. Weekly close is deferred to Tuesday."
                )
                return 0
            elif target_date.weekday() == 1:
                logger.info(
                    f"Tuesday ({target_date}) is a regular week day. Weekly close was completed on Monday."
                )
                return 0
            else:
                logger.info(
                    f"Today ({target_date}) is not an active Strategy Closing Day. Skipping cleanly."
                )
                return 0

    logger.info("=" * 65)
    logger.info(f"🚀 INITIATING POST-MARKET WEEKLY STRATEGY CLOSE: {target_date}")
    logger.info("=" * 65)

    python_bin = sys.executable
    success = True

    # -----------------------------------------------------------------
    # Step 1: Send Weekly Strategy Performance Report (PDF / Email)
    # -----------------------------------------------------------------
    logger.info("▶ Step 1/4: Generating and emailing weekly performance report...")
    report_script = os.path.join(PROJECT_ROOT, "scripts", "send_weekly_report.py")
    if os.path.exists(report_script):
        report_cmd = [
            python_bin,
            report_script,
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

    # -----------------------------------------------------------------
    # Step 2: Retrospective AI vs Static Benchmark Comparison
    # -----------------------------------------------------------------
    logger.info("▶ Step 2/4: Running retrospective AI vs static benchmark comparison...")
    benchmark_script = os.path.join(PROJECT_ROOT, "scripts", "compare_ai_vs_static_benchmark.py")
    if os.path.exists(benchmark_script):
        step2_ok = run_command(
            [python_bin, benchmark_script],
            cwd=PROJECT_ROOT,
            dry_run=args.dry_run,
        )
        if not step2_ok:
            success = False
    else:
        logger.warning(f"Script not found: {benchmark_script}")

    # -----------------------------------------------------------------
    # Step 3 & 4: Sentinel-Hermes Weekly Merge & Skill Generator
    # -----------------------------------------------------------------
    hermes_dir = locate_sentinel_hermes_dir()
    if hermes_dir:
        logger.info(f"Found sentinel-hermes directory at: {hermes_dir}")

        # Step 3: Run weekly merge script
        logger.info("▶ Step 3/4: Executing sentinel-hermes weekly merge pipeline...")
        merge_sh = os.path.join(hermes_dir, "run_weekly_merge.sh")
        if os.path.exists(merge_sh) and sys.platform != "win32":
            step3_ok = run_command(["bash", merge_sh], cwd=hermes_dir, dry_run=args.dry_run)
        else:
            # Fallback to python db_registry.py directly
            registry_py = os.path.join(hermes_dir, "db_registry.py")
            step3_ok = run_command([python_bin, registry_py], cwd=hermes_dir, dry_run=args.dry_run)
        if not step3_ok:
            success = False

        # Step 4: Run skill generator (synthesizes skill & syncs to MongoDB Atlas)
        logger.info("▶ Step 4/4: Generating weekly AI skill and syncing to MongoDB Atlas...")
        skill_py = os.path.join(hermes_dir, "skill_generator.py")
        if os.path.exists(skill_py):
            skill_cmd = [python_bin, skill_py, "--merged-db", "merged_latest.db"]
            if args.dry_run:
                skill_cmd.append("--dry-run")
            step4_ok = run_command(
                skill_cmd,
                cwd=hermes_dir,
                dry_run=args.dry_run,
            )
            if not step4_ok:
                success = False
    else:
        logger.warning("sentinel-hermes directory not located. Skipping merge and skill generation.")

    logger.info("=" * 65)
    if success:
        logger.info(f"✅ Weekly strategy close completed successfully for {target_date}.")
        return 0
    else:
        logger.error(f"⚠️ Weekly strategy close completed with one or more errors for {target_date}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
