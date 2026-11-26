# Root Cause: Unexpected SELL of NIFTY 01DEC2025800 PE (Tuesday cycle, 2026-11-26)

**Cycle:** `CYCLE-TUE-20261125` (Tuesday holiday; entry placed Wed 2026-11-25)
**Broker:** Upstox, tag `TUE-SL`
**Leg:** `NIFTY26D0125800PE` (NIFTY 01 DEC 2026, strike 25800, PE), qty 65
**Event:** MARKET SELL placed 2026-11-26 **09:15:03 IST** (03:45:03 UTC), filled at **₹194.10**
**Recorded as:** `MANUAL_CLOSED`, `"AUTO_DETECTED manual sell at ₹194.10 (qty=65, broker=upstox)"` at 09:31:03 IST

All times below are IST unless marked UTC. The VM clock and the log files are in UTC.

---

## 1. Summary

The SELL was **not placed by the operator**. It was placed by our automation, `scripts/Tuesday-Fno-Exit-Orders-V1.py`, running with `--execute`. The trigger was the **immediate square-off mode** (`EXIT_IMMEDIATELY_TUESDAY=true`), which sells every open Tuesday leg at market with no FSM check.

The leg was then mislabelled as a manual exit by `scripts/auto_record_manual_exit.py`. That reconciler only checks open positions and open pending orders, so it did not see the completed bot-placed SELL.

**Root cause, in one line:** a stale, armed exit-mode flag (`EXIT_IMMEDIATELY_TUESDAY=true` in Redis, `ORDERS_LIVE_TUE` still armed) let the exit manager's immediate-mode branch market-sell an Alpha FSM leg that was still ACTIVE. The logs record no FSM exit signal for this leg before the sell.

### Against design?
Yes, in the sense you describe. The README (§2.2 and the FSM section) says Alpha exits trigger **strictly** when an FSM exit signal fires (trailing-stop breach, harvest, or salvage). Immediate mode is a documented operator override that bypasses FSM (README §7, env table). So the code did what its override flag says, but the outcome breaks the FSM rule. Nothing in the logs shows that an FSM exit signal fired for this PUT leg before 09:15.

---

## 2. Evidence

### 2.1 Active snapshot (`/home/ubuntu/sqlite/strategies/current_week_buy.json`)

Relevant fields, copied verbatim:

```json
{
  "cycle_id": "CYCLE-TUE-20261125",
  "week_start_date": "20261125",
  "expiry_date": "20261201",
  "broker": "upstox",
  "call_strike": 25700,
  "put_strike": 25800,
  "entry_qty": 65,
  "entry_status": "FILLED",
  "put_order_id": "261125000028406",
  "put_buy_price": 236.75,
  "status": "closed",
  "closed_at": 1795665663,
  "alpha_fsm": {
    "state": "FLAT",
    "put_leg": {
      "strike": 25800,
      "buy_price": 236.75,
      "status": "MANUAL_CLOSED",
      "exit_price": 194.1,
      "exit_ts": 1795665663,
      "exit_reason": "AUTO_DETECTED manual sell at ₹194.10 (qty=65, broker=upstox)",
      "peak_ltp": 268.5,
      "trailing_stop": 177.56,
      "realized_pnl": -42.65,
      "is_estimated": false
    }
  }
}
```

Timestamp conversion: `exit_ts 1795665663` = **2026-11-26 09:31:03 IST** (04:01:03 UTC).

The snapshot's last FSM update before 09:15 was the Nov 25 cycle. At that point the PUT leg was ACTIVE with `trailing_stop 177.56` and `peak_ltp 268.5`.

### 2.2 Exit-manager log: the SELL (`/home/ubuntu/logs/tuesday_fno_exit_2026-11-26.log`, UTC)

