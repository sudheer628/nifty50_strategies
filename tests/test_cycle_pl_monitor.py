import importlib.util
import os
import re
import sqlite3

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "cycle_pl_monitor.py")


def _load():
    spec = importlib.util.spec_from_file_location("cycle_pl_monitor", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load()


# ---------------------------------------------------------------------------
# value_leg: pure P&L math
# ---------------------------------------------------------------------------

def test_open_leg_pl_from_buy_to_ltp():
    v = m.value_leg(buy_qty=65, buy_price=239.5, open_qty=65, ltp=170.2, sells_today=[], prior_sold_price=None)
    assert v["realised"] == 0.0
    assert v["unrealised"] == pytest.approx((170.2 - 239.5) * 65, abs=0.01)
    assert v["leg_pl"] == v["unrealised"]


def test_leg_sold_today_uses_broker_sell_price():
    v = m.value_leg(65, 234.9, 0, None, [(65, 304.3, "261126000112825")], None)
    assert v["realised"] == pytest.approx((304.3 - 234.9) * 65, abs=0.01)
    assert v["unrealised"] == 0.0
    assert v["sold_today_qty"] == 65
    assert v["sold_today_avg"] == pytest.approx(304.3)


def test_partial_sell_today_plus_open_remainder():
    v = m.value_leg(65, 200.0, 40, 210.0, [(25, 230.0, "x")], None)
    assert v["realised"] == pytest.approx((230.0 - 200.0) * 25, abs=0.01)
    assert v["unrealised"] == pytest.approx((210.0 - 200.0) * 40, abs=0.01)


def test_sold_on_earlier_day_uses_db_price():
    v = m.value_leg(65, 200.0, 0, None, [], prior_sold_price=190.0)
    assert v["prior_sold_qty"] == 65
    assert v["realised"] == pytest.approx((190.0 - 200.0) * 65, abs=0.01)


def test_sold_on_earlier_day_without_db_price_is_insufficient():
    with pytest.raises(m.InsufficientData, match="sell price not known"):
        m.value_leg(65, 200.0, 0, None, [], prior_sold_price=None)


def test_open_leg_without_ltp_is_insufficient():
    with pytest.raises(m.InsufficientData, match="live LTP"):
        m.value_leg(65, 200.0, 65, None, [], None)


def test_quantity_mismatch_is_insufficient():
    with pytest.raises(m.InsufficientData, match="Quantity mismatch"):
        m.value_leg(65, 200.0, 70, 210.0, [], None)


def test_unknown_buy_quantity_is_insufficient():
    with pytest.raises(m.InsufficientData, match="BUY quantity"):
        m.value_leg(0, 200.0, 0, None, [], None)


# ---------------------------------------------------------------------------
# Local DB fallback (read-only)
# ---------------------------------------------------------------------------

def _make_hourly_db(path, cycle_id, call_exit=None, put_exit=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE strategy_hourly_data (id INTEGER PRIMARY KEY, cycle_id TEXT, "
        "fsm_call_exit_price REAL, fsm_put_exit_price REAL)"
    )
    conn.execute(
        "INSERT INTO strategy_hourly_data (cycle_id, fsm_call_exit_price, fsm_put_exit_price) VALUES (?,?,?)",
        (cycle_id, call_exit, put_exit),
    )
    conn.commit()
    conn.close()


def test_load_prior_sold_price_reads_latest_non_null(tmp_path):
    db = tmp_path / "t.db"
    _make_hourly_db(str(db), "CYCLE-THU-X", call_exit=304.3, put_exit=None)
    assert m.load_prior_sold_price(str(db), "CYCLE-THU-X", "CALL") == pytest.approx(304.3)
    assert m.load_prior_sold_price(str(db), "CYCLE-THU-X", "PUT") is None
    assert m.load_prior_sold_price(str(db), "CYCLE-OTHER", "CALL") is None


def test_db_is_opened_read_only(tmp_path):
    db = tmp_path / "t.db"
    _make_hourly_db(str(db), "C")
    conn = m._ro_connect(str(db))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO strategy_hourly_data (cycle_id) VALUES ('x')")
    conn.close()


# ---------------------------------------------------------------------------
# End-to-end with a fake broker and temp DB (no network)
# ---------------------------------------------------------------------------

class FakeBroker:
    def __init__(self, positions, orders, ltps):
        self.positions = positions
        self.orders = orders
        self._ltps = ltps

    def order_by_id(self, order_id):
        for o in self.orders:
            if o["order_id"] == order_id:
                return o
        return None

    def position_for(self, instrument_token="", strike=0, opt_type="", expiry=""):
        for p in self.positions:
            if instrument_token and p["instrument_token"] == instrument_token:
                return p
        return None

    def sells_today(self, instrument_token, symbol):
        out = []
        for o in self.orders:
            if o["transaction_type"] == "SELL" and o["instrument_token"] == instrument_token and o["filled_quantity"] > 0:
                out.append((int(o["filled_quantity"]), o["average_price"], o["order_id"]))
        return out

    def ltp(self, instrument_token, symbol):
        return self._ltps.get(instrument_token)


def _thursday_db(dirpath):
    path = os.path.join(dirpath, "nifty50_thursday_data_20261126_20261201.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE strategy_buy_snapshots (strategy_name TEXT, cycle_id TEXT, week_start_date TEXT, "
        "expiry_date TEXT, call_strike INTEGER, put_strike INTEGER, call_buy_price REAL, put_buy_price REAL, "
        "captured_at INTEGER, call_order_id TEXT, put_order_id TEXT, entry_status TEXT, broker TEXT)"
    )
    conn.execute(
        "INSERT INTO strategy_buy_snapshots VALUES ('nifty50_thursday_3day_collector','CYCLE-THU-20261126',"
        "'20261126','20261201',25800,25900,234.9,239.5,1795665725,'B_CE','B_PE','FILLED','upstox')"
    )
    conn.execute(
        "CREATE TABLE strategy_hourly_data (id INTEGER PRIMARY KEY, cycle_id TEXT, "
        "fsm_call_exit_price REAL, fsm_put_exit_price REAL)"
    )
    conn.commit()
    conn.close()
    return path


def test_end_to_end_thursday_ce_sold_pe_open(tmp_path, monkeypatch):
    _thursday_db(str(tmp_path))
    monkeypatch.setattr(m, "SQLITE_DIR", str(tmp_path))
    fake = FakeBroker(
        positions=[
            {"instrument_token": "NSE_FO|47839", "trading_symbol": "NIFTY26D0125800CE", "quantity": 0,
             "overnight_buy_quantity": 0, "day_buy_quantity": 65},
            {"instrument_token": "NSE_FO|47892", "trading_symbol": "NIFTY26D0125900PE", "quantity": 65,
             "overnight_buy_quantity": 0, "day_buy_quantity": 65},
        ],
        orders=[
            {"order_id": "B_CE", "instrument_token": "NSE_FO|47839", "trading_symbol": "NIFTY26D0125800CE",
             "transaction_type": "BUY", "status": "complete", "filled_quantity": 65, "average_price": 234.9,
             "tag": "THU-BUY"},
            {"order_id": "B_PE", "instrument_token": "NSE_FO|47892", "trading_symbol": "NIFTY26D0125900PE",
             "transaction_type": "BUY", "status": "complete", "filled_quantity": 65, "average_price": 239.5,
             "tag": "THU-BUY"},
            {"order_id": "S_CE", "instrument_token": "NSE_FO|47839", "trading_symbol": "NIFTY26D0125800CE",
             "transaction_type": "SELL", "status": "complete", "filled_quantity": 65, "average_price": 304.3,
             "tag": ""},
        ],
        ltps={"NSE_FO|47892": 170.2},
    )
    monkeypatch.setattr(m, "BrokerView", lambda broker: fake)
    res = m.evaluate_cycle("thursday")
    call, put = res["legs"]["CALL"], res["legs"]["PUT"]
    assert call["status"] == "SOLD" and call["realised"] == pytest.approx((304.3 - 234.9) * 65, abs=0.01)
    assert put["status"] == "OPEN" and put["unrealised"] == pytest.approx((170.2 - 239.5) * 65, abs=0.01)
    assert res["total_pl"] == pytest.approx(call["leg_pl"] + put["leg_pl"], abs=0.01)
    assert res["total_cost"] == pytest.approx(234.9 * 65 + 239.5 * 65, abs=0.01)
    assert res["warnings"] == []


def test_end_to_end_missing_prior_sell_price_raises(tmp_path, monkeypatch):
    _thursday_db(str(tmp_path))
    monkeypatch.setattr(m, "SQLITE_DIR", str(tmp_path))
    # CE has no open position and no sell today -> sold on an earlier day, DB has no price
    fake = FakeBroker(
        positions=[],
        orders=[
            {"order_id": "B_CE", "instrument_token": "NSE_FO|47839", "trading_symbol": "NIFTY26D0125800CE",
             "transaction_type": "BUY", "status": "complete", "filled_quantity": 65, "average_price": 234.9,
             "tag": "THU-BUY"},
            {"order_id": "B_PE", "instrument_token": "NSE_FO|47892", "trading_symbol": "NIFTY26D0125900PE",
             "transaction_type": "BUY", "status": "complete", "filled_quantity": 65, "average_price": 239.5,
             "tag": "THU-BUY"},
        ],
        ltps={},
    )
    monkeypatch.setattr(m, "BrokerView", lambda broker: fake)
    with pytest.raises(m.InsufficientData, match="sell price not known"):
        m.evaluate_cycle("thursday")


# ---------------------------------------------------------------------------
# Safety: the script must never reference order-placing or DB-writing calls
# ---------------------------------------------------------------------------

FORBIDDEN = [
    "place_exit_order", "place_order", "cancel_order", "modify_exit_order", "place_entry",
    "set_config_value", "INSERT", "UPDATE ", "DELETE", "r.set(", "sadd(", "hset(",
]


def test_script_contains_no_order_or_write_calls():
    src = open(SCRIPT, encoding="utf-8").read()
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    for token in FORBIDDEN:
        assert token not in code, f"forbidden token {token!r} found in cycle_pl_monitor.py"


def test_script_uses_lf_line_endings():
    assert b"\r\n" not in open(SCRIPT, "rb").read()


# ---------------------------------------------------------------------------
# Quantity fallback for earlier-day BUYs (snapshot entry_qty, same cycle only)
# ---------------------------------------------------------------------------

def test_snapshot_entry_qty_only_for_same_cycle(tmp_path, monkeypatch):
    import json as _json

    monkeypatch.setattr(m, "SQLITE_DIR", str(tmp_path))
    (tmp_path / "current_week_buy.json").write_text(_json.dumps({"cycle_id": "CYCLE-TUE-20261125", "entry_qty": 65}))
    assert m._snapshot_entry_qty("tuesday", "CYCLE-TUE-20261125") == 65
    assert m._snapshot_entry_qty("tuesday", "CYCLE-TUE-20261118") is None
    assert m._snapshot_entry_qty("thursday", "CYCLE-TUE-20261125") is None  # thursday file missing


def test_end_to_end_earlier_day_leg_uses_snapshot_qty_and_db_price(tmp_path, monkeypatch):
    import json as _json

    path = os.path.join(str(tmp_path), "nifty50_weekly_data_20261125_20261201.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE strategy_buy_snapshots (strategy_name TEXT, cycle_id TEXT, week_start_date TEXT, "
        "expiry_date TEXT, call_strike INTEGER, put_strike INTEGER, call_buy_price REAL, put_buy_price REAL, "
        "captured_at INTEGER, call_order_id TEXT, put_order_id TEXT, entry_status TEXT, broker TEXT)"
    )
    conn.execute(
        "INSERT INTO strategy_buy_snapshots VALUES ('nifty50_weekly_option_collector','CYCLE-TUE-20261125',"
        "'20261125','20261201',25700,25800,287.1,236.75,1795579266,'W_CE','W_PE','FILLED','upstox')"
    )
    conn.execute(
        "CREATE TABLE strategy_hourly_data (id INTEGER PRIMARY KEY, cycle_id TEXT, "
        "fsm_call_exit_price REAL, fsm_put_exit_price REAL)"
    )
    conn.execute(
        "INSERT INTO strategy_hourly_data (cycle_id, fsm_call_exit_price, fsm_put_exit_price) "
        "VALUES ('CYCLE-TUE-20261125', 334.95, 194.10)"
    )
    conn.commit()
    conn.close()
    (tmp_path / "current_week_buy.json").write_text(_json.dumps({"cycle_id": "CYCLE-TUE-20261125", "entry_qty": 65}))
    monkeypatch.setattr(m, "SQLITE_DIR", str(tmp_path))

    # Nothing visible at the broker for the CE: sold yesterday, BUY not in today's book.
    fake = FakeBroker(positions=[], orders=[], ltps={})
    monkeypatch.setattr(m, "BrokerView", lambda broker: fake)
    res = m.evaluate_cycle("tuesday")
    call = res["legs"]["CALL"]
    assert call["status"] == "SOLD"
    assert call["qty_source"] == "local snapshot entry_qty (fallback)"
    assert call["realised"] == pytest.approx((334.95 - 287.1) * 65, abs=0.01)
    assert any("BUY quantity taken from local snapshot" in w for w in res["warnings"])
