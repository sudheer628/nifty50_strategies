# NIFTY 50 Opening Straddle Strategy Guide (09:14 – 10:00 AM IST)

---

## 1. Executive Summary & Strategy Philosophy

* **Objective**: Capture early morning directional breakouts without subjective chart guessing or dogmatic pre-market directional bias.
* **Operational Window**: Daily from **09:14 AM to 10:00 AM IST** (strictly analytical signal & position tracking; **order placement is out of scope**).
* **Guiding Principle**: Leg retention is strictly governed by **Total Straddle P&L** ($R_{\text{combined}}$). The winning runner must generate sufficient alpha to overcome the realized loss of the closed losing leg plus round-trip transaction friction. Live NIFTY spot movement is the ultimate authority.

---

## 2. Execution Timeline & State Machine

| Timestamp | Phase | Action |
|---|---|---|
| **09:14:00 AM** | **Pre-Market Ingestion** | Ingests 5 data streams (GIFT Nifty, Global Macro, News NLP, FII Flows, Upstox Pulse) to compute the Composite Directional Score (CDS) and Source Coverage. |
| **09:15:05 AM** | **Dual Long Entry** | Samples live Nifty spot $S_0$, locks closest ATM strike ($K_{\text{ATM}} = \text{round}(S_0 / 50) \times 50$), verifies bid-ask spread $\le 1.5\%$, and locks entry debit $D_0 = Q \times (C_0 + P_0)$. |
| **09:15 – 09:20** | **Live Observation** | 1 Hz live quote polling tracking $R_C(t)$, $R_P(t)$, and $R_N(t)$. |
| **09:20:00 AM** | **Checkpoint 1** | Decoupling evaluation. If criteria met $\implies$ Sell losing leg, retain winning runner. If chop $\implies$ Hold both legs. |
| **09:25:00 AM** | **Checkpoint 2 (Final)** | Decoupling re-evaluation. If decoupled $\implies$ Retain runner. If still indecisive $\implies$ **Scratch Exit** (Flatten both legs at market). |
| **09:25 – 09:55** | **Runner Safeguards** | Continuous monitoring: $+3.0\%$ Profit Target, $-3.0\%$ Stop Loss, Trailing Peak Protection. |
| **09:55:00 AM** | **Hard Time Stop** | Mandatory unconditional liquidation of open positions (5-minute buffer before 10:00 AM). |

---

## 3. Pre-Market Composite Directional Score (CDS) & Weights

$$\text{CDS} = \frac{1}{\text{Coverage}} \sum_{i \in \text{Available}} w_i \cdot S_i \quad \in [-1.0, +1.0]$$

$$\text{Coverage} = \sum_{i \in \text{Available \& Fresh}} w_i \quad \in [0.0, 1.0]$$

### Component Weights (Version 2 Baseline):
1. **GIFT Nifty (30%)**: `/tmp/upstox_live_quotes.json` quote age $\le 180\text{s}$. Gap scaled: $\Delta\% / 0.50\%$.
2. **Global Macro Basket (25%)**: Synthesizes Dow Jones ($+1\%$ = $+1.0$), Brent crude ($+1.5\%$ = $-1.0$), and USD/INR ($+0.30\%$ = $-1.0$).
3. **News NLP Sentiment (25%)**: Queries `NEWS#MARKET#DAILY` from monthly `signals_data_YYYY_MM.db`.
4. **FII Institutional Flow (10%)**: Queries `FLOW#FII_DII#DAILY` index futures 5-day flow.
5. **Upstox Pulse Newsletter (10%)**: Extracts scenario probabilities ($P_{\text{bullish}} - P_{\text{bearish}}$) and key support/resistance levels.

---

## 4. Dynamic Decoupling Matrix

* **Winning Leg Return**: $R_{\text{win}}(t) = \frac{\text{Bid}_{\text{active}}(t) - \text{Entry}}{\text{Entry}} \times 100$
* **Underlying Movement**: $R_N(t) = \frac{S(t) - S_0}{S_0} \times 100$

| Scenario | Min Winning Return | Outperformance Spread | Spot Confirmation Check | Action |
|---|---|---|---|---|
| **Aligned with Strong Prior**<br>($\text{Coverage} \ge 80\%$, $|\text{CDS}| \ge 0.25$) | **$+2.5\%$** | $R_{\text{win}} - R_{\text{lose}} \ge 2.0\text{ pp}$ | Directional alignment:<br>$R_N \ge +0.08\%$ (CE) or $R_N \le -0.08\%$ (PE) | Decouple (Sell losing leg, retain runner) |
| **Opposing Strong Prior**<br>(Contrarian Breakout) | **$+4.0\%$** | $R_{\text{win}} - R_{\text{lose}} \ge 3.0\text{ pp}$ | **Strong Spot Confirmation**:<br>$|R_N| \ge 0.15\%$ across **two consecutive API ticks** | Higher hurdle met $\implies$ Decouple and retain runner |
| **Neutral / Low Coverage Prior**<br>($\text{Coverage} < 80\%$ or $|\text{CDS}| < 0.25$) | **$+2.5\%$** | $R_{\text{win}} - R_{\text{lose}} \ge 2.0\text{ pp}$ | Spot confirms direction:<br>$|R_N| \ge 0.08\%$ | Decouple (Sell losing leg, retain runner) |
| **Spot Flatness Gate**<br>(Underlying flat despite option spike) | Any ($R_{\text{leg}} \ge 4.0\%$) | Any | Flat underlying:<br>$|R_N| < 0.05\%$ | **Do NOT decouple** (Gain attributed to spread/IV noise; hold) |