```
2026-11-26 03:45:02,475 - nifty50_strategies - INFO - Config LIVE_EXIT_TRIGGER_TUESDAY=true (source: Redis override)
2026-11-26 03:45:02,477 - nifty50_strategies - INFO - Config EXIT_IMMEDIATELY_TUESDAY=true (source: Redis override)
2026-11-26 03:45:02,478 - nifty50_strategies - INFO - Config LOTS_TUESDAY=1 (source: Redis override)
2026-11-26 03:45:02,478 - nifty50_strategies - INFO - Config NIFTY_LOT_SIZE=65 (source: Redis override)
2026-11-26 03:45:03,200 [INFO] [TUE-EXIT] [NIFTY26D0125800PE] Successfully placed immediate MARKET order for 65 qty (order_id: 261126000004642)
```

JSON summary printed by the same run:

```json
{
  "cycle": "tuesday",
  "track": "5day_strategy",
  "broker": "upstox",
  "positions": 1,
  "authorized_positions": 1,
  "target_lots": 1,
  "cycle_target_qty": 65,
  "placed": 1,
  "failed": 0,
  "actions": [
    {
      "symbol": "NIFTY26D0125800PE",
      "instrument_token": "NSE_FO|47846",
      "tag": "TUE-SL",
      "quantity": 65,
      "total_broker_qty": 65,
      "product": "D",
      "status": "place_market",
      "mode": "immediate",
      "order_id": "261126000004642",
      "transaction_type": "SELL"
    }
  ]
}
```

The same run was started by cron (`45-59 3 * * 1-5 … Tuesday-Fno-Exit-Orders-V1.py --execute`), which runs every minute from 03:45 UTC.

### 2.3 Exit-manager log: confirmation the leg was closed by our own order

```
2026-11-26 03:46:02,375 [INFO] [TUE-EXIT] Cleared Redis trailing state & authorization for fno:upstox:enabled:tue:NIFTY:2026-12-01:25800:PE:NIFTY25800PE:NOTOKEN
2026-11-26 03:46:02,384 [INFO] [TUE-EXIT] Purged 3 Redis trailing/detailed keys for fno:upstox:enabled:tue:2026-12-01:25800:PE (expiry: 2026-12-01)
2026-11-26 03:46:02,384 [INFO] [TUE-EXIT] Leg fno:upstox:enabled:tue:2026-12-01:25800:PE confirmed closed (no position, no open TUE-SL orders). Retired Redis state.
2026-11-26 03:46:02,384 [INFO] [TUE-EXIT] Reconciled and retired 1 closed Tuesday leg(s) in Redis.
2026-11-26 03:46:02,384 [INFO] [TUE-EXIT] Broker portfolio has 0 open derivative positions and 0 orphan orders. Resetting ORDERS_LIVE_TUE = 0.
```

### 2.4 Exit-gate state before the SELL

The gate was armed at 03:45:02 UTC, the run that placed the SELL. The 03:46 run then reset it:

```
2026-11-26 03:46:02,384 [INFO] [TUE-EXIT] Broker portfolio has 0 open derivative positions and 0 orphan orders. Resetting ORDERS_LIVE_TUE = 0.
2026-11-26 03:47:02,581 [INFO] [TUE-EXIT] ORDERS_LIVE_TUE is inactive in Redis. Exiting immediately in 1ms.
```

On Nov 25 the gate was inactive until the Nov 25 entry armed it (the Nov 25 exit log shows "inactive" every minute from 03:45 to at least 04:04 UTC, before the entry). Nothing in the logs disarmed it between the Nov 25 entry and the 03:45 UTC run on Nov 26.

### 2.5 Peak monitor: PUT price before the SELL (`nifty50_strategies_peak_monitor_2026-11-26.log`, UTC)

```
2026-11-26 03:46:04,699 - nifty50_strategies - INFO - Resolved NIFTY option: symbol=NIFTY01DEC2625800PE token=47846 exchange=NFO
2026-11-26 03:46:04,779 - nifty50_strategies - INFO - Option batch quote (LTP) HTTP 200 body={"status":true,"message":"SUCCESS","errorcode":"","data":{"fetched":[{"exchange":"NFO","tradingSymbol":"NIFTY01DEC2625700CE","symbolToken":"47835","ltp":256.45},{"exchange":"NFO","tradingSymbol":"NIFTY01DEC2625800PE","symbolToken":"47846","ltp":212.5}],"unfetched":[]}}
```

