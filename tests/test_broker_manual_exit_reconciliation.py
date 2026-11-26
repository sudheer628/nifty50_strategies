"""
tests/test_broker_manual_exit_reconciliation.py
Tests for automated broker manual exit detection, reconciliation, and DB/Redis state sync.
"""

import json
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from scripts.auto_record_manual_exit import (
    auto_record_manual_exit,
    reconcile_broker_positions_with_snapshot,
    check_prior_cycle_open_positions,
)


class MockRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, *args, **kwargs):
        self.store[key] = str(value)
        return True

    def delete(self, *keys):
        count = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                count += 1
        return count

    def scan_iter(self, match=None, count=None):
        import fnmatch
        pattern = match or "*"
        for k in list(self.store.keys()):
            if fnmatch.fnmatch(k, pattern):
                yield k


@pytest.fixture
def mock_snapshot():
    import config
    import common.unified_strategy_store as uss
    from pathlib import Path
    
    snap_file = Path(config.ACTIVE_SNAPSHOT_FILE)
    active_file = Path(uss.ACTIVE_STRATEGIES_JSON)
    snap_file.parent.mkdir(parents=True, exist_ok=True)
    
    snap_data = {
        "strategy_name": "NIFTY50_5DAY_STRATEGY",
        "cycle_id": "CYCLE-TUE-20261103",
        "week_start_date": "20261103",
        "expiry_date": "2026-11-03",
        "call_strike": 24000,
        "put_strike": 24200,
        "call_buy_price": 100.0,
        "put_buy_price": 120.0,
        "entry_status": "FILLED",
        "status": "ongoing",
        "broker": "upstox",
        "alpha_fsm": {
            "state": "ACTIVE",
            "call_leg": {
                "strike": 24000,
                "buy_price": 100.0,
                "status": "ACTIVE",
                "peak_ltp": 100.0,
                "trailing_stop": 75.0,
                "realized_pnl": 0.0,
            },
            "put_leg": {
                "strike": 24200,
                "buy_price": 120.0,
                "status": "ACTIVE",
                "peak_ltp": 120.0,
                "trailing_stop": 90.0,
                "realized_pnl": 0.0,
            },
        },
    }
    snap_file.write_text(json.dumps(snap_data), encoding="utf-8")
    
    active_data = {
        "5day_strategy": {
            "cycle_id": "CYCLE-TUE-20261103",
            "status": "active",
            "broker": "upstox",
            "exit_signals": {"alpha_fsm": {}},
            "manual_exits": [],
        }
    }
    active_file.write_text(json.dumps(active_data), encoding="utf-8")
    
    return snap_file, active_file


def test_auto_record_manual_exit_single_leg(mock_snapshot):
    snap_file, active_file = mock_snapshot
    r = MockRedis()
    r.set("fno:upstox:enabled:tue:2026-11-03:24000:CE", "1")
    r.set("ORDERS_LIVE_TUE", "1")
    
    res = auto_record_manual_exit(
        track="5day_strategy",
        broker="upstox",
        strike=24000,
        option_type="CE",
        expiry_iso="2026-11-03",
        sell_price=130.0,
        sell_ts=1793682800,
        sell_qty=65,
        redis_client=r,
    )
    
    assert res["success"] is True
    assert res["realized_pnl_pts"] == 30.0
    assert res["realized_pnl_pct"] == 30.0
    
    # Snapshot FSM check
    with open(snap_file, "r") as f:
        snap = json.load(f)
    assert snap["alpha_fsm"]["call_leg"]["status"] == "MANUAL_CLOSED"
    assert snap["alpha_fsm"]["call_leg"]["exit_price"] == 130.0
    assert snap["alpha_fsm"]["put_leg"]["status"] == "ACTIVE"
    # Cycle remains open because put leg is still ACTIVE
    assert snap.get("status") == "ongoing"
    
    # Auth key should be cleaned
    assert r.get("fno:upstox:enabled:tue:2026-11-03:24000:CE") is None


