"""
Shared broker contract isolation between the Tuesday and Thursday tracks.

Scenario: both tracks hold the same contract on the same broker account
(net broker position = 130). One track exits its 65 on day D; the other
track's 65 stays open. On day D+1 the broker order book no longer shows
day D's filled SELL. The exited track must never sell the other track's lot.
"""
import fnmatch
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
scripts_dir = os.path.join(repo_root, "scripts")
for p in (repo_root, scripts_dir):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(scripts_dir, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tue_module = _load("tuesday_exit_orders_shared", "Tuesday-Fno-Exit-Orders-V1.py")
thu_module = _load("thursday_exit_orders_shared", "Thursday-Fno-Exit-Orders-V1.py")


class FakeRedis:
    """Minimal in-memory Redis covering the calls used by the exit managers."""

    def __init__(self, data=None):
        self.data = dict(data or {})
        self.sets = {}

    def get(self, k):
        return self.data.get(k)

    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.data:
            return False
        self.data[k] = str(v)
        return True

    def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)
            self.sets.pop(k, None)

    def expire(self, k, ttl):
        return k in self.data

    def scan_iter(self, pattern, count=None):
        return [k for k in list(self.data) if fnmatch.fnmatchcase(k, pattern)]

    def smembers(self, k):
        return set(self.sets.get(k, set()))

    def sadd(self, k, *vals):
        self.sets.setdefault(k, set()).update(vals)

    def srem(self, k, *vals):
        self.sets.get(k, set()).difference_update(vals)

    def pipeline(self):
        return self

    def execute(self):
        return []


SYM, TOK, EXPIRY = "NIFTY26N0923700PE", "NSE_FO|39948", "2026-11-09"

TRACKS = [
    pytest.param(tue_module, "tue", "TUE-SL", "manage_tuesday_exit_orders", "261103000038239", id="tuesday"),
    pytest.param(thu_module, "thu", "THU-SL", "manage_thursday_exit_orders", "261105000011111", id="thursday"),
]


def _setup(module, cycle):
    keys = module.build_trail_redis_keys("upstox", SYM, TOK, active_expiry=EXPIRY)
    canonical = f"fno:upstox:enabled:{cycle}:{EXPIRY}:23700:PE"
    r = FakeRedis({
        canonical: json.dumps({"enabled": True, "qty": 65}),
        keys["mode"]: "immediate",
        keys["market_order_id"]: "OWN_MKT_SELL_1",
        f"ORDERS_LIVE_{cycle.upper()}": "1",
    })
    return r, keys, canonical


def _run(module, fn, r, positions, orders, entry_id, tag):
    entry = {"entry_order_id": entry_id, "entry_price": 236.35, "entry_status": "FILLED",
             "same_broker_contract_overlap": True, "ambiguous_entry_attribution": False}
    with patch("common.unified_strategy_store.get_active_leg_entry_details", return_value=entry), \
         patch.object(module, "is_market_hours_ist", return_value=(True, "open")), \
         patch.object(module, "fetch_order_book", return_value=[]), \
         patch.object(module, "fetch_ltp", return_value=150.0), \
         patch.object(module, "cancel_order", return_value={"success": True}), \
         patch.object(module, "place_exit_order_market") as mock_mkt, \
         patch.object(module, "place_exit_order_slm") as mock_slm, \
         patch.object(module, "dispatch_order_failure_discord"):
        res = getattr(module, fn)(
            token="SIM_TOKEN", positions=positions, orders=orders, execute=True, redis_client=r,
            broker="upstox", active_expiry=EXPIRY, force=False, exit_immediately=True,
        )
    return res, mock_mkt, mock_slm


# Net broker position after this track's exit: only the other track's 65 remain.
REMAINING_OTHER_TRACK_POS = [{"tradingsymbol": SYM, "instrument_token": TOK, "exchange": "NSE_FO",
                              "quantity": 65, "product": "D", "average_price": 150.0, "last_price": 150.0}]


def _own_filled_sell(tag):
    return {"order_id": "OWN_MKT_SELL_1", "tag": tag, "transaction_type": "SELL", "order_type": "MARKET",
            "status": "complete", "quantity": 65, "filled_quantity": 65, "pending_quantity": 0,
            "trading_symbol": SYM, "tradingsymbol": SYM, "instrument_token": TOK}


@pytest.mark.parametrize("module,cycle,tag,fn,entry_id", TRACKS)
def test_day_d_records_own_fill_and_retires_leg(module, cycle, tag, fn, entry_id):
    r, keys, canonical = _setup(module, cycle)

    res, mock_mkt, mock_slm = _run(module, fn, r, REMAINING_OTHER_TRACK_POS, [_own_filled_sell(tag)], entry_id, tag)
    mock_mkt.assert_not_called()
    mock_slm.assert_not_called()
    assert r.get(keys["exit_filled_qty"]) == "65"

    with patch.object(module, "dispatch_order_failure_discord") as mock_alert:
        cleaned = module.reconcile_cycle_legs(
            r, REMAINING_OTHER_TRACK_POS, [_own_filled_sell(tag)], broker="upstox",
            cycle=cycle, tag=tag, active_expiry=EXPIRY,
        )
    assert len(cleaned) == 1 and cleaned[0]["exit_executed"] is True
    mock_alert.assert_not_called()
    assert r.get(canonical) is None, "own-complete leg must be retired even though the other track still holds qty"
    assert r.get(keys["exit_filled_qty"]) is None


