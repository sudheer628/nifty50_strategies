---
name: nifty50-strategies-engineering
description: Specialized engineering, debugging, and operational workflow for the nifty50_strategies repository on the Ubuntu VM. Enforces prompt-scoped changes, live crontab inspection (crontab -l), production log analysis (/home/ubuntu/logs/), cross-system review of sentinel-hermes and sibling directories, pragmatic infrastructure failure assumptions, and a strict prohibition on triggering live broker orders.
---

# NIFTY50 Strategies Engineering Workflow

Act as a disciplined senior trading systems engineer working inside the `nifty50_strategies` repository on the Ubuntu VM (`/home/ubuntu/nifty50_strategies`). Prioritize operational safety, code correctness, minimal targeted diffs, and non-negotiable trade isolation.

---

## 1. Absolute Trading Safety Invariant (NO LIVE ORDERS)

> [!CAUTION]
> **NEVER TRIGGER LIVE BROKER ORDERS UNDER ANY CIRCUMSTANCES.**
> This system interfaces with real financial brokerage accounts (Upstox and Angel One SmartAPI). You are strictly prohibited from submitting, modifying, or cancelling live market or stop-loss orders across all repositories and scripts.

- **Mandatory Dry-Run / Mock Default**: Whenever executing, testing, or verifying strategy scripts or exit managers, **strictly use `--mock`, `--dry-run`, or `--check` flags**.
- **Prohibited Flags & Commands**:
  - NEVER run `scripts/Tuesday-Fno-Exit-Orders-V1.py` or `scripts/Thursday-Fno-Exit-Orders-V1.py` with `--execute` or without `--mock`/`--dry-run`.
  - NEVER run `common/entry_order_manager.py` against live broker endpoints.
  - NEVER set or export live trading flags: `LIVE_EXIT_TRIGGER_TUESDAY=true`, `LIVE_EXIT_TRIGGER_THURSDAY=true`, `ORDERS_LIVE_TUE=1`, `ORDERS_LIVE_THU=1`, `ORDERS_LIVE=1`, or `--force-live`.
  - NEVER alter Redis fast gate keys (`ORDERS_LIVE_TUE`, `ORDERS_LIVE_THU`) to trigger live execution.
- **Testing Standard**: Validate logic exclusively using automated pytest unit tests (`.venv/bin/pytest`) or mock execution fixtures. Live API credentials must never be invoked for testing.

---

## 2. Check Live Production State Before Modifying Code

Before making any diagnoses or code edits, inspect the active production runtime:

### Step 2.1: Inspect Crontab Jobs (`crontab -l`)
Run `crontab -l` in bash to determine which scripts are currently active and their execution cadence:
- **Master Weekly Track (Tuesday–Monday)**:
  - `strategies/weekly_option_collector.py` (:01 and :31 past every hour, 04:01–10:01 UTC)
  - `strategies/peak_profit_monitor.py` (:16 and :46 past every hour, 03:46–09:46 UTC)
  - `strategies/monday_gamma_sniper.py` (Mondays every 10m, 07:00–09:50 UTC)
  - `scripts/run_weekly_close.py` (Mondays 10:07 UTC / Tue 03:37 UTC deferred)
- **Parallel 3-Day Track (Thursday–Monday)**:
  - `strategies/thursday_option_collector.py` (:02 and :32 past every hour on Mon, Thu, Fri)
  - `strategies/peak_profit_monitor.py --track thursday` (:17 and :47 past every hour)
  - `scripts/run_thursday_close.py` (Mondays 10:08 UTC / Tue 03:38 UTC deferred)
- **Live Exit Managers**:
  - `scripts/Tuesday-Fno-Exit-Orders-V1.py` (Every 1 min during market hours)
  - `scripts/Thursday-Fno-Exit-Orders-V1.py` (Every 1 min during market hours)

> [!IMPORTANT]
> **Check timing before editing**: If a collector or monitor runs at `:01` or `:02` and the current time is near that trigger, avoid saving partial edits that could corrupt or crash a live cron run.

### Step 2.2: Review Logs in `/home/ubuntu/logs/`
Inspect recent runtime logs to diagnose errors, inspect price captures, and observe FSM state transitions before touching code:
- **Collector Logs**:
  - `tail -n 100 /home/ubuntu/logs/options_strategy_$(date +%F).log` (Tuesday master)
  - `tail -n 100 /home/ubuntu/logs/thursday_strategy_$(date +%F).log` (Thursday track)
- **Peak Monitor Logs**:
  - `tail -n 50 /home/ubuntu/logs/peak_monitor_$(date +%F).log`
  - `tail -n 50 /home/ubuntu/logs/thursday_peak_monitor_$(date +%F).log`
- **Exit & Close Orchestrator Logs**:
  - `tail -n 100 ~/nifty50_strategies/logs/tue_exit_$(date +%F).log`
  - `tail -n 100 ~/nifty50_strategies/logs/thu_exit_$(date +%F).log`
  - `tail -n 100 /home/ubuntu/logs/weekly_close_$(date +%F).log`
  - `tail -n 100 /home/ubuntu/logs/thursday_close_$(date +%F).log`
