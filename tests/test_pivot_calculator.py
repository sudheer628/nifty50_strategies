#!/usr/bin/env python3
"""
tests/test_pivot_calculator.py - Comprehensive Unit Tests for NIFTY 50 Pivot Engine.
"""

import datetime
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.pivot_calculator import (
    calculate_floor_pivots,
    validate_daily_pivots,
    calculate_proximity_margin,
    get_pivot_zone,
    get_or_calculate_daily_pivots,
)


class FakeRedis:
    def __init__(self, initial_data=None):
        self._data = dict(initial_data or {})

    def get(self, key):
        return self._data.get(str(key))

    def set(self, key, value, ex=None):
        self._data[str(key)] = str(value)
        return True


class PivotCalculatorTests(unittest.TestCase):
    def setUp(self):
        # Sample realistic NIFTY OHLC
        # High = 25950.0, Low = 25600.0, Close = 25800.0
        # P  = (25950 + 25600 + 25800) / 3 = 25783.33
        # Range = 350.0
        # R1 = 2 * 25783.33 - 25600 = 25966.66 -> 25966.67
        # S1 = 2 * 25783.33 - 25950 = 25616.66 -> 25616.67
        # R2 = 25783.33 + 350 = 26133.33
        # S2 = 25783.33 - 350 = 25433.33
        self.sample_pivots = {
            "valid_for_date": "2026-11-26",
            "prior_trading_date": "2026-11-25",
            "prev_open": 25700.0,
            "prev_high": 25950.0,
            "prev_low": 25600.0,
            "prev_close": 25800.0,
            "pivot": 25783.33,
            "r1": 25966.66,
            "r2": 26133.33,
            "s1": 25616.66,
            "s2": 25433.33,
            "range": 350.0,
            "source": "upstox_historical_d1",
            "created_at_ts": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        }

    def test_calculate_floor_pivots_math(self):
        """Verify classical floor pivot formulas with known values."""
        res = calculate_floor_pivots(high=25950.0, low=25600.0, close=25800.0)
        self.assertAlmostEqual(res["pivot"], 25783.33, places=2)
        self.assertAlmostEqual(res["r1"], 25966.66, places=2)
        self.assertAlmostEqual(res["s1"], 25616.66, places=2)
        self.assertAlmostEqual(res["r2"], 26133.33, places=2)
        self.assertAlmostEqual(res["s2"], 25433.33, places=2)
        self.assertAlmostEqual(res["range"], 350.0, places=2)

    def test_calculate_floor_pivots_invalid_inputs(self):
        """Verify error raised on invalid High/Low (High < Low or negative)."""
        with self.assertRaises(ValueError):
            calculate_floor_pivots(high=24000.0, low=25000.0, close=24500.0)
        with self.assertRaises(ValueError):
            calculate_floor_pivots(high=0.0, low=0.0, close=0.0)

    def test_validate_daily_pivots_valid(self):
        """Valid pivot payload passes validation."""
        today = datetime.date(2026, 11, 26)
        self.assertTrue(validate_daily_pivots(self.sample_pivots, today_ist=today))

    def test_validate_daily_pivots_ordering_violation(self):
        """Validation fails if R2 <= R1 or S1 <= S2 or P <= S1."""
        bad_pivots = dict(self.sample_pivots)
        bad_pivots["r1"] = 26200.0  # R1 > R2 violation
        self.assertFalse(validate_daily_pivots(bad_pivots))

        bad_pivots2 = dict(self.sample_pivots)
        bad_pivots2["s1"] = 25300.0  # S1 < S2 violation
        self.assertFalse(validate_daily_pivots(bad_pivots2))

        bad_pivots3 = dict(self.sample_pivots)
        bad_pivots3["pivot"] = 25500.0  # P < S1 violation
        self.assertFalse(validate_daily_pivots(bad_pivots3))

    def test_validate_daily_pivots_date_checks(self):
        """Validation fails if valid_for_date does not match today or prior date is invalid."""
        today = datetime.date(2026, 11, 27)  # Expected 27th but payload has 26th
        self.assertFalse(validate_daily_pivots(self.sample_pivots, today_ist=today))

        bad_date = dict(self.sample_pivots)
        bad_date["prior_trading_date"] = "2026-11-26"  # prior >= valid date
        self.assertFalse(validate_daily_pivots(bad_date))

        # Missing prior_trading_date must be rejected
        missing_prior = dict(self.sample_pivots)
        del missing_prior["prior_trading_date"]
        self.assertFalse(validate_daily_pivots(missing_prior))

        # Missing created_at_ts must be rejected
        missing_ts = dict(self.sample_pivots)
        del missing_ts["created_at_ts"]
        self.assertFalse(validate_daily_pivots(missing_ts))

        # Future timestamp (> now + 300s) must be rejected
        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        future_ts = dict(self.sample_pivots)
        future_ts["created_at_ts"] = now_ts + 600
        self.assertFalse(validate_daily_pivots(future_ts, current_ts=now_ts))

        # Stale timestamp (> 48h old) must be rejected
        stale_ts = dict(self.sample_pivots)
        stale_ts["created_at_ts"] = now_ts - 200000
        self.assertFalse(validate_daily_pivots(stale_ts, current_ts=now_ts))

    def test_validate_daily_pivots_sanity_bounds(self):
        """Validation fails if pivot is outside plausible NIFTY range (15k - 40k)."""
        out_of_bounds = dict(self.sample_pivots)
        out_of_bounds["pivot"] = 5000.0
        out_of_bounds["r1"] = 5100.0
        out_of_bounds["r2"] = 5200.0
        out_of_bounds["s1"] = 4900.0
        out_of_bounds["s2"] = 4800.0
        self.assertFalse(validate_daily_pivots(out_of_bounds))

    def test_validate_daily_pivots_relative_bounds(self):
        """Validation fails if S2 < 0.7P or R2 > 1.3P."""
        # Violate S2 lower bound (e.g. S2 < 0.70 * 25766.67 = 18036.67)
        violate_s2 = dict(self.sample_pivots)
        violate_s2["s2"] = 17000.0
        self.assertFalse(validate_daily_pivots(violate_s2))

        # Violate R2 upper bound (e.g. R2 > 1.30 * 25766.67 = 33496.67)
        violate_r2 = dict(self.sample_pivots)
        violate_r2["r2"] = 35000.0
        self.assertFalse(validate_daily_pivots(violate_r2))

    def test_calculate_proximity_margin(self):
        """Verify adaptive proximity margin calculation (0.10% capped at 30 pts)."""
        # At 25,000 -> 25.0 pts
        self.assertEqual(calculate_proximity_margin(25000.0), 25.0)
        # At 20,000 -> 20.0 pts
        self.assertEqual(calculate_proximity_margin(20000.0), 20.0)
        # At 35,000 -> 30.0 pts (capped)
        self.assertEqual(calculate_proximity_margin(35000.0), 30.0)

    def test_get_pivot_zone_classification(self):
        """Verify spot classification into R2, R1, S1, S2, or None."""
        pivots = self.sample_pivots
        margin = 25.0

        # R2 = 26133.33, margin = 25.0 -> threshold = 26108.33
        self.assertEqual(get_pivot_zone(26110.0, pivots, margin=margin), "R2")
        self.assertEqual(get_pivot_zone(26200.0, pivots, margin=margin), "R2")

        # R1 = 25966.67, threshold = 25941.67
        self.assertEqual(get_pivot_zone(25945.0, pivots, margin=margin), "R1")
        self.assertEqual(get_pivot_zone(26000.0, pivots, margin=margin), "R1")

        # Between R1 and Pivot -> None
        self.assertIsNone(get_pivot_zone(25850.0, pivots, margin=margin))

        # S1 = 25616.67, threshold = 25641.67
        self.assertEqual(get_pivot_zone(25630.0, pivots, margin=margin), "S1")
        self.assertEqual(get_pivot_zone(25500.0, pivots, margin=margin), "S1")

        # S2 = 25433.33, threshold = 25458.33
        self.assertEqual(get_pivot_zone(25450.0, pivots, margin=margin), "S2")
        self.assertEqual(get_pivot_zone(25300.0, pivots, margin=margin), "S2")

    def test_get_or_calculate_daily_pivots_from_redis_cache(self):
        """Verify cached valid pivots in Redis are returned directly without calling API."""
        today = datetime.date(2026, 11, 26)
        fake_redis = FakeRedis({
            "fno:nifty:daily_pivots:2026-11-26": json.dumps(self.sample_pivots)
        })

        res = get_or_calculate_daily_pivots(
            redis_client=fake_redis,
            today_date=today,
        )
        self.assertIsNotNone(res)
        self.assertEqual(res["pivot"], 25783.33)
        self.assertEqual(res["r1"], 25966.66)

    @patch("common.pivot_calculator.fetch_nifty_daily_ohlc")
    def test_get_or_calculate_daily_pivots_from_api_mock(self, mock_fetch):
        """Verify API fetch and caching when Redis cache is empty (holiday-aware)."""
        today = datetime.date(2026, 11, 26)
        fake_redis = FakeRedis()

        # Mock candles returned by Upstox API:
        # candles[0] is the most recent completed trading session (e.g. 2026-11-25)
        mock_fetch.return_value = [
            {
                "date": "2026-11-25",
                "open": 25700.0,
                "high": 25950.0,
                "low": 25600.0,
                "close": 25800.0,
                "volume": 100000,
            },
            {
                "date": "2026-11-24",
                "open": 25600.0,
                "high": 25800.0,
                "low": 25500.0,
                "close": 25700.0,
                "volume": 120000,
            },
        ]

        res = get_or_calculate_daily_pivots(
            redis_client=fake_redis,
            upstox_token="MOCK_UPSTOX_TOKEN",
            today_date=today,
        )
        self.assertIsNotNone(res)
        self.assertEqual(res["valid_for_date"], "2026-11-26")
        self.assertEqual(res["prior_trading_date"], "2026-11-25")
        self.assertEqual(res["pivot"], 25783.33)
        self.assertEqual(res["r1"], 25966.66)
        self.assertEqual(res["r2"], 26133.33)

        # Check cached into Redis
        cached_raw = fake_redis.get("fno:nifty:daily_pivots:2026-11-26")
        self.assertIsNotNone(cached_raw)
        cached = json.loads(cached_raw)
        self.assertEqual(cached["pivot"], 25783.33)

    def test_pivot_shadow_event_sqlite_persistence(self):
        """Verify inserting and querying counterfactual pivot shadow events in SQLite."""
        import tempfile
        import os
        from common.storage import insert_pivot_shadow_event, get_pivot_shadow_events

        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_signals.db")
            event_rec = {
                "cycle_id": "TEST-CYC-001",
                "track": "5day_strategy",
                "trade_date": "2026-11-26",
                "timestamp": 1764057600,
                "leg": "CALL",
                "zone": "R1",
                "spot": 25965.0,
                "option_peak": 200.0,
                "option_ltp": 185.0,
                "live_trail_stop": 164.0,
                "shadow_trail_stop": 190.0,
                "delta_points": 26.0,
                "shadow_exit_price": 185.0,
                "shadow_realized_pnl": 85.0,
                "live_fsm_status": "ACTIVE",
                "exit_reason": "SHADOW_TRAILING_STOP_HIT",
            }
            insert_pivot_shadow_event(test_db, event_rec)

            events = get_pivot_shadow_events(test_db, cycle_id="TEST-CYC-001")
            self.assertEqual(len(events), 1)
            e = events[0]
            self.assertEqual(e["cycle_id"], "TEST-CYC-001")
            self.assertEqual(e["leg"], "CALL")
            self.assertEqual(e["zone"], "R1")
            self.assertEqual(e["shadow_realized_pnl"], 85.0)
            self.assertEqual(e["shadow_exit_price"], 185.0)

    def test_update_active_fsm_state_syncs_to_active_strategies_json(self):
        """Verify update_active_fsm_state persists fsm & pivot_shadow to both cycle snapshot and active_strategies.json."""
        import tempfile
        import os
        import json
        from common.storage import update_active_fsm_state, load_active_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            snap_file = os.path.join(tmpdir, "current_week_buy.json")
            active_file = os.path.join(tmpdir, "active_strategies.json")

            # Initialize base snapshot
            base_snap = {"cycle_id": "CYC-SYNC-1", "status": "ongoing"}
            with open(snap_file, "w") as f:
                json.dump(base_snap, f)

            # Initialize active_strategies.json
            base_active = {"5day_strategy": {"cycle_id": "CYC-SYNC-1"}}
            with open(active_file, "w") as f:
                json.dump(base_active, f)

            fsm_dict = {
                "state": "DUAL_LONG",
                "pivot_shadow": {
                    "active": True,
                    "zone": "R1",
                    "shadow_trail_stop": 190.0,
                }
            }

            # Monkeypatch ACTIVE_STRATEGIES_JSON target
            with unittest.mock.patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", active_file):
                res = update_active_fsm_state(fsm_dict, filepath=snap_file)
                self.assertTrue(res)

            # Verify snapshot has alpha_fsm and pivot_shadow
            updated_snap = load_active_snapshot(snap_file)
            self.assertEqual(updated_snap["alpha_fsm"]["state"], "DUAL_LONG")
            self.assertTrue(updated_snap["pivot_shadow"]["active"])

            # Verify active_strategies.json has alpha_fsm and pivot_shadow
            with open(active_file, "r") as f:
                updated_active = json.load(f)
            self.assertEqual(updated_active["5day_strategy"]["alpha_fsm"]["state"], "DUAL_LONG")
            self.assertTrue(updated_active["5day_strategy"]["pivot_shadow"]["active"])
            self.assertEqual(updated_active["5day_strategy"]["pivot_shadow"]["shadow_trail_stop"], 190.0)

    @patch("common.pivot_calculator.fetch_nifty_daily_ohlc", side_effect=Exception("Upstox API 500 error"))
    def test_get_or_calculate_daily_pivots_fails_safe_on_api_error(self, mock_fetch):
        """Verify fails safe to None without raising exception when API fails."""
        today = datetime.date(2026, 11, 26)
        fake_redis = FakeRedis()

        res = get_or_calculate_daily_pivots(
            redis_client=fake_redis,
            upstox_token="MOCK_UPSTOX_TOKEN",
            today_date=today,
        )
        self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