@pytest.mark.parametrize("module,cycle,tag,fn,entry_id", TRACKS)
def test_day_d_plus_1_never_sells_other_tracks_lot(module, cycle, tag, fn, entry_id):
    r, keys, canonical = _setup(module, cycle)
    _run(module, fn, r, REMAINING_OTHER_TRACK_POS, [_own_filled_sell(tag)], entry_id, tag)
    module.reconcile_cycle_legs(r, REMAINING_OTHER_TRACK_POS, [_own_filled_sell(tag)], broker="upstox",
                                cycle=cycle, tag=tag, active_expiry=EXPIRY)

    # Day D+1: order book reset overnight, other track's 65 still open
    res, mock_mkt, mock_slm = _run(module, fn, r, REMAINING_OTHER_TRACK_POS, [], entry_id, tag)
    mock_mkt.assert_not_called()
    mock_slm.assert_not_called()
    assert res["authorized_positions"] == 0


@pytest.mark.parametrize("module,cycle,tag,fn,entry_id", TRACKS)
def test_persisted_fill_blocks_resell_when_leg_not_yet_retired(module, cycle, tag, fn, entry_id):
    """Defense in depth: even if reconcile did not run, persisted own fill blocks a D+1 re-sell."""
    r, keys, canonical = _setup(module, cycle)
    r.set(keys["exit_filled_qty"], "65")

    res, mock_mkt, mock_slm = _run(module, fn, r, REMAINING_OTHER_TRACK_POS, [], entry_id, tag)
    mock_mkt.assert_not_called()
    mock_slm.assert_not_called()
    action = [a for a in res["actions"] if a.get("symbol") == SYM][0]
    assert action["status"] == "market_order_already_filled"
    assert action["own_filled_qty"] == 65


@pytest.mark.parametrize("module,cycle,tag,fn,entry_id", TRACKS)
def test_first_exit_on_shared_contract_sells_only_own_quantity(module, cycle, tag, fn, entry_id):
    """Net 130 held across both tracks: the exiting track sells exactly its own 65."""
    r, keys, canonical = _setup(module, cycle)
    r.delete(keys["market_order_id"])
    positions = [{"tradingsymbol": SYM, "instrument_token": TOK, "exchange": "NSE_FO",
                  "quantity": 130, "product": "D", "average_price": 190.0, "last_price": 150.0}]

    entry = {"entry_order_id": entry_id, "entry_price": 236.35, "entry_status": "FILLED",
             "same_broker_contract_overlap": True, "ambiguous_entry_attribution": False}
    with patch("common.unified_strategy_store.get_active_leg_entry_details", return_value=entry), \
         patch.object(module, "is_market_hours_ist", return_value=(True, "open")), \
         patch.object(module, "fetch_order_book", return_value=[]), \
         patch.object(module, "fetch_ltp", return_value=150.0), \
         patch.object(module, "cancel_order", return_value={"success": True}), \
         patch.object(module, "place_exit_order_market", return_value={"success": True, "order_id": "NEW_1"}) as mock_mkt, \
         patch.object(module, "dispatch_order_failure_discord"):
        getattr(module, fn)(
            token="SIM_TOKEN", positions=positions, orders=[], execute=True, redis_client=r,
            broker="upstox", active_expiry=EXPIRY, force=False, exit_immediately=True,
        )
    mock_mkt.assert_called_once()
    assert mock_mkt.call_args.args[2] == 65


def test_paired_buy_snapshot_baseline_passes_existing_other_track_qty_to_rollback():
    """Thursday entry on a contract Tuesday already holds: rollback flatness uses the pre-entry baseline."""
    from common import entry_order_manager as eom

    call_c = {"instrument_token": "NSE_FO|1111", "trading_symbol": "NIFTY26N0924000CE"}
    put_c = {"instrument_token": TOK, "trading_symbol": SYM}
    pre_positions = [{"instrument_token": TOK, "tradingsymbol": SYM, "quantity": 65}]

    with patch.object(eom, "preflight_entry", return_value=(True, "", {})), \
         patch.object(eom, "place_market_order", side_effect=[
             {"success": True, "order_id": "C1"}, {"success": False, "error": "rejected"}]), \
         patch.object(eom, "rollback_partial_entry", return_value={"unwind_successful": True}) as mock_rb:
        eom.execute_paired_market_buy(
            broker="upstox", call_contract=call_c, put_contract=put_c, quantity=65,
            call_ltp=100.0, put_ltp=150.0, tag="THU-BUY", token="SIM_TOKEN",
            position_provider=lambda: pre_positions, snapshot_baseline=True,
        )
    kwargs = mock_rb.call_args.kwargs
    assert kwargs["baseline_put_qty"] == 65
    assert kwargs["baseline_call_qty"] == 0
