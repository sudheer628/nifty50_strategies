# NIFTY50 Weekly Option Data Collection Strategy

## 1. Objective

Build one self-contained option data collection strategy for NIFTY50 without placing any orders.

This strategy:
- Collects NIFTY50 market data via Angel One SmartAPI,
- Selects one PUT strike and one CALL strike based on the day open,
- Collects option prices for the selected strikes across the full weekly window,
- Stores all data in SQLite for long-term reuse,
- Is isolated from other strategies while sharing a common Angel One SmartAPI data layer.

---

## 2. Scope

### In scope
- Data collection only
- One strategy implementation
- Weekly expiry-based data collection
- Hourly price collection from 9:30 AM onward
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
├── requirements.txt                     # python-dotenv, redis, requests, pytz
├── config.py                            # Central config, Redis factory, constants
├── common/
│   ├── __init__.py
│   ├── angelone_client.py               # SmartAPI auth + market data fetchers
│   ├── expiry.py                        # Expiry resolution + strike selector
│   └── storage.py                       # SQLite persistence + JSON snapshot
└── strategies/
    ├── __init__.py
    └── weekly_option_collector.py       # Main strategy entry point
```

### Module descriptions

| Module | Purpose |
|--------|---------|
| `config.py` | Loads `.env`, Redis client factory, constants (strike step=100, NIFTY token, Angel One base URL, storage paths) |
| `common/angelone_client.py` | JWT auth from Redis `angelone_jwt_feed`, NIFTY spot LTP, daily instrument-master option lookup, and `get_nifty_option_chain()` for CALL+PUT LTPs |
| `common/expiry.py` | `get_next_weekly_expiry()`, `is_tuesday()`, `format_expiry_angelone()`, `format_expiry_file()`, `strike_selector()` |
| `common/storage.py` | `build_db_path()`, `init_db()`, `insert_record()`, `insert_buy_snapshot()`, `save_active_snapshot()`, `load_active_snapshot()`, `generate_cycle_id()` |
| `strategies/weekly_option_collector.py` | Cron-invoked collector: one cycle per invocation, Tuesday buy-price capture, SQLite + JSON persistence |

---

## 4. Weekly Data Collection Logic (Final)

### 4.1 Simplified cycle model

The original plan (section 4.4) proposed a "double collection" model where the old and new cycles ran in parallel on the second Tuesday. That proved too complex. The simplified model is:

- **Data collection ends on Monday.** The previous weekly cycle is complete.
- **Fresh collection starts every Tuesday** for the next weekly expiry.
- **No parallel (double) collection** on Tuesday — a clean break between cycles.

### 4.2 Collection schedule

- Every Tuesday at 9:30 AM IST, a fresh weekly cycle begins targeting the **next** weekly expiry.
- Data is collected hourly from 9:30 AM to 3:40 PM IST, Tuesday through Monday.
- The cycle ends on Monday; the next Tuesday starts a new cycle.

### 4.3 Example: Aug 4 through Aug 11

| Date | Day | Active cycle | Snapshot |
|------|-----|-------------|----------|
| 4 Aug | Tue | New: expiry Aug 11 | `current_week_buy.json` (4th Aug prices) |
| 5-10 Aug | Wed-Mon | Same cycle continues | Unchanged |
| 11 Aug | Tue | Old ends; new: expiry Aug 18 | Snapshot archived as `current_week_buy_20260804.json`; new `current_week_buy.json` written |

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
  "captured_at": "2026-08-04T04:00:00+00:00"
}
```

Save it as `/home/ubuntu/sqlite/strategies/current_week_buy.json`, then run
`python strategies/weekly_option_collector.py --dry-run` to validate that the
cycle is recognized without calling SmartAPI or writing to SQLite.

---

## 5. Data Collected

For each hourly interval, the strategy collects:

- NIFTY50 day open
- NIFTY50 current LTP
- NIFTY50 previous close (if available)
- PUT strike (selected from day open)
- CALL strike (selected from day open)
- Option LTPs for the selected strikes
- Tuesday buy prices for CALL and PUT

---

## 6. Strike Selection Rule

### 6.1 Strike grid
100-point strike increments (not 50-point).

### 6.2 Algorithm

```python
anchor = floor(day_open / 100) * 100
put_strike  = anchor - 100  # one step below
call_strike = anchor        # at the anchor
```

