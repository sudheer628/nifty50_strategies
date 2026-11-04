"""
Manual square-off handling (both tracks, same broker account).

Covers:
  A. Leg sold manually, contract not shared -> no SELL, authorization retired, alert.
  B. Manual SELL still pending -> left untouched (manual_sell_conflict).
  C. Leg sold manually on a contract the other track also holds -> no SELL for either track
     (owner unknown) until the manual exit is recorded with mark_manual_exit.py; afterwards the
     other track exits exactly its own lot.
  Plus scripts/mark_manual_exit.py behaviour.
"""
import importlib.util
import json
import os
import sys
from unittest.mock import patch

import pytest

from tests.test_shared_contract_isolation import FakeRedis, thu_module, tue_module

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("mark_manual_exit", os.path.join(repo_root, "scripts", "mark_manual_exit.py"))
mme = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mme)

SYM, TOK, EXPIRY = "NIFTY26N0923700PE", "NSE_FO|39948", "2026-11-09"


def _record(cycle_id, entry_qty=65, broker="upstox"):
    return {"cycle_id": cycle_id, "status": "active", "call_strike": 23500, "put_strike": 23700,
            "expiry_date": EXPIRY, "broker": broker, "entry_status": "FILLED", "entry_qty": entry_qty,
            "exit_signals": {"alpha_fsm": {}}}


def _snapshot(cycle_id, call_status="TRAILING_STOP_CLOSED", put_status="ACTIVE", strategy="nifty50_weekly_option_collector"):
    return {
        "strategy_name": strategy, "cycle_id": cycle_id, "status": "ongoing", "expiry_date": "20261109",
        "call_strike": 23500, "put_strike": 23700, "call_buy_price": 255.55, "put_buy_price": 236.35,
        "broker": "upstox", "entry_status": "FILLED", "entry_qty": 65,
        "alpha_fsm": {
            "state": "SOLO_PUT", "moneyness": "ITM", "total_initial_cost": 491.9, "total_extrinsic_risk": 291.9,
            "call_leg": {"strike": 23500, "buy_price": 255.55, "status": call_status, "exit_price": 437.95,
                         "realized_pnl": 182.4, "peak_ltp": 328.15, "trailing_stop": 301.9},
            "put_leg": {"strike": 23700, "buy_price": 236.35, "status": put_status, "exit_price": None,
                        "realized_pnl": 0.0, "peak_ltp": 236.35, "trailing_stop": 177.26},
        },
    }


@pytest.fixture
def stores(tmp_path):
    """Isolated unified store + snapshots holding both tracks on the same Upstox contract."""
    active_file = tmp_path / "active_strategies.json"
    tue_snap = tmp_path / "current_week_buy.json"
    thu_snap = tmp_path / "current_thursday_buy.json"
    active_file.write_text(json.dumps({
        "5day_strategy": _record("CYCLE-TUE-20261103"),
        "3day_strategy": _record("CYCLE-THU-20261105"),
    }))
    tue_snap.write_text(json.dumps(_snapshot("CYCLE-TUE-20261103")))
    thu_snap.write_text(json.dumps(_snapshot("CYCLE-THU-20261105", call_status="ACTIVE",
                                             strategy="nifty50_thursday_3day_collector")))
    with patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", str(active_file)), \
         patch("common.unified_strategy_store.ACTIVE_SNAPSHOT_FILE", str(tue_snap)), \
         patch("common.unified_strategy_store.THURSDAY_SNAPSHOT_FILE", str(thu_snap)):
        yield {"active": active_file, "tue_snap": tue_snap, "thu_snap": thu_snap}


MODS = {"tue": (tue_module, "manage_tuesday_exit_orders", "TUE-SL"),
        "thu": (thu_module, "manage_thursday_exit_orders", "THU-SL")}


def _authorize(r, cycle, qty=65):
    r.set(f"fno:upstox:enabled:{cycle}:{EXPIRY}:23700:PE", json.dumps({"enabled": True, "qty": qty}))
    r.set(f"ORDERS_LIVE_{cycle.upper()}", "1")


def _pos(qty):
    return [{"tradingsymbol": SYM, "instrument_token": TOK, "exchange": "NSE_FO", "quantity": qty, "product": "D"}] if qty else []


def _order(order_id, tag, status="complete", qty=65, filled=None, pending=0, order_type="MARKET"):
    return {"order_id": order_id, "tag": tag, "transaction_type": "SELL", "order_type": order_type, "status": status,
            "quantity": qty, "filled_quantity": qty if filled is None else filled, "pending_quantity": pending,
            "trading_symbol": SYM, "tradingsymbol": SYM, "instrument_token": TOK}


