"""
Per-track quantity isolation when Tuesday and Thursday share one broker account.

Scenario under test: Tuesday holds 2 lots (130) of a contract, Thursday buys 1 lot (65)
of the same contract on the same Upstox account -> broker net position 195.
Each track must buy, authorize and exit exactly its own quantity.
"""
import json
import os
import tempfile
from contextlib import ExitStack
from datetime import date
from unittest.mock import patch

import pytest

from tests.test_shared_contract_isolation import FakeRedis, thu_module, tue_module

SYM, TOK, EXPIRY = "NIFTY26N0923500CE", "NSE_FO|39939", "2026-11-09"


# ---------------------------------------------------------------------------
# Entry sizing: BUY quantity derives from each track's own LOTS_*
# ---------------------------------------------------------------------------

def test_entry_qty_uses_track_lots():
    from config import get_entry_qty_for_track
    with patch.dict(os.environ, {"LOTS_TUESDAY": "2", "LOTS_THURSDAY": "1", "NIFTY_LOT_SIZE": "65", "STRATEGY_BUY_QTY": "65"}):
        assert get_entry_qty_for_track("5day_strategy") == 130
        assert get_entry_qty_for_track("3day_strategy") == 65


def test_entry_qty_falls_back_to_strategy_buy_qty_when_lots_unset():
    from config import get_entry_qty_for_track
    env = {"NIFTY_LOT_SIZE": "65", "STRATEGY_BUY_QTY": "195"}
    with patch.dict(os.environ, env):
        os.environ.pop("LOTS_TUESDAY", None)
        assert get_entry_qty_for_track("5day_strategy") == 195


def test_entry_qty_rejects_non_positive_lots():
    from config import get_entry_qty_for_track
    with patch.dict(os.environ, {"LOTS_THURSDAY": "0"}):
        with pytest.raises(ValueError):
            get_entry_qty_for_track("3day_strategy")


def _tuesday_collector_live_buy(env, fill_qty=None):
    """Run the real Tuesday collector entry path with broker calls mocked; return (place mock, snapshot, unified record)."""
    from strategies.weekly_option_collector import collect_once
    saved = []
    with tempfile.TemporaryDirectory() as tmp_dir, ExitStack() as st:
        active_json = os.path.join(tmp_dir, "active_strategies.json")
        st.enter_context(patch("common.unified_strategy_store.ACTIVE_STRATEGIES_JSON", active_json))
        for target in ("sync_active_cycles_to_mongodb", "append_telemetry_log_to_mongodb", "reset_cycle_live_logs_in_mongodb"):
            st.enter_context(patch(f"common.unified_strategy_store.{target}"))
        st.enter_context(patch("strategies.weekly_option_collector._ensure_db", return_value=os.path.join(tmp_dir, "weekly.db")))
        st.enter_context(patch("strategies.weekly_option_collector.update_active_fsm_state"))
        st.enter_context(patch("strategies.weekly_option_collector.load_active_snapshot", side_effect=lambda **kw: saved[-1] if saved else None))
        st.enter_context(patch("strategies.weekly_option_collector.save_active_snapshot", side_effect=lambda s, **kw: saved.append(s)))
        st.enter_context(patch("strategies.weekly_option_collector.insert_buy_snapshot"))
        st.enter_context(patch("strategies.weekly_option_collector._today_ist", return_value=date(2026, 11, 3)))
        st.enter_context(patch("strategies.weekly_option_collector.check_nse_holiday", return_value=False))
        st.enter_context(patch("strategies.weekly_option_collector.is_tuesday", return_value=True))
        st.enter_context(patch("strategies.weekly_option_collector.get_nifty_spot", return_value={"ltp": 23600.0, "open": 23550.0, "close": 23500.0}))
        st.enter_context(patch("strategies.weekly_option_collector.select_strikes", return_value=(23500, 23700, 23500, 23700, "DYNAMIC", "Rationale", 0.0, "NEUTRAL", 0.68, -0.68)))
        st.enter_context(patch("strategies.weekly_option_collector.get_nifty_option_chain", return_value={
            "call": {"ltp": 250.0, "token": "39939", "trading_symbol": "NIFTY09NOV2623500CE"},
            "put": {"ltp": 230.0, "token": "39948", "trading_symbol": "NIFTY09NOV2623700PE"},
        }))
        st.enter_context(patch("common.entry_order_manager.resolve_upstox_option_key", side_effect=lambda exp, s, o: f"NSE_FO|{s}{o}"))
        st.enter_context(patch("common.entry_order_manager.get_available_funds", return_value=1_000_000.0))
        mock_place = st.enter_context(patch("common.entry_order_manager.place_market_order"))
        mock_poll = st.enter_context(patch("common.entry_order_manager.poll_order_fills"))
        st.enter_context(patch.dict(os.environ, {"LIVE_BUY_TRIGGER_TUESDAY": "true", "MOCK_BROKER": "true",
                                                 "TUESDAY_BROKER": "upstox", **env}))
        mock_place.side_effect = [{"success": True, "order_id": "TUE_C"}, {"success": True, "order_id": "TUE_P"}]
        qty = fill_qty or int(env["LOTS_TUESDAY"]) * 65
        mock_poll.return_value = (True, {
            "TUE_C": {"status": "complete", "average_price": 250.5, "filled_quantity": qty},
            "TUE_P": {"status": "complete", "average_price": 230.5, "filled_quantity": qty},
        })
        assert collect_once() is True
        with open(active_json) as f:
            unified = json.load(f)["5day_strategy"]
    return mock_place, saved[-1], unified


