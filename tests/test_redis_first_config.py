"""
Redis-first runtime configuration: Redis key -> .env -> hardcoded default.

Keys: LIVE_EXIT_TRIGGER_*, EXIT_IMMEDIATELY_*, LIVE_BUY_TRIGGER_*, LOTS_*, NIFTY_LOT_SIZE, STRATEGY_BUY_QTY.
"""
import json
import logging
import os
import sys
from unittest.mock import patch

import pytest

import config
from tests.test_shared_contract_isolation import FakeRedis, thu_module, tue_module


class FlakyRedis:
    """Redis client whose every call fails (network outage)."""

    def __init__(self):
        self.calls = 0

    def get(self, key):
        self.calls += 1
        raise ConnectionError("Redis Cloud unreachable")


@pytest.fixture
def redis_cfg():
    """Install a FakeRedis as the process-wide config client; restore afterwards."""
    saved = dict(config._config_redis_state)
    saved["failed_clients"] = set(config._config_redis_state["failed_clients"])
    r = FakeRedis()
    config._config_redis_state.update({"client": r, "unavailable": False, "failed_clients": set()})
    yield r
    config._config_redis_state.clear()
    config._config_redis_state.update(saved)


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------

def test_redis_value_overrides_env(redis_cfg):
    redis_cfg.set("EXIT_IMMEDIATELY_THURSDAY", "true")
    redis_cfg.set("LOTS_THURSDAY", "2")
    with patch.dict(os.environ, {"EXIT_IMMEDIATELY_THURSDAY": "false", "LOTS_THURSDAY": "1"}):
        assert config.get_config_bool("EXIT_IMMEDIATELY_THURSDAY") is True
        assert config.get_config_int("LOTS_THURSDAY", 1) == 2
        assert config.get_config_flag("LOTS_THURSDAY") == "2"


def test_missing_or_empty_redis_key_falls_back_to_env(redis_cfg):
    redis_cfg.set("LIVE_BUY_TRIGGER_THURSDAY", "   ")
    with patch.dict(os.environ, {"LIVE_BUY_TRIGGER_THURSDAY": "true", "LOTS_THURSDAY": "1"}):
        assert config.get_config_bool("LIVE_BUY_TRIGGER_THURSDAY") is True
        assert config.get_config_int("LOTS_THURSDAY", 5) == 1


def test_default_when_neither_redis_nor_env(redis_cfg):
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LIVE_EXIT_TRIGGER_THURSDAY", None)
        assert config.get_config_bool("LIVE_EXIT_TRIGGER_THURSDAY", default=False) is False
        assert config.get_config_flag("LIVE_EXIT_TRIGGER_THURSDAY", default="x") == "x"


def test_invalid_redis_value_is_ignored_and_env_used(redis_cfg, caplog):
    redis_cfg.set("LIVE_EXIT_TRIGGER_TUESDAY", "ture")
    redis_cfg.set("LOTS_TUESDAY", "two")
    redis_cfg.set("LOTS_THURSDAY", "0")
    with patch.dict(os.environ, {"LIVE_EXIT_TRIGGER_TUESDAY": "false", "LOTS_TUESDAY": "1", "LOTS_THURSDAY": "1"}), \
         caplog.at_level(logging.WARNING, logger="nifty50_strategies"):
        assert config.get_config_bool("LIVE_EXIT_TRIGGER_TUESDAY", default=True) is False
        assert config.get_config_int("LOTS_TUESDAY", 9) == 1
        assert config.get_config_int("LOTS_THURSDAY", 9) == 1
    assert "invalid boolean LIVE_EXIT_TRIGGER_TUESDAY" in caplog.text
    assert "invalid integer LOTS_TUESDAY" in caplog.text


def test_invalid_everywhere_uses_default(redis_cfg):
    redis_cfg.set("EXIT_IMMEDIATELY_TUESDAY", "maybe")
    with patch.dict(os.environ, {"EXIT_IMMEDIATELY_TUESDAY": "perhaps"}):
        assert config.get_config_bool("EXIT_IMMEDIATELY_TUESDAY", default=False) is False


