import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.fsm_strategy import (
    init_fsm_state,
    evaluate_fsm_tick,
    fetch_option_surface_iv,
)
from common.storage import init_db, insert_record


class FSMStrategyTests(unittest.TestCase):
    def test_init_fsm_state(self):
        state = init_fsm_state(
            call_strike=24600,
            put_strike=24400,
            call_buy_price=100.0,
            put_buy_price=80.0,
            entry_ts=1700000000,
            composite_score=0.42,
        )
        self.assertEqual(state["state"], "DUAL_LONG")
        self.assertEqual(state["total_initial_cost"], 180.0)
        self.assertEqual(state["composite_direction_score"], 0.42)
        self.assertEqual(state["call_leg"]["status"], "ACTIVE")
        self.assertEqual(state["put_leg"]["status"], "ACTIVE")
        self.assertFalse(state["free_roll_active"])

    def test_fsm_harvest_rule_locks_house_money(self):
        # Entry: Call=100, Put=80 (Total cost = 180)
        state = init_fsm_state(
            call_strike=24600,
            put_strike=24400,
            call_buy_price=100.0,
            put_buy_price=80.0,
            entry_ts=1700000000,
        )
        # Call surges to 285 (Gain = 185 >= total cost 180 and gain >= 100%)
        updated, events = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=285.0,
            current_put_ltp=20.0,
            current_ts=1700003600,
            days_to_expiry=3.0,
            call_delta=0.65,
            put_delta=-0.12,
        )
        self.assertEqual(updated["call_leg"]["status"], "HARVESTED_CLOSED")
        self.assertEqual(updated["call_leg"]["exit_price"], 285.0)
        self.assertEqual(updated["call_leg"]["realized_pnl"], 185.0)
        self.assertTrue(updated["free_roll_active"])
        self.assertEqual(updated["state"], "SOLO_PUT")
        self.assertTrue(any("House Money secured" in e for e in events))

    def test_fsm_salvage_rule_cuts_dead_leg(self):
        # Entry: Call=100, Put=80
        state = init_fsm_state(
            call_strike=24600,
            put_strike=24400,
            call_buy_price=100.0,
            put_buy_price=80.0,
            entry_ts=1700000000,
        )
        # Put drops to 30 (loss = -62.5% <= -55%), delta=-0.08 (<0.10), price >= 15
        updated, events = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=150.0,
            current_put_ltp=30.0,
            current_ts=1700003600,
            days_to_expiry=3.0,
            call_delta=0.48,
            put_delta=-0.08,
        )
        self.assertEqual(updated["put_leg"]["status"], "SALVAGED_CLOSED")
        self.assertEqual(updated["put_leg"]["exit_price"], 30.0)
        self.assertEqual(updated["put_leg"]["realized_pnl"], -50.0)
        self.assertEqual(updated["state"], "SOLO_CALL")
        self.assertTrue(any("SALVAGED" in e for e in events))

    def test_fsm_trailing_stop_protection(self):
        # Call enters at 100, rallies to peak 200, then falls back below trailing stop
        state = init_fsm_state(
            call_strike=24600,
            put_strike=24400,
            call_buy_price=100.0,
            put_buy_price=80.0,
            entry_ts=1700000000,
        )
        # 1st tick: Call peaks at 180 (gain=80% < 100%, days_to_expiry=6.0 on Tue/Wed -> trailing stop set to 180 * (1 - 0.25) = 135)
        state_t1, _ = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=180.0,
            current_put_ltp=60.0,
            current_ts=1700003600,
            days_to_expiry=6.0,
            call_delta=0.52,
            put_delta=-0.28,
        )
        self.assertEqual(state_t1["call_leg"]["peak_ltp"], 180.0)
        self.assertEqual(state_t1["call_leg"]["trailing_stop"], 135.0)

        # 2nd tick: Call pulls back to 130 (< trailing stop 135)
        state_t2, events = evaluate_fsm_tick(
            fsm_state=state_t1,
            current_call_ltp=130.0,
            current_put_ltp=55.0,
            current_ts=1700007200,
            days_to_expiry=6.0,
            call_delta=0.44,
            put_delta=-0.25,
        )
        self.assertEqual(state_t2["call_leg"]["status"], "TRAILING_STOP_CLOSED")
        self.assertEqual(state_t2["call_leg"]["exit_price"], 130.0)
        self.assertEqual(state_t2["call_leg"]["realized_pnl"], 30.0)
        self.assertTrue(any("CALL TRAILING STOP" in e for e in events))

    def test_fsm_trailing_stop_ladder_thresholds(self):
        """Verify trailing stop ladder: >5.0 DTE (25%), 1.5-5.0 DTE (18%), <=1.5 DTE (10%)."""
        base_state = init_fsm_state(24600, 24400, 100.0, 80.0, 1700000000)

        # Thursday (DTE = 4.5): 18% buffer -> 200 * (1 - 0.18) = 164.0
        thu_state, _ = evaluate_fsm_tick(
            fsm_state=base_state,
            current_call_ltp=200.0,
            current_put_ltp=50.0,
            current_ts=1700003600,
            days_to_expiry=4.5,
        )
        self.assertEqual(thu_state["call_leg"]["trailing_stop"], 164.0)

        # Monday (DTE = 1.0): 10% buffer -> 200 * (1 - 0.10) = 180.0
        mon_state, _ = evaluate_fsm_tick(
            fsm_state=base_state,
            current_call_ltp=200.0,
            current_put_ltp=50.0,
            current_ts=1700003600,
            days_to_expiry=1.0,
        )
        self.assertEqual(mon_state["call_leg"]["trailing_stop"], 180.0)

    def test_fsm_salvage_rule_low_premium_option(self):
        """Verify that an option bought at ₹25 can be salvaged at ₹10 (price >= 5.0)."""
        state = init_fsm_state(24600, 24400, 25.0, 30.0, 1700000000)
        # Call bought at 25 drops to 10 (loss = -60% <= -55%), delta = 0.07 (< 0.10), price = 10 >= 5.0
        updated, events = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=10.0,
            current_put_ltp=50.0,
            current_ts=1700003600,
            days_to_expiry=2.0,
            call_delta=0.07,
            put_delta=-0.45,
        )
        self.assertEqual(updated["call_leg"]["status"], "SALVAGED_CLOSED")
        self.assertEqual(updated["call_leg"]["exit_price"], 10.0)
        self.assertEqual(updated["call_leg"]["realized_pnl"], -15.0)

    def test_storage_auto_migration_fsm_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "test_strat.db")
            init_db(db_path)

            conn = sqlite3.connect(db_path)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(strategy_hourly_data)")}
            finally:
                conn.close()

            expected = [
                "fsm_state", "fsm_call_status", "fsm_put_status",
                "fsm_call_exit_price", "fsm_put_exit_price",
                "fsm_realized_pnl", "fsm_unrealized_pnl",
                "fsm_total_gainloss", "fsm_roi_pct"
            ]
            for c in expected:
                self.assertIn(c, cols)

    def test_fsm_itm_gut_harvest_against_extrinsic_risk(self):
        """ITM Long Gut: House money harvest triggers when leg gain covers total extrinsic risk."""
        # Entry: Call 24400 @ 170.0 (intrinsic=100, extrinsic=70)
        #        Put 24600 @ 150.0 (intrinsic=100, extrinsic=50)
        # Total cost = 320.0, Spread = 200, Extrinsic risk = 120.0
        state = init_fsm_state(
            call_strike=24400,
            put_strike=24600,
            call_buy_price=170.0,
            put_buy_price=150.0,
            entry_ts=1700000000,
            moneyness="ITM",
            call_intrinsic=100.0,
            call_extrinsic=70.0,
            put_intrinsic=100.0,
            put_extrinsic=50.0,
            spread_width=200,
        )
        self.assertEqual(state["moneyness"], "ITM")
        self.assertEqual(state["total_extrinsic_risk"], 120.0)

        # Call surges to 295 (gain = 125 >= 120 extrinsic risk)
        updated, events = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=295.0,
            current_put_ltp=80.0,
            current_ts=1700003600,
            days_to_expiry=3.0,
            call_delta=0.82,
            put_delta=-0.40,
        )
        self.assertEqual(updated["call_leg"]["status"], "HARVESTED_CLOSED")
        self.assertEqual(updated["call_leg"]["exit_price"], 295.0)
        self.assertEqual(updated["call_leg"]["realized_pnl"], 125.0)
        self.assertTrue(updated["free_roll_active"])
        self.assertIn("Extrinsic Risk", updated["call_leg"]["exit_reason"])
        self.assertTrue(any("House Money secured" in e for e in events))

    def test_fsm_itm_gut_salvage_preserves_intrinsic(self):
        """ITM Long Gut: Losing leg salvaged when loss >= 50% and delta drops below 0.35."""
        state = init_fsm_state(
            call_strike=24400,
            put_strike=24600,
            call_buy_price=170.0,
            put_buy_price=150.0,
            entry_ts=1700000000,
            moneyness="ITM",
        )
        # Call drops to 80 (loss = -52.9% <= -50%), delta falls to 0.28 (< 0.35)
        updated, events = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=80.0,
            current_put_ltp=200.0,
            current_ts=1700003600,
            days_to_expiry=3.0,
            call_delta=0.28,
            put_delta=-0.75,
        )
        self.assertEqual(updated["call_leg"]["status"], "SALVAGED_CLOSED")
        self.assertEqual(updated["call_leg"]["exit_price"], 80.0)
        self.assertEqual(updated["state"], "SOLO_PUT")
        self.assertTrue(any("SALVAGED" in e for e in events))

    def test_fsm_put_iv_spike_acceleration(self):
        """Put trailing stop tightens to 8% buffer when in profit (>=30%) and |Delta| >= 0.70."""
        state = init_fsm_state(
            call_strike=24400,
            put_strike=24600,
            call_buy_price=170.0,
            put_buy_price=150.0,
            entry_ts=1700000000,
            moneyness="ITM",
        )
        # Put rises to 200 (gain = 33% >= 30%) with Put delta = -0.75 (|Delta| >= 0.70)
        # 8% buffer -> Stop = 200 * (1 - 0.08) = 184.0
        updated, _ = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=120.0,
            current_put_ltp=200.0,
            current_ts=1700003600,
            days_to_expiry=3.0,  # Ordinarily 18% buffer (would be 164.0)
            call_delta=0.35,
            put_delta=-0.75,
        )
        self.assertEqual(updated["put_leg"]["trailing_stop"], 184.0)

    def test_fsm_legacy_otm_state_without_moneyness_key(self):
        """Legacy OTM state without 'moneyness' key must infer OTM from strike geometry (call_strike > put_strike)."""
        # Legacy OTM strangle: CE 24600, PE 24400
        state = {
            "state": "DUAL_LONG",
            "entry_ts": 1700000000,
            "total_initial_cost": 200.0,
            "realized_pnl_pts": 0.0,
            "unrealized_pnl_pts": 0.0,
            "total_gainloss": 0.0,
            "roi_pct": 0.0,
            "call_leg": {
                "strike": 24600,
                "buy_price": 100.0,
                "status": "ACTIVE",
                "trailing_stop": 0.0,
                "peak_ltp": 100.0,
            },
            "put_leg": {
                "strike": 24400,
                "buy_price": 100.0,
                "status": "ACTIVE",
                "trailing_stop": 0.0,
                "peak_ltp": 100.0,
            },
            # NOTE: No "moneyness" key present
        }

        # Case A: Call LTP drops to 48 (52% loss), Delta = 0.25.
        # Under ITM rules, 50% loss & Delta < 0.35 would have triggered salvage.
        # Under OTM rules, salvage requires <= 45% (55% loss) AND Delta < 0.10.
        # It must NOT salvage under OTM rules!
        updated_a, events_a = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=48.0,
            current_put_ltp=100.0,
            current_ts=1700003600,
            days_to_expiry=3.0,
            call_delta=0.25,
            put_delta=-0.35,
        )
        self.assertEqual(updated_a["call_leg"]["status"], "ACTIVE")

        # Case B: Call LTP drops to 40 (60% loss), Delta = 0.08 (< 0.10).
        # Meets OTM salvage rule: <= 45% & Delta < 0.10.
        updated_b, events_b = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=40.0,
            current_put_ltp=100.0,
            current_ts=1700003600,
            days_to_expiry=3.0,
            call_delta=0.08,
            put_delta=-0.35,
        )
        self.assertEqual(updated_b["call_leg"]["status"], "SALVAGED_CLOSED")
        self.assertEqual(updated_b["state"], "SOLO_PUT")

    def test_fsm_legacy_itm_state_without_moneyness_key(self):
        """Legacy ITM state without 'moneyness' key must infer ITM from strike geometry (put_strike > call_strike)."""
        # ITM Long Gut: CE 24400, PE 24600
        state = {
            "state": "DUAL_LONG",
            "entry_ts": 1700000000,
            "total_initial_cost": 320.0,
            "realized_pnl_pts": 0.0,
            "unrealized_pnl_pts": 0.0,
            "total_gainloss": 0.0,
            "roi_pct": 0.0,
            "call_leg": {
                "strike": 24400,
                "buy_price": 170.0,
                "status": "ACTIVE",
                "trailing_stop": 0.0,
                "peak_ltp": 170.0,
            },
            "put_leg": {
                "strike": 24600,
                "buy_price": 150.0,
                "status": "ACTIVE",
                "trailing_stop": 0.0,
                "peak_ltp": 150.0,
            },
            # NOTE: No "moneyness" key present
        }

        # Call LTP drops to 80 (<= 170 * 0.50 = 85), Delta = 0.28 (< 0.35).
        # ITM salvage trigger fires!
        updated, events = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=80.0,
            current_put_ltp=150.0,
            current_ts=1700003600,
            days_to_expiry=3.0,
            call_delta=0.28,
            put_delta=-0.70,
        )
        self.assertEqual(updated["call_leg"]["status"], "SALVAGED_CLOSED")
        self.assertEqual(updated["state"], "SOLO_PUT")

    def test_fsm_iv_spike_tightens_trail_via_put_iv(self):
        """Put trailing stop tightens to 8% when put_iv >= PUT_IV_SPIKE_THRESHOLD even with modest delta."""
        state = init_fsm_state(
            call_strike=24400,
            put_strike=24600,
            call_buy_price=170.0,
            put_buy_price=150.0,
            entry_ts=1700000000,
            moneyness="ITM",
        )
        # Put rises to 200 (in profit). Put delta is only -0.55 (not -0.70).
        # But put_iv = 21.5 (>= 18.0 threshold) -> 8% buffer accelerates trailing stop to 200 * 0.92 = 184.0
        updated, _ = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=120.0,
            current_put_ltp=200.0,
            current_ts=1700003600,
            days_to_expiry=3.0,
            call_delta=0.45,
            put_delta=-0.55,
            put_iv=21.5,
        )
        self.assertEqual(updated["put_leg"]["trailing_stop"], 184.0)

    def test_fsm_call_iv_spike_tightens_trail(self):
        """Call trailing stop tightens to 8% buffer when call_iv >= PUT_IV_SPIKE_THRESHOLD."""
        state = init_fsm_state(
            call_strike=24400,
            put_strike=24600,
            call_buy_price=170.0,
            put_buy_price=150.0,
            entry_ts=1700000000,
            moneyness="ITM",
        )
        # Call rises to 230 (in profit). Call delta is 0.58.
        # call_iv = 19.5 (>= 18.0 threshold) -> 8% buffer accelerates stop to 230 * 0.92 = 211.6
        updated, _ = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=230.0,
            current_put_ltp=110.0,
            current_ts=1700003600,
            days_to_expiry=3.0,
            call_delta=0.58,
            put_delta=-0.42,
            call_iv=19.5,
        )
        self.assertEqual(updated["call_leg"]["trailing_stop"], 211.6)

    def test_fetch_option_surface_iv_from_signals_db(self):
        """fetch_option_surface_iv reads surface implied_volatility and falls back to VIX correctly."""
        import json
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Test 1: No DB present -> returns None, None safely
            c_iv, p_iv = fetch_option_surface_iv(24400, 24600, sqlite_dir=tmp_dir)
            self.assertIsNone(c_iv)
            self.assertIsNone(p_iv)

            # Test 2: Create mock signals_data_2026_10.db
            db_path = Path(tmp_dir) / "signals_data_2026_10.db"
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE market_data (
                    pk TEXT,
                    ts INTEGER,
                    vix REAL,
                    data TEXT
                )
            """)
            surface_payload = {
                "option_chain_surface": {
                    "24400": {
                        "CALL": {"implied_volatility": 19.2},
                        "PUT": {"implied_volatility": 21.4}
                    },
                    "24600": {
                        "CALL": {"implied_volatility": 18.8},
                        "PUT": {"implied_volatility": 22.6}
                    }
                }
            }
            conn.execute(
                "INSERT INTO market_data VALUES ('INDEX#NIFTY50', 1791100000, 16.5, ?)",
                (json.dumps(surface_payload),)
            )
            conn.commit()
            conn.close()

            c_iv, p_iv = fetch_option_surface_iv(24400, 24600, sqlite_dir=tmp_dir, reference_ts=1791100000)
            self.assertEqual(c_iv, 19.2)
            self.assertEqual(p_iv, 22.6)

    def test_fetch_option_surface_iv_with_strategies_subdir_normalization(self):
        """Production layout regression: passing /sqlite/strategies/ correctly finds signals_data in /sqlite/ parent."""
        import json
        with tempfile.TemporaryDirectory() as tmp_dir:
            # DB lives in parent dir: tmp_dir / signals_data_2026_10.db
            db_path = Path(tmp_dir) / "signals_data_2026_10.db"
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE market_data (
                    pk TEXT,
                    ts INTEGER,
                    vix REAL,
                    data TEXT
                )
            """)
            surface_payload = {
                "option_chain_surface": {
                    "24400": {"CALL": {"implied_volatility": 19.2}, "PUT": {"implied_volatility": 21.4}},
                    "24600": {"CALL": {"implied_volatility": 18.8}, "PUT": {"implied_volatility": 22.6}}
                }
            }
            conn.execute(
                "INSERT INTO market_data VALUES ('INDEX#NIFTY50', 1791100000, 16.5, ?)",
                (json.dumps(surface_payload),)
            )
            conn.commit()
            conn.close()

            # Caller passes strategies child directory as in production SQLITE_DIR
            child_dir = os.path.join(tmp_dir, "strategies")
            os.makedirs(child_dir, exist_ok=True)

            c_iv, p_iv = fetch_option_surface_iv(24400, 24600, sqlite_dir=child_dir, reference_ts=1791100000)
            self.assertEqual(c_iv, 19.2)
            self.assertEqual(p_iv, 22.6)


if __name__ == "__main__":
    unittest.main()

