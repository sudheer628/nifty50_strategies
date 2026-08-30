# NIFTY50 Weekly Option Data Collection Strategy

## 1. Objective

Build one self-contained option data collection strategy for NIFTY50 without placing any orders.

This strategy:

- Collects NIFTY50 market data via Angel One SmartAPI,
- Selects one PUT strike and one CALL strike from Tuesday's first-trigger LTP,
- Collects option prices for the selected strikes across the full weekly window,
- Stores all data in SQLite for long-term reuse,
- Is isolated from other strategies while sharing a common Angel One SmartAPI data layer.

---

## 2. Scope

### In scope

- Data collection only
- One strategy implementation
- Weekly expiry-based data collection
- Price collection every 30 minutes from 10:00 AM onward
- Common data source via Angel One SmartAPI
- Persistent storage in SQLite for analysis and later strategy review

### Out of scope

- Order placement
- Trade execution
- Risk management
- Live portfolio updates
- Cross-strategy dependencies

---

## 3. Project Structure

```
nifty50_strategies/
├── .env.example                         # Template for credentials
├── .gitignore
├── requirements.txt                     # Runtime dependencies
├── config.py                            # Central config, Redis factory, constants
├── common/
│   ├── __init__.py
│   ├── angelone_client.py               # SmartAPI auth + market data fetchers
│   ├── expiry.py                        # Expiry resolution + static strike selector
│   ├── ai_strike_selector.py            # Dynamic AI strike selector (VIX, ATR, Greeks)
│   └── storage.py                       # SQLite persistence + JSON snapshot
├── strategies/
│   ├── __init__.py
│   └── weekly_option_collector.py       # Main strategy entry point
├── scripts/
│   ├── send_weekly_report.py            # Monday HTML email + chart
│   └── compare_ai_vs_static_benchmark.py # Retrospective side-by-side P&L comparison
└── cron/
    └── nifty50_weekly_report.cron       # Monday EOD report schedule
```

### Module descriptions

| Module | Purpose |
|---|---|
| `config.py` | Loads `.env`, Redis client factory, constants (strike step=100, NIFTY token, Angel One base URL, storage paths) |
| `common/angelone_client.py` | JWT auth from Redis `angelone_jwt_feed`, NIFTY spot LTP, daily instrument-master option lookup, and `get_nifty_option_chain()` for CALL+PUT LTPs |
| `common/expiry.py` | `get_next_weekly_expiry()`, `is_tuesday()`, `format_expiry_angelone()`, `format_expiry_file()`, `strike_selector()` |
| `common/ai_strike_selector.py` | Tuesday 9:30 AM AI Strike Selector: optimizes strikes based on VIX, ATR, Greeks ($\Delta \approx 0.35$), and IV skew via OpenRouter with static fallback |
| `common/storage.py` | `build_db_path()`, `init_db()`, `insert_record()`, `insert_buy_snapshot()`, `save_active_snapshot()`, `load_active_snapshot()`, `generate_cycle_id()` |
| `strategies/weekly_option_collector.py` | Cron-invoked collector: one cycle per invocation, Tuesday buy-price capture, SQLite + JSON persistence |
| `scripts/compare_ai_vs_static_benchmark.py` | Reconstructs static $\pm 100$ strike LTPs from `signals_data_*.db` `option_chain_surface` to compute side-by-side outperformance delta vs AI strategy |

---

## 4. Weekly Data Collection Logic (Final)

### 4.1 Simplified cycle model

The original plan (section 4.4) proposed a "double collection" model where the old and new cycles ran in parallel on the second Tuesday. That proved too complex. The simplified model is:

- **Data collection ends on Monday.** The previous weekly cycle is complete.
- **Fresh collection starts every Tuesday** for the next weekly expiry.
- **No parallel (double) collection** on Tuesday — a clean break between cycles.

### 4.2 Collection schedule

- Every Tuesday at 9:30 AM IST, a fresh weekly cycle begins targeting the **next** weekly expiry.
- Data is collected every 30 minutes from 10:00 AM to 3:30 PM IST, Tuesday through Monday.
- The cycle ends on Monday; the next Tuesday starts a new cycle.

### 4.3 Example: Aug 4 through Aug 11

| Date     | Day     | Active cycle                 | Snapshot                                                                                   |
| -------- | ------- | ---------------------------- | ------------------------------------------------------------------------------------------ |
| 4 Aug    | Tue     | New: expiry Aug 11           | `current_week_buy.json` (4th Aug prices)                                                   |
| 5-10 Aug | Wed-Mon | Same cycle continues         | Unchanged                                                                                  |
| 11 Aug   | Tue     | Old ends; new: expiry Aug 18 | Snapshot archived as `current_week_buy_20260804.json`; new `current_week_buy.json` written |

### 4.4 Mid-Tuesday start (Scenario 1)

