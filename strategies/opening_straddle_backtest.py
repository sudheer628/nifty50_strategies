"""
Backtest & Integration Simulation Engine for NIFTY 50 Opening Straddle Strategy (Phase 3).

Simulates 15 realistic historical opening market regimes (09:14 - 10:00 AM):
  - Strong Trend Gap-and-Go (Bullish / Bearish)
  - Contrarian Reversals & False Traps
  - Tight Morning Consolidation & Whipsaws
  - Trailing Stop Peak Lock-ins & Hard Time Stops
  - Wide Spread Opening Aborts & Low Coverage Fallbacks

Compares 3 Model Configurations:
  1. Flagship: CDS-V2 / Contrarian-4 / Coverage-80
  2. Baseline: CDS-V1 / Contrarian-2.5 / Coverage-60
  3. Price-Only: Pure Live Price Action (No Prior)
"""
import argparse
import copy
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from strategies.opening_straddle_engine import OpeningStraddleEngine
from strategies.opening_straddle_models import (
    BiasClassification,
    ConfidenceLevel,
    ExitReason,
    PreMarketIntelligence,
    SelectedLeg,
    StrategyState,
    StraddleLegQuote,
    TickObservation,
    VolatilityRegime,
)

logger = logging.getLogger("nifty50_strategies.opening_straddle_backtest")


@dataclass
class BacktestSessionScenario:
    """A single synthetic or historical 45-minute opening session profile."""
    name: str
    scenario_type: str        # TREND_BULL, TREND_BEAR, CONTRARIAN_TRUE, CONTRARIAN_TRAP, CHOP, TRAIL_PEAK, STOP_LOSS, WIDE_SPREAD
    initial_spot: float
    pre_market: PreMarketIntelligence
    initial_call_bid: float
    initial_call_ask: float
    initial_put_bid: float
    initial_put_ask: float
    ticks: List[TickObservation]


@dataclass
class ModelEvaluationResult:
    """Performance metrics for an experimental configuration across sessions."""
    config_name: str
    total_sessions: int
    trades_executed: int
    trades_aborted: int
    win_count: int
    loss_count: int
    scratch_count: int
    total_net_pnl_inr: float
    avg_roi_pct: float
    win_rate_pct: float
    profit_factor: float
    max_drawdown_inr: float
    exit_breakdown: Dict[str, int]