- **Cross-Repo Logs (sentinel-hermes & others)**:
  - `tail -n 100 /home/ubuntu/logs/sentinel-hermes_$(date +%F).log`
- Search for errors or stack traces:
  ```bash
  grep -Ei "error|exception|traceback|critical" /home/ubuntu/logs/*$(date +%F).log | tail -n 50
  ```

### Step 2.3: Check Active Strategy Snapshots
Examine active JSON snapshot states under `/home/ubuntu/sqlite/strategies/`:
- `cat /home/ubuntu/sqlite/strategies/current_week_buy.json`
- `cat /home/ubuntu/sqlite/strategies/current_thursday_buy.json`
Verify `cycle_id`, `status` (`"ongoing"` vs `"closed"`), strikes, entry prices, and `alpha_fsm` leg statuses.

---

## 3. Cross-Repository Review & Sibling Ecosystem (`sentinel-hermes` & Others)

`nifty50_strategies` does not run in a vacuum; it is part of an integrated trading ecosystem on the VM. You are **explicitly authorized and encouraged to review and inspect sibling directories** whenever necessary:

| Directory | Relationship & What to Review |
|---|---|
| **`/home/ubuntu/sentinel-hermes`** | **Primary Sibling Repository**: Ingests `nifty50_strategies` snapshots and runs inference (`inference_runner.py`), fast exit checks (`fast_exit_check.py`), AI exit runners (`ai_exit_runner.py`), exit alert dispatches, weekly merges (`run_weekly_merge.sh`), and skill generation (`skill_generator.py`). Review its code, tests, and databases (`dispatched_exit_alerts.db`, `dispatched_peak_alerts.db`) to verify cross-system contracts. |
| **`/home/ubuntu/market_signal_agent`** | Upstream market data provider for NIFTY spot LTP, 1m/5m candles, and option chain surface data (`signals_data_*.db`). Review when verifying price continuity or strike selection feeds. |
| **`/home/ubuntu/nifty_signal_features`** | Technical feature engine (ATR, India VIX, volatility bands, trend regime filters). Review when debugging Smart Entry Gate inputs. |
| **`/home/ubuntu/generate_trading_keys`** | SmartAPI authentication refresher that writes `angelone_jwt_feed` to Redis. Review when troubleshooting SmartAPI token or login failures. |
| **`/home/ubuntu/discord-bot`** | Notification bot and command interface for Discord alerts. |
| **`/home/ubuntu/sqlite`** | Shared SQLite databases, including `/home/ubuntu/sqlite/strategies/` for cycle DBs, snapshots, and alert deduplication tables. |
| **`/home/ubuntu/data`** | Daily instrument master tokens, scrip mapping, and intraday cache stores. |
| **`/home/ubuntu/reports`** | Output directory for weekly HTML performance reports and P&L charts. |
| **`/home/ubuntu/logs`** | Unified VM log directory for all collectors, monitors, runners, and orchestrators. |

### Review vs. Modification Scope
- **Inspection (Permitted & Encouraged)**: You may freely read files, check git history, run tests, and inspect databases in `sentinel-hermes` or other sibling directories to understand data structures, verify integration contracts, or troubleshoot root causes.
- **Modification Scope**: Only make edits inside `sentinel-hermes` or other sibling repositories when the user's prompt explicitly requests or confirms changes to that repository. Otherwise, focus code changes strictly on `nifty50_strategies`.

---

## 4. Strict Scope Bounding & Practical Failure Assumptions

### Step 4.1: Prompt-Driven Scope Only
- **Make changes strictly as requested in the user prompt**: Do not add unsolicited features, opportunistic refactors, or stylistic churn.
- **Preserve Business Logic & Risk Invariants**: Unless explicitly directed by the user prompt, DO NOT modify:
  - Strike selection algorithms or grids (50-pt AI grid, 100-pt static fallback).
  - Target Delta calculations ($\Delta \approx 0.35$).
  - Decoupled FSM trailing buffers (25% for >5 DTE, 18% for 1.5–5 DTE, 10% for <1.5 DTE, 5% tight trail).
  - Harvest rule (+50% leg profit) or salvage stop rules (-50% ITM / -55% OTM).
  - Peak-profit threshold ($\ge 20\%$) and anti-spam deduplication rules.
  - Track segregation boundaries (Tuesday = Upstox; Thursday = AngelOne SmartAPI).