Result: PUT 25800 LTP was **₹212.50** at 03:46 UTC, one minute **after** the SELL at 03:45:03 UTC. The logs contain no PUT price before 03:45 UTC, so they do not show whether the trailing stop (177.56) was breached before the SELL. The reading after the SELL also doesn't match the 194.10 fill, which is unexplained.

### 2.6 Broker data (read-only query, `fetch_positions` and `fetch_order_book`; no orders placed)

Position `NIFTY26D0125800PE`:

```json
{
  "tradingsymbol": "NIFTY26D0125800PE",
  "exchange": "NFO",
  "product": "D",
  "quantity": 0,
  "overnight_quantity": 65,
  "overnight_buy_quantity": 65,
  "overnight_buy_amount": 15388.75,
  "average_price": 236.75,
  "day_sell_quantity": 65,
  "day_sell_price": 194.1,
  "day_sell_value": 12616.5,
  "realised": -2772.25,
  "last_price": 194.15
}
```

Order book entries for this strike (only these two are relevant for the PUT leg):

```
OrderID: 261126000004642, Status: complete, Type: SELL, Price: 187.7, AvgPrice: 194.1, Qty: 65.0, Tag: TUE-SL, Time: 2026-11-26 09:15:03
```

Our automation placed the order, tagged `TUE-SL`. It was not placed by a manual action, an Upstox auto square-off, or a GTT. The 25800 CE order (`THU-BUY`, 09:32) is the Thursday track, which is out of scope.

### 2.7 Collector log at 09:31 IST (`nifty50_strategies_options_strategy_2026-11-26.log`, UTC)

This is the entire file. It has 14 lines, all written at 04:01 UTC.

```
2026-11-26 04:01:03,080 - nifty50_strategies - INFO - [5day_strategy] Manual exit detected for 25800 PE: active in FSM but no open broker position or pending order
2026-11-26 04:01:03,086 - nifty50_strategies - INFO - [5day_strategy] Updated FSM state for 25800 PE: MANUAL_CLOSED at ₹194.10
2026-11-26 04:01:03,093 - nifty50_strategies - INFO - [5day_strategy] Recorded exited qty 65 in Redis: fno:upstox:exited:tue:2026-12-01:25800:PE
2026-11-26 04:01:03,098 - nifty50_strategies - INFO - [5day_strategy] Cleaned up authorization keys for 25800 PE
2026-11-26 04:01:03,100 - nifty50_strategies - INFO - [5day_strategy] Cleaned up 0 trailing state keys for 25800 PE
2026-11-26 04:01:03,101 - nifty50_strategies - INFO - Marked active strategy cycle CYCLE-TUE-20261125 as CLOSED in /home/ubuntu/sqlite/strategies/current_week_buy.json
2026-11-26 04:01:03,101 - nifty50_strategies - INFO - [5day_strategy] All legs closed. Marked active cycle as CLOSED in /home/ubuntu/sqlite/strategies/current_week_buy.json
2026-11-26 04:01:03,512 - nifty50_strategies - INFO - [5day_strategy] Updated active_strategies.json for 25800 PE
2026-11-26 04:01:03,516 - nifty50_strategies - INFO - [5day_strategy] All legs closed or no active legs remain. Reset ORDERS_LIVE_TUE = 0
2026-11-26 04:01:03,516 - nifty50_strategies - INFO - [5day_strategy] Auto-recorded manual exit: 25800 PE @ ₹194.10 (P&L: -42.65 pts / -18.0%)
2026-11-26 04:01:03,567 - nifty50_strategies - INFO - Active cycle CYCLE-TUE-20261125 is closed; skipping mid-cycle processing and preventing duplicate entry.
2026-11-26 04:01:03,567 - nifty50_strategies - INFO - Collection cycle completed successfully.
```

