"""Tests for the fixed Thursday ITM1 strike rule (common.expiry.itm1_strikes)."""

import pytest

from common.expiry import itm1_strikes


def test_itm1_reported_spot_2026_11_12():
    # Spot 25325.15 at 09:32 IST on Thu 2026-11-12: CE 25300 / PE 25350 (not 25150 / 25550)
    put, call = itm1_strikes(25325.15, step=50)
    assert call == 25300
    assert put == 25350
    assert call < 25325.15 < put


def test_itm1_exact_spot_on_strike_is_strictly_itm():
    # When spot sits exactly on a strike, both legs still move one step ITM.
    put, call = itm1_strikes(25300.0, step=50)
    assert call == 25250
    assert put == 25350


def test_itm1_just_below_strike_boundary():
    put, call = itm1_strikes(25299.9, step=50)
    assert call == 25250
    assert put == 25300


def test_itm1_100_point_grid():
    put, call = itm1_strikes(25325.15, step=100)
    assert call == 25300
    assert put == 25400


@pytest.mark.parametrize("bad_spot, bad_step", [(0, 50), (-1, 50), (25000, 0)])
def test_itm1_rejects_invalid_inputs(bad_spot, bad_step):
    with pytest.raises(ValueError):
        itm1_strikes(bad_spot, step=bad_step)


def test_no_ai_strike_selection_in_any_collector():
    """Neither the Tuesday weekly nor the Thursday 3-day collector may select strikes via AI."""
    import importlib

    for mod_name in ("strategies.weekly_option_collector", "strategies.thursday_option_collector"):
        mod = importlib.import_module(mod_name)
        assert not hasattr(mod, "select_strikes"), f"{mod_name} still imports AI selector"
        assert not hasattr(mod, "get_last_composite_score"), f"{mod_name} still reads AI score"
    with pytest.raises(ImportError):
        importlib.import_module("common.ai_strike_selector")
