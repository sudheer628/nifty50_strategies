"""
test_fsm_pivot_acceleration.py - Test Suite for Pivot-Aware Alpha FSM Exit Acceleration.

Tests:
1. Default behavior (no pivots / spot provided): exact parity with existing FSM.
2. Spot outside pivot zones: trailing stops maintain normal DTE buffers.
3. Call leg in R1/R2 zone with profit: 5% buffer tightens trailing stop when enabled.
4. Put leg in S1/S2 zone with profit: 5% buffer tightens trailing stop when enabled.
5. Breach arm retention (+40% peak gain required): entry near R1 without +40% gain does NOT exit on minor pullback.
6. Monday afternoon cliff precedence: 5% cliff buffer is NEVER loosened by IV spike (8%) or other modifiers.
7. Shadow mode: records counterfactual exit telemetry without closing live position.
8. Live mode: exits with standard TRAILING_STOP_CLOSED and descriptive exit_reason when 5% buffer breached.
"""

import pytest
from datetime import datetime, timezone
import pytz

from common.fsm_strategy import init_fsm_state, evaluate_fsm_tick

IST_TZ = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def base_pivots():
    return {
        "pivot": 25783.33,
        "r1": 25966.66,
        "r2": 26166.66,
        "s1": 25600.00,
        "s2": 25416.67,
        "valid_for_date": "2026-11-25",
        "prior_trading_date": "2026-11-24",
        "created_at_ts": 1764057600,
    }


@pytest.fixture
def sample_state():
    # Buy Call @ 100, Put @ 100, total cost 200, OTM
    return init_fsm_state(
        call_strike=26000,
        put_strike=25500,
        call_buy_price=100.0,
        put_buy_price=100.0,
        entry_ts=1764057600,
        moneyness="OTM",
    )