---

## 5. Total Straddle P&L Safeguards ($R_{\text{combined}}$)

$$\text{P\&L}_{\text{net}}(t) = Q \times \left[(\text{Bid}_{\text{active}}(t) - \text{Entry}_{\text{active}}) + (\text{Exit}_{\text{closed}} - \text{Entry}_{\text{closed}})\right] - \text{Friction}$$

$$R_{\text{combined}}(t) = \frac{\text{P\&L}_{\text{net}}(t)}{D_0} \times 100$$

1. **Profit Target**: $R_{\text{combined}} \ge +3.0\%$ (Liquidates runner immediately).
2. **Stop Loss**: $R_{\text{combined}} \le -3.0\%$ (Strict aggregate risk ceiling).
3. **Trailing Peak Protection**: Armed when Peak $R_{\text{combined}} \ge +2.0\%$; triggers exit if current drops $\ge 1.0\text{ pp}$ below peak.
4. **Hard Time Stop**: Unconditional liquidation by **09:55:00 AM**.

---

## 6. Persistence, Audit & SQLite Schema (`data/opening_straddle.db`)

All data is stored in an independent database file `data/opening_straddle.db`.

### Quick Inspection via CLI (Recommended):
```bash
# Print historical session performance table
python -m strategies.opening_straddle_selector --report
```

### End-of-Day Quick Verification Query:
```bash
sqlite3 -header -column /home/ubuntu/nifty50_strategies/data/opening_straddle.db "SELECT trade_date, atm_strike, selected_leg, net_roi_pct, exit_reason FROM opening_straddle_sessions;"
```

### Full Detailed Schema Query:
```sql
SELECT 
    trade_date,
    atm_strike,
    composite_directional_score AS cds,
    bias_classification AS bias,
    selected_leg,
    decision_time,
    closed_leg_exit_price,
    closed_leg_pnl_inr,
    runner_exit_price,
    runner_pnl_inr,
    net_pnl_inr,
    net_roi_pct,
    peak_roi_pct,
    exit_reason
FROM opening_straddle_sessions
ORDER BY id DESC;
```

---

## 7. Discord Webhook Alerts & Reporting (`.env`)

Alerts are strictly routed to the dedicated channel defined in `/home/ubuntu/nifty50_strategies/.env`:

```bash
# Add this line to /home/ubuntu/nifty50_strategies/.env
DISCORD_OPENING_STRADDLE=https://discord.com/api/webhooks/your_dedicated_webhook_url
```

* **Zero `.bashrc` Dependency**: The scripts load `.env` automatically via `python-dotenv`.
* **Channel Isolation**: If `DISCORD_OPENING_STRADDLE` is not set, alerts are cleanly skipped. It will never leak into other Discord channels.
* **Automatic Session Completion Report**: The moment the morning session closes (e.g. at 09:40 on trailing stop, or 09:55 hard stop), the script **automatically** dispatches a comprehensive embed report (Net P&L, ROI, Peak ROI, ATM Strike, Closed/Runner Leg exit prices, Prior Bias) to your dedicated channel.

---

## 8. Production Crontab Setup

On the Ubuntu VM (UTC time, 09:14 AM IST = 03:44 AM UTC):

```bash
# NIFTY 50 Opening Straddle Strategy (09:14 - 10:00 AM IST) - Auto-sends completion report upon exit:
44 3 * * 1-5 cd /home/ubuntu/nifty50_strategies && /usr/bin/flock -n /tmp/opening_straddle.lock .venv/bin/python -m strategies.opening_straddle_selector >> /home/ubuntu/logs/opening_straddle_$(date +\%F).log 2>&1
```

> [!NOTE] Automatic Single Daily Report
> When the session closes (trailing stop, profit target, stop loss, scratch, or 09:55 AM hard stop), the process automatically delivers the single completion report directly to the Discord webhook defined by `DISCORD_OPENING_STRADDLE` in `.env`. No secondary cron job is required.

---

## 9. Clean Decommissioning Instructions

If you ever wish to discontinue this strategy, it can be deleted in under 60 seconds with zero side effects:
1. Remove the cron line from `crontab -e`.
2. Delete the SQLite database: `rm -f /home/ubuntu/nifty50_strategies/data/opening_straddle.db`.
3. Delete the strategy files: `rm -f strategies/opening_straddle_*.py tests/test_opening_straddle_*.py`.
4. Your Tuesday and Thursday cycles continue running with zero downtime or left-over artifacts.
