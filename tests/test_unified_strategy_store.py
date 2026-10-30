#!/usr/bin/env python3
"""
Unit tests for common.unified_strategy_store.
"""

import os
import json
import unittest
from unittest.mock import patch, MagicMock

from common.unified_strategy_store import (
    normalize_track,
    to_simple_action,
    load_active_strategies,
    save_active_strategies,
    init_or_rotate_cycle,
    update_locked_prices,
    update_latest_ltp,
    record_exit_signal_and_sync_mongo,
)


class UnifiedStrategyStoreTests(unittest.TestCase):
    def setUp(self):
        self.test_json = os.path.join(os.path.dirname(__file__), "test_active_strategies.json")
        if os.path.exists(self.test_json):
            os.remove(self.test_json)

    def tearDown(self):
        if os.path.exists(self.test_json):
            os.remove(self.test_json)
        lock_file = f"{self.test_json}.lock"
        if os.path.exists(lock_file):
            os.remove(lock_file)

    def test_normalize_track(self):
        self.assertEqual(normalize_track("weekly"), "5day_strategy")
        self.assertEqual(normalize_track("5day_strategy"), "5day_strategy")
        self.assertEqual(normalize_track("thursday"), "3day_strategy")
        self.assertEqual(normalize_track("3day"), "3day_strategy")
        self.assertEqual(normalize_track("3day_strategy"), "3day_strategy")

    def test_to_simple_action(self):
        self.assertEqual(to_simple_action("TAKE_PROFIT_CALL"), "CALL ONLY EXIT")
        self.assertEqual(to_simple_action("STOP_LOSS_CALL"), "CALL ONLY EXIT")
        self.assertEqual(to_simple_action("HARVESTED_CALL"), "CALL ONLY EXIT")
        self.assertEqual(to_simple_action("TAKE_PROFIT_PUT"), "PUT ONLY EXIT")
        self.assertEqual(to_simple_action("STOP_LOSS_PUT"), "PUT ONLY EXIT")
        self.assertEqual(to_simple_action("TAKE_PROFIT_BOTH"), "BOTH CALL & PUT EXIT")
        self.assertEqual(to_simple_action("PEAK_20PCT"), "20% PEAK PROFIT REACHED")
        self.assertEqual(to_simple_action("EXIT", leg="CALL"), "CALL ONLY EXIT")
        self.assertEqual(to_simple_action("EXIT", leg="PUT"), "PUT ONLY EXIT")
        self.assertEqual(to_simple_action(None), "HOLDING (NO EXIT)")

    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_independent_rotation(self, mock_mongo):
        """Verify rotating 5-day cycle does not affect 3-day cycle and vice versa."""
        # 1. Rotate 5-day cycle
        init_or_rotate_cycle(
            "5day_strategy",
            {
                "cycle_id": "CYCLE-TUE-20261027",
                "start_date": "2026-10-27",
                "expiry_date": "2026-11-02",
                "call_strike": 22900,
                "put_strike": 23100,
            },
            filepath=self.test_json,
        )

        data = load_active_strategies(filepath=self.test_json)
        self.assertEqual(data["5day_strategy"]["cycle_id"], "CYCLE-TUE-20261027")
        self.assertEqual(data["5day_strategy"]["call_strike"], 22900)
        self.assertEqual(data["3day_strategy"]["status"], "pending_entry")
        self.assertIsNone(data["3day_strategy"]["cycle_id"])

        # 2. Rotate 3-day cycle (simulating Thursday morning)
        init_or_rotate_cycle(
            "3day_strategy",
            {
                "cycle_id": "CYCLE-THU-20261029",
                "start_date": "2026-10-29",
                "expiry_date": "2026-11-02",
                "call_strike": 23000,
                "put_strike": 23000,
            },
            filepath=self.test_json,
        )

        data = load_active_strategies(filepath=self.test_json)
        # 5-day strategy remains untouched
        self.assertEqual(data["5day_strategy"]["cycle_id"], "CYCLE-TUE-20261027")
        self.assertEqual(data["5day_strategy"]["call_strike"], 22900)
        # 3-day strategy is now active
        self.assertEqual(data["3day_strategy"]["cycle_id"], "CYCLE-THU-20261029")
        self.assertEqual(data["3day_strategy"]["call_strike"], 23000)

    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_update_locked_prices(self, mock_mongo):
        init_or_rotate_cycle(
            "5day_strategy",
            {"cycle_id": "CYCLE-TUE-20261027", "call_strike": 22900, "put_strike": 23100},
            filepath=self.test_json,
        )
        update_locked_prices(
            "5day_strategy",
            call_buy=300.0,
            put_buy=150.0,
            spot=23000.0,
            timestamp=1700000000,
            filepath=self.test_json,
        )

        data = load_active_strategies(filepath=self.test_json)
        locked = data["5day_strategy"]["day1_locked_price"]
        self.assertIsNotNone(locked)
        self.assertEqual(locked["call_buy_price"], 300.0)
        self.assertEqual(locked["put_buy_price"], 150.0)
        self.assertEqual(locked["combined_buy_price"], 450.0)
        self.assertEqual(locked["entry_spot"], 23000.0)

    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_update_latest_ltp_and_pnl(self, mock_mongo):
        init_or_rotate_cycle(
            "5day_strategy",
            {"cycle_id": "CYCLE-TUE-20261027", "call_strike": 22900, "put_strike": 23100},
            filepath=self.test_json,
        )
        update_locked_prices(
            "5day_strategy",
            call_buy=300.0,
            put_buy=150.0,
            spot=23000.0,
            filepath=self.test_json,
        )

        # Call surges to 350, Put drops to 120 -> Combined = 470 (+20 pts profit)
        update_latest_ltp(
            "5day_strategy",
            spot=23050.0,
            call_ltp=350.0,
            put_ltp=120.0,
            filepath=self.test_json,
        )

        data = load_active_strategies(filepath=self.test_json)
        latest = data["5day_strategy"]["latest_ltp"]
        self.assertEqual(latest["combined_ltp"], 470.0)
        self.assertEqual(latest["combined_pnl_pts"], 20.0)
        self.assertAlmostEqual(latest["combined_pnl_pct"], round(20.0 / 450.0 * 100, 2))
        self.assertEqual(latest["combined_pnl_inr"], round(20.0 * 65, 2))

    @patch("common.unified_strategy_store.get_redis_client", return_value=None)
    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_record_exit_signal_simplified_and_idempotent(self, mock_mongo, mock_redis):
        init_or_rotate_cycle(
            "5day_strategy",
            {"cycle_id": "CYCLE-TUE-20261027", "call_strike": 22900, "put_strike": 23100},
            filepath=self.test_json,
        )

        # Trigger Alpha FSM exit
        res1 = record_exit_signal_and_sync_mongo(
            "5day_strategy",
            "alpha_fsm",
            {
                "action": "TAKE_PROFIT_CALL",
                "leg": "CALL",
                "realized_pnl_pts": 50.0,
                "timestamp": 1700001000,
                "time_ist": "2026-10-28 11:30:00 IST",
                "details": "Harvest rule covered risk",
            },
            filepath=self.test_json,
        )
        self.assertTrue(res1)

        data = load_active_strategies(filepath=self.test_json)
        fsm_exit = data["5day_strategy"]["exit_signals"]["alpha_fsm"]
        self.assertTrue(fsm_exit["triggered"])
        self.assertEqual(fsm_exit["status"], "TRIGGERED")
        self.assertEqual(fsm_exit["display_symbol"], "CALL ONLY EXIT")
        self.assertEqual(fsm_exit["simple_action"], "CALL ONLY EXIT")
        self.assertEqual(fsm_exit["realized_pnl_pts"], 50.0)
        self.assertEqual(fsm_exit["timestamp"], 1700001000)

        # Second trigger should be skipped (idempotency)
        res2 = record_exit_signal_and_sync_mongo(
            "5day_strategy",
            "alpha_fsm",
            {
                "action": "STOP_LOSS_CALL",
                "leg": "CALL",
                "realized_pnl_pts": 10.0,
                "timestamp": 1700002000,
            },
            filepath=self.test_json,
        )
        self.assertFalse(res2)

        # Timestamp and simple_action should remain unchanged from first trigger
        data_after = load_active_strategies(filepath=self.test_json)
        self.assertEqual(data_after["5day_strategy"]["exit_signals"]["alpha_fsm"]["timestamp"], 1700001000)
        self.assertEqual(data_after["5day_strategy"]["exit_signals"]["alpha_fsm"]["simple_action"], "CALL ONLY EXIT")

    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_record_exit_signal_separate_call_and_put_exits(self, mock_mongo):
        """Verify CALL exit and PUT exit are tracked independently and publish canonical Redis keys."""
        mock_redis = MagicMock()
        init_or_rotate_cycle(
            "5day_strategy",
            {
                "cycle_id": "CYCLE-TUE-20261027",
                "call_strike": 22900,
                "put_strike": 23100,
                "expiry_date": "2026-11-03",
            },
            filepath=self.test_json,
        )

        # 1. First trigger: CALL exit
        res1 = record_exit_signal_and_sync_mongo(
            "5day_strategy",
            "alpha_fsm",
            {
                "action": "TAKE_PROFIT_CALL",
                "leg": "CALL",
                "realized_pnl_pts": 50.0,
                "timestamp": 1700001000,
                "time_ist": "2026-10-28 11:30:00 IST",
                "details": "Harvest rule covered risk",
            },
            filepath=self.test_json,
            redis_client=mock_redis,
        )
        self.assertTrue(res1)

        # Verify Redis calls for CALL exit
        call_key = "fno:upstox:enabled:tue:2026-11-03:22900:CE"
        mock_redis.set.assert_any_call(call_key, json.dumps({"enabled": True, "qty": 130}), ex=604800)
        mock_redis.set.assert_any_call("ORDERS_LIVE_TUE", "1")
        mock_redis.set.assert_any_call("fno:orders_live:tuesday", "1")

        # Verify PUT key was NOT written
        put_key = "fno:upstox:enabled:tue:2026-11-03:23100:PE"
        set_keys = [call[0][0] for call in mock_redis.set.call_args_list]
        self.assertIn(call_key, set_keys)
        self.assertNotIn(put_key, set_keys)

        # 2. Second trigger: PUT exit (must NOT be suppressed by earlier CALL exit!)
        mock_redis.reset_mock()
        res2 = record_exit_signal_and_sync_mongo(
            "5day_strategy",
            "alpha_fsm",
            {
                "action": "STOP_LOSS_PUT",
                "leg": "PUT",
                "realized_pnl_pts": -20.0,
                "timestamp": 1700002000,
                "time_ist": "2026-10-28 12:15:00 IST",
                "details": "FSM stop breach",
            },
            filepath=self.test_json,
            redis_client=mock_redis,
        )
        self.assertTrue(res2)
        mock_redis.set.assert_any_call(put_key, json.dumps({"enabled": True, "qty": 130}), ex=604800)
        mock_redis.set.assert_any_call("ORDERS_LIVE_TUE", "1")

        # 3. Third trigger: Duplicate CALL exit (must be suppressed!)
        res3 = record_exit_signal_and_sync_mongo(
            "5day_strategy",
            "alpha_fsm",
            {
                "action": "STOP_LOSS_CALL",
                "leg": "CALL",
                "realized_pnl_pts": 10.0,
                "timestamp": 1700003000,
            },
            filepath=self.test_json,
            redis_client=mock_redis,
        )
        self.assertFalse(res3)

        # 4. Fourth trigger: Duplicate PUT exit (must be suppressed!)
        res4 = record_exit_signal_and_sync_mongo(
            "5day_strategy",
            "alpha_fsm",
            {
                "action": "TAKE_PROFIT_PUT",
                "leg": "PUT",
                "realized_pnl_pts": -15.0,
                "timestamp": 1700004000,
            },
            filepath=self.test_json,
            redis_client=mock_redis,
        )
        self.assertFalse(res4)

    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_record_exit_signal_both_legs_exit(self, mock_mongo):
        """Verify EXIT_BOTH authorizes both CALL and PUT canonical keys simultaneously."""
        mock_redis = MagicMock()
        init_or_rotate_cycle(
            "5day_strategy",
            {
                "cycle_id": "CYCLE-TUE-20261027",
                "call_strike": 22900,
                "put_strike": 23100,
                "expiry_date": "2026-11-03",
            },
            filepath=self.test_json,
        )

        res = record_exit_signal_and_sync_mongo(
            "5day_strategy",
            "alpha_fsm",
            {
                "action": "EXIT_BOTH",
                "timestamp": 1700005000,
                "time_ist": "2026-10-28 14:00:00 IST",
                "details": "Double leg exit",
            },
            filepath=self.test_json,
            redis_client=mock_redis,
        )
        self.assertTrue(res)

        call_key = "fno:upstox:enabled:tue:2026-11-03:22900:CE"
        put_key = "fno:upstox:enabled:tue:2026-11-03:23100:PE"
        mock_redis.set.assert_any_call(call_key, json.dumps({"enabled": True, "qty": 130}), ex=604800)
        mock_redis.set.assert_any_call(put_key, json.dumps({"enabled": True, "qty": 130}), ex=604800)
        mock_redis.set.assert_any_call("ORDERS_LIVE_TUE", "1")

        # Subsequent exit should be suppressed
        res_dup = record_exit_signal_and_sync_mongo(
            "5day_strategy",
            "alpha_fsm",
            {"action": "EXIT_BOTH", "timestamp": 1700006000},
            filepath=self.test_json,
            redis_client=mock_redis,
        )
        self.assertFalse(res_dup)

    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_record_exit_signal_thursday_track(self, mock_mongo):
        """Verify Thursday track writes 'thu' keys and 65 qty (1 lot)."""
        mock_redis = MagicMock()
        init_or_rotate_cycle(
            "3day_strategy",
            {
                "cycle_id": "CYCLE-THU-20261029",
                "call_strike": 23000,
                "put_strike": 23000,
                "expiry_date": "2026-11-03",
            },
            filepath=self.test_json,
        )

        res = record_exit_signal_and_sync_mongo(
            "3day_strategy",
            "alpha_fsm",
            {
                "action": "TAKE_PROFIT_CALL",
                "leg": "CALL",
                "timestamp": 1700007000,
            },
            filepath=self.test_json,
            redis_client=mock_redis,
        )
        self.assertTrue(res)

        thu_call_key = "fno:upstox:enabled:thu:2026-11-03:23000:CE"
        mock_redis.set.assert_any_call(thu_call_key, json.dumps({"enabled": True, "qty": 65}), ex=604800)
        mock_redis.set.assert_any_call("ORDERS_LIVE_THU", "1")
        mock_redis.set.assert_any_call("fno:orders_live:thursday", "1")

    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_peak_exit_does_not_publish_redis_authorization(self, mock_mongo):
        """Verify peak_20pct exit does NOT publish Redis authorization keys or activate safety gates."""
        mock_redis = MagicMock()
        init_or_rotate_cycle(
            "5day_strategy",
            {
                "cycle_id": "CYCLE-TUE-20261027",
                "call_strike": 22900,
                "put_strike": 23100,
                "expiry_date": "2026-11-03",
            },
            filepath=self.test_json,
        )

        res = record_exit_signal_and_sync_mongo(
            "5day_strategy",
            "peak_20pct",
            {
                "action": "PEAK_20PCT",
                "peak_pnl_pct": 21.5,
                "peak_pnl_pts": 95.0,
                "peak_pnl_inr": 6175.0,
                "timestamp": 1700008000,
                "time_ist": "2026-10-28 14:30:00 IST",
                "details": "20% Peak Target Achieved",
            },
            filepath=self.test_json,
            redis_client=mock_redis,
        )
        self.assertTrue(res)

        # Ensure Redis was NEVER called to set keys or activate gates
        mock_redis.set.assert_not_called()
        self.assertEqual(len(mock_redis.set.mock_calls), 0)

        # JSON should still reflect the peak exit
        data = load_active_strategies(filepath=self.test_json)
        self.assertTrue(data["5day_strategy"]["exit_signals"]["peak_20pct"]["triggered"])

    def test_strategy_lock_reentrant(self):
        """Verify strategy_lock is re-entrant and does not self-deadlock."""
        from common.unified_strategy_store import strategy_lock
        with strategy_lock(self.test_json):
            with strategy_lock(self.test_json):
                save_active_strategies({"test": 123}, filepath=self.test_json)
                data = load_active_strategies(filepath=self.test_json)
                self.assertEqual(data.get("test"), 123)

    @patch("common.unified_strategy_store.get_mongodb_db")
    def test_reset_cycle_live_logs_in_mongodb(self, mock_get_db):
        """Verify reset_cycle_live_logs_in_mongodb deletes prior cycle logs for the track."""
        from common.unified_strategy_store import reset_cycle_live_logs_in_mongodb
        mock_col = MagicMock()
        mock_col.delete_many.return_value.deleted_count = 142
        mock_db = {"derivative_cycle_live_logs": mock_col}
        mock_get_db.return_value = mock_db

        res = reset_cycle_live_logs_in_mongodb("5day_strategy")
        self.assertTrue(res)
        mock_col.delete_many.assert_called_once_with({"strategy_track": "5day_strategy"})


if __name__ == "__main__":
    unittest.main()
