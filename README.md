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

- Automated weekly option data collection (NIFTY50 spot + option chains)
- Tuesday dynamic AI strike selection and static benchmark tracking
- Parallel 3-Day Thursday Strategy Track (Thursday–Monday cycle tracking fresh strikes locked on Thursday open, running alongside the Tuesday master cycle)
- Price collection every 30 minutes from 09:31 AM to 15:31 PM IST
- Smart Entry Gate timing (volatility, momentum, and technical filters with counterfactual shadow logging)
- Decoupled FSM Strategy Engine (trailing stop ladders, profit harvest, salvage stops)
- Monday Gamma Sniper (intraday simulated scalping with targets, SL, and time-stops)
- Common data source via Angel One SmartAPI with Redis JWT auth
- Persistent storage in SQLite (per-cycle DBs) and MongoDB Atlas synchronization
- Automated post-market weekly close orchestration and HTML/chart reporting

### Out of scope

- Real-money order placement to broker (all execution is simulated / paper / shadow)
- Real-time order execution engine for external brokerage accounts
- Live broker margin / funds management

---

## 3. Project Structure

```
nifty50_strategies/
├── .env.example                         # Template for credentials & config
├── .gitignore
├── requirements.txt                     # Runtime dependencies
├── config.py                            # Central config, Redis factory, constants
├── common/
│   ├── __init__.py
│   ├── angelone_client.py               # SmartAPI auth + market data fetchers
│   ├── expiry.py                        # Expiry resolution + static strike selector
│   ├── ai_strike_selector.py            # Dynamic AI strike selector (VIX, ATR, Greeks)
│   ├── calendar_utils.py                # Holiday detection via Upstox API & cycle calendar
│   ├── entry_gate.py                    # Smart Entry Gate (deferral & shadow logging)
│   ├── fsm_strategy.py                  # Decoupled FSM (harvest, trailing stop, salvage)
│   ├── profit_monitor.py                # Peak-profit alert engine & future broker execution seam
│   └── storage.py                       # SQLite persistence, JSON snapshot & migrations
├── strategies/
│   ├── __init__.py
│   ├── weekly_option_collector.py       # Master weekly strategy entry point (runs :01 & :31)
│   ├── thursday_option_collector.py     # Standalone Thursday-to-Monday collector (runs :02 & :32)
│   ├── monday_gamma_sniper.py           # Monday afternoon gamma scalper (runs every 10m)
│   └── peak_profit_monitor.py           # Standalone 15-min peak monitor (runs :16/:46 & :17/:47)
├── scripts/
│   ├── run_weekly_close.py              # Master weekly close orchestrator (runs Mon 15:37)
│   ├── run_thursday_close.py            # Thursday cycle close orchestrator (runs Mon 15:38)
│   ├── send_weekly_report.py            # Monday HTML email + chart generator (supports --track)
│   ├── compare_ai_vs_static_benchmark.py # Retrospective side-by-side P&L comparison
│   └── sync_to_mongodb.py               # MongoDB Atlas derivatives synchronization CLI (supports --track)
├── tests/                               # 74 automated pytest unit tests
│   ├── test_angelone_resolution.py
│   ├── test_calendar_utils.py
│   ├── test_entry_gate.py
│   ├── test_fsm_strategy.py
│   ├── test_gamma_sniper.py
│   ├── test_profit_monitor.py
│   ├── test_standalone_peak_monitor.py
│   ├── test_storage.py
│   ├── test_thursday_strategy.py
│   ├── test_weekly_collector.py
│   └── test_weekly_report.py
└── cron/
    └── nifty50_weekly_report.cron       # Legacy reference (orchestrated by run_weekly_close)
```

### Module descriptions