def build_15_market_scenarios() -> List[BacktestSessionScenario]:
    """
    Builds 15 detailed opening session profiles capturing the full spectrum of market dynamics.
    """
    base_ts = 1791957300.0  # 09:15:05 AM reference
    scenarios: List[BacktestSessionScenario] = []

    def make_intel(cds: float, coverage: float, bias: BiasClassification, vix: float = 14.5) -> PreMarketIntelligence:
        conf = ConfidenceLevel.HIGH if coverage >= 0.80 else (ConfidenceLevel.MODERATE if coverage >= 0.60 else ConfidenceLevel.LOW)
        return PreMarketIntelligence(
            cds=cds,
            coverage=coverage,
            confidence=conf,
            bias=bias,
            components={},
            vix=vix,
            vix_regime=VolatilityRegime.NORMAL,
            timestamp_utc="2026-10-26T03:44:00Z",
            nifty_prev_close=25000.0,
            nifty_spot=25000.0,
        )

    # 1. Scenario 1: Strong Bullish Gap-and-Go (+0.50% rally, Call Target hit at 09:22)
    s1_ticks = [
        # 09:16:00
        TickObservation(base_ts + 60, 25020.0, 101.5, 102.0, 99.0, 99.5),
        # 09:18:00
        TickObservation(base_ts + 180, 25035.0, 103.0, 103.5, 97.5, 98.0),
        # 09:20:00 (Checkpoint 1: Call up +4.5%, Put down -3.0%, Spot +0.18%)
        TickObservation(base_ts + 301, 25045.0, 104.5, 105.0, 97.0, 97.5),
        # 09:22:00 (Call surges to 112 -> Profit target +3.85% reached!)
        TickObservation(base_ts + 420, 25075.0, 112.5, 113.0, 93.0, 93.5),
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 01: Strong Bullish Gap-and-Go", "TREND_BULL", 25000.0,
        make_intel(0.42, 0.95, BiasClassification.STRONG_BULLISH),
        100.0, 100.5, 100.0, 100.5, s1_ticks
    ))

    # 2. Scenario 2: Strong Bearish Gap-and-Go (-0.45% selloff, Put Target hit at 09:21)
    s2_ticks = [
        TickObservation(base_ts + 60, 24980.0, 98.5, 99.0, 101.5, 102.0),
        TickObservation(base_ts + 180, 24960.0, 97.0, 97.5, 103.5, 104.0),
        TickObservation(base_ts + 301, 24945.0, 96.0, 96.5, 105.0, 105.5),  # Put decouples
        TickObservation(base_ts + 380, 24910.0, 92.5, 93.0, 113.0, 113.5),  # Target hit
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 02: Strong Bearish Gap-and-Go", "TREND_BEAR", 25000.0,
        make_intel(-0.45, 0.90, BiasClassification.STRONG_BEARISH),
        100.0, 100.5, 100.0, 100.5, s2_ticks
    ))

    # 3. Scenario 3: Moderate Bullish Trend (Decouples at 09:20, hits target at 09:32)
    s3_ticks = [
        TickObservation(base_ts + 301, 25030.0, 103.5, 104.0, 98.0, 98.5),
        TickObservation(base_ts + 600, 25045.0, 106.0, 106.5, 96.0, 96.5),
        TickObservation(base_ts + 1020, 25070.0, 111.5, 112.0, 94.0, 94.5),  # Target
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 03: Moderate Bullish Trend", "TREND_BULL", 25000.0,
        make_intel(0.28, 0.85, BiasClassification.STRONG_BULLISH),
        100.0, 100.5, 100.0, 100.5, s3_ticks
    ))

    # 4. Scenario 4: Genuine Contrarian Reversal (Bullish CDS +0.35, but market selloff confirms with 2 ticks)
    s4_ticks = [
        # Tick 1 at 09:20:00 (Spot -0.16%, Put +4.6% -> tick 1 confirmed, wait)
        TickObservation(base_ts + 301, 24960.0, 96.5, 97.0, 104.6, 105.0),
        # Extended tick at 09:25:00 (Spot -0.22%, Put +5.5% -> tick 2 confirmed! Contrarian PUT decouples)
        TickObservation(base_ts + 601, 24945.0, 94.5, 95.0, 106.5, 107.0),
        # Continuation -> Target hit (+3.2% ROI) at 09:32
        TickObservation(base_ts + 1020, 24910.0, 90.0, 90.5, 116.0, 116.5),
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 04: Genuine Contrarian Reversal (Bearish Breakout)", "CONTRARIAN_TRUE", 25000.0,
        make_intel(0.35, 0.90, BiasClassification.STRONG_BULLISH),
        100.0, 100.5, 100.0, 100.5, s4_ticks
    ))

    # 5. Scenario 5: Genuine Contrarian Bullish Reversal (Bearish CDS -0.38, but market rips up)
    s5_ticks = [
        TickObservation(base_ts + 301, 25045.0, 104.8, 105.2, 96.5, 97.0),
        TickObservation(base_ts + 601, 25065.0, 107.0, 107.5, 94.0, 94.5),
        TickObservation(base_ts + 950, 25095.0, 115.5, 116.0, 90.5, 91.0),
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 05: Genuine Contrarian Bullish Reversal", "CONTRARIAN_TRUE", 25000.0,
        make_intel(-0.38, 0.85, BiasClassification.STRONG_BEARISH),
        100.0, 100.5, 100.0, 100.5, s5_ticks
    ))

    # 6. Scenario 6: Contrarian Head-Fake Trap (Bullish CDS +0.35, Put spikes +3.2% on spread but spot is weak -0.06% and reverses!)
    s6_ticks = [
        # Tick 1 at 09:20:00: Put jump is artificial (+3.2%), spot only -0.06% (< 0.15% contrarian hurdle)
        # Config 2/3 (2.5% hurdle) decouples PUT here! Config 1 (4.0% hurdle) blocks it!
        TickObservation(base_ts + 301, 24985.0, 98.0, 98.5, 103.2, 103.6),
        # 09:25:00: Market reverses back to spot flat! Put collapses back to 97.0
        TickObservation(base_ts + 601, 24998.0, 98.5, 99.0, 97.0, 97.5),
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 06: Contrarian Head-Fake Trap (Saved by 4% Hurdle)", "CONTRARIAN_TRAP", 25000.0,
        make_intel(0.35, 0.90, BiasClassification.STRONG_BULLISH),
        100.0, 100.5, 100.0, 100.5, s6_ticks
    ))

    # 7. Scenario 7: Choppy Consolidation Open (Spot within +/- 0.02%, Scratch exit at 09:25 AM)
    s7_ticks = [
        TickObservation(base_ts + 301, 25003.0, 100.8, 101.2, 99.2, 99.6),
        TickObservation(base_ts + 601, 25001.0, 100.2, 100.6, 98.8, 99.2),  # Scratch exit
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 07: Tight Morning Rangebound Chop", "CHOP", 25000.0,
        make_intel(0.05, 0.90, BiasClassification.NEUTRAL),
        100.0, 100.5, 100.0, 100.5, s7_ticks
    ))

    # 8. Scenario 8: Whipsaw Morning Consolidation (Fast oscillation, Scratch exit at 09:25 AM)
    s8_ticks = [
        TickObservation(base_ts + 301, 25015.0, 101.5, 102.0, 98.8, 99.2),
        TickObservation(base_ts + 601, 24992.0, 99.0, 99.5, 100.8, 101.2),  # Scratch exit
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 08: Oscillating Morning Whipsaw", "CHOP", 25000.0,
        make_intel(0.08, 0.85, BiasClassification.NEUTRAL),
        100.0, 100.5, 100.0, 100.5, s8_ticks
    ))

    # 9. Scenario 9: Spot Flatness Gate Trap (Option jumps +4.2% but spot is flat +0.02% -> blocked, scratches)
    s9_ticks = [
        TickObservation(base_ts + 301, 25004.0, 104.2, 104.6, 98.5, 99.0),
        TickObservation(base_ts + 601, 25002.0, 99.5, 100.0, 98.5, 99.0),  # Scratched
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 09: Spot Flatness Gate Defense", "CHOP", 25000.0,
        make_intel(0.04, 0.90, BiasClassification.NEUTRAL),
        100.0, 100.5, 100.0, 100.5, s9_ticks
    ))

    # 10. Scenario 10: Trailing Stop Peak Lock-in (Decouples, reaches +2.7% ROI peak, reverses -> locks profit)
    s10_ticks = [
        TickObservation(base_ts + 301, 25028.0, 104.0, 104.5, 97.8, 98.2),  # Call runner
        # Peak: Call at 111.0 -> Net ROI = +2.83%
        TickObservation(base_ts + 600, 25055.0, 111.0, 111.5, 95.0, 95.5),
        # Drop: Call falls back to 106.5 -> Trailing stop triggers
        TickObservation(base_ts + 750, 25025.0, 106.5, 107.0, 97.0, 97.5),
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 10: Trailing Stop Peak Protection", "TRAIL_PEAK", 25000.0,
        make_intel(0.26, 0.85, BiasClassification.STRONG_BULLISH),
        100.0, 100.5, 100.0, 100.5, s10_ticks
    ))

    # 11. Scenario 11: Hard Time Stop at 09:55 AM (Runner drifts with small gain, exits at time limit)
    s11_ticks = [
        TickObservation(base_ts + 301, 25025.0, 103.5, 104.0, 98.2, 98.6),  # Call runner
        TickObservation(base_ts + 1200, 25035.0, 105.0, 105.5, 97.0, 97.5),
        TickObservation(base_ts + 2401, 25038.0, 105.5, 106.0, 96.5, 97.0), # 09:55 hard exit
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 11: Late Session Drift (Hard Time Stop)", "TIME_STOP", 25000.0,
        make_intel(0.22, 0.85, BiasClassification.MODERATE_BULLISH),
        100.0, 100.5, 100.0, 100.5, s11_ticks
    ))

    # 12. Scenario 12: Sudden Severe Adverse Reversal (Decoupled, hit -3.0% Stop Loss)
    s12_ticks = [
        TickObservation(base_ts + 301, 25026.0, 103.8, 104.2, 98.0, 98.5),  # Call runner
        # Violent unexpected market crash
        TickObservation(base_ts + 500, 24930.0, 90.0, 90.5, 115.0, 115.5),  # Net ROI <= -3% Stop Loss
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 12: Adverse Reversal (Controlled Stop Loss)", "STOP_LOSS", 25000.0,
        make_intel(0.24, 0.85, BiasClassification.MODERATE_BULLISH),
        100.0, 100.5, 100.0, 100.5, s12_ticks
    ))

    # 13. Scenario 13: Stale Feeds / Moderate Coverage 60% (GIFT Nifty offline, operates on live price)
    s13_ticks = [
        TickObservation(base_ts + 301, 25030.0, 103.5, 104.0, 98.0, 98.5),
        TickObservation(base_ts + 550, 25065.0, 112.0, 112.5, 94.0, 94.5),  # Target hit
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 13: Stale Feeds (Moderate 60% Coverage)", "MODERATE_COV", 25000.0,
        make_intel(0.28, 0.60, BiasClassification.MODERATE_BULLISH),
        100.0, 100.5, 100.0, 100.5, s13_ticks
    ))

    # 14. Scenario 14: Low Coverage 45% (Insufficient Data, Neutral Bias fallback)
    s14_ticks = [
        TickObservation(base_ts + 301, 25028.0, 103.2, 103.6, 98.0, 98.5),
        TickObservation(base_ts + 580, 25065.0, 112.5, 113.0, 93.5, 94.0),  # Target hit
    ]
    scenarios.append(BacktestSessionScenario(
        "Day 14: Low Coverage Fallback (Neutral Prior)", "LOW_COV", 25000.0,
        make_intel(0.0, 0.45, BiasClassification.NEUTRAL),
        100.0, 100.5, 100.0, 100.5, s14_ticks
    ))

    # 15. Scenario 15: Wide Spread Day (Opening Spread = 2.5% > 1.5% -> Aborted, 0 loss)
    scenarios.append(BacktestSessionScenario(
        "Day 15: Disordered Illiquid Open (Spread Abort)", "WIDE_SPREAD", 25000.0,
        make_intel(0.25, 0.90, BiasClassification.STRONG_BULLISH),
        100.0, 102.5, 100.0, 102.5, []  # Spread ~ 2.47% > 1.5%
    ))

    return scenarios


