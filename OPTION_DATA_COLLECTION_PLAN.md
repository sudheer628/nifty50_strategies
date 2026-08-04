# Option Strategy Data Collection Plan

## 1. Objective

Build one self-contained option data collection strategy for NIFTY50 without placing any orders.

This plan covers:
- collecting NIFTY50 market data,
- selecting one PUT strike and one CALL strike based on the day open,
- collecting option prices for the selected strikes across the full weekly window,
- storing the data in SQLite for long-term reuse,
- keeping the strategy isolated from other strategies while sharing a common Angel One SmartAPI data layer.

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

## 3. Strategy Design Principles

### 3.1 Strategy isolation
Each strategy script will be independent and should not depend on another strategy script.

### 3.2 Shared infrastructure
All strategies will reuse a common module for:
- SmartAPI authentication,
- market data fetch,
- expiry resolution,
- data persistence.

### 3.3 Active vs inactive strategy model
A strategy may be:
- active: currently running in the VM,
- inactive: present in the codebase but not running in the live environment.

This plan is for the first strategy and is designed so it can later be cloned or replicated without changing the core data collection layer.

## 4. Weekly Data Collection Logic

### 4.1 Collection schedule
- The strategy will start collecting data every Tuesday.
- It will target the next weekly expiry, not the current week expiry.

### 4.2 Example timing
If today is 4th August and the current week expiry is for 4th August, the strategy will not use that expiry.
Instead, it will collect data for the next weekly expiry, which is the upcoming Tuesday expiry (for example, 11th August).

### 4.3 Repeated weekly behavior
Every Tuesday:
- the strategy will start a new weekly cycle,
- it will select the next weekly expiry,
- it will begin collecting data for that expiry.

### 4.4 Double collection on the second Tuesday
On the second Tuesday of the cycle:
- the strategy will continue collecting data for the previous week’s expiry,
- and simultaneously start collecting data for the new next weekly expiry.

This behavior should be supported explicitly in the scheduler and data storage model.

## 5. Data to Collect

For each active weekly cycle, the strategy will collect:
- NIFTY50 day open
- NIFTY50 current LTP
- NIFTY50 previous close (if available from the data feed)
- one PUT strike
- one CALL strike
- option prices for the selected strikes
- option market snapshot at each collection interval
- the Tuesday buy prices for CALL and PUT for the current cycle

## 6. Strike Selection Rule

### 6.1 Strike grid
The strategy should use a 100-point strike increment, not a 50-point increment.

### 6.2 Base selection
At the start time of data collection, which is 9:30 AM, the strategy will use the day open of NIFTY50 as the reference point.

### 6.3 Example rule
If the day open is 24540:
- the strategy will pick a PUT strike of 24000,
- and a CALL strike of 24500.

This means the strategy will use the 100-point strike logic instead of the 50-point option strike convention.

### 6.4 Implementation note for strike logic
The logic should be based on a simple rule such as:
- derive a rounded 100-point anchor from the day open,
- select one lower 100-point strike for PUT,
- select one upper 100-point strike for CALL.

The exact mapping can be finalized later, but the plan must preserve the intended behavior of using 100-point step sizes.

## 7. Collection Frequency

### 7.1 Start time
- The first collection should begin at 9:30 AM.

### 7.2 Interval
- Data should be collected hourly after the start time.
- The collector should continue collecting for the entire week until the next Tuesday boundary.

### 7.3 Data points per cycle
The system should store one record per collection interval for:
- NIFTY50 spot data,
- PUT option data,
- CALL option data,
- timestamp,
- expiry reference,
- strategy identifier.

## 8. Buy Price Snapshot Rule

A key requirement of this plan is to preserve the Tuesday entry prices for later gain/loss evaluation without losing the previous week’s reference data.

### 8.1 Clarification for the August 4 to August 11 case
This is the important point:
- on 4th August, the strategy captures the buy prices for the current week’s expiry and stores them,
- on 11th August, the strategy must still keep the 4th August buy prices for the old week,
- and at the same time it must create a new buy snapshot for the new week.

A single file such as /home/ubuntu/sqlite/strategies/current_week_buy.json cannot represent both weeks at once unless it is changed into a container format with history.

### 8.2 Recommended approach: active snapshot + historical archive
The safest design is a two-layer approach:

1. Active snapshot
- Keep one live file at:
  - /home/ubuntu/sqlite/strategies/current_week_buy.json
- This file represents the buy prices for the currently active expiry cycle only.
- It is used by the daily collector for the ongoing week.

2. Historical snapshot archive
- Every Tuesday snapshot should also be stored permanently in SQLite.
- A table such as strategy_buy_snapshots should hold:
  - strategy_name
  - cycle_id
  - week_start_date
  - expiry_date
  - call_strike
  - put_strike
  - call_buy_price
  - put_buy_price
  - captured_at