def test_redis_outage_falls_back_to_env_and_does_not_retry(redis_cfg):
    flaky = FlakyRedis()
    with patch.dict(os.environ, {"LIVE_EXIT_TRIGGER_THURSDAY": "true", "LOTS_THURSDAY": "1"}):
        assert config.get_config_bool("LIVE_EXIT_TRIGGER_THURSDAY", redis_client=flaky) is True
        assert config.get_config_int("LOTS_THURSDAY", 3, redis_client=flaky) == 1
    assert flaky.calls == 1, "a failed client must not be retried (avoids repeated socket timeouts)"


def test_lazy_client_outage_marks_unavailable(redis_cfg):
    flaky = FlakyRedis()
    config._config_redis_state["client"] = flaky
    with patch.dict(os.environ, {"LOTS_TUESDAY": "1"}):
        assert config.get_config_int("LOTS_TUESDAY", 2) == 1
        assert config.get_config_int("LOTS_TUESDAY", 2) == 1
    assert flaky.calls == 1
    assert config._config_redis_state["unavailable"] is True


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def test_entry_qty_uses_redis_lots(redis_cfg):
    redis_cfg.set("LOTS_THURSDAY", "2")
    with patch.dict(os.environ, {"LOTS_THURSDAY": "1", "NIFTY_LOT_SIZE": "65"}):
        assert config.get_entry_qty_for_track("3day_strategy") == 130
        redis_cfg.delete("LOTS_THURSDAY")  # DEL key -> back to .env
        assert config.get_entry_qty_for_track("3day_strategy") == 65


def test_entry_qty_strategy_buy_qty_fallback_from_redis(redis_cfg):
    redis_cfg.set("STRATEGY_BUY_QTY", "130")
    with patch.dict(os.environ, {"NIFTY_LOT_SIZE": "65"}):
        os.environ.pop("LOTS_TUESDAY", None)
        assert config.get_entry_qty_for_track("5day_strategy") == 130


def test_publisher_legacy_cycle_uses_redis_lots(redis_cfg, tmp_path):
    """Cycles without a recorded entry_qty fall back to LOTS_* (Redis first) for the exit authorization."""
    from common.unified_strategy_store import publish_exit_authorization_to_redis
    snap = tmp_path / "current_week_buy.json"
    snap.write_text("{}")
    redis_cfg.set("LOTS_TUESDAY", "2")
    strategy = {"cycle_id": "CYCLE-TUE-20261110", "call_strike": 23500, "put_strike": 23700,
                "expiry_date": "2026-11-16", "broker": "upstox", "entry_status": "FILLED"}
    with patch("common.unified_strategy_store.ACTIVE_SNAPSHOT_FILE", str(snap)), \
         patch.dict(os.environ, {"LOTS_TUESDAY": "1"}):
        publish_exit_authorization_to_redis("5day_strategy", True, False, strategy, redis_client=redis_cfg)
    assert json.loads(redis_cfg.get("fno:upstox:enabled:tue:2026-11-16:23500:CE"))["qty"] == 130


def test_publisher_recorded_entry_qty_wins_over_redis_lots(redis_cfg, tmp_path):
    """An open cycle keeps exiting what it bought, even if LOTS_* is changed in Redis mid-cycle."""
    from common.unified_strategy_store import publish_exit_authorization_to_redis
    snap = tmp_path / "current_week_buy.json"
    snap.write_text("{}")
    redis_cfg.set("LOTS_TUESDAY", "3")
    strategy = {"cycle_id": "CYCLE-TUE-20261110", "call_strike": 23500, "put_strike": 23700,
                "expiry_date": "2026-11-16", "broker": "upstox", "entry_status": "FILLED", "entry_qty": 65}
    with patch("common.unified_strategy_store.ACTIVE_SNAPSHOT_FILE", str(snap)):
        publish_exit_authorization_to_redis("5day_strategy", True, False, strategy, redis_client=redis_cfg)
    assert json.loads(redis_cfg.get("fno:upstox:enabled:tue:2026-11-16:23500:CE"))["qty"] == 65