def test_auto_record_manual_exit_dual_leg_closes_cycle_and_gate(mock_snapshot):
    snap_file, active_file = mock_snapshot
    r = MockRedis()
    r.set("fno:upstox:enabled:tue:2026-11-03:24000:CE", "1")
    r.set("fno:upstox:enabled:tue:2026-11-03:24200:PE", "1")
    r.set("ORDERS_LIVE_TUE", "1")
    
    # Exit Call leg
    auto_record_manual_exit(
        track="5day_strategy",
        broker="upstox",
        strike=24000,
        option_type="CE",
        expiry_iso="2026-11-03",
        sell_price=130.0,
        sell_ts=1793682800,
        sell_qty=65,
        redis_client=r,
    )
    
    # Exit Put leg
    res_put = auto_record_manual_exit(
        track="5day_strategy",
        broker="upstox",
        strike=24200,
        option_type="PE",
        expiry_iso="2026-11-03",
        sell_price=110.0,
        sell_ts=1793682900,
        sell_qty=65,
        redis_client=r,
    )
    
    assert res_put["success"] is True
    
    with open(snap_file, "r") as f:
        snap = json.load(f)
    assert snap["status"] == "closed"
    assert snap["alpha_fsm"]["call_leg"]["status"] == "MANUAL_CLOSED"
    assert snap["alpha_fsm"]["put_leg"]["status"] == "MANUAL_CLOSED"
    
    with open(active_file, "r") as f:
        active = json.load(f)
    assert active["5day_strategy"]["status"] == "closed"
    
    # Gate should be reset to 0
    assert r.get("ORDERS_LIVE_TUE") == "0"


def test_reconcile_broker_positions_detects_manual_exit(mock_snapshot):
    snap_file, active_file = mock_snapshot
    r = MockRedis()
    r.set("ORDERS_LIVE_TUE", "0")  # Even when gate is 0!
    
    mock_adapter = MagicMock()
    # Call leg was sold (qty=0 in closed positions with fill price 145.0)
    # Put leg is still open (qty=65)
    mock_adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [
            {
                "tradingsymbol": "NIFTY03NOV2624000CE",
                "quantity": 0,
                "day_sell_quantity": 65,
                "day_sell_price": 145.0,
                "exchange": "NSE_FO",
            },
            {
                "tradingsymbol": "NIFTY03NOV2624200PE",
                "quantity": 65,
                "day_buy_quantity": 65,
                "exchange": "NSE_FO",
            },
        ],
    }
    mock_adapter.fetch_order_book.return_value = {"status": "success", "data": []}
    
    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        rec = reconcile_broker_positions_with_snapshot(track="5day_strategy", redis_client=r)
        
    assert rec["reconciled"] is True
    assert len(rec["exits_recorded"]) == 1
    assert rec["exits_recorded"][0]["strike"] == 24000
    assert rec["exits_recorded"][0]["option_type"] == "CE"
    assert rec["exits_recorded"][0]["sell_price"] == 145.0
    
    with open(snap_file, "r") as f:
        snap = json.load(f)
    assert snap["alpha_fsm"]["call_leg"]["status"] == "MANUAL_CLOSED"
    assert snap["alpha_fsm"]["put_leg"]["status"] == "ACTIVE"