No Upstox position-fetch warnings or errors appear in this window.

### 2.8 Precedent: the Nov 25 CALL exit used the same path (`tuesday_fno_exit_2026-11-25.log`, UTC)

```
2026-11-25 07:05:02,835 [INFO] [TUE-EXIT] [NIFTY26D0125700CE] Successfully placed immediate MARKET order for 65 qty (order_id: 261125000148451)
```

This leg was closed by the same immediate-mode path, about 4 minutes after its FSM `TRAILING_STOP_CLOSED` timestamp (12:31 IST). It shows the immediate path is used routinely. It does not prove the PUT exit was allowed.

---

## 3. Code and design references

| Item | Location | What it shows |
|---|---|---|
| Immediate-mode flag read | `scripts/Tuesday-Fno-Exit-Orders-V1.py:2731` | `exit_immediately = args.exit_immediately or get_config_bool("EXIT_IMMEDIATELY_TUESDAY", …)` |
| Immediate-mode branch | `scripts/Tuesday-Fno-Exit-Orders-V1.py:1482` | `is_immediate = bool(exit_immediately)` (true for every open Tuesday leg). The trailing-stop logic is not consulted in this branch. |
| Immediate MARKET order | `scripts/Tuesday-Fno-Exit-Orders-V1.py:2228–2245` | `place_exit_order_market(...)`, then logs "Successfully placed immediate MARKET order" |
| Gate check | `scripts/Tuesday-Fno-Exit-Orders-V1.py:2700–2740` | `ORDERS_LIVE_TUE` is only the on/off gate. `EXIT_IMMEDIATELY` is not checked against FSM state. |
| Reconciler: manual exit test | `scripts/auto_record_manual_exit.py:677–725` | Manual exit = no open position AND no OPEN pending order. Completed orders are not considered. |
| Reconciler: no managed-order check | `scripts/auto_record_manual_exit.py` (docstring line 6 says "no managed order"; code does not check `fno:{broker}:managed_order_ids:tue` or the `TUE-SL` tag) | Bot-placed completed SELL looks like a manual sell |
| Design: FSM-gated exits | `README.md:32` | "triggered strictly when Alpha FSM exit signals trigger in sentinel-hermes" |
| Design: trailing-stop exit | `README.md:339` | "If the price retraces from peak and breaches the trailing stop, that leg closes" |
| Design: immediate override | `README.md:426–440` (§7) | Immediate mode is an operator override that bypasses trailing SL and submits a MARKET sell |
| Design: env flag | `README.md:907`, `README.md:918` | `EXIT_IMMEDIATELY_TUESDAY` default `false`; `redis-cli SET EXIT_IMMEDIATELY_TUESDAY true` forces immediate MARKET exits |

---

## 4. Timeline (IST, unless marked UTC)

| Time | Event | Source |
|---|---|---|
| Nov 25 09:31 | Tuesday cycle entry (PE 25800 @ 236.75, CE 25700 @ 287.10). Exit gate armed. | `current_week_buy.json` (`captured_at`, buy prices) |
| Nov 25 12:31 | CALL 25700 CE `TRAILING_STOP_CLOSED` (FSM) | snapshot `call_leg.exit_ts` |
| Nov 25 12:35 | CALL 25700 CE closed by immediate MARKET SELL `261125000148451` | `tuesday_fno_exit_2026-11-25.log` line 2206 |
| Nov 25–26 overnight | PUT leg remains ACTIVE in FSM. `ORDERS_LIVE_TUE` and `EXIT_IMMEDIATELY_TUESDAY=true` remain in Redis. | exit logs, snapshot |
| Nov 26 09:15:02 (03:45:02 UTC) | Exit-manager cron runs with `EXIT_IMMEDIATELY_TUESDAY=true`, `LIVE_EXIT_TRIGGER_TUESDAY=true` | `tuesday_fno_exit_2026-11-26.log` lines 1–4 |
| **Nov 26 09:15:03 (03:45:03 UTC)** | **MARKET SELL 65 qty `261126000004642`, tag `TUE-SL`, placed for NIFTY26D0125800PE** | exit log line 5; broker order book |
| Nov 26 09:15 | Broker fill at ₹194.10 avg | broker order book |
| Nov 26 09:16 (03:46 UTC) | Peak monitor: PUT LTP ₹212.50 (one minute after the SELL). Exit manager retires the leg. | peak monitor log, exit log 03:46:02 |
| Nov 26 09:31:03 (04:01:03 UTC) | Collector reconciler: "Manual exit detected" → `MANUAL_CLOSED` at ₹194.10. Cycle closed. | collector log (14 lines) |