| Module | Purpose |
|---|---|
| `config.py` | Loads `.env`, Redis client factory, constants (strike step=100, lot size=65, NIFTY token, Angel One base URL, storage paths, Thursday strategy constants) |
| `common/angelone_client.py` | JWT auth from Redis `angelone_jwt_feed`, NIFTY spot LTP, daily instrument-master option lookup, and `get_nifty_option_chain()` for CALL+PUT LTPs |
| `common/expiry.py` | `get_next_weekly_expiry()`, `is_tuesday()`, `format_expiry_angelone()`, `format_expiry_file()`, `strike_selector()` (static 100-pt grid) |
| `common/ai_strike_selector.py` | AI Strike Selector: optimizes strikes on standard 50-pt grid using VIX, ATR, Greeks ($\Delta \approx 0.35$), and IV skew via OpenRouter with asymmetric technical fallback |
| `common/calendar_utils.py` | NSE holiday calendar resolution via Upstox API with in-process caching, strategy start/closing day detection (Tuesday master & Thursday 3-day), and Monday-holiday deferred close handling |
| `common/entry_gate.py` | Smart Entry Gate: checks trend, ATR, and momentum filters at cycle open to gate entry; writes counterfactual shadow records on deferral |
| `common/fsm_strategy.py` | Decoupled Finite State Machine trade manager: tracks trailing stop ladders by days-to-expiry (25% buffer for >5 DTE, 18% for 1.5–5 DTE), harvest rules (+50% leg profit), and capital salvage stop rules |
| `common/profit_monitor.py` | Peak-profit alert engine & future broker execution seam; parameterized with `snapshot_filepath` and `track_label` for track-isolated high-water marks |
| `common/storage.py` | SQLite persistence layer: `init_db()`, `build_thursday_db_path()`, auto-migrations for integer timestamps and FSM columns, trade updating, and active/archive JSON snapshot management (`filepath` parameterized) |
| `strategies/weekly_option_collector.py` | Cron-invoked master collector (:01 & :31): handles Tuesday cycle initialization, Smart Gate evaluation, option LTP capture, FSM state updates, and counterfactual logging |
| `strategies/thursday_option_collector.py` | Cron-invoked standalone Thursday collector (:02 & :32): manages parallel 3-day strangle (Thu–Mon), evaluates Smart Gate, selects strikes, updates 18% trailing stop FSM, and logs to `current_thursday_buy.json` |
| `strategies/monday_gamma_sniper.py` | Monday afternoon gamma scalper (13:15–15:00 IST): evaluates 5-min VWAP and volume surges ($\ge 1.5\times$) with target (+75%), stop-loss (-35%), and 15:10 IST time-stop |
| `strategies/peak_profit_monitor.py` | Standalone 15-minute peak monitor (runs :16 & :46 for weekly; :17 & :47 for thursday via `--track thursday`): checks floating strangle profits against high-water marks |
| `scripts/run_weekly_close.py` | Unified master weekly orchestrator (Mon 15:37 IST / Tue 09:07 deferred): closes active cycle upfront, runs report email, benchmark scoring, SQLite merge, and skill generation |
| `scripts/run_thursday_close.py` | Dedicated Thursday cycle close orchestrator (Mon 15:38 IST / Tue 09:08 deferred): marks Thursday snapshot closed, sends Thursday report email, and syncs to MongoDB Atlas (skips LLM skill generation) |
| `scripts/compare_ai_vs_static_benchmark.py` | Reconstructs static $\pm 100$ strike LTPs from `signals_data_*.db` `option_chain_surface` to compute side-by-side outperformance delta vs AI strategy |
| `scripts/send_weekly_report.py` | Monday HTML email + chart generator; parameterized with `--track weekly|thursday` (uses fixed 65-share lot size) |
| `scripts/sync_to_mongodb.py` | CLI tool to push weekly strategy dossiers (`derivative_strategies`) and recompute the living KPI rollup (`derivative_master`) in MongoDB Atlas; supports `--track` and auto-detection |

---

## 4. Weekly Data Collection Logic (Final)

### 4.1 Simplified cycle model

The original plan (section 4.4) proposed a "double collection" model where the old and new cycles ran in parallel on the second Tuesday. That proved too complex. The simplified model is:

- **Data collection ends on Monday.** The previous weekly cycle is complete.
- **Fresh collection starts every Tuesday** for the next weekly expiry.
- **No parallel (double) collection** on Tuesday — a clean break between cycles.

### 4.2 Collection schedule

- Every Tuesday at 09:31 AM IST, a fresh weekly cycle begins targeting the **next** weekly expiry.
- Data is collected every 30 minutes from 09:31 AM to 15:31 PM IST, Tuesday through Monday.
- The cycle ends on Monday; the next Tuesday starts a new cycle.

### 4.3 Example: Aug 4 through Aug 11

| Date     | Day     | Active cycle                 | Snapshot                                                                                   |
| -------- | ------- | ---------------------------- | ------------------------------------------------------------------------------------------ |
| 4 Aug    | Tue     | New: expiry Aug 11           | `current_week_buy.json` (4th Aug prices)                                                   |
| 5-10 Aug | Wed-Mon | Same cycle continues         | Unchanged                                                                                  |
| 11 Aug   | Tue     | Old ends; new: expiry Aug 18 | Snapshot archived as `current_week_buy_20260804.json`; new `current_week_buy.json` written |

### 4.4 Mid-Tuesday start (Scenario 1)

If the script is started mid-Tuesday (missing the 9:31 AM trigger) with **no** pre-existing `current_week_buy.json`:

- The code creates the snapshot automatically using the current option LTPs as buy prices.
- No manual intervention is required — the script handles it gracefully.
- The `week_start_date` in the snapshot reflects the actual Tuesday date.

### 4.5 Manual snapshot (Scenario 2)

If `current_week_buy.json` is manually created with correct Tuesday 9:31 AM
prices before the script runs:

- The code detects that the snapshot covers the currently active Tuesday-Monday cycle.
- It reuses that snapshot's expiry, CALL/PUT strikes, and buy prices instead of
  recalculating them from later daily opens.
- This is useful when the first few triggers are missed and accurate 9:31 AM prices are known.

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