def _run(r, cycle, positions, orders, new_order_id=None):
    m, fn, tag = MODS[cycle]
    entry = {"entry_order_id": f"{cycle}_BUY", "entry_price": 236.35, "entry_status": "FILLED",
             "same_broker_contract_overlap": True, "ambiguous_entry_attribution": False}
    with patch("common.unified_strategy_store.get_active_leg_entry_details", return_value=entry), \
         patch.object(m, "is_market_hours_ist", return_value=(True, "open")), \
         patch.object(m, "fetch_order_book", return_value=orders), \
         patch.object(m, "fetch_ltp", return_value=150.0), \
         patch.object(m, "cancel_order", return_value={"success": True}), \
         patch.object(m, "place_exit_order_slm") as mock_slm, \
         patch.object(m, "place_exit_order_market", return_value={"success": True, "order_id": new_order_id or f"{cycle}_MKT"}) as mock_mkt, \
         patch.object(m, "dispatch_order_failure_discord") as mock_alert:
        res = getattr(m, fn)(token="SIM_TOKEN", positions=positions, orders=orders, execute=True, redis_client=r,
                             broker="upstox", active_expiry=EXPIRY, force=False, exit_immediately=True,
                             target_lots=1, tag=tag)
        m.reconcile_cycle_legs(r, positions, orders, broker="upstox", cycle=cycle, tag=tag, active_expiry=EXPIRY)
    mock_slm.assert_not_called()
    return {"sells": [c.args[2] for c in mock_mkt.call_args_list], "res": res,
            "alerts": [c.kwargs.get("action") for c in mock_alert.call_args_list]}


MANUAL_FILLED = _order("MANUAL_1", "", order_type="LIMIT")


def test_case_a_manual_close_not_shared_no_sell(stores):
    data = json.loads(stores["active"].read_text())
    data["3day_strategy"]["put_strike"] = 24000  # Thursday does not hold this contract
    stores["active"].write_text(json.dumps(data))
    r = FakeRedis()
    _authorize(r, "tue")
    out = _run(r, "tue", _pos(0), [MANUAL_FILLED])
    assert out["sells"] == []
    assert r.get(f"fno:upstox:enabled:tue:{EXPIRY}:23700:PE") is None
    assert "SELL_AUTH_UNMATCHED" in out["alerts"]


def test_case_b_pending_manual_sell_left_untouched(stores):
    r = FakeRedis()
    _authorize(r, "tue")
    data = json.loads(stores["active"].read_text())
    data["3day_strategy"]["put_strike"] = 24000
    stores["active"].write_text(json.dumps(data))
    out = _run(r, "tue", _pos(65), [_order("MANUAL_1", "", status="open", filled=0, pending=65, order_type="LIMIT")])
    assert out["sells"] == []
    assert [a["status"] for a in out["res"]["actions"]] == ["manual_sell_conflict"]


@pytest.mark.parametrize("cycle", ["tue", "thu"])
def test_case_c_shared_contract_manual_sale_blocks_both_tracks(stores, cycle):
    """User sold one track's 65 manually; net 65 left; either track's exit must not guess the owner."""
    r = FakeRedis()
    _authorize(r, cycle)
    out = _run(r, cycle, _pos(65), [MANUAL_FILLED])
    assert out["sells"] == []
    assert out["res"]["actions"][-1]["status"] == "shared_contract_shortfall"
    assert "SHARED_CONTRACT_SHORTFALL" in out["alerts"]


def test_case_c_after_mark_manual_exit_other_track_exits_own_lot(stores):
    r = FakeRedis()
    mme.mark_manual_exit("tuesday", "PUT", 152.40, 1793856000, snapshot_file=str(stores["tue_snap"]),
                         active_file=str(stores["active"]), redis_client=r)
    assert r.get(f"fno:upstox:exited:tue:{EXPIRY}:23700:PE") == "65"
    _authorize(r, "thu")
    out = _run(r, "thu", _pos(65), [MANUAL_FILLED], new_order_id="THU_MKT_1")
    assert out["sells"] == [65]


def test_shared_contract_both_hold_each_exits_only_own_lot(stores):
    r = FakeRedis()
    _authorize(r, "tue")
    out = _run(r, "tue", _pos(130), [], new_order_id="TUE_MKT_1")
    assert out["sells"] == [65]
    # Thursday right after Tuesday's fill (Tuesday's manager has not recorded it yet): must still sell its 65
    _authorize(r, "thu")
    r.delete(f"fno:upstox:exited:tue:{EXPIRY}:23700:PE")
    out = _run(r, "thu", _pos(65), [_order("TUE_MKT_1", "TUE-SL")], new_order_id="THU_MKT_1")
    assert out["sells"] == [65]


