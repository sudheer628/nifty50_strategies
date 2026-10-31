# NIFTY 50 Directional Single-Leg Opening Scalper Guide (09:14 – 10:00 AM IST)

---

## 1. Executive Summary & Strategy Philosophy

* **Objective**: Transform the early morning opening strategy from an ATM dual-long straddle into a **Directional Single-Leg Quick Scalper** (09:14 – 10:00 AM IST) to capture momentum or decay on single high-conviction trades and completely eliminate scratch spread losses.
* **Operational Window**: Daily from **09:14 AM to 10:00 AM IST** (analytical signal, position tracking, and simulated/paper order execution; real broker orders trigger via connected API).
* **Guiding Principles**:
  * **Rule 1 (Single Leg Only)**: Strictly **one order per day** (1 contract, 1 entry at 09:16:05, 1 exit before 10:00 AM). Ready to take **SHORT positions** (SELL first, BUY next).
  * **Rule 2 (High-VIX Skip Filter)**: If India VIX $> 20.0$, skip the entire day (`SKIPPED_HIGH_VIX`, zero orders, zero friction). If VIX is unresolvable or missing, fail closed (`SKIPPED_MISSING_VIX`).
  * **Rule 3 (Quick Scalp Exit)**: Exit on Min Profit Target (+8.0 pts net / +10% BUY / +12% SELL on premium), Dynamic Trailing Ratchet (+4.0 pts arm, 2.0 pt trail), Stop Loss (-8.0 pts / -15%), or Hard Time Stop at **09:55:00 AM IST** (maximum duration 2,335s from 09:16:05).
  * **User Tip Priority**: Prioritize **SELL CALL (1-step OTM)** over BUY PUT on moderate weakness, capitalizing on rapid opening call premium decay and theta crush.

---

## 2. Execution Timeline & State Machine

| Timestamp | Phase | Action |
|---|---|---|
| **09:14:00 AM** | **Pre-Market Ingestion & VIX Gate** | Ingests pre-market data streams (GIFT Nifty, Prior-Day OHLC, Grok X/Twitter Sentiment, Upstox Pulse, FII Flows, News NLP). Fetches live India VIX. If $\text{VIX} > 20.0$ or missing, halts with zero orders. |
| **09:15:00 – 09:16:00 AM** | **Opening Candle Observation** | Observes the first 1-minute NIFTY candle ($O_{1m}, H_{1m}, L_{1m}, C_{1m}$). Computes Opening Momentum score ($S_{\text{open}} = \text{clip}((C_{1m} - O_{1m}) / 40.0, -1.0, 1.0)$). |
| **09:16:05 AM** | **CMS Synthesis & Leg Selection** | Synthesizes Final CMS: $CMS = 0.80 \times CDS_{\text{premarket}} + 0.20 \times S_{\text{open}}$. Evaluates decision matrix to select exactly one contract (BUY CALL ATM, SELL PUT 1-step OTM, SELL CALL 1-step OTM, or SKIP). |
| **09:16:05 – 09:55:00 AM** | **1 Hz Active Scalp Monitoring** | Continuously tracks live option Bid/Ask quotes and evaluates exit conditions: Min Profit Target, Trailing Ratchet, Stop Loss. |
| **09:55:00 AM** | **Mandatory Hard Time Stop** | Mandatory unconditional liquidation of open positions (ensures 100% flat book well before 10:00 AM IST). |

---

## 3. Directional Composite Morning Score (CMS) & Factor Weights

$$\text{CDS}_{\text{premarket}} = \frac{1}{\text{Coverage}} \sum_{i \in \text{Available}} w_i \cdot S_i \quad \in [-1.0, +1.0]$$

$$\text{Coverage} = \sum_{i \in \text{Available \& Fresh}} w_i \quad \in [0.0, 1.0]$$

> [!IMPORTANT]
> If $\text{Coverage} < 0.50$, the system defaults $\text{CDS}_{\text{premarket}} = 0.0$, forcing a `SKIPPED_NEUTRAL_CHOP` decision to prevent trading on low-confidence data.