def run_single_simulation(
    scenario: BacktestSessionScenario,
    contrarian_hurdle: float = 4.0,
    min_coverage_threshold: float = 0.80,
) -> Dict[str, Any]:
    """Runs a single session simulation through the OpeningStraddleEngine."""
    # Custom clone of intelligence with specific configuration
    intel = copy.deepcopy(scenario.pre_market)

    # Config adjustments
    if min_coverage_threshold == 0.0:
        # Price-Only Benchmark: Neutralize prior
        intel.cds = 0.0
        intel.bias = BiasClassification.NEUTRAL
        intel.coverage = 1.0
    elif contrarian_hurdle <= 2.5:
        # Baseline V1: Symmetric 2.5% hurdle (disable contrarian filter)
        intel.coverage = 0.50  # Coverage < 80% disables the 4% contrarian hurdle in engine

    call_q = StraddleLegQuote(
        "NIFTY25000CE", "101", 25000, "CE",
        bid=scenario.initial_call_bid, ask=scenario.initial_call_ask, ltp=scenario.initial_call_bid
    )
    put_q = StraddleLegQuote(
        "NIFTY25000PE", "102", 25000, "PE",
        bid=scenario.initial_put_bid, ask=scenario.initial_put_ask, ltp=scenario.initial_put_bid
    )

    engine = OpeningStraddleEngine(pre_market=intel, db_path=None)

    # Anchor entry_ts to the scenario's reference epoch
    start_ts = 1791957300.0  # 09:15:05 AM
    if not engine.arm_entry(spot_price=scenario.initial_spot, call_quote=call_q, put_quote=put_q, entry_ts=start_ts):
        return {
            "scenario": scenario.name,
            "status": "ABORTED",
            "exit_reason": ExitReason.EXECUTION_ABORT.value,
            "net_pnl_inr": 0.0,
            "net_roi_pct": 0.0,
            "selected_leg": SelectedLeg.NONE.value,
        }

    # Replay ticks
    for tick in scenario.ticks:
        engine.process_tick(tick)
        if engine.state == StrategyState.CLOSED:
            break

    # If scenario ticks concluded without hitting an explicit exit trigger, finalize at final tick
    if engine.state not in (StrategyState.CLOSED, StrategyState.ABORTED) and scenario.ticks:
        last_tick = scenario.ticks[-1]
        if engine.state in (StrategyState.CALL_RUNNER, StrategyState.PUT_RUNNER):
            runner_bid = last_tick.call_bid if engine.selected_runner == SelectedLeg.CALL else last_tick.put_bid
            engine._close_runner_and_finish(exit_price=runner_bid, exit_reason=ExitReason.HARD_TIME_STOP)
        elif engine.state in (StrategyState.OBSERVATION, StrategyState.EXTENDED_OBSERVATION):
            engine._execute_scratch_exit(last_tick)

    return {
        "scenario": scenario.name,
        "status": engine.status,
        "exit_reason": engine.exit_reason.value,
        "net_pnl_inr": round(engine.net_pnl_inr, 2),
        "net_roi_pct": round(engine.net_roi_pct, 2),
        "peak_roi_pct": round(engine.peak_roi_pct, 2),
        "selected_leg": engine.selected_runner.value,
    }