def test_tuesday_collector_buys_and_records_two_lots():
    mock_place, snap, unified = _tuesday_collector_live_buy({"LOTS_TUESDAY": "2", "STRATEGY_BUY_QTY": "65"})
    assert [c.kwargs["quantity"] for c in mock_place.call_args_list] == [130, 130]
    assert snap["entry_status"] == "FILLED"
    assert snap["entry_qty"] == 130
    assert unified["entry_qty"] == 130


# ---------------------------------------------------------------------------
# Exit authorization quantity = recorded entry quantity
# ---------------------------------------------------------------------------

def _publish(strategy, env, snapshot=None, tmp_path=None):
    from unittest.mock import MagicMock
    from common.unified_strategy_store import publish_exit_authorization_to_redis
    snap_file = tmp_path / "current_week_buy.json"
    snap_file.write_text(json.dumps(snapshot or {}))
    r = MagicMock()
    with patch("common.unified_strategy_store.ACTIVE_SNAPSHOT_FILE", str(snap_file)), patch.dict(os.environ, env):
        publish_exit_authorization_to_redis("5day_strategy", True, False, strategy, redis_client=r)
    return {c.args[0]: c.args[1] for c in r.set.call_args_list}


BASE_STRAT = {"cycle_id": "CYCLE-TUE-20261110", "call_strike": 23500, "put_strike": 23700,
              "expiry_date": "2026-11-16", "broker": "upstox", "entry_status": "FILLED"}


def test_publisher_authorizes_recorded_entry_qty_over_env_lots(tmp_path):
    sets = _publish({**BASE_STRAT, "entry_qty": 130}, {"LOTS_TUESDAY": "1"}, tmp_path=tmp_path)
    assert json.loads(sets["fno:upstox:enabled:tue:2026-11-16:23500:CE"])["qty"] == 130


def test_publisher_uses_snapshot_entry_qty_for_same_cycle(tmp_path):
    snap = {**BASE_STRAT, "expiry_date": "20261116", "entry_qty": 130}
    sets = _publish(dict(BASE_STRAT), {"LOTS_TUESDAY": "1"}, snapshot=snap, tmp_path=tmp_path)
    assert json.loads(sets["fno:upstox:enabled:tue:2026-11-16:23500:CE"])["qty"] == 130


def test_publisher_falls_back_to_lots_for_legacy_cycle(tmp_path):
    sets = _publish(dict(BASE_STRAT), {"LOTS_TUESDAY": "1"}, tmp_path=tmp_path)
    assert json.loads(sets["fno:upstox:enabled:tue:2026-11-16:23500:CE"])["qty"] == 65


# ---------------------------------------------------------------------------
# Exits on a shared contract: Tuesday 130 + Thursday 65 = broker net 195
# ---------------------------------------------------------------------------

def _pos(qty):
    return [{"tradingsymbol": SYM, "instrument_token": TOK, "exchange": "NSE_FO", "quantity": qty,
             "product": "D", "average_price": 260.0, "last_price": 300.0}]


def _sell(order_id, tag, qty, status="complete", pending=0):
    filled = 0 if status != "complete" else qty
    return {"order_id": order_id, "tag": tag, "transaction_type": "SELL", "order_type": "MARKET",
            "status": status, "quantity": qty, "filled_quantity": filled, "pending_quantity": pending,
            "trading_symbol": SYM, "tradingsymbol": SYM, "instrument_token": TOK}


TRACK = {
    "tue": dict(module=tue_module, fn="manage_tuesday_exit_orders", tag="TUE-SL", qty=130, entry="TUE_BUY_C", lots=2),
    "thu": dict(module=thu_module, fn="manage_thursday_exit_orders", tag="THU-SL", qty=65, entry="THU_BUY_C", lots=1),
}


def _authorize(r, cycle):
    r.set(f"fno:upstox:enabled:{cycle}:{EXPIRY}:23500:CE", json.dumps({"enabled": True, "qty": TRACK[cycle]["qty"]}))
    r.set(f"ORDERS_LIVE_{cycle.upper()}", "1")