### Premarket Factor Weights:
1. **GIFT Nifty Gap (30%)**: `/tmp/upstox_live_quotes.json` quote age $\le 180\text{s}$. Gap scaled: $\Delta\% / 0.50\%$.
2. **Prior-Day OHLC Structure (20%)**: Compares previous day Open vs Close, distance to Day High/Low, and Close vs 5-day EMA.
3. **Grok / Social Sentiment (15%)**: Real-time X/Twitter financial community sentiment extracted from Upstox/Telegram/X summaries.
4. **News NLP Sentiment (15%)**: FinBERT sentiment from `NEWS#MARKET#DAILY`.
5. **FII Institutional Flow (10%)**: Daily net index futures/options positioning.
6. **Upstox Pulse Newsletter (10%)**: Support/resistance zones and daily probability skews.

### Final Blended Score:
$$\text{Final CMS} = 0.80 \times \text{CDS}_{\text{premarket}} + 0.20 \times S_{\text{open}}$$

---

## 4. Leg Selection Decision Matrix & Execution

At **09:16:05 AM IST**, the selector rounds the live NIFTY spot to determine ATM strike $K_{\text{ATM}}$ and evaluates the decision matrix:

| Final CMS Range | Market Bias | Action | Leg | Target Strike | Rationale |
|---|---|---|---|---|---|
| **$CMS \ge +0.30$** | Strong Bullish | **BUY** | **CALL** | $K_{\text{ATM}}$ | Aggressive upward delta capture on strong breakout momentum |
| **$+0.10 \le CMS < +0.30$** | Moderate Bullish | **SELL** | **PUT** | $K_{\text{ATM}} - 50$ (1-step OTM) | Bullish credit scalp; captures fast opening theta decay with safety margin |
| **$-0.30 < CMS \le -0.10$** | Moderate Bearish | **SELL** | **CALL** | $K_{\text{ATM}} + 50$ (1-step OTM) | **User Tip Priority**: Bearish credit scalp; captures opening call decay |
| **$CMS \le -0.30$** | Strong Bearish | **SELL** (default)<br>*or BUY* | **CALL** (default)<br>*or PUT* | $K_{\text{ATM}} + 50$ (1-step OTM)<br>*or $K_{\text{ATM}}$ (ATM)* | Default: SELL CALL OTM (decay priority). Pass `--prefer-buy-put-on-strong-bearish` to select BUY PUT ATM. |
| **$-0.10 < CMS < +0.10$** | Neutral / Chop | **SKIP** | **NONE** | None | `SKIPPED_NEUTRAL_CHOP` — zero orders placed, zero transaction friction |

---

## 5. Dual-Sided Executable Pricing & P&L Mechanics

To eliminate paper slippage and maintain realistic backtesting and live accounting, orders are priced using market bid/ask quotes:

### Long Positions (BUY CALL / BUY PUT):
* **Entry**: Executes at the **Ask** price ($P_{\text{entry}} = \text{Ask}$).
* **Current Value / Exit**: Liquidates at the **Bid** price ($P_{\text{exit}} = \text{Bid}$).
* **Point Change**: $\Delta P = \text{Bid} - P_{\text{entry}}$.
* **Net P&L (INR)**: $\text{P\&L}_{\text{net}} = Q \times \Delta P - \text{Friction}$.
* **Capital Deployed**: Debit paid $D_0 = Q \times P_{\text{entry}}$.
* **ROI %**: $(\text{P\&L}_{\text{net}} / D_0) \times 100$.

### Short Positions (SELL CALL / SELL PUT):
* **Entry**: Executes at the **Bid** price ($P_{\text{entry}} = \text{Bid}$).
* **Current Value / Exit**: Bought back at the **Ask** price ($P_{\text{exit}} = \text{Ask}$).
* **Point Change**: $\Delta P = P_{\text{entry}} - \text{Ask}$ (positive when premium drops).
* **Net P&L (INR)**: $\text{P\&L}_{\text{net}} = Q \times \Delta P - \text{Friction}$.
* **Capital Deployed**: Required short option margin per lot (Default: ₹1,20,000, configurable via `--short-margin` or `SHORT_MARGIN_PER_LOT` in `.env`).
* **ROI %**: $(\text{P\&L}_{\text{net}} / \text{Margin}) \times 100$.
* **Premium Gain %**: $(\Delta P / P_{\text{entry}}) \times 100$.

---

## 6. Quick Scalp Exit Safeguards (Rule 3)