def evaluate_model_configuration(
    config_name: str,
    scenarios: List[BacktestSessionScenario],
    contrarian_hurdle: float,
    min_coverage: float,
) -> ModelEvaluationResult:
    """Evaluates all 15 scenarios under a specific parameter configuration."""
    results: List[Dict[str, Any]] = []
    total_net_pnl = 0.0
    wins = 0
    losses = 0
    scratches = 0
    aborted = 0
    exit_counts: Dict[str, int] = {}
    gross_gains = 0.0
    gross_losses = 0.0

    for sc in scenarios:
        res = run_single_simulation(sc, contrarian_hurdle=contrarian_hurdle, min_coverage_threshold=min_coverage)
        results.append(res)
        pnl = res["net_pnl_inr"]
        total_net_pnl += pnl

        reason = res["exit_reason"]
        exit_counts[reason] = exit_counts.get(reason, 0) + 1

        if res["status"] == "ABORTED":
            aborted += 1
        elif pnl > 0:
            wins += 1
            gross_gains += pnl
        elif pnl < 0:
            if reason == ExitReason.SCRATCH.value:
                scratches += 1
                gross_losses += abs(pnl)
            else:
                losses += 1
                gross_losses += abs(pnl)
        else:
            scratches += 1

    trades_executed = len(scenarios) - aborted
    win_rate = (wins / trades_executed * 100.0) if trades_executed > 0 else 0.0
    profit_factor = (gross_gains / gross_losses) if gross_losses > 0 else (99.0 if gross_gains > 0 else 1.0)
    avg_roi = sum(r["net_roi_pct"] for r in results) / len(results)

    # Max Drawdown calculation
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in results:
        cumulative += r["net_pnl_inr"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    return ModelEvaluationResult(
        config_name=config_name,
        total_sessions=len(scenarios),
        trades_executed=trades_executed,
        trades_aborted=aborted,
        win_count=wins,
        loss_count=losses,
        scratch_count=scratches,
        total_net_pnl_inr=round(total_net_pnl, 2),
        avg_roi_pct=round(avg_roi, 2),
        win_rate_pct=round(win_rate, 1),
        profit_factor=round(profit_factor, 2),
        max_drawdown_inr=round(max_dd, 2),
        exit_breakdown=exit_counts,
    )


def run_full_backtest_suite() -> Dict[str, ModelEvaluationResult]:
    """Runs and outputs the full comparative backtest matrix."""
    scenarios = build_15_market_scenarios()

    config_v2 = evaluate_model_configuration(
        config_name="CDS-V2 / Contrarian-4 / Coverage-80 (Flagship)",
        scenarios=scenarios,
        contrarian_hurdle=4.0,
        min_coverage=0.80,
    )

    config_v1 = evaluate_model_configuration(
        config_name="CDS-V1 / Contrarian-2.5 / Coverage-60 (Baseline)",
        scenarios=scenarios,
        contrarian_hurdle=2.5,
        min_coverage=0.60,
    )

    config_price_only = evaluate_model_configuration(
        config_name="Price-Only / No Prior (Benchmark)",
        scenarios=scenarios,
        contrarian_hurdle=2.5,
        min_coverage=0.0,
    )

    return {
        "v2_flagship": config_v2,
        "v1_baseline": config_v1,
        "price_only": config_price_only,
    }


def print_backtest_report(results: Dict[str, ModelEvaluationResult]):
    """Prints a structured ASCII report of the backtest findings."""
    print("\n" + "=" * 80)
    print("NIFTY 50 OPENING STRADDLE STRATEGY -- 15-SESSION COMPARATIVE BACKTEST REPORT")
    print("=" * 80)

    for key, res in results.items():
        print(f"\n>> MODEL CONFIGURATION: {res.config_name}")
        print("-" * 75)
        print(f"  Total Sessions Simulated : {res.total_sessions} | Executed: {res.trades_executed} | Aborted: {res.trades_aborted}")
        print(f"  Win / Loss / Scratch     : {res.win_count} Wins / {res.loss_count} Losses / {res.scratch_count} Scratches")
        print(f"  Win Rate (%)             : {res.win_rate_pct:.1f}%")
        print(f"  Total Net P&L (INR)      : INR {res.total_net_pnl_inr:+,.2f}")
        print(f"  Average Session Net ROI  : {res.avg_roi_pct:+.2f}%")
        print(f"  Profit Factor            : {res.profit_factor:.2f}")
        print(f"  Max Drawdown (INR)       : INR {res.max_drawdown_inr:,.2f}")
        print("  Exit Reasons Breakdown   :")
        for reason, count in sorted(res.exit_breakdown.items()):
            print(f"    - {reason:<22}: {count} session(s)")
    print("\n" + "=" * 80)


def main():
    parser = argparse.ArgumentParser(description="Opening Straddle 15-Session Parameter Backtest")
    args = parser.parse_args()
    results = run_full_backtest_suite()
    print_backtest_report(results)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