def test_reconcile_broker_positions_detects_bot_exit_via_tag(mock_snapshot):
    snap_file, active_file = mock_snapshot
    r = MockRedis()
    r.set("ORDERS_LIVE_TUE", "0")
    
    mock_adapter = MagicMock()
    mock_adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [
            {
                "tradingsymbol": "NIFTY03NOV2624000CE",
                "quantity": 0,
                "day_sell_quantity": 65,
                "day_sell_price": 145.0,
                "exchange": "NSE_FO",
            },
            {
                "tradingsymbol": "NIFTY03NOV2624200PE",
                "quantity": 65,
                "day_buy_quantity": 65,
                "exchange": "NSE_FO",
            },
        ],
    }
    mock_adapter.fetch_order_book.return_value = {
        "status": "success",
        "data": [
            {
                "tradingsymbol": "NIFTY03NOV2624000CE",
                "transaction_type": "SELL",
                "status": "complete",
                "tag": "TUE-SL",
                "order_id": "261126000004642",
                "average_price": 145.0,
                "quantity": 65,
            }
        ],
    }
    
    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        rec = reconcile_broker_positions_with_snapshot(track="5day_strategy", redis_client=r)
        
    assert rec["reconciled"] is True
    assert len(rec["exits_recorded"]) == 1
    assert rec["exits_recorded"][0]["strike"] == 24000
    assert rec["exits_recorded"][0]["option_type"] == "CE"
    assert rec["exits_recorded"][0]["sell_price"] == 145.0
    
    with open(snap_file, "r") as f:
        snap = json.load(f)
    assert snap["alpha_fsm"]["call_leg"]["status"] == "TRAILING_STOP_CLOSED"
    assert "AUTO_DETECTED bot exit" in snap["alpha_fsm"]["call_leg"]["exit_reason"]
    assert "tag=TUE-SL" in snap["alpha_fsm"]["call_leg"]["exit_reason"]
    assert "order_id=261126000004642" in snap["alpha_fsm"]["call_leg"]["exit_reason"]
    assert snap["alpha_fsm"]["put_leg"]["status"] == "ACTIVE"



def test_check_prior_cycle_open_positions(mock_snapshot):
    snap_file, _ = mock_snapshot
    mock_adapter = MagicMock()
    
    # When long position exists
    mock_adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [
            {
                "tradingsymbol": "NIFTY03NOV2624000CE",
                "quantity": 65,
                "exchange": "NSE_FO",
            }
        ],
    }
    mock_adapter.fetch_order_book.return_value = {"status": "success", "data": []}
    
    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions("5day_strategy")
        assert has_open is True
        assert is_safe is False
        assert "24000CE" in reason
        
    # When 0 open positions exist
    mock_adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [
            {
                "tradingsymbol": "NIFTY03NOV2624000CE",
                "quantity": 0,
                "exchange": "NSE_FO",
            }
        ],
    }
    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions("5day_strategy")
        assert has_open is False
        assert is_safe is True

    # When short position exists (net quantity < 0 is open exposure)
    mock_adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [
            {
                "tradingsymbol": "NIFTY03NOV2624000CE",
                "quantity": -65,
                "exchange": "NSE_FO",
            }
        ],
    }
    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions("5day_strategy")
        assert has_open is True
        assert is_safe is False

    # Fail-closed: When broker position fetch fails
    mock_adapter.fetch_positions.side_effect = RuntimeError("Broker API 500 Down")
    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions("5day_strategy")
        assert has_open is False
        assert is_safe is False
        assert "fail-closed" in reason


def test_auto_record_manual_exit_missing_buy_price_does_not_crash(mock_snapshot):
    """When buy_price is 0/missing and sell_price is available, realized_pnl is None and FSM does not crash."""
    snap_file, active_file = mock_snapshot

    # Modify snapshot to have missing buy price (0.0)
    with open(snap_file, "r") as f:
        s = json.load(f)
    s["alpha_fsm"]["call_leg"]["buy_price"] = 0.0
    with open(snap_file, "w") as f:
        json.dump(s, f)

    res = auto_record_manual_exit(
        track="5day_strategy",
        broker="upstox",
        strike=24000,
        option_type="CE",
        expiry_iso="2026-11-03",
        sell_price=150.0,
        sell_ts=int(datetime.now(tz=timezone.utc).timestamp()),
        sell_qty=None,
        redis_client=MockRedis(),
        dry_run=False,
    )
    assert res["success"] is True
    assert res["realized_pnl_pts"] is None
    assert res["realized_pnl_pct"] is None

    with open(snap_file, "r") as f:
        updated_s = json.load(f)
    call_leg = updated_s["alpha_fsm"]["call_leg"]
    assert call_leg["status"] == "MANUAL_CLOSED"
    assert call_leg["is_estimated"] is True
    assert call_leg["realized_pnl"] is None


