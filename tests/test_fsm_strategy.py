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
        # 1st tick: Call peaks at 180 (gain=80% < 100%, days_to_expiry=5.0 on Tue -> trailing stop set to 180 * (1 - 0.25) = 135)
        state_t1, _ = evaluate_fsm_tick(
            fsm_state=state,
            current_call_ltp=180.0,
            current_put_ltp=60.0,
            current_ts=1700003600,
            days_to_expiry=5.0,
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
            days_to_expiry=5.0,
            call_delta=0.44,
            put_delta=-0.25,
        )
        self.assertEqual(state_t2["call_leg"]["status"], "TRAILING_STOP_CLOSED")
        self.assertEqual(state_t2["call_leg"]["exit_price"], 130.0)
        self.assertEqual(state_t2["call_leg"]["realized_pnl"], 30.0)
        self.assertTrue(any("CALL TRAILING STOP" in e for e in events))

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


if __name__ == "__main__":
    unittest.main()