This means:
- 4th August snapshot is preserved in SQLite as the old week’s reference,
- 11th August snapshot becomes the new active snapshot for the new week.

### 8.3 Tuesday buy capture
On Tuesday, when the strategy first runs for the selected expiry, it will capture:
- the CALL buy price,
- the PUT buy price,
- the corresponding CALL and PUT strikes,
- the collection timestamp.

These values will be written to:
- the active JSON snapshot file for the current week, and
- the SQLite historical table for permanent retention.

### 8.4 Wednesday and onward behavior
On Wednesday and every following day, the strategy will continue using the buy prices from the active snapshot that belongs to the current expiry cycle.
This keeps the weekly gain/loss calculation consistent for the existing week.

### 8.5 Tuesday rollover behavior
When the next Tuesday cycle begins:
- the old week’s buy snapshot remains stored in SQLite,
- a new snapshot is created for the new expiry,
- the active JSON file is updated for the new week,
- the old week can also be copied to a dated backup file if desired, such as:
  - /home/ubuntu/sqlite/strategies/current_week_buy_20250804.json

### 8.6 Best implementation rule
Do not rely on the JSON file alone as the source of truth.
Instead:
- use SQLite as the authoritative store for all weekly buy snapshots,
- use current_week_buy.json as the convenience file for the currently active cycle only.

If you want a single JSON file to contain both weeks, then the file should be structured as a history container rather than a single overwrite-only snapshot.

## 9. Suggested System Architecture

### 9.1 Core modules
The implementation should be organized as follows:

- common data module
  - SmartAPI authentication
  - market data fetch helpers
  - expiry resolution helpers

- strategy runner
  - weekly cycle logic
  - strike selection logic
  - hourly collector trigger
  - Tuesday buy-price snapshot logic

- storage module
  - write collected data to SQLite
  - maintain a weekly snapshot JSON file for buy prices
  - support append-only storage per cycle

- scheduler
  - run the collector on Tuesday morning
  - maintain weekly cycle state

### 9.2 Shared component design
The first strategy should not contain duplicate logic for:
- fetching option chain data,
- parsing expiry dates,
- connecting to SmartAPI,
- writing data to storage.

These should exist once in the common layer and be reused by all future strategies.

## 10. Data Model

Each hourly record should include the following fields:
- strategy_name
- collection_timestamp
- expiry_date
- nifty_open
- nifty_ltp
- nifty_previous_close
- put_strike
- put_ltp
- call_strike
- call_ltp
- call_buy_price
- put_buy_price
- source
- cycle_id

If an option price is unavailable at a given interval, the record should still be written with a null or empty field rather than being skipped.

## 11. Storage Plan

### Primary storage
- Store all collected data in SQLite at:
  - /home/ubuntu/sqlite/strategies/

### Recommended database file naming
A good weekly naming pattern would be:
- nifty50_weekly_data_{YYYYMMDD}_{expiry}.db

Example:
- nifty50_weekly_data_20260804_20260811.db

This format keeps the file name clear, sortable, and tied to the relevant week and expiry.

### Snapshot file
- Save the current week’s Tuesday buy prices in:
  - /home/ubuntu/sqlite/strategies/current_week_buy.json

### Storage rules
- One row per collection interval
- One database file per weekly cycle or per strategy week
- Keep the buy-price snapshot as a separate JSON file for easy access by later reporting logic

## 12. Implementation Phases

### Phase 1: Common foundation
- create shared SmartAPI wrapper
- create expiry resolution helper
- create SQLite storage handler
- create JSON snapshot handler for current week buy prices

### Phase 2: One strategy collector
- implement the weekly cycle scheduler
- implement strike selection logic
- implement hourly interval collection
- capture Tuesday buy prices and persist them in the JSON snapshot
- store hourly data in SQLite

### Phase 3: Validation and monitoring
- verify that the collector runs on Tuesday
- verify that the correct expiry is selected
- verify that the chosen PUT and CALL strikes are logged correctly
- verify that data is recorded hourly from 9:30 AM onward
- verify that the JSON buy-price snapshot remains stable after Tuesday until the next weekly rollover

## 13. Expected Output

The output of this strategy will be a clean dataset containing:
- NIFTY50 spot information,
- selected PUT and CALL option information,
- weekly time-series records for analysis,
- stored Tuesday buy prices for CALL and PUT,
- a base foundation for future strategy development.

## 14. Implementation Notes

- Keep this strategy simple and deterministic.
- Avoid introducing order placement or execution logic.
- Prefer explicit configuration over hardcoding where possible.
- Log every collection attempt and any missing data clearly.
- Make the code compatible with future reuse by other strategies.

## 15. Recommended Next Step

Build the common SmartAPI data layer first, then implement the first weekly collector strategy on top of it.

The first version should be minimal and focused only on collecting data faithfully according to the rules above.