def test_contract_matches_requires_exact_resolved_expiry():
    """P1 Verification: contract_matches must reject symbols with missing or unresolvable expiry against target."""
    from common.broker_adapters import contract_match_status, contract_matches

    # 1. Symbol without expiry (NIFTY24000CE) MUST NOT match an exact expiry target
    assert contract_matches("NIFTY24000CE", target_strike=24000, target_opt_type="CE", target_expiry="2026-11-03") is False
    assert contract_match_status(
        "NIFTY24000CE",
        target_strike=24000,
        target_opt_type="CE",
        target_expiry="2026-11-03",
    ) is None

    # 2. Matching expiry matches
    assert contract_matches("NIFTY03NOV2624000CE", target_strike=24000, target_opt_type="CE", target_expiry="2026-11-03") is True

    # 3. Different expiry does not match
    assert contract_matches("NIFTY10NOV2624000CE", target_strike=24000, target_opt_type="CE", target_expiry="2026-11-03") is False

    # 4. Different strike or option type does not match
    assert contract_matches("NIFTY03NOV2624200CE", target_strike=24000, target_opt_type="CE", target_expiry="2026-11-03") is False
    assert contract_matches("NIFTY03NOV2624000PE", target_strike=24000, target_opt_type="CE", target_expiry="2026-11-03") is False
    assert contract_matches(
        {
            "strike": 24000,
            "option_type": "CE",
            "tradingsymbol": "NIFTY03NOV2624000CE",
        },
        target_strike=24000,
        target_opt_type="CE",
        target_expiry="2026-11-03",
    ) is True


def test_partially_filled_pending_order_detected_in_guard(mock_snapshot):
    """P1 Verification: Orders with statuses containing spaces like 'Partially Filled' must be caught."""
    mock_adapter = MagicMock()
    mock_adapter.fetch_positions.return_value = {"status": "success", "data": []}
    mock_adapter.fetch_order_book.return_value = {
        "status": "success",
        "data": [
            {
                "trading_symbol": "NIFTY03NOV2624000CE",
                "status": "Partially Filled",
                "order_id": "ORD_PARTIAL_001",
            }
        ],
    }
    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions("5day_strategy")
        assert has_open is True
        assert is_safe is False
        assert "ORD_PARTIAL_001" in reason


def test_missing_contracts_in_snapshot_fails_closed():
    """P1 Verification: When snapshot has no contracts/strikes, check_prior_cycle_open_positions must fail closed."""
    snap_data = {
        "strategy_name": "NIFTY50_5DAY_STRATEGY",
        "cycle_id": "CYCLE-EMPTY",
        "broker": "upstox",
    }
    mock_adapter = MagicMock()
    mock_adapter.fetch_positions.return_value = {"status": "success", "data": []}
    mock_adapter.fetch_order_book.return_value = {"status": "success", "data": []}

    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions("5day_strategy", active_snapshot=snap_data)
        assert has_open is False
        assert is_safe is False
        assert "fail-closed" in reason


def test_order_book_fetch_failure_aborts_manual_reconciliation(mock_snapshot):
    """P1 Verification: When order book fetch fails, reconciliation must abort and NOT record manual exits."""
    mock_adapter = MagicMock()
    # Broker shows no open position
    mock_adapter.fetch_positions.return_value = {"status": "success", "data": []}
    # But order book fetch fails
    mock_adapter.fetch_order_book.side_effect = RuntimeError("503 Service Unavailable")

    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        rec = reconcile_broker_positions_with_snapshot("5day_strategy")
        assert rec["reconciled"] is False
        assert len(rec["exits_recorded"]) == 0
        assert any("order book" in err.lower() for err in rec["errors"])


def test_unresolved_open_position_fails_closed_in_exposure_guard(mock_snapshot):
    mock_adapter = MagicMock()
    mock_adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [{"tradingsymbol": "NIFTY24000CE", "exchange": "NSE_FO", "quantity": 65}],
    }
    mock_adapter.fetch_order_book.return_value = []

    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions("5day_strategy")

    assert has_open is False
    assert is_safe is False
    assert "identity" in reason.lower()