def _run(r, cycle, positions, orders, new_order_id=None):
    t = TRACK[cycle]
    m = t["module"]
    entry = {"entry_order_id": t["entry"], "entry_price": 255.0, "entry_status": "FILLED",
             "same_broker_contract_overlap": True, "ambiguous_entry_attribution": False}
    with patch("common.unified_strategy_store.get_active_leg_entry_details", return_value=entry), \
         patch.object(m, "is_market_hours_ist", return_value=(True, "open")), \
         patch.object(m, "fetch_order_book", return_value=orders), \
         patch.object(m, "fetch_ltp", return_value=300.0), \
         patch.object(m, "cancel_order", return_value={"success": True}), \
         patch.object(m, "place_exit_order_slm") as mock_slm, \
         patch.object(m, "place_exit_order_market", return_value={"success": True, "order_id": new_order_id or f"{cycle}_MKT"}) as mock_mkt, \
         patch.object(m, "dispatch_order_failure_discord"):
        getattr(m, t["fn"])(
            token="SIM_TOKEN", positions=positions, orders=orders, execute=True, redis_client=r,
            broker="upstox", active_expiry=EXPIRY, force=False, exit_immediately=True,
            target_lots=t["lots"], tag=t["tag"],
        )
        with patch.object(m, "dispatch_order_failure_discord"):
            m.reconcile_cycle_legs(r, positions, orders, broker="upstox", cycle=cycle, tag=t["tag"], active_expiry=EXPIRY)
    mock_slm.assert_not_called()
    return [c.args[2] for c in mock_mkt.call_args_list]


def test_tuesday_exits_first_then_thursday():
    r = FakeRedis()
    _authorize(r, "tue")
    assert _run(r, "tue", _pos(195), [], new_order_id="TUE_MKT_1") == [130]
    # Next minute: Tuesday's 130 filled, Thursday's 65 remain -> Tuesday sells nothing more and retires
    book = [_sell("TUE_MKT_1", "TUE-SL", 130)]
    assert _run(r, "tue", _pos(65), book) == []
    assert r.get(f"fno:upstox:enabled:tue:{EXPIRY}:23500:CE") is None
    # Day D+1 (order book reset): Tuesday still sells nothing
    assert _run(r, "tue", _pos(65), []) == []
    # Thursday's own exit later sells exactly its 65
    _authorize(r, "thu")
    assert _run(r, "thu", _pos(65), [], new_order_id="THU_MKT_1") == [65]


def test_thursday_exits_first_then_tuesday():
    r = FakeRedis()
    _authorize(r, "thu")
    assert _run(r, "thu", _pos(195), [], new_order_id="THU_MKT_1") == [65]
    book = [_sell("THU_MKT_1", "THU-SL", 65)]
    assert _run(r, "thu", _pos(130), book) == []
    assert r.get(f"fno:upstox:enabled:thu:{EXPIRY}:23500:CE") is None
    _authorize(r, "tue")
    assert _run(r, "tue", _pos(130), book, new_order_id="TUE_MKT_1") == [130]


@pytest.mark.parametrize("tue_order_state,net_qty", [
    ("in_flight", 195),          # Tuesday MARKET still pending, position unchanged
    ("filled_stale_pos", 195),   # Tuesday filled but position snapshot not yet updated
    ("filled_updated_pos", 65),  # Tuesday filled and position updated
])
def test_same_minute_both_exit_never_oversell(tue_order_state, net_qty):
    r = FakeRedis()
    _authorize(r, "tue")
    _authorize(r, "thu")
    assert _run(r, "tue", _pos(195), [], new_order_id="TUE_MKT_1") == [130]
    if tue_order_state == "in_flight":
        book = [_sell("TUE_MKT_1", "TUE-SL", 130, status="open", pending=130)]
    else:
        book = [_sell("TUE_MKT_1", "TUE-SL", 130)]
    # Thursday runs right after (serialized by the broker lock) and must sell only its own 65
    assert _run(r, "thu", _pos(net_qty), book, new_order_id="THU_MKT_1") == [65]


def test_partial_fill_retries_only_own_remainder():
    r = FakeRedis()
    _authorize(r, "tue")
    assert _run(r, "tue", _pos(195), [], new_order_id="TUE_MKT_1") == [130]
    partial = dict(_sell("TUE_MKT_1", "TUE-SL", 130), filled_quantity=100, status="cancelled", pending_quantity=0)
    # 100 of Tuesday's 130 filled; net now 95 (Tuesday 30 + Thursday 65) -> Tuesday retries exactly 30
    assert _run(r, "tue", _pos(95), [partial], new_order_id="TUE_MKT_2") == [30]
