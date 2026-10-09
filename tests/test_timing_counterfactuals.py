import datetime
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.storage import init_db, insert_timing_shadow_entry
from scripts.compare_entry_timing_counterfactuals import (
    get_surface_quote_for_strike,
    extract_prices_from_signals_db,
    analyze_weekly_cycle,
    run_timing_comparison,
    simulate_fsm_run,
)


class TimingCounterfactualsTests(unittest.TestCase):
    def test_get_surface_quote_for_strike(self):
        surface = {
            "25000": {
                "CALL": {"ltp": 250.5, "close": 248.0},
                "PUT": {"ltp": 210.0, "close": 212.0},
            }
        }
        self.assertEqual(get_surface_quote_for_strike(surface, 25000, "CALL"), 250.5)
        self.assertEqual(get_surface_quote_for_strike(surface, 25000, "PUT"), 210.0)
        self.assertIsNone(get_surface_quote_for_strike(surface, 25100, "CALL"))

    def test_analyze_weekly_cycle_with_recorded_shadows(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            strat_db = str(Path(temp_dir) / "nifty50_weekly_data_20261006_20261012.db")
            init_db(strat_db)

            # Insert strategy hourly data
            conn = sqlite3.connect(strat_db)
            conn.execute("""
                INSERT INTO strategy_hourly_data (
                    strategy_name, collection_timestamp, expiry_date,
                    put_strike, call_strike, put_ltp, call_ltp,
                    call_buy_price, put_buy_price, cycle_id, nifty_ltp
                ) VALUES (
                    'test', 1786452660, '20261012',
                    25200, 25000, 200.0, 300.0,
                    280.0, 270.0, 'CYCLE-TEST', 25100.0
                )
            """)
            # Final row at expiry
            conn.execute("""
                INSERT INTO strategy_hourly_data (
                    strategy_name, collection_timestamp, expiry_date,
                    put_strike, call_strike, put_ltp, call_ltp,
                    call_buy_price, put_buy_price, cycle_id, nifty_ltp
                ) VALUES (
                    'test', 1786950000, '20261012',
                    25200, 25000, 100.0, 400.0,
                    280.0, 270.0, 'CYCLE-TEST', 25300.0
                )
            """)
            conn.commit()
            conn.close()

            # Insert timing shadow entries
            shadow_v0 = {
                "cycle_id": "CYCLE-TEST",
                "trade_date": "2026-10-06",
                "variant": "V0_baseline_0931",
                "call_strike": 25000,
                "put_strike": 25200,
                "call_entry_time": "09:31:00",
                "call_entry_price": 280.0,
                "call_entry_ts": 1786452660,
                "put_entry_time": "09:31:00",
                "put_entry_price": 270.0,
                "put_entry_ts": 1786452660,
                "combined_entry_price": 550.0,
                "spot_at_call_entry": 25100.0,
                "spot_at_put_entry": 25100.0,
                "nifty_spot_entry": 25100.0,
                "moneyness": "ITM",
                "created_at": 1786452660,
            }
            shadow_v1 = dict(shadow_v0)
            shadow_v1["variant"] = "V1_delayed_1000"
            shadow_v1["call_entry_price"] = 270.0
            shadow_v1["put_entry_price"] = 260.0
            shadow_v1["combined_entry_price"] = 530.0

            insert_timing_shadow_entry(strat_db, shadow_v0)
            insert_timing_shadow_entry(strat_db, shadow_v1)

            res = analyze_weekly_cycle(strat_db, signals_dir=temp_dir, exit_model="hold_to_expiry")
            self.assertIsNotNone(res)
            self.assertEqual(res["cycle_id"], "CYCLE-TEST")
            self.assertEqual(res["variants"]["V0_baseline_0931"]["entry_cost"], 550.0)
            self.assertEqual(res["variants"]["V1_delayed_1000"]["entry_cost"], 530.0)
            # Terminal payoff = 100 + 400 = 500
            # V0 PnL = 500 - 550 = -50
            # V1 PnL = 500 - 530 = -30
            self.assertEqual(res["variants"]["V0_baseline_0931"]["pnl_pts"], -50.0)
            self.assertEqual(res["variants"]["V1_delayed_1000"]["pnl_pts"], -30.0)
            self.assertEqual(res["edge"]["v1_cheaper_by_pts"], 20.0)
            self.assertEqual(res["edge"]["v1_pnl_advantage_pts"], 20.0)

            # Test run_timing_comparison
            results = run_timing_comparison(strategies_dir=temp_dir, signals_dir=temp_dir, target_db=strat_db)
            self.assertEqual(len(results), 1)

    def test_extract_prices_from_signals_db(self):
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        UTC = pytz.UTC

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            sig_db = str(Path(temp_dir) / "signals_data_2026_10.db")
            conn = sqlite3.connect(sig_db)
            conn.execute("""
                CREATE TABLE market_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    pk TEXT NOT NULL,
                    price REAL,
                    data TEXT
                )
            """)

            dt_0930 = IST.localize(datetime.datetime(2026, 10, 6, 9, 30))
            ts_0930 = int(dt_0930.astimezone(UTC).timestamp())

            dt_1000 = IST.localize(datetime.datetime(2026, 10, 6, 10, 0))
            ts_1000 = int(dt_1000.astimezone(UTC).timestamp())

            surface_0930 = {
                "option_chain_surface": {
                    "25000": {"CALL": {"ltp": 290.0}},
                    "25200": {"PUT": {"ltp": 270.0}},
                }
            }
            surface_1000 = {
                "option_chain_surface": {
                    "25000": {"CALL": {"ltp": 280.0}},
                    "25200": {"PUT": {"ltp": 260.0}},
                }
            }

            conn.execute(
                "INSERT INTO market_data (ts, pk, price, data) VALUES (?, 'INDEX#NIFTY50', ?, ?)",
                (ts_0930, 25100.0, json.dumps(surface_0930))
            )
            conn.execute(
                "INSERT INTO market_data (ts, pk, price, data) VALUES (?, 'INDEX#NIFTY50', ?, ?)",
                (ts_1000, 25120.0, json.dumps(surface_1000))
            )
            conn.commit()
            conn.close()

            p_09, p_10 = extract_prices_from_signals_db(sig_db, "2026-10-06", 25000, 25200)
            self.assertIsNotNone(p_09)
            self.assertIsNotNone(p_10)
            self.assertEqual(p_09["call_ltp"], 290.0)
            self.assertEqual(p_09["put_ltp"], 270.0)
            self.assertEqual(p_10["call_ltp"], 280.0)
            self.assertEqual(p_10["put_ltp"], 260.0)

    def test_simulate_fsm_run_filters_pre_entry_ticks(self):
        strategy_rows = [
            {"ts": 1000, "call_ltp": 300.0, "put_ltp": 300.0, "nifty_ltp": 25000.0},
            {"ts": 2000, "call_ltp": 250.0, "put_ltp": 250.0, "nifty_ltp": 25000.0},
            {"ts": 3000, "call_ltp": 200.0, "put_ltp": 200.0, "nifty_ltp": 25000.0},
        ]
        expiry_dt = datetime.date(2026, 10, 12)
        # Entry at ts=2000: row at ts=1000 should be ignored
        res = simulate_fsm_run(
            strategy_rows=strategy_rows,
            call_strike=25000,
            put_strike=25000,
            call_buy=250.0,
            put_buy=250.0,
            expiry_dt=expiry_dt,
            sqlite_dir="dummy",
            entry_ts=2000,
        )
        self.assertIn("total_gainloss", res)
        self.assertIn("final_state", res)
        self.assertIn("roi_pct", res)

    def test_analyze_weekly_cycle_fsm_sim_mode(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            strat_db = str(Path(temp_dir) / "nifty50_weekly_data_20261006_20261012.db")
            init_db(strat_db)

            conn = sqlite3.connect(strat_db)
            # Insert ticks
            conn.execute("""
                INSERT INTO strategy_hourly_data (
                    strategy_name, collection_timestamp, expiry_date,
                    put_strike, call_strike, put_ltp, call_ltp,
                    call_buy_price, put_buy_price, cycle_id, nifty_ltp
                ) VALUES (
                    'test', 1786452660, '20261012',
                    25200, 25000, 270.0, 280.0,
                    280.0, 270.0, 'CYCLE-TEST', 25100.0
                )
            """)
            conn.execute("""
                INSERT INTO strategy_hourly_data (
                    strategy_name, collection_timestamp, expiry_date,
                    put_strike, call_strike, put_ltp, call_ltp,
                    call_buy_price, put_buy_price, cycle_id, nifty_ltp
                ) VALUES (
                    'test', 1786454460, '20261012',
                    25200, 25000, 260.0, 270.0,
                    280.0, 270.0, 'CYCLE-TEST', 25120.0
                )
            """)
            conn.execute("""
                INSERT INTO strategy_hourly_data (
                    strategy_name, collection_timestamp, expiry_date,
                    put_strike, call_strike, put_ltp, call_ltp,
                    call_buy_price, put_buy_price, cycle_id, nifty_ltp
                ) VALUES (
                    'test', 1786950000, '20261012',
                    25200, 25000, 100.0, 400.0,
                    280.0, 270.0, 'CYCLE-TEST', 25300.0
                )
            """)
            conn.commit()
            conn.close()

            # Insert timing shadow entries for V0 and V1
            shadow_v0 = {
                "cycle_id": "CYCLE-TEST",
                "trade_date": "2026-10-06",
                "variant": "V0_baseline_0931",
                "call_strike": 25000,
                "put_strike": 25200,
                "call_entry_time": "09:31:00",
                "call_entry_price": 280.0,
                "call_entry_ts": 1786452660,
                "put_entry_time": "09:31:00",
                "put_entry_price": 270.0,
                "put_entry_ts": 1786452660,
                "combined_entry_price": 550.0,
                "spot_at_call_entry": 25100.0,
                "spot_at_put_entry": 25100.0,
                "nifty_spot_entry": 25100.0,
                "moneyness": "ITM",
                "created_at": 1786452660,
            }
            shadow_v1 = dict(shadow_v0)
            shadow_v1["variant"] = "V1_delayed_1000"
            shadow_v1["call_entry_price"] = 270.0
            shadow_v1["put_entry_price"] = 260.0
            shadow_v1["combined_entry_price"] = 530.0
            shadow_v1["call_entry_ts"] = 1786454460
            shadow_v1["put_entry_ts"] = 1786454460

            insert_timing_shadow_entry(strat_db, shadow_v0)
            insert_timing_shadow_entry(strat_db, shadow_v1)

            res = analyze_weekly_cycle(strat_db, signals_dir=temp_dir, exit_model="fsm_sim")
            self.assertIsNotNone(res)
            self.assertEqual(res["exit_model"], "fsm_sim")
            self.assertIn("V0_baseline_0931", res["variants"])
            self.assertIn("V1_delayed_1000", res["variants"])
            self.assertIn("status", res["variants"]["V0_baseline_0931"])
            self.assertIn("pnl_pts", res["variants"]["V0_baseline_0931"])


if __name__ == "__main__":
    unittest.main()