def test_unresolved_open_position_aborts_manual_exit_reconciliation(mock_snapshot):
    mock_adapter = MagicMock()
    mock_adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [{"tradingsymbol": "NIFTY24000CE", "exchange": "NSE_FO", "quantity": 65}],
    }
    mock_adapter.fetch_order_book.return_value = []

    with patch("common.broker_adapters.get_broker_adapter", return_value=mock_adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        result = reconcile_broker_positions_with_snapshot("5day_strategy")

    assert result["reconciled"] is False
    assert result["exits_recorded"] == []
    assert any("identity" in error.lower() for error in result["errors"])


def test_unresolved_pending_order_fails_closed_in_exposure_guard(mock_snapshot):
    adapter = MagicMock()
    adapter.fetch_positions.return_value = {"status": "success", "data": []}
    adapter.fetch_order_book.return_value = {
        "status": "success",
        "data": [
            {
                "tradingsymbol": "NIFTY24000CE",
                "status": "open",
                "order_id": "ORD_UNKNOWN_EXPIRY",
            }
        ],
    }

    with patch("common.broker_adapters.get_broker_adapter", return_value=adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions("5day_strategy")

    assert has_open is False
    assert is_safe is False
    assert "identity" in reason.lower()


def test_incomplete_snapshot_aborts_manual_exit_reconciliation(mock_snapshot):
    snap_file, _ = mock_snapshot
    with open(snap_file, "r", encoding="utf-8") as f:
        snapshot = json.load(f)
    snapshot.pop("expiry_date")
    with open(snap_file, "w", encoding="utf-8") as f:
        json.dump(snapshot, f)

    adapter = MagicMock()
    with patch("common.broker_adapters.get_broker_adapter", return_value=adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        result = reconcile_broker_positions_with_snapshot("5day_strategy")

    assert result["reconciled"] is False
    assert result["exits_recorded"] == []
    assert any("incomplete contract identity" in error.lower() for error in result["errors"])
    adapter.fetch_positions.assert_not_called()


def test_snapshot_with_unknown_expiry_fails_closed(mock_snapshot):
    snapshot = {
        "cycle_id": "CYCLE-TUE-20261103",
        "call_strike": 24000,
        "put_strike": 24200,
        "broker": "upstox",
    }
    adapter = MagicMock()
    adapter.fetch_positions.return_value = {"status": "success", "data": []}
    adapter.fetch_order_book.return_value = []

    with patch("common.broker_adapters.get_broker_adapter", return_value=adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions(
            "5day_strategy",
            active_snapshot=snapshot,
        )

    assert has_open is False
    assert is_safe is False
    assert "incomplete" in reason.lower()


def test_snapshot_missing_one_leg_fails_closed_with_open_put(mock_snapshot):
    snapshot = {
        "cycle_id": "CYCLE-TUE-20261103",
        "expiry_date": "2026-11-03",
        "call_strike": 24000,
        "broker": "upstox",
    }
    adapter = MagicMock()
    adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [{"tradingsymbol": "NIFTY03NOV2624200PE", "exchange": "NSE_FO", "quantity": 65}],
    }
    adapter.fetch_order_book.return_value = []

    with patch("common.broker_adapters.get_broker_adapter", return_value=adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions(
            "5day_strategy",
            active_snapshot=snapshot,
        )

    assert has_open is False
    assert is_safe is False
    assert "incomplete" in reason.lower()


def test_cross_track_overlap_fails_closed_without_tuesday_snapshot(mock_snapshot):
    thursday_snapshot = {
        "cycle_id": "CYCLE-THU-20261105",
        "expiry_date": "2026-11-10",
        "call_strike": 25000,
        "put_strike": 25200,
        "broker": "upstox",
    }
    adapter = MagicMock()
    adapter.fetch_positions.return_value = {"status": "success", "data": []}
    adapter.fetch_order_book.return_value = []

    with patch("scripts.auto_record_manual_exit.load_active_snapshot", return_value=None), \
         patch("common.broker_adapters.get_broker_adapter", return_value=adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions(
            "3day_strategy",
            active_snapshot=thursday_snapshot,
            check_cross_track_overlap=True,
        )

    assert has_open is False
    assert is_safe is False
    assert "tuesday overlap snapshot" in reason.lower()


def test_cross_track_overlap_checks_tuesday_contracts_even_when_snapshot_says_closed(mock_snapshot):
    thursday_snapshot = {
        "cycle_id": "CYCLE-THU-20261105",
        "expiry_date": "2026-11-10",
        "call_strike": 25000,
        "put_strike": 25200,
        "broker": "upstox",
    }
    tuesday_snapshot = {
        "cycle_id": "CYCLE-TUE-20261103",
        "expiry_date": "2026-11-03",
        "call_strike": 24000,
        "put_strike": 24200,
        "status": "closed",
    }
    adapter = MagicMock()
    adapter.fetch_positions.return_value = {
        "status": "success",
        "data": [{"tradingsymbol": "NIFTY03NOV2624000CE", "exchange": "NSE_FO", "quantity": 65}],
    }
    adapter.fetch_order_book.return_value = []

    with patch("scripts.auto_record_manual_exit.load_active_snapshot", return_value=tuesday_snapshot), \
         patch("common.broker_adapters.get_broker_adapter", return_value=adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="MOCK_TOKEN"):
        has_open, is_safe, reason = check_prior_cycle_open_positions(
            "3day_strategy",
            active_snapshot=thursday_snapshot,
            check_cross_track_overlap=True,
        )

    assert has_open is True
    assert is_safe is False
    assert "tuesday_cycle_call" in reason


def test_auto_record_manual_exit_ignores_profit_target_closed_legs(mock_snapshot):
    snap_file, active_file = mock_snapshot
    with open(snap_file, "r") as f:
        snap = json.load(f)
    snap["alpha_fsm"]["call_leg"]["status"] = "PROFIT_TARGET_CLOSED"
    snap["alpha_fsm"]["call_leg"]["exit_price"] = 180.0
    with open(snap_file, "w") as f:
        json.dump(snap, f)

    res = auto_record_manual_exit(
        track="5day_strategy",
        broker="upstox",
        strike=24000,
        option_type="CE",
        expiry_iso="2026-11-03",
        sell_price=180.0,
        sell_ts=1793682800,
        sell_qty=65,
        redis_client=MockRedis(),
    )
    assert res["success"] is True
    assert "already closed with status PROFIT_TARGET_CLOSED" in res["info"]
    with open(snap_file, "r") as f:
        updated_snap = json.load(f)
    assert updated_snap["alpha_fsm"]["call_leg"]["status"] == "PROFIT_TARGET_CLOSED"


def test_reconcile_broker_positions_rejects_cross_track_tag(mock_snapshot, tmp_path):
    """Verify Tuesday reconciliation skips and alerts on a shared contract when only Thursday's tag (THU-SL) is present."""
    snap_file, active_file = mock_snapshot
    from scripts.auto_record_manual_exit import reconcile_broker_positions_with_snapshot
    # PE is still open at broker, CE is flat
    broker_positions = [{
        "tradingsymbol": "NIFTY03NOV2624200PE",
        "instrument_token": "TOK_PE",
        "exchange": "NSE_FO",
        "quantity": 65,
        "average_price": 120.0,
    }]
    # Thursday bot exit order on shared broker contract
    orders = [{
        "order_id": "ORD_THU_SL_999",
        "tag": "THU-SL",
        "transaction_type": "SELL",
        "status": "complete",
        "trading_symbol": "NIFTY03NOV2624000CE",
        "instrument_token": "TOK_CE",
        "average_price": 210.0,
        "quantity": 65,
    }]
    adapter = MagicMock()
    adapter.fetch_order_book.return_value = orders
    adapter.fetch_positions.return_value = {"status": "success", "data": broker_positions}

    mock_redis = MockRedis()
    # Mock Thursday snapshot to simulate shared contract (same broker, expiry, and strike)
    thu_snap_file = str(tmp_path / "thursday_snap.json")
    with open(thu_snap_file, "w") as f:
        json.dump({
            "status": "active",
            "broker": "upstox",
            "expiry_date": "2026-11-03",
            "call_strike": 24000,
            "put_strike": 23500,
            "alpha_fsm": {"call_leg": {"status": "ACTIVE"}, "put_leg": {"status": "ACTIVE"}}
        }, f)

    with patch("scripts.auto_record_manual_exit.ACTIVE_SNAPSHOT_FILE", snap_file), \
         patch("scripts.auto_record_manual_exit.THURSDAY_SNAPSHOT_FILE", thu_snap_file), \
         patch("common.broker_adapters.get_broker_adapter", return_value=adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="SIM_TOKEN"):
        res = reconcile_broker_positions_with_snapshot("5day_strategy", broker="upstox", redis_client=mock_redis)

    assert res["reconciled"] is True
    # Must NOT record MANUAL_CLOSED for Tuesday because this is a shared contract with only Thursday's tag!
    assert len(res["exits_recorded"]) == 0
    assert any("Shared contract" in err for err in res["errors"])

    with open(snap_file, "r") as f:
        updated_snap = json.load(f)
    call_leg = updated_snap["alpha_fsm"]["call_leg"]
    # Tuesday leg must remain ACTIVE (not overwritten to MANUAL_CLOSED)
    assert call_leg.get("status") == "ACTIVE"


def test_reconcile_broker_positions_different_contract_not_treated_as_shared(mock_snapshot, tmp_path):
    """Verify that if Thursday is active with a DIFFERENT strike, Tuesday is NOT treated as shared and records manual exit."""
    snap_file, active_file = mock_snapshot
    from scripts.auto_record_manual_exit import reconcile_broker_positions_with_snapshot
    # PE is still open at broker, CE is flat
    broker_positions = [{
        "tradingsymbol": "NIFTY03NOV2624200PE",
        "instrument_token": "TOK_PE",
        "exchange": "NSE_FO",
        "quantity": 65,
        "average_price": 120.0,
    }]
    # Untagged manual sell order for Tuesday's 24000 CE
    orders = [{
        "order_id": "ORD_MANUAL_123",
        "tag": "",
        "transaction_type": "SELL",
        "status": "complete",
        "trading_symbol": "NIFTY03NOV2624000CE",
        "instrument_token": "TOK_CE",
        "average_price": 205.0,
        "quantity": 65,
    }]
    adapter = MagicMock()
    adapter.fetch_order_book.return_value = orders
    adapter.fetch_positions.return_value = {"status": "success", "data": broker_positions}

    mock_redis = MockRedis()
    # Thursday snapshot has a DIFFERENT strike (24500 CE instead of 24000 CE)
    thu_snap_file = str(tmp_path / "thursday_snap.json")
    with open(thu_snap_file, "w") as f:
        json.dump({
            "status": "active",
            "broker": "upstox",
            "expiry_date": "2026-11-03",
            "call_strike": 24500,
            "put_strike": 23500,
            "alpha_fsm": {"call_leg": {"status": "ACTIVE"}, "put_leg": {"status": "ACTIVE"}}
        }, f)

    with patch("scripts.auto_record_manual_exit.ACTIVE_SNAPSHOT_FILE", snap_file), \
         patch("scripts.auto_record_manual_exit.THURSDAY_SNAPSHOT_FILE", thu_snap_file), \
         patch("common.broker_adapters.get_broker_adapter", return_value=adapter), \
         patch("common.entry_order_manager.get_broker_token", return_value="SIM_TOKEN"):
        res = reconcile_broker_positions_with_snapshot("5day_strategy", broker="upstox", redis_client=mock_redis)

    assert res["reconciled"] is True
    assert len(res["exits_recorded"]) == 1
    call_rec = res["exits_recorded"][0]
    assert call_rec["success"] is True
    assert call_rec["sell_price"] == 205.0

    with open(snap_file, "r") as f:
        updated_snap = json.load(f)
    assert updated_snap["alpha_fsm"]["call_leg"]["status"] == "MANUAL_CLOSED"