### 6.3 Example

Day open = 24540 → PUT = 24000, CALL = 24500.

---

## 7. Collection Frequency

- **Start time**: 9:30 AM IST
- **Interval**: Hourly
- **Duration**: Tuesday through Monday during market hours (9:30 AM - 3:40 PM IST)

### Cron entry

```
30 4-10 * * 1-5 cd /path/to/nifty50_strategies && python strategies/weekly_option_collector.py
```

(UTC hours `4-10` correspond to IST 9:30 AM - 3:40 PM)

---

## 8. Authentication (Angel One SmartAPI)

Follows the same pattern as the existing `news-analyzer-for-market-sentiment` project:

- **Static**: `ANGELONE_API_KEY` from `.env`
- **Dynamic**: JWT from Redis key `angelone_jwt_feed` (populated externally by `generate_trading_keys`)
- Base URL: `https://apiconnect.angelone.in`

---

## 9. Data Model

### 9.1 Hourly record (`strategy_hourly_data` table)

| Column | Type | Description |
|--------|------|-------------|
| `strategy_name` | TEXT | Strategy identifier |
| `collection_timestamp` | TEXT | ISO 8601 timestamp |
| `expiry_date` | TEXT | Expiry in `YYYYMMDD` |
| `nifty_open` | REAL | NIFTY50 day open |
| `nifty_ltp` | REAL | NIFTY50 current LTP |
| `nifty_previous_close` | REAL | NIFTY50 previous close |
| `put_strike` | INTEGER | PUT strike price |
| `put_ltp` | REAL | PUT option LTP |
| `call_strike` | INTEGER | CALL strike price |
| `call_ltp` | REAL | CALL option LTP |
| `call_buy_price` | REAL | Tuesday CALL buy price (carried through week) |
| `put_buy_price` | REAL | Tuesday PUT buy price (carried through week) |
| `source` | TEXT | Always `"angelone"` |
| `cycle_id` | TEXT | Unique cycle identifier |

### 9.2 Buy snapshot table (`strategy_buy_snapshots`)

| Column | Type | Description |
|--------|------|-------------|
| `strategy_name` | TEXT | Strategy identifier |
| `cycle_id` | TEXT | Unique cycle identifier |
| `week_start_date` | TEXT | Tuesday date in `YYYYMMDD` |
| `expiry_date` | TEXT | Expiry date in `YYYYMMDD` |
| `call_strike` | INTEGER | CALL strike |
| `put_strike` | INTEGER | PUT strike |
| `call_buy_price` | REAL | CALL buy price at capture |
| `put_buy_price` | REAL | PUT buy price at capture |
| `captured_at` | TEXT | ISO 8601 capture timestamp |

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
- [x] Hourly interval collection
- [x] Tuesday buy price capture
- [x] SQLite + JSON persistence
- [x] Automated snapshot archival on Tuesday rollover
- [x] Scenario 1 support (auto-create snapshot mid-Tuesday)
- [x] Scenario 2 support (respect manually-created snapshot)

### Phase 3: Validation and monitoring (PENDING)

- [ ] Verify collection runs on Tuesday
- [ ] Verify correct expiry selected
- [ ] Verify PUT/CALL strikes logged correctly
- [ ] Verify hourly data from 9:30 AM onward
- [ ] Verify JSON snapshot stability through the week

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

# Cron (every hour during market hours)
30 4-10 * * 1-5 cd /path/to/nifty50_strategies && python strategies/weekly_option_collector.py
```

---

## 13. Expected Output

- Clean dataset containing NIFTY50 spot and option information,
- Weekly time-series records for analysis,
- Stored Tuesday buy prices for CALL and PUT,
- Base foundation for future strategy development.

---

## 14. Design Decisions (from implementation)

| Decision | Rationale |
|----------|-----------|
| No `smartapi-python` SDK | Reuses existing direct HTTP + Redis JWT pattern from `check_active_positions.py` |
| Synchronous code | Hourly cron-triggered; no need for async |
| Simplified single-cycle model | Eliminates double-collection complexity on rollover Tuesday |
| Two-scenario Tuesday handling | Supports both fresh auto-start and manual snapshot injection |
| SQLite per weekly cycle | Clear separation, sortable filenames, append-only per cycle |