If the script is started mid-Tuesday (missing the 9:30 AM trigger) with **no** pre-existing `current_week_buy.json`:

- The code creates the snapshot automatically using the current option LTPs as buy prices.
- No manual intervention is required — the script handles it gracefully.
- The `week_start_date` in the snapshot reflects the actual Tuesday date.

### 4.5 Manual snapshot (Scenario 2)

If `current_week_buy.json` is manually created with correct Tuesday 9:30 AM
prices before the script runs:

- The code detects that the snapshot covers the currently active Tuesday-Monday cycle.
- It reuses that snapshot's expiry, CALL/PUT strikes, and buy prices instead of
  recalculating them from later daily opens.
- This is useful when the first few triggers are missed and accurate 9:30 AM prices are known.

For a mid-cycle start on Monday 10 Aug 2026, with the cycle that began Tuesday
4 Aug and expires Tuesday 11 Aug, the manual snapshot is:

```json
{
  "strategy_name": "nifty50_weekly_option_collector",
  "cycle_id": "20260804-manual",
  "week_start_date": "20260804",
  "expiry_date": "20260811",
  "call_strike": 24700,
  "put_strike": 24500,
  "call_buy_price": 92.0,
  "put_buy_price": 82.0,
  "captured_at": 1785854400
}
```

### 4.6 Dynamic AI Strike Selection & Retrospective Benchmark (September 2026+)