def test_shared_contract_own_exit_recorded_survives_retirement(stores):
    r = FakeRedis()
    _authorize(r, "tue")
    _run(r, "tue", _pos(130), [], new_order_id="TUE_MKT_1")
    _run(r, "tue", _pos(65), [_order("TUE_MKT_1", "TUE-SL")])  # records fill, retires Tuesday leg
    assert r.get(f"fno:upstox:enabled:tue:{EXPIRY}:23700:PE") is None
    assert r.get(f"fno:upstox:exited:tue:{EXPIRY}:23700:PE") == "65"
    # Next day: order book reset; Thursday exits and must not be blocked by a stale Tuesday reservation
    _authorize(r, "thu")
    out = _run(r, "thu", _pos(65), [], new_order_id="THU_MKT_1")
    assert out["sells"] == [65]


# ---------------------------------------------------------------------------
# mark_manual_exit.py
# ---------------------------------------------------------------------------

def test_mark_manual_exit_updates_fsm_store_and_redis(stores):
    r = FakeRedis()
    _authorize(r, "tue")
    plan = mme.mark_manual_exit("tuesday", "PUT", 152.40, 1793856000, snapshot_file=str(stores["tue_snap"]),
                                active_file=str(stores["active"]), redis_client=r)
    snap = json.loads(stores["tue_snap"].read_text())
    put = snap["alpha_fsm"]["put_leg"]
    assert put["status"] == "MANUAL_CLOSED" and put["exit_price"] == 152.40
    assert put["realized_pnl"] == round(152.40 - 236.35, 2)
    assert snap["alpha_fsm"]["state"] == "FLAT"
    assert snap["alpha_fsm"]["realized_pnl_pts"] == round(182.4 + 152.40 - 236.35, 2)
    rec = json.loads(stores["active"].read_text())["5day_strategy"]
    assert rec["exit_signals"]["alpha_fsm"]["put_triggered"] is True
    assert rec["manual_exits"][0]["price"] == 152.40
    assert r.get(f"fno:upstox:exited:tue:{EXPIRY}:23700:PE") == "65"
    assert r.get(f"fno:upstox:enabled:tue:{EXPIRY}:23700:PE") is None
    assert r.get("ORDERS_LIVE_TUE") == "0" and plan["gate_reset"] == "ORDERS_LIVE_TUE"
    # Thursday's state untouched
    assert json.loads(stores["thu_snap"].read_text())["alpha_fsm"]["put_leg"]["status"] == "ACTIVE"


def test_mark_manual_exit_dry_run_writes_nothing(stores):
    r = FakeRedis()
    _authorize(r, "tue")
    before = (stores["tue_snap"].read_text(), stores["active"].read_text(), dict(r.data))
    plan = mme.mark_manual_exit("tuesday", "PUT", 152.40, 1793856000, snapshot_file=str(stores["tue_snap"]),
                                active_file=str(stores["active"]), redis_client=r, dry_run=True)
    assert plan["dry_run"] is True and plan["retire_authorization"] is True
    assert (stores["tue_snap"].read_text(), stores["active"].read_text(), dict(r.data)) == before


def test_mark_manual_exit_refuses_closed_leg(stores):
    with pytest.raises(RuntimeError, match="already TRAILING_STOP_CLOSED"):
        mme.mark_manual_exit("tuesday", "CALL", 437.95, 1793856000, snapshot_file=str(stores["tue_snap"]),
                             active_file=str(stores["active"]), redis_client=FakeRedis())


def test_mark_manual_exit_keeps_authorization_when_managed_order_tracked(stores):
    """An SL-M still tracked for the leg: keep keys so the exit manager cancels the orphan order itself."""
    r = FakeRedis()
    _authorize(r, "tue")
    trail_key = f"fno:upstox:trail:tue:NIFTY:26N09:23700:PE:{SYM}:NSE_FO_39948:order_id"
    r.set(trail_key, "SLM_1")
    plan = mme.mark_manual_exit("tuesday", "PUT", 152.40, 1793856000, snapshot_file=str(stores["tue_snap"]),
                                active_file=str(stores["active"]), redis_client=r)
    assert plan["retire_authorization"] is False
    assert r.get(f"fno:upstox:enabled:tue:{EXPIRY}:23700:PE") is not None
    assert r.get("ORDERS_LIVE_TUE") == "1"
    assert r.get(f"fno:upstox:exited:tue:{EXPIRY}:23700:PE") == "65"