The 1 Hz polling loop tracks live Bid and Ask ticks from 09:16:05 to 09:55:00 AM IST, evaluating the following hierarchy:

1. **Min Profit Target**:
   * Net points $\ge +8.0$ pts (₹520 net / ~₹595 gross per lot of 65), OR
   * BUY leg: Premium gain $\ge +10.0\%$, OR
   * SELL leg: Premium decay captured $\ge +12.0\%$.
   * *Action*: Instant market liquidation.
2. **Dynamic Trailing Ratchet**:
   * **Arming**: Arms once net points reach $\ge +4.0$ pts above entry.
   * **Trailing**: Tracks peak points; triggers exit if current points fall $\ge 2.0$ pts below peak.
   * *Action*: Locks in positive scalp gains.
3. **Stop Loss**:
   * Net points $\le -8.0$ pts (disciplined risk stop ceiling of -₹520 net per lot of 65), OR
   * Premium drawdown $\le -15.0\%$.
   * *Action*: Immediate risk containment exit.
4. **Hard Time Stop (09:55:00 AM IST)**:
   * Absolute clock cutoff at 09:55:00 AM IST (maximum duration: 2,335 seconds).
   * Unconditionally closes any open position at market, leaving book 100% clean before 10:00 AM IST.

---

## 7. Persistence, Audit & SQLite Schema (`data/opening_straddle.db`)

All session runs and skips are persisted to `data/opening_straddle.db` with full audit fields.

### View Session History via CLI:
```bash
# Print formatted session performance table
python -m strategies.opening_straddle_selector --report
```

### Direct SQLite Query:
```sql
SELECT 
    trade_date,
    execution_mode,
    order_action,
    trade_leg,
    executed_strike,
    entry_price,
    exit_price,
    vix_value,
    composite_directional_score AS cms,
    net_pnl_inr,
    net_roi_pct,
    exit_reason
FROM opening_straddle_sessions
ORDER BY id DESC;
```

---

## 8. Discord Webhook Alerts & Reporting (`.env`)

Add your dedicated webhook URL to `/home/ubuntu/nifty50_strategies/.env`:

```bash
DISCORD_OPENING_STRADDLE=https://discord.com/api/webhooks/your_dedicated_webhook_url
```

* **Entry Notification**: Dispatched immediately at 09:16:05 AM IST detailing Action, Leg, Strike, Executable Price, Margin/Capital, VIX, and CMS score.
* **Skip Notification**: Dispatched if Rule 2 (High VIX) or Neutral Chop condition is met, confirming 0 orders placed and ₹0 friction.
* **Exit Summary**: Dispatched immediately upon position closure detailing realized points, Net P&L in INR, Net ROI %, and specific exit trigger (e.g., `MIN_PROFIT_TARGET`, `TRAILING_RATCHET`, `STOP_LOSS`, `TIME_STOP_0955`).

---

## 9. Production Crontab Setup & CLI Flags

On the production VM (09:14 AM IST = 03:44 AM UTC):

```bash
# NIFTY 50 Directional Single-Leg Scalper (09:14 - 10:00 AM IST):
44 3 * * 1-5 cd /home/ubuntu/nifty50_strategies && /usr/bin/flock -n /tmp/opening_straddle.lock .venv/bin/python -m strategies.opening_straddle_selector >> /home/ubuntu/logs/opening_straddle_$(date +\%F).log 2>&1
```

### Optional CLI Arguments:
* `--dry-run`: Runs full premarket intelligence, 1-minute candle observation, and leg selection without entering the live tracking loop.
* `--prefer-buy-put-on-strong-bearish`: Switches strong bearish ($CMS \le -0.30$) action from SELL CALL OTM to BUY PUT ATM.
* `--short-margin <amount>`: Overrides short margin requirement per lot (default ₹120,000).
* `--report`: Queries SQLite and outputs the formatted historical performance table.

---

## 10. Clean Decommissioning Instructions

To decommission the strategy at any time:
1. Remove or comment out the cron entry in `crontab -e`.
2. Remove SQLite history: `rm -f /home/ubuntu/nifty50_strategies/data/opening_straddle.db`.
3. Remove strategy files: `rm -f strategies/opening_straddle_*.py tests/test_opening_straddle_*.py`.
4. Tuesday and Thursday weekly option cycles continue uninterrupted.