On Tuesday at 09:31 AM IST, `weekly_option_collector.py` invokes [`common/ai_strike_selector.py`](file:///c:/Users/sai-s/Documents/GitHub/nifty50_strategies/common/ai_strike_selector.py):
1. **Dynamic Greeks & Volatility Ingestion**: Ingests live India VIX, ATR(14) daily range, Bollinger Band Width %, and option chain Greeks ($\Delta \approx 0.35, \Theta, \text{IV Skew}$) from SQLite.
2. **AI Strike Calibration**: Queries OpenRouter LLMs via Redis `finance_llm_models` to select volatility-adaptive strikes on a standard 50-point NIFTY strike grid (e.g. widening in high VIX, balancing Put IV skew).
3. **Fail-Safe Fallback**: If OpenRouter times out (>5s) or returns invalid output, the system falls back to the deterministic **directional asymmetric rule** (`ASYM_FALLBACK_{bias}`: Bullish $\rightarrow$ +50 CE / -150 PE, Bearish $\rightarrow$ +150 CE / -50 PE, Neutral $\rightarrow$ +100 CE / -100 PE). The symmetric static $\pm 100$ strike benchmark is applied only if `--force-static` is specified or when no `OPENROUTER_API_KEY` is present.
4. **Snapshot Persistence**: Saves both the AI strikes and the static benchmark strikes in `current_week_buy.json`:
   ```json
   {
     "strategy_name": "nifty50_weekly_option_collector",
     "cycle_id": "20260901-a1b2c3d4",
     "status": "ongoing",
     "week_start_date": "20260901",
     "expiry_date": "20260908",
     "call_strike": 24650,
     "put_strike": 24400,
     "call_buy_price": 128.5,
     "put_buy_price": 112.0,
     "captured_at": 1788244200,
     "static_call_strike": 24600,
     "static_put_strike": 24400,
     "composite_direction_score": 0.42,
     "directional_bias": "BULLISH",
     "target_call_delta": 0.35,
     "target_put_delta": -0.30,
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

### 4.7 Weekly Closed-Loop Lifecycle & Holiday Adaptation

```
                    WEEKLY LIFECYCLE WITH HOLIDAY RESOLUTION
                    
  Strategy Start Day     nifty50_strategies evaluates Smart Gate & selects AI Strikes
  (Tue 09:31 IST,        • Captures buy prices into current_week_buy.json (status: ongoing)
  or Wed if Tue Hol):    • Evaluated 3 mins later at 09:34 IST by sentinel-hermes
                                          │
                                          ▼
  Market Trading Days    • Collector (:01 & :31) saves option LTPs & updates FSM states
  (Every 30 mins):       • Inference runner (:04 & :34) evaluates P&L, stops & Greeks
                         • Skips automatically on exchange holidays
                                          │
                                          ▼
  Strategy Closing Day   nifty50_strategies scripts/run_weekly_close.py orchestrates:
  • Normal Monday:       1. Marks active cycle status: "closed" upfront
    15:37 IST (10:07 UTC)2. send_weekly_report.py (HTML email & chart report)
  • Monday Holiday:      3. compare_ai_vs_static_benchmark.py (AI vs Static scoring)
    Deferred to Tuesday  4. sentinel-hermes/run_weekly_merge.sh (builds merged SQLite)
    09:07 IST (03:37 UTC)5. sentinel-hermes/skill_generator.py (synthesizes skill & Mongo sync)
                                          │
                                          ▼
  Dynamic Injection:     inference_runner.py automatically reads the newly synthesized
                         skill on cycle start, continuously compounding learned rules!
```

### 4.8 Parallel 3-Day Thursday Strategy Track (September 2026+)

Running concurrently with the 5-day Tuesday-to-Monday master cycle, the project includes a standalone **Parallel 3-Day Thursday Strategy Track** (`strategies/thursday_option_collector.py`):

#### 1. Strategic Thesis & Rationale
- **Targeting Shorter Expiry Horizon**: Initiating long strangles on Thursday morning (~5.0 Days to Expiry, DTE) targets accelerated theta decay and explosive gamma convexity heading into Monday afternoon.
- **Fresh Strike Calibration**: Striking decisions on Thursday morning use the updated Thursday spot LTP rather than holding strikes locked on Tuesday morning. This prevents the position from becoming deeply out-of-the-money if the market made a large trending move on Tuesday or Wednesday.
- **Zero Disturbance to Master Track**: The Thursday strategy runs completely independently from the master Tuesday strategy with segregated databases, dedicated JSON snapshots, isolated FSM state, and dedicated report dispatchers.

#### 2. Thursday Closed-Loop Lifecycle

```
                    PARALLEL 3-DAY THURSDAY LIFECYCLE
                    
  Thursday 09:31 IST     thursday_option_collector.py evaluates Smart Gate & selects Strikes
  (or Fri if Thu Hol):   • Captures buy prices into current_thursday_buy.json (status: ongoing)
                         • Evaluated 3 mins later at 09:35 IST by sentinel-hermes (--track thursday)
                                           │
                                           ▼
  Active Trading Days    • Collector (:02 & :32) saves option LTPs & updates 18% FSM states
  (Thu, Fri, Mon):       • Standalone peak monitor (:17 & :47) checks high-water mark profits
                         • Inference runner (:05 & :35) evaluates P&L, stops & Greeks
                         • Skips automatically on exchange holidays
                                           │
                                           ▼
  Cycle Closing Day      nifty50_strategies scripts/run_thursday_close.py orchestrates:
  • Normal Monday:       1. Marks active cycle status: "closed" in current_thursday_buy.json
    15:38 IST (10:08 UTC)2. send_weekly_report.py --track thursday (HTML email & chart report)
  • Monday Holiday:      3. sync_to_mongodb.py --track thursday (MongoDB Atlas sync)
    Deferred to Tuesday  4. Intentionally skips LLM skill generation (per design specification)
    09:08 IST (03:38 UTC)
```

#### 3. Core Isolation Guarantees

| Component | Master Weekly Track (Tuesday Start) | Parallel 3-Day Track (Thursday Start) |
|---|---|---|
| **Strategy Identifier** | `nifty50_weekly_option_collector` | `nifty50_thursday_3day_collector` |
| **Active Cycle Snapshot** | `current_week_buy.json` | `current_thursday_buy.json` |
| **Archived Snapshot** | `current_week_buy_{YYYYMMDD}.json` | `current_thursday_buy_{YYYYMMDD}.json` |
| **SQLite Database** | `nifty50_weekly_data_{start}_{expiry}.db` | `nifty50_thursday_data_{start}_{expiry}.db` |
| **Collection Collector** | `strategies/weekly_option_collector.py` (:01, :31) | `strategies/thursday_option_collector.py` (:02, :32) |
| **Peak-Profit Monitor** | `strategies/peak_profit_monitor.py` (:16, :46) | `strategies/peak_profit_monitor.py --track thursday` (:17, :47) |
| **Cycle Close Script** | `scripts/run_weekly_close.py` (Mon 15:37 IST) | `scripts/run_thursday_close.py` (Mon 15:38 IST) |
| **Email Report Dispatch** | `scripts/send_weekly_report.py` | `scripts/send_weekly_report.py --track thursday` |
| **MongoDB Atlas Document**| `_id: NIFTY50_{start}_{expiry}` | `_id: NIFTY50_THU_{start}_{expiry}` |
| **MongoDB Master Rollup** | Updates `derivative_master` living KPIs | **Excluded** from updating `derivative_master` |
| **LLM Skill Generation** | Generates `skills/skill_*.md` via Claude Sonnet | **Skipped** (bypasses skill generation) |

#### 4. Decoupled FSM Trailing Stop Adaptation
Because the Thursday strangle begins at $\approx 5.0$ DTE, [`common/fsm_strategy.py`](file:///c:/Users/sai-s/Documents/GitHub/nifty50_strategies/common/fsm_strategy.py) automatically places entry directly into the **18% trailing buffer bracket** (covering 1.5–5.0 DTE). This tighter buffer is economically optimized for shorter-duration options experiencing rapid theta decay, compared to the looser 25% buffer applied to Tuesday entries (>5 DTE).

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

- **AI Strike Selection Mode**: Operates on the standard **50-point NIFTY strike grid** (`ai_strike_selector.py` prompt rule 1), allowing precise Delta centering ($\Delta \approx 0.35$) and IV skew balancing.
- **Static Benchmark / Fallback Mode**: Uses **100-point strike increments** (`STRIKE_STEP = 100` in `config.py` and `expiry.py`) for the deterministic $\pm 100$ anchor comparison.

### 6.2 Static Algorithm (Benchmark & Fallback)

```python
anchor = floor(first_trigger_ltp / 100) * 100
put_strike  = anchor - 100  # one step below
call_strike = anchor + 100  # one step above
```

### 6.3 Example

First-trigger LTP = 25000:
- Static benchmark: PUT = 24900, CALL = 25100.
- AI selection (e.g. bullish tilt with high put IV skew): PUT = 24850, CALL = 25150.

---

## 7. Collection Frequency

### 7.1 Master Weekly Track (Tuesday to Monday)
- **Start time**: 09:31 AM IST (04:01 UTC) on Tuesday
- **Interval**: Every 30 minutes (:01 and :31 past every hour)
- **Duration**: Tuesday through Monday during market hours (09:31 AM - 15:31 PM IST)

```bash
# Master Weekly Collector (:01 and :31 past every hour from 09:31 to 15:31 IST)
1 4-10 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/weekly_option_collector.py >> /home/ubuntu/logs/options_strategy_$(date +\%F).log 2>&1
31 4-9 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/weekly_option_collector.py >> /home/ubuntu/logs/options_strategy_$(date +\%F).log 2>&1
```

(Fires at 04:01, 04:31, 05:01, ..., 09:31, 10:01 UTC = every 30 min from 09:31 to 15:31 IST, followed 3 minutes later by `sentinel-hermes` inference runner.)

### 7.2 Parallel 3-Day Track (Thursday to Monday)
- **Start time**: 09:32 AM IST (04:02 UTC) on Thursday
- **Interval**: Every 30 minutes (:02 and :32 past every hour)
- **Duration**: Thursday, Friday, and Monday during market hours (09:32 AM - 15:32 PM IST)
- **Stagger**: Runs +1 minute after the master weekly collector (:01/:31) to eliminate resource/network contention.

```bash
# Thursday 3-Day Collector (:02 and :32 past every hour from 09:32 to 15:32 IST on Mon, Thu, Fri)
2,32 4-10 * * 1,4,5 cd ~/nifty50_strategies && .venv/bin/python strategies/thursday_option_collector.py >> /home/ubuntu/logs/thursday_strategy_$(date +\%F).log 2>&1
```

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
| `gainloss`             | REAL    | Combined benchmark P&L: `(CALL - buy) + (PUT - buy)`  |
| `fsm_state`            | TEXT    | FSM state: `ACTIVE`, `CALL_HARVESTED`, `CLOSED`, etc.  |
| `fsm_call_status`      | TEXT    | CALL leg state: `ACTIVE`, `HARVESTED`, `STOPPED`      |
| `fsm_put_status`       | TEXT    | PUT leg state: `ACTIVE`, `HARVESTED`, `STOPPED`       |
| `fsm_call_exit_price`  | REAL    | Locked exit LTP for CALL leg (NULL if active)         |
| `fsm_put_exit_price`   | REAL    | Locked exit LTP for PUT leg (NULL if active)          |
| `fsm_realized_pnl`     | REAL    | Realized profit points locked from closed legs        |
| `fsm_unrealized_pnl`   | REAL    | Floating mark-to-market profit points on active legs  |
| `fsm_total_gainloss`   | REAL    | Net strategy P&L points: realized + unrealized        |
| `source`               | TEXT    | Always `"angelone"`                                   |
| `cycle_id`             | TEXT    | Unique cycle identifier                               |

### 9.2 Buy snapshot table (`strategy_buy_snapshots`)

| Column                      | Type    | Description                                       |
| --------------------------- | ------- | ------------------------------------------------- |
| `strategy_name`             | TEXT    | Strategy identifier                               |
| `cycle_id`                  | TEXT    | Unique cycle identifier                           |
| `week_start_date`           | TEXT    | Tuesday date in `YYYYMMDD`                        |
| `expiry_date`               | TEXT    | Expiry date in `YYYYMMDD`                         |
| `call_strike`               | INTEGER | CALL strike                                       |
| `put_strike`                | INTEGER | PUT strike                                        |
| `call_buy_price`            | REAL    | CALL buy price at capture                         |
| `put_buy_price`             | REAL    | PUT buy price at capture                          |
| `captured_at`               | INTEGER | Unix timestamp (seconds since epoch, UTC)         |
| `composite_direction_score` | REAL    | Directional score from technical/macro indicators |
| `directional_bias`          | TEXT    | Directional classification: BULLISH, BEARISH, etc.|
| `target_call_delta`         | REAL    | Target Call Delta calibrated at entry (~0.35)     |
| `target_put_delta`          | REAL    | Target Put Delta calibrated at entry (~-0.35)    |

### 9.3 Monday Gamma Sniper trades (`gamma_sniper_trades` table)

| Column               | Type    | Description                                             |
| -------------------- | ------- | ------------------------------------------------------- |
| `id`                 | INTEGER | Primary key                                             |
| `trade_timestamp`    | INTEGER | Entry Unix timestamp (seconds since epoch, UTC)         |
| `expiry_date`        | TEXT    | Expiry date in `YYYYMMDD`                               |
| `nifty_spot`         | REAL    | NIFTY spot LTP at execution                             |
| `option_type`        | TEXT    | `CE` or `PE`                                            |
| `strike`             | INTEGER | Scalp strike price                                      |
| `entry_price`        | REAL    | Entry option premium LTP                                |
| `exit_price`         | REAL    | Exit option premium LTP (NULL if open)                  |
| `exit_timestamp`     | INTEGER | Exit Unix timestamp (NULL if open)                      |
| `pnl_points`         | REAL    | P&L points per share: `exit_price - entry_price`        |
| `pnl_pct`            | REAL    | P&L percentage gain/loss                                |
| `pnl_inr`            | REAL    | Net INR gain/loss: `pnl_points * lot_size`              |
| `exit_reason`        | TEXT    | Reason: `TARGET_HIT (+75%)`, `STOP_LOSS_HIT (-35%)`, etc|
| `allocated_risk_inr` | REAL    | House money allocated for trade risk                    |
| `status`             | TEXT    | Trade status: `OPEN` or `CLOSED`                        |

### 9.4 Smart Gate deferred counterfactuals (`gate_deferred_shadows` table)

| Column                      | Type    | Description                                             |
| --------------------------- | ------- | ------------------------------------------------------- |
| `id`                        | INTEGER | Primary key                                             |
| `collection_timestamp`      | INTEGER | Trigger Unix timestamp (seconds since epoch, UTC)       |
| `expiry_date`               | TEXT    | Expiry date in `YYYYMMDD`                               |
| `nifty_ltp`                 | REAL    | NIFTY spot LTP at evaluation                            |
| `hypothetical_call_strike`  | INTEGER | AI-recommended CALL strike had entry proceeded          |
| `hypothetical_put_strike`   | INTEGER | AI-recommended PUT strike had entry proceeded           |
| `call_ltp`                  | REAL    | Live market CALL LTP at evaluation                      |
| `put_ltp`                   | REAL    | Live market PUT LTP at evaluation                       |
| `gate_score`                | REAL    | Smart Gate composite evaluation score                   |
| `gate_reason`               | TEXT    | Deferral rationale (e.g. high ATR expansion)            |
| `composite_direction_score` | REAL    | Directional score                                       |
| `directional_bias`          | TEXT    | Bias classification                                     |
| `target_call_delta`         | REAL    | Target Call Delta                                       |
| `target_put_delta`          | REAL    | Target Put Delta                                        |


---

## 10. Storage Plan

### 10.1 SQLite databases

- **Master Weekly Cycle (Tuesday Start)**:
  - Path: `/home/ubuntu/sqlite/strategies/nifty50_weekly_data_{YYYYMMDD}_{expiry}.db`
  - Example: `nifty50_weekly_data_20260901_20260908.db`
- **Parallel 3-Day Cycle (Thursday Start)**:
  - Path: `/home/ubuntu/sqlite/strategies/nifty50_thursday_data_{YYYYMMDD}_{expiry}.db`
  - Example: `nifty50_thursday_data_20260903_20260908.db`

### 10.2 JSON snapshots

- **Master Weekly Cycle**:
  - Active: `/home/ubuntu/sqlite/strategies/current_week_buy.json`
  - Archived: `/home/ubuntu/sqlite/strategies/current_week_buy_{YYYYMMDD}.json` (one per past week)
- **Parallel 3-Day Thursday Cycle**:
  - Active: `/home/ubuntu/sqlite/strategies/current_thursday_buy.json`
  - Archived: `/home/ubuntu/sqlite/strategies/current_thursday_buy_{YYYYMMDD}.json` (one per past week)

The active snapshot persists the current cycle's entry buy prices, FSM state (`alpha_fsm`), and the peak profit high-water mark state (`peak_profit`):
```json
"peak_profit": {
  "peak_pnl_pts": 45.2,
  "peak_pnl_pct": 28.5,
  "peak_pnl_inr": 2938.0,
  "peak_timestamp": 1788256800,
  "last_notified_pts": 45.2,
  "last_notified_pct": 28.5,
  "last_notified_timestamp": 1788256800,
  "alerts_sent_today": 1,
  "last_alert_date": "2026-09-22"
}
```

### 10.3 Storage rules

- One row per collection interval
- One database file per strategy cycle
- Complete physical isolation between Tuesday master and Thursday 3-day databases
- SQLite is the authoritative store; the JSON snapshot is a convenience file for the active cycle
- Zero SQLite schema modifications for peak monitoring: high-water marks persist in-place inside `current_week_buy.json` (weekly) or `current_thursday_buy.json` (thursday)

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

#### 1. Master Weekly Option Collector (`strategies/weekly_option_collector.py`)

```bash
# Manual single collection
python strategies/weekly_option_collector.py

# Dry run (no API calls)
python strategies/weekly_option_collector.py --dry-run

# Force run outside market hours
python strategies/weekly_option_collector.py --force

# Force static +/-100 benchmark strikes (bypasses OpenRouter AI selector)
python strategies/weekly_option_collector.py --force-static

# Force immediate strangle entry (bypasses Smart Entry Gate deferral)
python strategies/weekly_option_collector.py --force-entry
```

#### 2. Parallel 3-Day Thursday Option Collector (`strategies/thursday_option_collector.py`)

```bash
# Manual single collection (Thursday to Monday cycle)
python strategies/thursday_option_collector.py

# Dry run (simulates collection with mock quotes)
python strategies/thursday_option_collector.py --dry-run

# Force run outside Thursday-Monday market hours
python strategies/thursday_option_collector.py --force

# Force static +/-100 benchmark strikes
python strategies/thursday_option_collector.py --force-static

# Force immediate strangle entry (bypasses Smart Entry Gate deferral)
python strategies/thursday_option_collector.py --force-entry
```

#### 3. Monday Gamma Sniper (`strategies/monday_gamma_sniper.py`)

```bash
# Check status and eligibility without trading
python strategies/monday_gamma_sniper.py --check

# Dry-run evaluation (simulated prices)
python strategies/monday_gamma_sniper.py --dry-run

# Force execution (bypasses time window and zero-house-money guard)
python strategies/monday_gamma_sniper.py --force
```

#### 4. Master Weekly Close Orchestrator (`scripts/run_weekly_close.py`)

```bash
# Execute weekly close orchestrator (closes active cycle upfront, then steps 1-4)
python scripts/run_weekly_close.py

# Dry-run orchestrator (validates steps without closing cycle or modifying DBs)
python scripts/run_weekly_close.py --dry-run

# Force close outside scheduled closing window
python scripts/run_weekly_close.py --force

# Target specific cycle by start date
python scripts/run_weekly_close.py --date 20260908
```

> [!NOTE]
> `run_weekly_close.py` steps 3 & 4 invoke `sentinel-hermes/run_weekly_merge.sh` and `sentinel-hermes/skill_generator.py`. This requires `sentinel-hermes` to exist as a sibling checkout at `~/sentinel-hermes` or the `SENTINEL_HERMES_DIR` environment variable to be configured.

#### 5. Thursday Strategy Close Orchestrator (`scripts/run_thursday_close.py`)

```bash
# Execute Thursday close orchestrator (closes snapshot, sends email, syncs to MongoDB Atlas)
python scripts/run_thursday_close.py

# Dry-run orchestrator (validates steps without closing cycle or sending email)
python scripts/run_thursday_close.py --dry-run

# Force close outside Monday 15:30-16:30 IST window
python scripts/run_thursday_close.py --force

# Tuesday morning deferred holiday check (runs Tuesday 09:08 IST if Monday was holiday)
python scripts/run_thursday_close.py --morning-holiday-check
```

### Production Crontab Entries

All production crontab schedules execute in **UTC** on AWS EC2:

```cron
# ============================================================================
# MASTER WEEKLY STRATEGY TRACK (Tuesday - Monday)
# ============================================================================
# Collector: Every 30 minutes during market hours (:01 and :31 past every hour from 09:31 to 15:31 IST)
# Stagger: Allows upstream collectors (market_signal_agent & nifty_signal_features) to commit :00/:30 data.
1 4-10 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/weekly_option_collector.py >> /home/ubuntu/logs/options_strategy_$(date +\%F).log 2>&1
31 4-9 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/weekly_option_collector.py >> /home/ubuntu/logs/options_strategy_$(date +\%F).log 2>&1

# Standalone Peak-Profit Monitor (Runs :16 and :46, alternating with :01 and :31 collector for 15-min cadence)
# 09:16 to 15:16 IST = 03:46 to 09:46 UTC
46 3 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/peak_profit_monitor.py >> /home/ubuntu/logs/peak_monitor_$(date +\%F).log 2>&1
16,46 4-9 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/peak_profit_monitor.py >> /home/ubuntu/logs/peak_monitor_$(date +\%F).log 2>&1

# Monday Afternoon Gamma Sniper (Expiry Afternoon Scalp: runs every 10 min, 12:30–15:20 IST = 07:00–09:50 UTC)
*/10 7-9 * * 1 cd ~/nifty50_strategies && .venv/bin/python strategies/monday_gamma_sniper.py >> /home/ubuntu/logs/gamma_sniper_$(date +\%F).log 2>&1

# Master Weekly Close Orchestrator (Runs Mondays at 10:07 UTC = 15:37 IST; skips if already closed)
# Steps: 1. send_weekly_report.py -> 2. compare_ai_vs_static_benchmark.py -> 3. run_weekly_merge.sh -> 4. skill_generator.py + Mongo sync
7 10 * * 1 cd ~/nifty50_strategies && .venv/bin/python scripts/run_weekly_close.py >> /home/ubuntu/logs/weekly_close_$(date +\%F).log 2>&1

# Tuesday Deferred Close for Master Track (Runs Tuesday 09:07 IST = 03:37 UTC if Monday was an exchange holiday)
37 3 * * 2 cd ~/nifty50_strategies && .venv/bin/python scripts/run_weekly_close.py >> /home/ubuntu/logs/weekly_close_$(date +\%F).log 2>&1

# ============================================================================
# PARALLEL 3-DAY THURSDAY STRATEGY TRACK (Thursday - Monday)
# ============================================================================
# Thursday Collector: Runs :02 and :32 past every hour (09:32 to 15:32 IST = 04:02 to 10:02 UTC on Mon, Thu, Fri)
# Stagger: +1 min after weekly collector (:01/:31) for clean lock and API separation.
2,32 4-10 * * 1,4,5 cd ~/nifty50_strategies && .venv/bin/python strategies/thursday_option_collector.py >> /home/ubuntu/logs/thursday_strategy_$(date +\%F).log 2>&1

# Thursday Peak-Profit Monitor: Runs :17 and :47 (09:17 to 15:17 IST = 03:47 to 09:47 UTC on Mon, Thu, Fri)
47 3 * * 1,4,5 cd ~/nifty50_strategies && .venv/bin/python strategies/peak_profit_monitor.py --track thursday >> /home/ubuntu/logs/thursday_peak_monitor_$(date +\%F).log 2>&1
17,47 4-9 * * 1,4,5 cd ~/nifty50_strategies && .venv/bin/python strategies/peak_profit_monitor.py --track thursday >> /home/ubuntu/logs/thursday_peak_monitor_$(date +\%F).log 2>&1

# Thursday Strategy Close Orchestrator (Runs Mondays at 10:08 UTC = 15:38 IST; +1m after master close)
# Steps: 1. Closes current_thursday_buy.json -> 2. send_weekly_report.py --track thursday -> 3. sync_to_mongodb.py --track thursday
8 10 * * 1 cd ~/nifty50_strategies && .venv/bin/python scripts/run_thursday_close.py >> /home/ubuntu/logs/thursday_close_$(date +\%F).log 2>&1

# Tuesday Deferred Close for Thursday Track (Runs Tuesday 09:08 IST = 03:38 UTC if Monday was an exchange holiday)
38 3 * * 2 cd ~/nifty50_strategies && .venv/bin/python scripts/run_thursday_close.py --morning-holiday-check >> /home/ubuntu/logs/thursday_close_$(date +\%F).log 2>&1
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
# Master weekly preview (reads latest nifty50_weekly_data_*.db)
python scripts/send_weekly_report.py --dry-run

# Thursday 3-day preview (reads latest nifty50_thursday_data_*.db)
python scripts/send_weekly_report.py --track thursday --dry-run
```

Send immediately:

```bash
# Send master weekly report
python scripts/send_weekly_report.py

# Send Thursday 3-day report
python scripts/send_weekly_report.py --track thursday
```

The preview HTML and PNG chart are archived under
`/home/ubuntu/sqlite/strategies/reports/` by default (`nifty50_report_*` for weekly, `nifty50_thursday_report_*` for Thursday).

### Real-Time Peak-Profit Discord Alerts & Future Broker Execution Seam

The strategy monitors floating strangle profit against the historical high-water mark achieved since entry order locking via [`common/profit_monitor.py`](file:///c:/Users/sai-s/Documents/GitHub/nifty50_strategies/common/profit_monitor.py):

- **Dual-Track & Dual-Runner 15-Minute Cadence**: 
  - **Master Weekly**: `weekly_option_collector.py` evaluates peaks at `:01` and `:31`; `peak_profit_monitor.py` evaluates at `:16` and `:46` using `current_week_buy.json`.
  - **Thursday 3-Day Track**: `thursday_option_collector.py` evaluates peaks at `:02` and `:32`; `peak_profit_monitor.py --track thursday` evaluates at `:17` and `:47` using `current_thursday_buy.json`.
- **$\ge 20.0\%$ Profit Validation**: Alerts strictly trigger only when profit is $\ge 20.0\%$ (`combined_pnl_pct >= 20.0`). Any minor fluctuations below 20% update the snapshot high-water mark but send **zero alerts**.
- **Anti-Spam Hysteresis**: Subsequent peak alerts require at least a **$+5.0\%$ jump** or **$+10.0$ points** ($\sim ₹650$/lot) above the last notified peak.
- **Zero SQLite Changes & Atomic Writes**: Peak state is stored in-place directly in `current_week_buy.json` (weekly) or `current_thursday_buy.json` (thursday) under `"peak_profit"` via atomic temporary file replacement (`os.replace`). SQLite schema remains 100% untouched.
- **Future Live Broker Hook**: Qualifying new peaks trigger `common.profit_monitor.on_new_peak(event)`. Currently, this dispatches a high-priority styled Discord alert via Webhook; in future phases, live broker order execution (Angel One / Upstox) can be connected directly into this seam.

```env
PEAK_ALERT_ENABLED=true
PEAK_ALERT_MIN_PROFIT_PCT=20.0
PEAK_ALERT_HYSTERESIS_PCT=5.0
PEAK_ALERT_HYSTERESIS_PTS=10.0
DISCORD_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_URL_HERE
```

#### Standalone Peak Monitor Crontab

```bash
# Master Weekly Peak Monitor (Runs at :16 and :46; 09:16 to 15:16 IST = 03:46 to 09:46 UTC on Mon-Fri)
46 3 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/peak_profit_monitor.py >> /home/ubuntu/logs/peak_monitor_$(date +\%F).log 2>&1
16,46 4-9 * * 1-5 cd ~/nifty50_strategies && .venv/bin/python strategies/peak_profit_monitor.py >> /home/ubuntu/logs/peak_monitor_$(date +\%F).log 2>&1

# Thursday 3-Day Peak Monitor (Runs at :17 and :47; 09:17 to 15:17 IST = 03:47 to 09:47 UTC on Mon, Thu, Fri)
47 3 * * 1,4,5 cd ~/nifty50_strategies && .venv/bin/python strategies/peak_profit_monitor.py --track thursday >> /home/ubuntu/logs/thursday_peak_monitor_$(date +\%F).log 2>&1
17,47 4-9 * * 1,4,5 cd ~/nifty50_strategies && .venv/bin/python strategies/peak_profit_monitor.py --track thursday >> /home/ubuntu/logs/thursday_peak_monitor_$(date +\%F).log 2>&1
```

Smoke check peak monitor state:
```bash
# Check master weekly snapshot peak state
python strategies/peak_profit_monitor.py --check

# Check Thursday snapshot peak state
python strategies/peak_profit_monitor.py --track thursday --check
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

---

## 15. MongoDB Atlas Derivatives Synchronization

Strategy results and weekly prices can be persisted directly to MongoDB Atlas (`stock_recommendations` database) in the collections:
- `derivative_strategies`: Full strategy cycle performance dossier
- `derivative_master`: Living rollup KPI for NIFTY50 options (maintained exclusively by the master 5-day cycle)

### Track Isolation & Schema Contract

| Field | Master Weekly Track | Parallel 3-Day Track |
|---|---|---|
| **Document ID (`_id`)** | `NIFTY50_{start_date}_{expiry_date}` | `NIFTY50_THU_{start_date}_{expiry_date}` |
| **Cycle ID (`cycle_id`)** | `CYCLE-NIFTY50-{start_date}-{expiry_date}` | `CYCLE-THU-{start_date}` |
| **Strategy Name** | `nifty50_weekly_option_collector` | `nifty50_thursday_3day_collector` |
| **Active Snapshot Source** | `current_week_buy.json` | `current_thursday_buy.json` |
| **`derivative_master` Rollup** | Recomputed with cycle stats | **Preserved intact** (Thursday cycles are excluded from master KPI rollups) |

### CLI Usage

```bash
# Check collection status and document counts
python scripts/sync_to_mongodb.py --status

# Sync latest master weekly strategy DB
python scripts/sync_to_mongodb.py

# Sync latest Thursday strategy DB (or pass DB path directly)
python scripts/sync_to_mongodb.py --track thursday
python scripts/sync_to_mongodb.py --db-path /home/ubuntu/sqlite/strategies/nifty50_thursday_data_20260903_20260908.db

# Auto-detection: sync_to_mongodb.py automatically infers track from DB filename prefix:
# nifty50_thursday_data_*.db -> track='thursday'
# nifty50_weekly_data_*.db   -> track='weekly'
```

---

## 16. External API Services Utilized

This project interacts with external brokerage, AI, database, and email services:

| Service | Category | Purpose / Where Used | Auth / Environment Variables | Quota / Billing Notes |
| :--- | :--- | :--- | :--- | :--- |
| **Angel One SmartAPI** | Brokerage & Market Data | • `common/angelone_client.py` (NIFTY spot LTP, daily option scrip master, CALL/PUT quotes) | `ANGELONE_API_KEY`<br>*(Prioritizes Redis key `angelone_jwt_feed`)* | Free API access. Rate-limited. Managed at [smartapi.angelbroking.com](https://smartapi.angelbroking.com/) |
| **OpenRouter** | LLM Gateway | • `common/ai_strike_selector.py` (Dynamic Tuesday strike optimization via DeepSeek/Claude) | `OPENROUTER_API_KEY`<br>*(Reads model list from Redis `finance_llm_models`)* | Prepaid USD credit. Volume: ~1–4 calls/week (1 entry call + up to 3 shadow counterfactual calls). Managed at [openrouter.ai/credits](https://openrouter.ai/credits) |
| **Upstox API** | Market Calendar | • `common/calendar_utils.py` (Fetches live NSE holiday calendar for deferred closes) | None (Public API: `api.upstox.com/v2/market/holidays`) | Free public endpoint. Cached in-process to minimize network calls. |
| **MongoDB Atlas** | Cloud NoSQL DB | • `scripts/sync_to_mongodb.py` (Persists weekly dossiers in `derivative_strategies` & `derivative_master`) | `MONGODB_URI` | Free M0 cluster (512MB storage). Managed at [cloud.mongodb.com](https://cloud.mongodb.com/) |
| **Gmail SMTP** | Email Notifications | • `scripts/send_weekly_report.py` (Dispatches Monday weekly performance reports & charts) | `EMAIL_SENDER`, `EMAIL_APP_PASSWORD`<br>`EMAIL_SMTP_SERVER`, `EMAIL_SMTP_PORT` | Daily sending limit (500/day). |