---

## 5. Why the design was broken in this instance

1. **Immediate mode is not FSM-gated.** `is_immediate` in `Tuesday-Fno-Exit-Orders-V1.py:1482` is true for any open leg when `EXIT_IMMEDIATELY_TUESDAY=true`. The MARKET exit then fires without checking the trailing stop, harvest, or salvage state. The README says exits must come only from FSM signals.
2. **The flag was left armed.** Nothing reset `EXIT_IMMEDIATELY_TUESDAY` or `ORDERS_LIVE_TUE` between the Nov 25 entry and the Nov 26 open. The gate stayed armed across the overnight hold, so the first cron tick after 09:15 sold the still-ACTIVE PUT.
3. **The reconciler mislabelled the bot's order.** `auto_record_manual_exit.py` treats "no open position and no open pending order" as manual. It does not check whether a completed SELL was placed by our exit manager (`TUE-SL` tag or `managed_order_ids:tue`). So the bot's exit was recorded as `MANUAL_CLOSED` with a manual-sell message.

---

## 6. Open questions (not answered by the evidence)

- **Who set `EXIT_IMMEDIATELY_TUESDAY=true`, and when?** It is a Redis override with no `.env` source, and no shell history or log entry records an operator action. The Redis change history would answer this. We did not check it.
- **Was there an FSM exit signal for the PUT leg before 09:15?** The logs record no FSM exit event for this leg on Nov 26 before the SELL. The last snapshot FSM update was Nov 25 and the only PUT price on Nov 26 is ₹212.50 at 03:46 UTC, after the SELL. Whether a trailing-stop breach existed in Redis at 03:45 UTC is unknown, because the trail keys were purged at 03:46 UTC.
- **Why did the SELL fill at ₹194.10 when the next price read was ₹212.50?** Not explained by the logs. It may be a fast move, or a different price source. Worth checking against the 1-minute candle data.
- **Was `EXIT_IMMEDIATELY_TUESDAY=true` intended to persist overnight?** The README documents it as a manual override, not a standing setting.

---

## 7. Recommended fixes (not applied)

1. **Gate immediate mode on FSM state.** Only allow the immediate MARKET branch when the leg has an FSM exit signal (`TRAILING_STOP_CLOSED`, `HARVESTED`, or `SALVAGED` pending), or when an explicit one-shot operator command is issued. A standing `EXIT_IMMEDIATELY_*=true` flag must not sell an ACTIVE leg.
2. **Auto-reset one-shot overrides.** `EXIT_IMMEDIATELY_TUESDAY` should clear itself after one successful immediate exit, and at the end of each cycle. `ORDERS_LIVE_TUE` should also be checked against the snapshot status.
3. **Fix the reconciler's manual test.** Before marking `MANUAL_CLOSED`, check for a completed SELL whose order ID is in `fno:{broker}:managed_order_ids:tue` or whose tag is `TUE-SL`. If so, record it as an automated exit with its real trigger, not as a manual sell.
4. **Log the trigger reason** on every immediate MARKET placement (which flag, which FSM state), so the next incident can be traced from the logs alone.

---

*Prepared from VM logs and a read-only broker query on 2026-11-26. No orders were placed, modified, or cancelled, and no Redis or snapshot state was changed in producing this report.*