### Step 4.2: Avoid Over-Engineering Impractical Failure Scenarios
AI models often become excessively cautious about rare or extreme failure scenarios, adding defensive bloat and speculative fallbacks. In this repository, follow these pragmatic ground rules:
- **Assume Managed Infrastructure is Operational**: Avoid designing elaborate workarounds, offline queues, or complex multi-tiered fallback cascades for core managed infrastructure being down or unreachable (e.g., **Redis Cloud**, **MongoDB Atlas**, or **OpenRouter API**).
- **Standard Error Handling is Sufficient**: A straightforward `try...except` block with a clear log message and a safe return (e.g., logging a warning and safely skipping a cycle or falling back to deterministic static strikes) is completely sufficient.
- **No Defensive Bloat**: Never introduce local queue synchronizers, secondary fallback broker adapters, emergency offline stores, or redundant connection pools to guard against catastrophic cloud outages unless the user prompt explicitly requests them.
- **Focus on Core Trading Logic**: Dedicate engineering effort to signal accuracy, correct strike math, accurate P&L tracking, and fulfilling the user's specific prompt rather than speculating on catastrophic cloud-provider outages.

---

## 5. Architectural Rules & Code Integrity

- **Strict Track Segregation**:
  - Master Weekly Track (`weekly`): Uses `current_week_buy.json`, `nifty50_weekly_data_{start}_{expiry}.db`, Upstox broker adapter, and `tag="TUE-SL"`.
  - Parallel 3-Day Track (`thursday`): Uses `current_thursday_buy.json`, `nifty50_thursday_data_{start}_{expiry}.db`, AngelOne SmartAPI, and Redis `order_id` authority.
  - Never allow state, database files, or broker credentials from one track to leak into the other.
- **Linux LF Line Endings**: All files MUST use Unix LF (`\n`) line endings. Never introduce CRLF (`\r\n`).
- **Atomic File Operations**: Always write snapshot updates to temporary files and atomically replace them via `os.replace` to prevent corrupting state during concurrent cron reads.
- **Storage Compatibility**: SQLite timestamps must remain Unix integers (seconds since epoch UTC). When extending schemas, add non-destructive column auto-migrations in `common/storage.py`.
- **Git Hygiene**:
  - Run `git status` and `git diff` to ensure zero unintended changes or whitespace churn.
  - Do NOT run `git commit`, `git push`, or `git reset` unless explicitly instructed by the user.

---

## 6. Secrets and Financial Credentials

- **Protect All Secrets**: Never print, echo, or log API keys, tokens, passwords, or connection strings.
- Files containing credentials (`.env`, auth tokens, private keys) are strictly confidential.
- Redact values when referencing configuration in responses or debugging logs.

---

## 7. Execution & Validation Commands

Always execute commands within the project's virtual environment:

```bash
# Set repository directory
cd /home/ubuntu/nifty50_strategies

# Run targeted pytest tests (fastest validation)
.venv/bin/pytest tests/test_<relevant_module>.py -v

# Run the complete test suite (must pass 100%)
.venv/bin/pytest

# Safe script verification (ALWAYS with safe flags)
.venv/bin/python strategies/weekly_option_collector.py --dry-run
.venv/bin/python strategies/thursday_option_collector.py --dry-run
.venv/bin/python strategies/peak_profit_monitor.py --check
.venv/bin/python strategies/peak_profit_monitor.py --track thursday --check
.venv/bin/python scripts/Tuesday-Fno-Exit-Orders-V1.py --mock
.venv/bin/python scripts/Thursday-Fno-Exit-Orders-V1.py --mock
.venv/bin/python scripts/run_weekly_close.py --dry-run
.venv/bin/python scripts/run_thursday_close.py --dry-run

# Validating sibling sentinel-hermes tests when reviewing cross-repo contracts:
# cd /home/ubuntu/sentinel-hermes && .venv/bin/pytest tests/test_<module>.py -v
```

---

## 8. Pre-Completion Review Checklist

Before declaring the task complete:
1. **Safety Verification**: Did you confirm that no live broker orders were triggered and no live order flags were enabled?
2. **Cron & Log Harmony**: Did you verify that any modified script matches the live production schedule in `crontab -l` and doesn't log unhandled exceptions in `/home/ubuntu/logs/`?
3. **Cross-Repo Consistency**: If changes impact data contracts, did you check compatibility with `sentinel-hermes`?
4. **Automated Tests**: Did you run `.venv/bin/pytest` and verify that all affected unit tests pass without regressions?
5. **Git Diff Inspection**: Is `git diff` clean, targeted, and free of extraneous debug logs or formatting changes?
6. **Line Endings**: Are all modified files confirmed to have Linux LF endings?

---

## 9. Completion Response Format

Provide a concise, practical report with the following structure:

1. **Production Context**: Summary of active crontab schedules and log status reviewed in `/home/ubuntu/logs/` (and sibling logs if applicable).
2. **Changes Made**: Specific files modified and the exact logic changed as requested in the user prompt.
3. **Cross-System Review**: Relevant contracts, schema dependencies, or logs reviewed in `sentinel-hermes` or other sibling directories.
4. **Safety Confirmation**: Explicit verification that execution remained in mock/dry-run mode with ZERO live order triggering.
5. **Validation Results**: Exact `pytest` commands executed and their pass/fail outcomes.
6. **Operational Notes**: Any pending follow-ups or operational considerations for the next cron cycle.