class TestFSMPivotAcceleration:

    def test_default_behavior_unchanged_without_pivots(self, sample_state):
        """Without daily_pivots or nifty_spot, behavior is strictly identical to existing FSM."""
        # Call climbs to 150 (peak gain 50% >= breach arm 40%)
        t1 = 1764057600  # Midday Tue/Wed (DTE: 7.0 -> trail_buffer = 0.25)
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=150.0,
            current_put_ltp=90.0,
            current_ts=t1,
            days_to_expiry=6.0,
        )
        # 25% trail buffer on peak 150: 150 * 0.75 = 112.50
        assert s1["call_leg"]["trailing_stop"] == 112.50
        assert s1["call_leg"]["status"] == "ACTIVE"
        assert "pivot_shadow" not in s1 or not s1["pivot_shadow"].get("active")

    def test_spot_outside_pivot_zone_does_not_accelerate(self, sample_state, base_pivots):
        """Spot in neutral zone (e.g., 25800 between P and R1) uses standard trail buffer."""
        t1 = 1764057600
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=150.0,
            current_put_ltp=90.0,
            current_ts=t1,
            days_to_expiry=4.0,  # Thu/Fri: trail_buffer = 0.18
            nifty_spot=25800.0,  # Below R1 (25966.66)
            daily_pivots=base_pivots,
            pivot_accel_enabled=True,
        )
        # 18% buffer on 150: 150 * 0.82 = 123.00
        assert s1["call_leg"]["trailing_stop"] == 123.00
        assert s1["call_leg"]["status"] == "ACTIVE"
        assert s1["pivot_shadow"]["active"] is False

    def test_unqualified_leg_in_pivot_zone_reports_zone_without_tightening_stops(self, sample_state, base_pivots):
        """Spot in R1 reports market zone in telemetry, but stops do NOT accelerate to 5% if option is < 30% profit."""
        t1 = 1764057600
        # Call bought at 100 is at 115 (+15%, below 30% profit qualification threshold)
        # Spot is at 25965.0 (inside R1 zone)
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=115.0,
            current_put_ltp=85.0,
            current_ts=t1,
            days_to_expiry=4.0,  # Base buffer 18%
            nifty_spot=25965.0,  # In R1
            daily_pivots=base_pivots,
            pivot_accel_enabled=True,
            pivot_shadow_mode=True,
        )
        # Telemetry accurately reflects index spot in R1 zone
        assert s1["pivot_shadow"]["zone"] == "R1"
        assert s1["pivot_shadow"]["in_proximity"] is True
        assert s1["pivot_shadow"]["leg_qualified_for_accel"] is False
        assert s1["pivot_shadow"]["active"] is True

        # Stops must NOT be accelerated: Call is not profit-qualified (peak 115 < arm 140)
        # Trailing stop remains at baseline initial stop (100 * 0.75 = 75.0), NOT tightened to 5% (115 * 0.95 = 109.25)
        assert s1["call_leg"]["trailing_stop"] == 75.0
        assert s1["call_leg"].get("shadow_pivot_zone") is None
        assert s1["call_leg"]["status"] == "ACTIVE"
        assert s1["put_leg"]["status"] == "ACTIVE"

        # Similarly for Put in S1 zone when Put is at 110 (+10% < 30%)
        s2, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=80.0,
            current_put_ltp=110.0,
            current_ts=t1 + 60,
            days_to_expiry=4.0,
            nifty_spot=25590.0,  # In S1
            daily_pivots=base_pivots,
            pivot_accel_enabled=True,
            pivot_shadow_mode=True,
        )
        assert s2["pivot_shadow"]["zone"] == "S1"
        assert s2["pivot_shadow"]["in_proximity"] is True
        assert s2["pivot_shadow"]["leg_qualified_for_accel"] is False
        assert s2["put_leg"]["trailing_stop"] == 75.0
        assert s2["put_leg"].get("shadow_pivot_zone") is None

    def test_call_in_r1_zone_accelerates_to_5pct_when_enabled(self, sample_state, base_pivots):
        """Call in profit while NIFTY enters R1 zone tightens trailing stop to 5%."""
        t1 = 1764057600
        # NIFTY spot at 25950 (within margin 25.8 of R1 25966.66)
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=150.0,
            current_put_ltp=70.0,
            current_ts=t1,
            days_to_expiry=4.0,  # Normal buffer 18%
            nifty_spot=25950.0,
            daily_pivots=base_pivots,
            pivot_accel_enabled=True,
        )
        # 5% buffer on peak 150: 150 * 0.95 = 142.50
        assert s1["call_leg"]["trailing_stop"] == 142.50
        assert s1["call_leg"]["status"] == "ACTIVE"
        assert s1["pivot_shadow"]["active"] is True
        assert s1["pivot_shadow"]["zone"] == "R1"

    def test_call_exits_on_5pct_pullback_with_pivot_reason(self, sample_state, base_pivots):
        """When Call pulls back 5% from peak while in R1, trailing stop triggers cleanly."""
        t1 = 1764057600
        # Tick 1: Peak at 200 in R1
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=200.0,
            current_put_ltp=60.0,
            current_ts=t1,
            days_to_expiry=4.0,
            nifty_spot=25960.0,
            daily_pivots=base_pivots,
            pivot_accel_enabled=True,
        )
        # Stop is 200 * 0.95 = 190.00
        assert s1["call_leg"]["trailing_stop"] == 190.00
        assert s1["call_leg"]["status"] == "ACTIVE"

        # Tick 2: Call price drops to 189.0 (breaching 190.00)
        s2, events = evaluate_fsm_tick(
            s1,
            current_call_ltp=189.0,
            current_put_ltp=65.0,
            current_ts=t1 + 60,
            days_to_expiry=4.0,
            nifty_spot=25960.0,
            daily_pivots=base_pivots,
            pivot_accel_enabled=True,
        )
        assert s2["call_leg"]["status"] == "TRAILING_STOP_CLOSED"
        assert s2["call_leg"]["exit_price"] == 189.0
        assert "PivotZone: R1" in s2["call_leg"]["exit_reason"]
        assert "Buffer: 5%" in s2["call_leg"]["exit_reason"]
        assert any("CALL TRAILING STOP" in ev for ev in events)

    def test_breach_arm_prevents_premature_exit_near_r1(self, sample_state, base_pivots):
        """Leg entering R1 without meeting breach arm (+40% peak gain) does not exit on minor pullback."""
        t1 = 1764057600
        # Call bought at 100, now at 120 (gain is +20%, below breach arm +40% = 140)
        # Spot is in R1
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=120.0,
            current_put_ltp=90.0,
            current_ts=t1,
            days_to_expiry=4.0,
            nifty_spot=25970.0,
            daily_pivots=base_pivots,
            pivot_accel_enabled=True,
        )
        # Pulls back to 110 (still below breach arm 140)
        s2, _ = evaluate_fsm_tick(
            s1,
            current_call_ltp=110.0,
            current_put_ltp=95.0,
            current_ts=t1 + 60,
            days_to_expiry=4.0,
            nifty_spot=25970.0,
            daily_pivots=base_pivots,
            pivot_accel_enabled=True,
        )
        # Leg MUST remain ACTIVE because peak (120) never reached breach arm (140)
        assert s2["call_leg"]["status"] == "ACTIVE"

    def test_monday_cliff_precedence_never_loosened(self, sample_state, base_pivots):
        """On Monday afternoon (14:00+ IST), the 5% theta cliff is never loosened by IV spike (8%)."""
        # Timestamp for Monday 14:30 IST: 2026-11-23 09:00:00 UTC = 14:30 IST (Monday)
        mon_cliff_ts = int(datetime(2026, 11, 23, 9, 0, 0, tzinfo=timezone.utc).timestamp())

        # Call in profit at 200, with IV spike (call_iv=25.0)
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=200.0,
            current_put_ltp=30.0,
            current_ts=mon_cliff_ts,
            days_to_expiry=0.1,
            call_iv=25.0,  # Triggers call_iv_spike
            nifty_spot=25780.0,  # Neutral spot
            daily_pivots=base_pivots,
            pivot_accel_enabled=False,
        )
        # Buffer must be strictly 5% (Monday cliff), NOT loosened to 8% (IV spike)!
        # 200 * 0.95 = 190.00 (NOT 200 * 0.92 = 184.00)
        assert s1["call_leg"]["trailing_stop"] == 190.00

    def test_shadow_mode_records_telemetry_without_live_exit(self, sample_state, base_pivots):
        """In shadow mode (pivot_accel_enabled=False), counterfactual exit is logged while live leg continues."""
        t1 = 1764057600
        # Peak at 200 in R1
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=200.0,
            current_put_ltp=50.0,
            current_ts=t1,
            days_to_expiry=4.0,  # Base buffer 18%
            nifty_spot=25965.0,  # In R1
            daily_pivots=base_pivots,
            pivot_accel_enabled=False,
            pivot_shadow_mode=True,
        )
        # Live stop uses 18%: 200 * 0.82 = 164.00
        assert s1["call_leg"]["trailing_stop"] == 164.00
        # Shadow stop uses 5%: 200 * 0.95 = 190.00
        assert s1["pivot_shadow"]["active"] is True
        assert s1["pivot_shadow"]["live_trail_stop"] == 164.00
        assert s1["pivot_shadow"]["shadow_trail_stop"] == 190.00
        assert s1["pivot_shadow"]["delta_points"] == 26.00
        assert s1["pivot_shadow"]["would_have_exited"] is False

        # Drop to 185: below shadow stop 190.00, but above live stop 164.00
        s2, _ = evaluate_fsm_tick(
            s1,
            current_call_ltp=185.0,
            current_put_ltp=55.0,
            current_ts=t1 + 60,
            days_to_expiry=4.0,
            nifty_spot=25965.0,
            daily_pivots=base_pivots,
            pivot_accel_enabled=False,
            pivot_shadow_mode=True,
        )
        # Live leg is STILL ACTIVE!
        assert s2["call_leg"]["status"] == "ACTIVE"
        # Shadow telemetry indicates it WOULD have exited!
        assert s2["pivot_shadow"]["would_have_exited"] is True

    def test_put_in_s1_zone_accelerates_and_exits_when_enabled(self, sample_state, base_pivots):
        """Put in profit while NIFTY breaks into S1 zone tightens to 5% and exits on pullback."""
        t1 = 1764057600
        # Put climbs to 200, NIFTY spot drops to 25590 (S1 is 25600.00, within margin 25.6)
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=40.0,
            current_put_ltp=200.0,
            current_ts=t1,
            days_to_expiry=4.0,
            nifty_spot=25590.0,
            daily_pivots=base_pivots,
            pivot_accel_enabled=True,
        )
        # 5% buffer on peak 200: 200 * 0.95 = 190.00
        assert s1["put_leg"]["trailing_stop"] == 190.00
        assert s1["put_leg"]["status"] == "ACTIVE"
        assert s1["pivot_shadow"]["active"] is True
        assert s1["pivot_shadow"]["zone"] == "S1"

        # Market bounces, Put drops to 188.0 (below 190.00)
        s2, events = evaluate_fsm_tick(
            s1,
            current_call_ltp=45.0,
            current_put_ltp=188.0,
            current_ts=t1 + 60,
            days_to_expiry=4.0,
            nifty_spot=25590.0,
            daily_pivots=base_pivots,
            pivot_accel_enabled=True,
        )
        assert s2["put_leg"]["status"] == "TRAILING_STOP_CLOSED"
        assert s2["put_leg"]["exit_price"] == 188.0
        assert "PivotZone: S1" in s2["put_leg"]["exit_reason"]
        assert "Buffer: 5%" in s2["put_leg"]["exit_reason"]
        assert any("PUT TRAILING STOP" in ev for ev in events)

    def test_shadow_stop_persists_and_triggers_after_spot_leaves_zone(self, sample_state, base_pivots):
        """Shadow stop ratchets at R1 and remains locked even after spot pulls back outside R1."""
        t1 = 1764057600
        # Tick 1: Call reaches 200 while NIFTY is in R1 (25965.0)
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=200.0,
            current_put_ltp=50.0,
            current_ts=t1,
            days_to_expiry=4.0,  # Base buffer 18%
            nifty_spot=25965.0,  # In R1
            daily_pivots=base_pivots,
            pivot_accel_enabled=False,
            pivot_shadow_mode=True,
        )
        assert s1["call_leg"]["trailing_stop"] == 164.00  # Live stop (18%)
        assert s1["call_leg"]["shadow_trailing_stop"] == 190.00  # Shadow stop (5%)
        assert s1["call_leg"]["shadow_pivot_zone"] == "R1"
        assert s1["pivot_shadow"]["would_have_exited"] is False

        # Tick 2: Spot reverses sharply down to 25800.0 (WELL OUTSIDE R1 margin)
        # Call LTP pulls back to 185.0 (below 190.00 shadow stop, above 164.00 live stop)
        s2, _ = evaluate_fsm_tick(
            s1,
            current_call_ltp=185.0,
            current_put_ltp=60.0,
            current_ts=t1 + 60,
            days_to_expiry=4.0,
            nifty_spot=25800.0,  # Neutral zone outside R1
            daily_pivots=base_pivots,
            pivot_accel_enabled=False,
            pivot_shadow_mode=True,
        )
        # Shadow stop MUST be retained at 190.00!
        assert s2["call_leg"]["shadow_trailing_stop"] == 190.00
        # Shadow status is CLOSED!
        assert s2["call_leg"]["shadow_status"] == "SHADOW_TRAILING_STOP_CLOSED"
        assert s2["call_leg"]["shadow_exit_price"] == 185.0
        assert s2["call_leg"]["shadow_realized_pnl"] == 85.0  # 185 - 100
        # Live position remains unaffected
        assert s2["call_leg"]["status"] == "ACTIVE"
        # Shadow telemetry reflects the counterfactual exit and records an event
        assert s2["pivot_shadow"]["would_have_exited"] is True
        assert s2["pivot_shadow"]["counterfactual_terminal"] is True
        assert s2["pivot_shadow"]["counterfactual_terminal_leg"] == "CALL"
        assert len(s2["pivot_shadow"]["events"]) == 1
        ev = s2["pivot_shadow"]["events"][0]
        assert ev["leg"] == "CALL"
        assert ev["exit_price"] == 185.0
        assert ev["realized_pnl"] == 85.0
        assert ev["live_fsm_status"] == "ACTIVE"

    def test_shadow_exit_not_triggered_if_never_in_pivot_zone(self, sample_state, base_pivots):
        """If spot never touched a pivot zone, shadow status does NOT trigger closed and no events are recorded."""
        t1 = 1764057600
        # Call climbs to 200 at neutral spot 25780 (far from R1=25970)
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=200.0,
            current_put_ltp=50.0,
            current_ts=t1,
            days_to_expiry=4.0,  # Base buffer 18%
            nifty_spot=25780.0,  # Neutral spot outside R1
            daily_pivots=base_pivots,
            pivot_accel_enabled=False,
            pivot_shadow_mode=True,
        )
        assert s1["call_leg"]["trailing_stop"] == 164.00  # 200 * 0.82
        assert s1["call_leg"].get("shadow_pivot_zone") is None

        # Price drops to 160 (breaching base trailing stop 164.00)
        s2, _ = evaluate_fsm_tick(
            s1,
            current_call_ltp=160.0,
            current_put_ltp=50.0,
            current_ts=t1 + 60,
            days_to_expiry=4.0,
            nifty_spot=25780.0,
            daily_pivots=base_pivots,
            pivot_accel_enabled=False,
            pivot_shadow_mode=True,
        )
        # Base FSM live exit triggers
        assert s2["call_leg"]["status"] == "TRAILING_STOP_CLOSED"
        # Shadow status must NOT trigger a pivot counterfactual exit because shadow_pivot_zone is None!
        assert s2["call_leg"].get("shadow_status") == "ACTIVE"
        assert len(s2.get("pivot_shadow", {}).get("events", [])) == 0

    def test_shadow_exit_terminal_leg_put_and_both(self, sample_state, base_pivots):
        """Verify counterfactual_terminal_leg is set to 'PUT' or 'BOTH' when corresponding legs exit."""
        t1 = 1764057600
        # Put climbs to 200 in S1 (spot 25590)
        s1, _ = evaluate_fsm_tick(
            sample_state,
            current_call_ltp=50.0,
            current_put_ltp=200.0,
            current_ts=t1,
            days_to_expiry=4.0,
            nifty_spot=25590.0,  # In S1
            daily_pivots=base_pivots,
            pivot_accel_enabled=False,
            pivot_shadow_mode=True,
        )
        assert s1["put_leg"]["shadow_trailing_stop"] == 190.00
        assert s1["put_leg"]["shadow_pivot_zone"] == "S1"

        # Put drops to 185, triggering shadow stop
        s2, _ = evaluate_fsm_tick(
            s1,
            current_call_ltp=50.0,
            current_put_ltp=185.0,
            current_ts=t1 + 60,
            days_to_expiry=4.0,
            nifty_spot=25590.0,
            daily_pivots=base_pivots,
            pivot_accel_enabled=False,
            pivot_shadow_mode=True,
        )
        assert s2["put_leg"]["shadow_status"] == "SHADOW_TRAILING_STOP_CLOSED"
        assert s2["pivot_shadow"]["counterfactual_terminal"] is True
        assert s2["pivot_shadow"]["counterfactual_terminal_leg"] == "PUT"

        # Now simulate Call also closing
        s2["call_leg"]["shadow_status"] = "SHADOW_TRAILING_STOP_CLOSED"
        s2["call_leg"]["shadow_pivot_zone"] = "R1"
        s3, _ = evaluate_fsm_tick(
            s2,
            current_call_ltp=45.0,
            current_put_ltp=180.0,
            current_ts=t1 + 120,
            days_to_expiry=4.0,
            nifty_spot=25590.0,
            daily_pivots=base_pivots,
            pivot_accel_enabled=False,
            pivot_shadow_mode=True,
        )
        assert s3["pivot_shadow"]["counterfactual_terminal"] is True
        assert s3["pivot_shadow"]["counterfactual_terminal_leg"] == "BOTH"