On Tuesday at 09:30 AM IST, `weekly_option_collector.py` invokes [`common/ai_strike_selector.py`](file:///c:/Users/sai-s/Documents/GitHub/nifty50_strategies/common/ai_strike_selector.py):
1. **Dynamic Greeks & Volatility Ingestion**: Ingests live India VIX, ATR(14) daily range, Bollinger Band Width %, and option chain Greeks ($\Delta \approx 0.35, \Theta, \text{IV Skew}$) from SQLite.
2. **AI Strike Calibration**: Queries OpenRouter LLMs via Redis `finance_llm_models` to select volatility-adaptive strikes (e.g. widening in high VIX, balancing Put IV skew).
3. **Fail-Safe Fallback**: If OpenRouter times out (>5s) or returns invalid output, it automatically reverts to the standard static `anchor +/- 100` rule.
4. **Snapshot Persistence**: Saves both the AI strikes and the static benchmark strikes in `current_week_buy.json`:
   ```json
   {
     "strategy_name": "nifty50_weekly_option_collector",
     "cycle_id": "20260901-a1b2c3d4",
     "week_start_date": "20260901",
     "expiry_date": "20260908",
     "call_strike": 24650,
     "put_strike": 24400,
     "call_buy_price": 128.5,
     "put_buy_price": 112.0,
     "captured_at": 1788244200,
     "static_call_strike": 24600,
     "static_put_strike": 24400,
     "selection_mode": "AI_deepseek-v4-pro",
     "selection_rationale": "VIX at 15.4 with ATR 160; selected +100 CE / -150 PE due to elevated Put IV skew."
   }
   ```
5. **Retrospective Benchmark Comparison**:
   Run the comparison script anytime to see side-by-side performance:
   ```bash
   python scripts/compare_ai_vs_static_benchmark.py
   ```
   This reconstructs the static $\pm 100$ strike performance using `option_chain_surface` from `market_signal_agent` without requiring duplicate live API calls.

### 4.7 Weekly Closed-Loop Lifecycle (Weeks 1 to 20)

```
                            WEEKLY LIFECYCLE (Weeks 1 to 20)
                            
  Tuesday 09:30 AM IST:  nifty50_strategies picks AI Strikes & enters Strangle
                                          │
                                          ▼
  Tue-Mon (Every 30m):   sentinel-hermes inference_runner.py evaluates trade:
                         • Logs predictions to predictions.db (Paper Trading)
                         • Sends immediate Email Alert on TAKE_PROFIT / STOP_LOSS
                                          │
                                          ▼
  Monday 15:40 IST:      compare_ai_vs_static_benchmark.py scores AI vs Static
                                          │
                                          ▼
  Monday 15:50 IST:      run_weekly_merge.sh creates merged_weekly_*.db
                                          │
                                          ▼
  Monday 15:55 IST:      skill_generator.py generates skills/skill_YYYYMMDD.md
                                          │
                                          ▼
  Dynamic Injection:     inference_runner.py automatically reads the new skill
                         on Tuesday morning, making Week 2 smarter than Week 1!
```

---

## 5. Data Collected

For each 30-minute interval, the strategy collects:

- NIFTY50 day open (stored independently from the strike-selection LTP)
- NIFTY50 current LTP
- NIFTY50 previous close (if available)
- PUT strike (selected from Tuesday's first-trigger LTP)
- CALL strike (selected from Tuesday's first-trigger LTP)
- Option LTPs for the selected strikes
- Tuesday buy prices for CALL and PUT
- Combined option gain/loss: `(CALL LTP - CALL buy) + (PUT LTP - PUT buy)`

---

## 6. Strike Selection Rule

### 6.1 Strike grid

100-point strike increments (not 50-point).

### 6.2 Algorithm

```python
anchor = floor(first_trigger_ltp / 100) * 100
put_strike  = anchor - 100  # one step below
call_strike = anchor + 100  # one step above
```

### 6.3 Example

First-trigger LTP = 25000 → PUT = 24900, CALL = 25100.

---

## 7. Collection Frequency

- **Start time**: 10:00 AM IST
- **Interval**: Every 30 minutes
- **Duration**: Tuesday through Monday during market hours (10:00 AM - 3:30 PM IST)

### Cron entry

```bash
# Every 30 minutes during market hours (10:00-15:30 IST)
# Two entries: :30 past (hours 4-9 UTC) and :00 past (hours 5-10 UTC)
30 4-9 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/weekly_option_collector.py
0 5-10 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/weekly_option_collector.py
```

(Combined, these fire at 4:30, 5:00, 5:30, …, 9:30, 10:00 UTC = every 30 min from 10:00 to 15:30 IST.)

**Timestamp format:** All timestamps in SQLite are stored as Unix integers (seconds since epoch, UTC) matching the format used in `market_signal_agent` for cross-project joins and consistent querying. The storage layer (`storage.init_db()`) automatically migrates any legacy TEXT (ISO 8601) timestamps to integers on startup.

---

## 8. Authentication (Angel One SmartAPI)

Follows the same pattern as the existing `news-analyzer-for-market-sentiment` project:

- **Static**: `ANGELONE_API_KEY` from `.env`
- **Dynamic**: JWT from Redis key `angelone_jwt_feed` (populated externally by `generate_trading_keys`)
- Base URL: `https://apiconnect.angelone.in`

---

## 9. Data Model

### 9.1 Collection record (`strategy_hourly_data` table)

| Column                 | Type    | Description                                           |
| ---------------------- | ------- | ----------------------------------------------------- |
| `strategy_name`        | TEXT    | Strategy identifier                                   |
| `collection_timestamp` | INTEGER | Unix timestamp (seconds since epoch, UTC)             |
| `expiry_date`          | TEXT    | Expiry in `YYYYMMDD`                                  |
| `nifty_open`           | REAL    | NIFTY50 day open                                      |
| `nifty_ltp`            | REAL    | NIFTY50 current LTP                                   |
| `nifty_previous_close` | REAL    | NIFTY50 previous close                                |
| `put_strike`           | INTEGER | PUT strike price                                      |
| `put_ltp`              | REAL    | PUT option LTP                                        |
| `call_strike`          | INTEGER | CALL strike price                                     |
| `call_ltp`             | REAL    | CALL option LTP                                       |
| `call_buy_price`       | REAL    | Tuesday CALL buy price (carried through week)         |
| `put_buy_price`        | REAL    | Tuesday PUT buy price (carried through week)          |
| `gainloss`             | REAL    | `(current CALL - CALL buy) + (current PUT - PUT buy)` |
| `source`               | TEXT    | Always `"angelone"`                                   |
| `cycle_id`             | TEXT    | Unique cycle identifier                               |

### 9.2 Buy snapshot table (`strategy_buy_snapshots`)

| Column            | Type    | Description                               |
| ----------------- | ------- | ----------------------------------------- |
| `strategy_name`   | TEXT    | Strategy identifier                       |
| `cycle_id`        | TEXT    | Unique cycle identifier                   |
| `week_start_date` | TEXT    | Tuesday date in `YYYYMMDD`                |
| `expiry_date`     | TEXT    | Expiry date in `YYYYMMDD`                 |
| `call_strike`     | INTEGER | CALL strike                               |
| `put_strike`      | INTEGER | PUT strike                                |
| `call_buy_price`  | REAL    | CALL buy price at capture                 |
| `put_buy_price`   | REAL    | PUT buy price at capture                  |
| `captured_at`     | INTEGER | Unix timestamp (seconds since epoch, UTC) |

---

## 10. Storage Plan

### 10.1 SQLite databases (per weekly cycle)

Path: `/home/ubuntu/sqlite/strategies/nifty50_weekly_data_{YYYYMMDD}_{expiry}.db`

Example: `nifty50_weekly_data_20260804_20260811.db`

### 10.2 JSON snapshots

- **Active**: `/home/ubuntu/sqlite/strategies/current_week_buy.json`
- **Archived**: `/home/ubuntu/sqlite/strategies/current_week_buy_{YYYYMMDD}.json` (one per past week)

### 10.3 Storage rules

- One row per collection interval
- One database file per weekly cycle
- SQLite is the authoritative store; the JSON snapshot is a convenience file for the active cycle

---

## 11. Implementation Status

### Phase 1: Common foundation (COMPLETE)

- [x] Shared SmartAPI wrapper (`common/angelone_client.py`)
- [x] Expiry resolution helper (`common/expiry.py`)
- [x] SQLite storage handler (`common/storage.py`)
- [x] JSON snapshot handler (`common/storage.py`)
- [x] Central configuration (`config.py`)

### Phase 2: Strategy collector (COMPLETE)

- [x] Weekly cycle logic
- [x] Strike selection logic (100-point grid)
- [x] 30-minute interval collection
- [x] Tuesday buy price capture
- [x] SQLite + JSON persistence
- [x] Automated snapshot archival on Tuesday rollover
- [x] Scenario 1 support (auto-create snapshot mid-Tuesday)
- [x] Scenario 2 support (respect manually-created snapshot)

### Phase 3: Validation and monitoring (COMPLETE)

- [x] Verify collection runs on Tuesday
- [x] Verify correct expiry selected
- [x] Verify PUT/CALL strikes logged correctly
- [x] Verify hourly data from 9:30 AM onward
- [x] Verify JSON snapshot stability through the week

### Phase 4: Timestamp alignment (COMPLETE)

- [x] Migrated `collection_timestamp` from ISO 8601 TEXT to Unix INTEGER
- [x] Migrated `captured_at` from ISO 8601 TEXT to Unix INTEGER
- [x] Automatic migration in `storage.init_db()` for existing databases
- [x] Aligned with `market_signal_agent` (`ts` column) and `nifty_signal_features` (`ts` column)
- [x] `send_daily_report.py` (in `market_signal_agent`) queries using integer range comparison
- [x] `send_weekly_report.py` handles both integer and legacy TEXT timestamps via `_parse_timestamp()`

---

## 12. Setup & Running

### Prerequisites

- Python 3.9+
- Redis Cloud with `angelone_jwt_feed` key populated
- Angel One trading account with API access

### Installation

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with real ANGELONE_API_KEY, REDIS_HOST, REDIS_PASSWORD
```

### Running

```bash
# Manual single collection
python strategies/weekly_option_collector.py

# Dry run (no API calls)
python strategies/weekly_option_collector.py --dry-run

# Force run outside market hours
python strategies/weekly_option_collector.py --force

# Cron (every 30 minutes during market hours, 10:00-15:30 IST)
30 4-9 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/weekly_option_collector.py
0 5-10 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/weekly_option_collector.py
```

### Weekly Monday email report

The report reads the active Tuesday-Monday SQLite database and sends a styled
HTML email containing summary metrics, the complete hourly gain/loss table,
and an embedded chart of NIFTY LTP and combined option gain/loss.

It uses the same Gmail SMTP environment variables as the legacy market signal
reporter:

```env
EMAIL_SENDER=your_email@gmail.com
EMAIL_APP_PASSWORD=your_gmail_app_password
EMAIL_RECIPIENT=recipient@example.com
EMAIL_SMTP_SERVER=smtp.gmail.com
EMAIL_SMTP_PORT=465
```

Generate a preview without sending email:

```bash
python scripts/send_weekly_report.py --dry-run
```

Send immediately:

```bash
python scripts/send_weekly_report.py
```

The preview HTML and PNG chart are archived under
`/home/ubuntu/sqlite/strategies/reports/` by default.

Monday EOD cron (10:10 UTC / 3:40 PM IST, after the final 3:15 PM collector):

```cron
10 10 * * 1 cd ~/nifty50_strategies && .venv/bin/python scripts/send_weekly_report.py >> /home/ubuntu/logs/options_strategy_$(date +\%F).log 2>&1
```

---

## 13. Expected Output

- Clean dataset containing NIFTY50 spot and option information,
- Weekly time-series records for analysis,
- Stored Tuesday buy prices for CALL and PUT,
- Base foundation for future strategy development.

---

## 14. Design Decisions (from implementation)

| Decision                      | Rationale                                                                        |
| ----------------------------- | -------------------------------------------------------------------------------- |
| No `smartapi-python` SDK      | Reuses existing direct HTTP + Redis JWT pattern from `check_active_positions.py` |
| Synchronous code              | 30-minute cron-triggered; no need for async                                      |
| Simplified single-cycle model | Eliminates double-collection complexity on rollover Tuesday                      |
| Two-scenario Tuesday handling | Supports both fresh auto-start and manual snapshot injection                     |
| SQLite per weekly cycle       | Clear separation, sortable filenames, append-only per cycle                      |
| Unix integer timestamps       | Aligns with `market_signal_agent` and `nifty_signal_features` for cross-project joins |
| Auto-migration in `init_db()` | Converts legacy TEXT timestamps on startup; no manual scripts needed             |