# ---------------------------------------------------------------------------
# Exit managers: live gate read from Redis before .env
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module,up,gate", [(tue_module, "TUESDAY", "ORDERS_LIVE_TUE"), (thu_module, "THURSDAY", "ORDERS_LIVE_THU")])
@pytest.mark.parametrize("redis_val,env_val,expect_dry_run_warning", [
    ("false", "true", True),   # Redis disables live exits even though .env enables them
    ("true", "false", False),  # Redis enables live exits even though .env disables them
    (None, "false", True),     # no Redis key -> .env
])
def test_exit_manager_live_gate_redis_first(module, up, gate, redis_val, env_val, expect_dry_run_warning, caplog):
    r = FakeRedis({gate: "0"})
    if redis_val is not None:
        r.set(f"LIVE_EXIT_TRIGGER_{up}", redis_val)
    argv = [f"{up.title()}-Fno-Exit-Orders-V1.py", "--execute"]
    with patch.object(module, "get_redis_client", return_value=r), \
         patch.object(sys, "argv", argv), \
         patch.dict(os.environ, {f"LIVE_EXIT_TRIGGER_{up}": env_val}), \
         caplog.at_level(logging.INFO):
        rc = module.main()
    assert rc == 0  # idle: gate inactive
    assert (f"LIVE_EXIT_TRIGGER_{up} is disabled/false" in caplog.text) is expect_dry_run_warning


@pytest.mark.parametrize("module,up", [(tue_module, "TUESDAY"), (thu_module, "THURSDAY")])
def test_exit_manager_lots_and_exit_mode_from_redis(module, up):
    """LOTS_* and EXIT_IMMEDIATELY_* are resolved from Redis and passed to the order manager."""
    gate = "ORDERS_LIVE_TUE" if up == "TUESDAY" else "ORDERS_LIVE_THU"
    r = FakeRedis({gate: "1", f"LOTS_{up}": "3", f"EXIT_IMMEDIATELY_{up}": "true", f"LIVE_EXIT_TRIGGER_{up}": "false"})
    captured = {}

    def fake_manage(**kwargs):
        captured.update(kwargs)
        return {"actions": [], "alerts": [], "orphan_managed_orders": []}

    manage_name = "manage_tuesday_exit_orders" if up == "TUESDAY" else "manage_thursday_exit_orders"
    with patch.object(module, "get_redis_client", return_value=r), \
         patch.object(module, manage_name, side_effect=fake_manage), \
         patch.object(module, "is_market_hours_ist", return_value=(True, "open")), \
         patch.object(module, "check_nse_holiday", return_value=False, create=True), \
         patch.object(module, "get_access_token", return_value="SIM_TOKEN"), \
         patch.object(module, "fetch_positions", return_value={"status": "success", "data": []}), \
         patch.object(module, "fetch_order_book", return_value=[]), \
         patch.object(module, "reconcile_cycle_legs", return_value=[]), \
         patch.object(sys, "argv", [f"{up.title()}-Fno-Exit-Orders-V1.py", "--execute"]), \
         patch.dict(os.environ, {f"LOTS_{up}": "1", f"EXIT_IMMEDIATELY_{up}": "false", f"LIVE_EXIT_TRIGGER_{up}": "true"}):
        module.main()
    assert captured, "order manager was not invoked"
    assert captured["target_lots"] == 3
    assert captured["exit_immediately"] is True
    assert captured["execute"] is False  # LIVE_EXIT_TRIGGER from Redis=false forces dry-run


# ---------------------------------------------------------------------------
# Collector: LIVE_BUY_TRIGGER from Redis
# ---------------------------------------------------------------------------

def test_tuesday_collector_live_buy_disabled_by_redis(redis_cfg):
    """Redis LIVE_BUY_TRIGGER_TUESDAY=false overrides .env=true -> paper entry, no broker BUY."""
    from tests.test_track_quantity_isolation import _tuesday_collector_live_buy
    redis_cfg.set("LIVE_BUY_TRIGGER_TUESDAY", "false")
    mock_place, snap, unified = _tuesday_collector_live_buy({"LOTS_TUESDAY": "1"})
    mock_place.assert_not_called()
    assert snap["entry_status"] == "PAPER_ENTRY"


def test_tuesday_collector_lots_from_redis(redis_cfg):
    """Redis LOTS_TUESDAY=2 overrides .env=1 for a new cycle's paired BUY."""
    from tests.test_track_quantity_isolation import _tuesday_collector_live_buy
    redis_cfg.set("LOTS_TUESDAY", "2")
    mock_place, snap, unified = _tuesday_collector_live_buy({"LOTS_TUESDAY": "1"}, fill_qty=130)
    assert [c.kwargs["quantity"] for c in mock_place.call_args_list] == [130, 130]
    assert snap["entry_qty"] == 130
