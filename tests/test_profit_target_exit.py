"""
tests/test_profit_target_exit.py
Comprehensive unit tests for the Automated 20% Strangle Profit Exit Engine.

Covers:
1. Scenario 1: 20% profit reached when both legs active -> authorizes full exit in Redis & marks snapshot closed.
2. Scenario 2: Alpha FSM exited CALL first -> 20% exit strictly suppressed/invalid; zero Redis authorization.
3. Scenario 3: Alpha FSM exited PUT first -> 20% exit strictly suppressed/invalid; zero Redis authorization.
4. Scenario 4: Both legs already closed -> clean skip.
5. Scenario 5: Discord webhook outage -> live order authorization in Redis still succeeds (decoupled).
6. Scenario 6: Remote Redis config override via PROFIT_TARGET_EXIT_PCT.
7. Scenario 7: Thursday track (AngelOne) parity with ORDERS_LIVE_THU.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.profit_monitor import evaluate_peak_profit, on_new_peak
from common.storage import (
    load_active_snapshot,
    save_active_snapshot,
    update_snapshot_for_profit_target_exit,
)
from common.unified_strategy_store import (
    init_or_rotate_cycle,
    record_exit_signal_and_sync_mongo,
    load_active_strategies,
)


class FakeRedis:
    def __init__(self, initial_data=None):
        self._data = dict(initial_data or {})
        self.sets = {}

    def get(self, key):
        return self._data.get(str(key))

    def set(self, key, value, ex=None):
        self._data[str(key)] = str(value)
        return True

    def delete(self, *keys):
        for k in keys:
            self._data.pop(str(k), None)
        return True

    def sadd(self, key, *members):
        s = self.sets.setdefault(str(key), set())
        for m in members:
            s.add(str(m))
        return len(members)

    def scan_iter(self, match=None, count=None):
        import fnmatch
        for k in list(self._data.keys()):
            if match is None or fnmatch.fnmatch(k, match):
                yield k


class ProfitTargetExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.snap_tue = os.path.join(self.tmp_dir, "current_week_buy.json")
        self.snap_thu = os.path.join(self.tmp_dir, "current_thursday_buy.json")
        self.active_strategies_json = os.path.join(self.tmp_dir, "active_strategies.json")
        self.dedup_db = os.path.join(self.tmp_dir, "dispatched_peak_alerts.db")
        os.environ["PEAK_DEDUP_DB_PATH"] = self.dedup_db

        # Base Tuesday snapshot with both legs ACTIVE
        self.tue_snapshot = {
            "cycle_id": "CYCLE-TUE-20261124",
            "week_start_date": "20261124",
            "expiry_date": "20261201",
            "call_strike": 25500,
            "call_buy_price": 150.0,
            "put_strike": 25500,
            "put_buy_price": 150.0,
            "broker": "upstox",
            "status": "open",
            "alpha_fsm": {
                "state": "DUAL_LONG",
                "call_leg": {
                    "status": "ACTIVE",
                    "state": "LONG_CALL",
                    "exit_price": None,
                },
                "put_leg": {
                    "status": "ACTIVE",
                    "state": "LONG_PUT",
                    "exit_price": None,
                },
            },
            "peak_profit": {
                "cycle_id": "CYCLE-TUE-20261124",
                "peak_pnl_pts": 10.0,
                "peak_pnl_pct": 3.3,
                "last_notified_pts": None,
                "last_notified_pct": None,
                "alert_dispatched": False,
                "alerts_sent_today": 0,
            },
        }
        with open(self.snap_tue, "w", encoding="utf-8") as f:
            json.dump(self.tue_snapshot, f)

        # Base Thursday snapshot with both legs ACTIVE
        self.thu_snapshot = {
            "cycle_id": "CYCLE-THU-20261126",
            "week_start_date": "20261126",
            "expiry_date": "20261201",
            "call_strike": 25500,
            "call_buy_price": 100.0,
            "put_strike": 25500,
            "put_buy_price": 100.0,
            "broker": "angelone",
            "status": "open",
            "alpha_fsm": {
                "state": "DUAL_LONG",
                "call_leg": {
                    "status": "ACTIVE",
                    "state": "LONG_CALL",
                    "exit_price": None,
                },
                "put_leg": {
                    "status": "ACTIVE",
                    "state": "LONG_PUT",
                    "exit_price": None,
                },
            },
            "peak_profit": {
                "cycle_id": "CYCLE-THU-20261126",
                "peak_pnl_pts": 10.0,
                "peak_pnl_pct": 5.0,
                "last_notified_pts": None,
                "last_notified_pct": None,
                "alert_dispatched": False,
                "alerts_sent_today": 0,
            },
        }
        with open(self.snap_thu, "w", encoding="utf-8") as f:
            json.dump(self.thu_snapshot, f)

    def tearDown(self):
        os.environ.pop("PEAK_DEDUP_DB_PATH", None)

    @patch("common.profit_monitor.send_peak_alert_discord", return_value=True)
    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_scenario_1_both_legs_active_triggers_20pct_full_strangle_exit(self, mock_mongo, mock_discord):
        """Scenario 1: Strangle reaches +20% and both legs are ACTIVE -> authorizes full exit in Redis."""
        fake_redis = FakeRedis()

        # Initialize active_strategies
        init_or_rotate_cycle(
            "5day_strategy",
            {
                "cycle_id": "CYCLE-TUE-20261124",
                "call_strike": 25500,
                "put_strike": 25500,
                "expiry_date": "2026-12-01",
                "broker": "upstox",
            },
            filepath=self.active_strategies_json,
        )

        with patch("common.unified_strategy_store.ACTIVE_SNAPSHOT_FILE", self.snap_tue), \
             patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", self.active_strategies_json), \
             patch("config.get_redis_client", return_value=fake_redis), \
             patch("config._config_redis", return_value=fake_redis):

            # Entry cost = 150 + 150 = 300. 20% gain = +60 pts (e.g. Call=190, Put=170 -> 360 total, +60 pts = +20.0%)
            peak_state, alert_triggered, reason = evaluate_peak_profit(
                snapshot=self.tue_snapshot,
                nifty_ltp=25500.0,
                call_strike=25500,
                call_ltp=190.0,
                call_buy=150.0,
                put_strike=25500,
                put_ltp=170.0,
                put_buy=150.0,
                snapshot_filepath=self.snap_tue,
                track="weekly",
                redis_client=fake_redis,
                active_strategies_filepath=self.active_strategies_json,
            )

            self.assertTrue(alert_triggered)
            self.assertIn("Qualifying peak alert triggered", reason)

            # 1. Verify Redis keys were authorized for BOTH legs
            c_key = "fno:upstox:enabled:tue:2026-12-01:25500:CE"
            p_key = "fno:upstox:enabled:tue:2026-12-01:25500:PE"
            self.assertEqual(fake_redis.get(c_key), '{"enabled": true, "qty": 130}')
            self.assertEqual(fake_redis.get(p_key), '{"enabled": true, "qty": 130}')

            # 2. Verify ORDERS_LIVE_TUE gate is activated
            self.assertEqual(fake_redis.get("ORDERS_LIVE_TUE"), "1")
            self.assertEqual(fake_redis.get("fno:orders_live:tuesday"), "1")

            # 3. Verify snapshot was updated: legs marked PROFIT_TARGET_CLOSED and cycle CLOSED
            reloaded_snap = load_active_snapshot(filepath=self.snap_tue)
            self.assertEqual(reloaded_snap["status"], "closed")
            self.assertEqual(reloaded_snap["close_reason"], "PROFIT_TARGET_20PCT")
            call_leg = reloaded_snap["alpha_fsm"]["call_leg"]
            put_leg = reloaded_snap["alpha_fsm"]["put_leg"]
            self.assertEqual(call_leg["status"], "PROFIT_TARGET_CLOSED")
            self.assertEqual(call_leg["exit_price"], 190.0)
            self.assertEqual(put_leg["status"], "PROFIT_TARGET_CLOSED")
            self.assertEqual(put_leg["exit_price"], 170.0)

    @patch("common.profit_monitor.send_peak_alert_discord", return_value=True)
    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_scenario_2_call_already_exited_by_alpha_fsm_suppresses_20pct_exit(self, mock_mongo, mock_discord):
        """Scenario 2: Alpha FSM exited CALL first -> 20% exit is INVALID and suppressed."""
        fake_redis = FakeRedis()

        # Update snapshot: Call is HARVESTED_CLOSED at 250.0 (+66%), Put is still ACTIVE at 60.0
        self.tue_snapshot["alpha_fsm"]["call_leg"]["status"] = "HARVESTED_CLOSED"
        self.tue_snapshot["alpha_fsm"]["call_leg"]["exit_price"] = 250.0
        with open(self.snap_tue, "w", encoding="utf-8") as f:
            json.dump(self.tue_snapshot, f)

        init_or_rotate_cycle(
            "5day_strategy",
            {
                "cycle_id": "CYCLE-TUE-20261124",
                "call_strike": 25500,
                "put_strike": 25500,
                "expiry_date": "2026-12-01",
                "broker": "upstox",
            },
            filepath=self.active_strategies_json,
        )

        with patch("common.unified_strategy_store.ACTIVE_SNAPSHOT_FILE", self.snap_tue), \
             patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", self.active_strategies_json), \
             patch("config.get_redis_client", return_value=fake_redis), \
             patch("config._config_redis", return_value=fake_redis):

            # Call was locked at 250 exit, Put rises to 120 -> effective combined: (250-150) + (120-150) = 100 - 30 = 70 pts (+23.3%)
            peak_state, alert_triggered, reason = evaluate_peak_profit(
                snapshot=self.tue_snapshot,
                nifty_ltp=25500.0,
                call_strike=25500,
                call_ltp=250.0,
                call_buy=150.0,
                put_strike=25500,
                put_ltp=120.0,
                put_buy=150.0,
                snapshot_filepath=self.snap_tue,
                track="weekly",
                redis_client=fake_redis,
                active_strategies_filepath=self.active_strategies_json,
            )

            # Must NOT trigger
            self.assertFalse(alert_triggered)
            self.assertIn("Profit target exit skipped", reason)
            self.assertIn("Only Alpha FSM manages remaining leg", reason)

            # Redis must NOT be authorized
            self.assertIsNone(fake_redis.get("ORDERS_LIVE_TUE"))
            self.assertIsNone(fake_redis.get("fno:upstox:enabled:tue:2026-12-01:25500:PE"))

            # Put leg must still be ACTIVE
            reloaded_snap = load_active_snapshot(filepath=self.snap_tue)
            self.assertEqual(reloaded_snap["status"], "open")
            self.assertEqual(reloaded_snap["alpha_fsm"]["put_leg"]["status"], "ACTIVE")

    @patch("common.profit_monitor.send_peak_alert_discord", return_value=True)
    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_scenario_3_put_already_exited_suppresses_20pct_exit(self, mock_mongo, mock_discord):
        """Scenario 3: Alpha FSM exited PUT first (stop-loss) -> 20% exit is strictly suppressed."""
        fake_redis = FakeRedis()

        # Update snapshot: Put is STOP_LOSS_CLOSED at 70.0 (-53%)
        self.tue_snapshot["alpha_fsm"]["put_leg"]["status"] = "STOP_LOSS_CLOSED"
        self.tue_snapshot["alpha_fsm"]["put_leg"]["exit_price"] = 70.0
        with open(self.snap_tue, "w", encoding="utf-8") as f:
            json.dump(self.tue_snapshot, f)

        with patch("config.get_redis_client", return_value=fake_redis), \
             patch("config._config_redis", return_value=fake_redis):
            peak_state, alert_triggered, reason = evaluate_peak_profit(
                snapshot=self.tue_snapshot,
                nifty_ltp=25500.0,
                call_strike=25500,
                call_ltp=300.0,  # Call surges to +100%
                call_buy=150.0,
                put_strike=25500,
                put_ltp=70.0,
                put_buy=150.0,
                snapshot_filepath=self.snap_tue,
                track="weekly",
                redis_client=fake_redis,
                active_strategies_filepath=self.active_strategies_json,
            )

            self.assertFalse(alert_triggered)
            self.assertIn("Profit target exit skipped", reason)
            self.assertIsNone(fake_redis.get("ORDERS_LIVE_TUE"))

    def test_scenario_4_snapshot_already_closed_skips_cleanly(self):
        """Scenario 4: Closed cycle is skipped cleanly."""
        self.tue_snapshot["status"] = "closed"
        peak_state, alert_triggered, reason = evaluate_peak_profit(
            snapshot=self.tue_snapshot,
            nifty_ltp=25500.0,
            call_strike=25500,
            call_ltp=200.0,
            call_buy=150.0,
            put_strike=25500,
            put_ltp=200.0,
            put_buy=150.0,
            snapshot_filepath=self.snap_tue,
            track="weekly",
        )
        self.assertFalse(alert_triggered)
        self.assertIn("Cycle is closed", reason)

    @patch("common.profit_monitor.send_peak_alert_discord", return_value=False)
    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_scenario_5_discord_outage_does_not_block_live_order_authorization(self, mock_mongo, mock_discord):
        """Scenario 5: Discord webhook failure does NOT prevent live exit authorization in Redis."""
        fake_redis = FakeRedis()

        init_or_rotate_cycle(
            "5day_strategy",
            {
                "cycle_id": "CYCLE-TUE-20261124",
                "call_strike": 25500,
                "put_strike": 25500,
                "expiry_date": "2026-12-01",
                "broker": "upstox",
            },
            filepath=self.active_strategies_json,
        )

        with patch("common.unified_strategy_store.ACTIVE_SNAPSHOT_FILE", self.snap_tue), \
             patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", self.active_strategies_json), \
             patch("config.get_redis_client", return_value=fake_redis), \
             patch("config._config_redis", return_value=fake_redis):

            event = {
                "cycle_id": "CYCLE-TUE-20261124",
                "track": "weekly",
                "combined_pnl_pct": 22.5,
                "combined_pnl_pts": 67.5,
                "combined_pnl_inr": 4387.5,
                "call_ltp": 195.0,
                "put_ltp": 172.5,
                "timestamp": 1700009000,
                "both_legs_active": True,
                "snapshot_filepath": self.snap_tue,
                "dry_run": False,
                "alerts_sent_today": 1,
                "redis_client": fake_redis,
                "active_strategies_filepath": self.active_strategies_json,
            }

            dispatch_ok = on_new_peak(event)

            # Even if Discord returned False (outage), live orders ARE authorized
            self.assertEqual(fake_redis.get("ORDERS_LIVE_TUE"), "1")
            self.assertIsNotNone(fake_redis.get("fno:upstox:enabled:tue:2026-12-01:25500:CE"))
            self.assertIsNotNone(fake_redis.get("fno:upstox:enabled:tue:2026-12-01:25500:PE"))

            # Snapshot is closed
            reloaded_snap = load_active_snapshot(filepath=self.snap_tue)
            self.assertEqual(reloaded_snap["status"], "closed")

    @patch("common.profit_monitor.send_peak_alert_discord", return_value=True)
    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_scenario_6_remote_override_profit_target_exit_pct(self, mock_mongo, mock_discord):
        """Scenario 6: Setting PROFIT_TARGET_EXIT_PCT=25.0 in Redis raises threshold."""
        fake_redis = FakeRedis({"PROFIT_TARGET_EXIT_PCT": "25.0"})

        with patch("config.get_redis_client", return_value=fake_redis), \
             patch("config._config_redis", return_value=fake_redis):
            # Combined profit +21.0% (below 25.0%)
            peak_state, alert_triggered, reason = evaluate_peak_profit(
                snapshot=self.tue_snapshot,
                nifty_ltp=25500.0,
                call_strike=25500,
                call_ltp=185.0,
                call_buy=150.0,
                put_strike=25500,
                put_ltp=178.0,
                put_buy=150.0,
                snapshot_filepath=self.snap_tue,
                track="weekly",
                redis_client=fake_redis,
                active_strategies_filepath=self.active_strategies_json,
            )
            self.assertFalse(alert_triggered)
            self.assertIn("below minimum 25.0% threshold", reason)

    @patch("common.profit_monitor.send_peak_alert_discord", return_value=True)
    @patch("common.unified_strategy_store.get_mongodb_db", return_value=None)
    def test_scenario_7_thursday_track_angelone_parity(self, mock_mongo, mock_discord):
        """Scenario 7: Thursday track triggers AngelOne keys and ORDERS_LIVE_THU."""
        fake_redis = FakeRedis()

        init_or_rotate_cycle(
            "3day_strategy",
            {
                "cycle_id": "CYCLE-THU-20261126",
                "call_strike": 25500,
                "put_strike": 25500,
                "expiry_date": "2026-12-01",
                "broker": "angelone",
            },
            filepath=self.active_strategies_json,
        )

        with patch("common.unified_strategy_store.THURSDAY_SNAPSHOT_FILE", self.snap_thu), \
             patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", self.active_strategies_json), \
             patch("config.get_redis_client", return_value=fake_redis), \
             patch("config._config_redis", return_value=fake_redis):

            # Thursday buy cost = 100 + 100 = 200. +20% = +40 pts (Call=125, Put=115 -> 240, +40 pts = +20%)
            peak_state, alert_triggered, reason = evaluate_peak_profit(
                snapshot=self.thu_snapshot,
                nifty_ltp=25500.0,
                call_strike=25500,
                call_ltp=125.0,
                call_buy=100.0,
                put_strike=25500,
                put_ltp=115.0,
                put_buy=100.0,
                snapshot_filepath=self.snap_thu,
                track="thursday",
                track_label="3-Day Thursday Strategy",
                redis_client=fake_redis,
                active_strategies_filepath=self.active_strategies_json,
            )

            self.assertTrue(alert_triggered)

            # 1. Verify Redis keys were authorized for AngelOne
            c_key = "fno:angelone:enabled:thu:2026-12-01:25500:CE"
            p_key = "fno:angelone:enabled:thu:2026-12-01:25500:PE"
            self.assertIsNotNone(fake_redis.get(c_key))
            self.assertIsNotNone(fake_redis.get(p_key))

            # 2. Verify ORDERS_LIVE_THU is activated
            self.assertEqual(fake_redis.get("ORDERS_LIVE_THU"), "1")
            self.assertEqual(fake_redis.get("fno:orders_live:thursday"), "1")

            # 3. Verify Thursday snapshot is closed with PROFIT_TARGET_CLOSED
            reloaded_snap = load_active_snapshot(filepath=self.snap_thu)
            self.assertEqual(reloaded_snap["status"], "closed")
            self.assertEqual(reloaded_snap["alpha_fsm"]["call_leg"]["status"], "PROFIT_TARGET_CLOSED")
            self.assertEqual(reloaded_snap["alpha_fsm"]["put_leg"]["status"], "PROFIT_TARGET_CLOSED")


if __name__ == "__main__":
    unittest.main()
