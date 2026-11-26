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
    "place_order", "modify_exit_order", "place_entry",
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


# ---------------------------------------------------------------------------
# Profit-exit: planning, duplicate guard, sequencing, dry-run, execute (mocked)
# ---------------------------------------------------------------------------

from datetime import datetime as _dt


class FakeBrokerState:
    """Stands in for BrokerView and for the Upstox adapter. Never touches the network."""

    def __init__(self, positions, orders, ltps, fill_price, fill_behaviour=None, cancel_behaviour=None):
        self.positions = positions
        self.orders = orders
        self._ltps = ltps
        self.fill_price = fill_price
        self.fill_behaviour = fill_behaviour or {}
        self.cancel_behaviour = cancel_behaviour or {}
        self.placed = []
        self.cancelled = []
        self.token = "T"
        self.adapter = self

    def cancel_order(self, token, order_id, variety="STOPLOSS"):
        self.cancelled.append(order_id)
        for o in self.orders:
            if o["order_id"] == order_id:
                o["status"] = self.cancel_behaviour.get(order_id, "cancelled")
        return {"success": True, "order_id": order_id}

    # adapter interface
    def fetch_positions(self, token):
        return {"data": self.positions}

    def fetch_order_book(self, token):
        return list(self.orders)

    def get_exit_product(self, pos):
        return "D"

    def fetch_ltp(self, token, tok, sym=""):
        return self._ltps.get(tok)

    def place_exit_order_market(self, token, position, target_qty, product=None, tag=""):
        tok = position["instrument_token"]
        self.placed.append({"tok": tok, "qty": target_qty, "tag": tag})
        oid = f"SELL{len(self.placed)}"
        behaviour = self.fill_behaviour.get(tok, "complete")
        self.orders.append({
            "order_id": oid, "instrument_token": tok, "trading_symbol": position.get("tradingsymbol", ""),
            "transaction_type": "SELL", "status": behaviour,
            "filled_quantity": target_qty if behaviour == "complete" else 0,
            "average_price": self.fill_price[tok] if behaviour == "complete" else 0.0, "tag": tag,
        })
        if behaviour == "complete":
            for p in self.positions:
                if p["instrument_token"] == tok:
                    p["quantity"] = 0
        return {"success": True, "order_id": oid, "action": "place_market"}

    # BrokerView interface used by evaluate_cycle / run_profit_exit
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
        return [(int(o["filled_quantity"]), o["average_price"], o["order_id"]) for o in self.orders
                if o["transaction_type"] == "SELL" and o["instrument_token"] == instrument_token
                and o["filled_quantity"] > 0]

    def ltp(self, instrument_token, symbol):
        return self._ltps.get(instrument_token)


CE_TOK, PE_TOK = "NSE_FO|47839", "NSE_FO|47892"


def _buy_orders():
    return [
        {"order_id": "B_CE", "instrument_token": CE_TOK, "trading_symbol": "NIFTY26D0125800CE",
         "transaction_type": "BUY", "status": "complete", "filled_quantity": 65, "average_price": 234.9, "tag": "THU-BUY"},
        {"order_id": "B_PE", "instrument_token": PE_TOK, "trading_symbol": "NIFTY26D0125900PE",
         "transaction_type": "BUY", "status": "complete", "filled_quantity": 65, "average_price": 239.5, "tag": "THU-BUY"},
    ]


def _both_active_positions():
    return [
        {"instrument_token": CE_TOK, "trading_symbol": "NIFTY26D0125800CE", "quantity": 65,
         "overnight_buy_quantity": 0, "day_buy_quantity": 65},
        {"instrument_token": PE_TOK, "trading_symbol": "NIFTY26D0125900PE", "quantity": 65,
         "overnight_buy_quantity": 0, "day_buy_quantity": 65},
    ]


def _setup(tmp_path, monkeypatch, positions, orders, ltps, fill_price, fill_behaviour=None, cancel_behaviour=None):
    _thursday_db(str(tmp_path))
    monkeypatch.setattr(m, "SQLITE_DIR", str(tmp_path))
    monkeypatch.setattr(m, "FILL_POLL_SECONDS", 0.0)
    monkeypatch.setattr(m, "FILL_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(m, "_market_holiday_flag", lambda: None)  # no Redis in tests
    fake = FakeBrokerState(positions, orders, ltps, fill_price, fill_behaviour, cancel_behaviour)
    monkeypatch.setattr(m, "BrokerView", lambda broker: fake)  # no network in tests
    recorded = []

    def fake_record(**kw):
        recorded.append(kw)
        return {"status": "recorded"}

    import scripts.auto_record_manual_exit as ar
    monkeypatch.setattr(ar, "auto_record_manual_exit", fake_record)
    return fake, recorded


MARKET_TIME = _dt(2026, 11, 26, 10, 0, tzinfo=m.IST)  # Thursday 10:00 IST


def test_plan_below_threshold_does_nothing():
    res = {"legs": {"CALL": {"open_qty": 65, "leg_pl": 100.0, "symbol": "a", "strike": 1, "opt_type": "CE", "ltp": 1, "instrument_token": "x", "leg": "CALL"}},
           "total_pl_pct": 5.0}
    assert m.plan_profit_exit(res, 20.0)["action"] == "none"


def test_plan_orders_higher_leg_pl_first_and_single_negative_leg_allowed():
    legs = {
        "CALL": {"open_qty": 65, "leg_pl": 500.0, "symbol": "C", "strike": 25800, "opt_type": "CE", "ltp": 1, "instrument_token": "c", "leg": "CALL"},
        "PUT": {"open_qty": 65, "leg_pl": 2000.0, "symbol": "P", "strike": 25900, "opt_type": "PE", "ltp": 1, "instrument_token": "p", "leg": "PUT"},
    }
    plan = m.plan_profit_exit({"legs": legs, "total_pl_pct": 25.0}, 20.0)
    assert [s["leg"] for s in plan["sequence"]] == ["PUT", "CALL"]
    # single active leg that is negative, but combined >= threshold -> still sold
    neg = {"CALL": {"open_qty": 0, "leg_pl": 900.0, "symbol": "C", "strike": 1, "opt_type": "CE", "ltp": None, "instrument_token": "c", "leg": "CALL"},
           "PUT": {"open_qty": 65, "leg_pl": -300.0, "symbol": "P", "strike": 1, "opt_type": "PE", "ltp": 1, "instrument_token": "p", "leg": "PUT"}}
    plan2 = m.plan_profit_exit({"legs": neg, "total_pl_pct": 21.0}, 20.0)
    assert plan2["action"] == "square_off" and [s["leg"] for s in plan2["sequence"]] == ["PUT"]


def test_dry_run_sends_nothing(tmp_path, monkeypatch):
    fake, recorded = _setup(tmp_path, monkeypatch, _both_active_positions(), _buy_orders(),
                            {CE_TOK: 300.0, PE_TOK: 280.0}, {CE_TOK: 300.0, PE_TOK: 280.0})
    res = m.run_profit_exit("thursday", 20.0, execute=False, now=MARKET_TIME)
    assert res["plan"]["action"] == "square_off"
    assert fake.placed == [] and recorded == []


SL_M_OPEN = {"order_id": "OPEN1", "instrument_token": CE_TOK, "trading_symbol": "NIFTY26D0125800CE",
             "transaction_type": "SELL", "status": "trigger_pending", "filled_quantity": 0,
             "average_price": 0.0, "tag": "THU-SL"}


def test_threshold_met_cancels_open_cycle_order_then_sells(tmp_path, monkeypatch):
    fake, recorded = _setup(tmp_path, monkeypatch, _both_active_positions(), _buy_orders() + [dict(SL_M_OPEN)],
                            {CE_TOK: 300.0, PE_TOK: 280.0}, {CE_TOK: 300.0, PE_TOK: 280.0})
    res = m.run_profit_exit("thursday", 20.0, execute=True, now=MARKET_TIME)
    assert fake.cancelled == ["OPEN1"]
    assert res["cancel"]["confirmed"] is True
    assert res["action"] == "square_off_executed"
    assert [p["tok"] for p in fake.placed] == [CE_TOK, PE_TOK]  # sells happen only after the cancel


def test_cancel_not_confirmed_aborts_without_selling(tmp_path, monkeypatch):
    fake, recorded = _setup(tmp_path, monkeypatch, _both_active_positions(), _buy_orders() + [dict(SL_M_OPEN)],
                            {CE_TOK: 300.0, PE_TOK: 280.0}, {CE_TOK: 300.0, PE_TOK: 280.0},
                            cancel_behaviour={"OPEN1": "trigger_pending"})  # cancel never confirmed
    res = m.run_profit_exit("thursday", 20.0, execute=True, now=MARKET_TIME)
    assert res["action"] == "aborted_open_orders_not_cancelled"
    assert fake.placed == [] and recorded == []


def test_order_filled_during_cancel_aborts_without_selling(tmp_path, monkeypatch):
    fake, recorded = _setup(tmp_path, monkeypatch, _both_active_positions(), _buy_orders() + [dict(SL_M_OPEN)],
                            {CE_TOK: 300.0, PE_TOK: 280.0}, {CE_TOK: 300.0, PE_TOK: 280.0},
                            cancel_behaviour={"OPEN1": "complete"})  # SL-M triggered and filled first
    res = m.run_profit_exit("thursday", 20.0, execute=True, now=MARKET_TIME)
    assert res["action"] == "aborted_open_orders_not_cancelled"
    assert fake.placed == []


def test_below_threshold_never_touches_open_orders(tmp_path, monkeypatch):
    # CE 240 / PE 240: combined P&L small -> below 20% -> open SL-M must be left alone
    fake, recorded = _setup(tmp_path, monkeypatch, _both_active_positions(), _buy_orders() + [dict(SL_M_OPEN)],
                            {CE_TOK: 240.0, PE_TOK: 240.0}, {})
    res = m.run_profit_exit("thursday", 20.0, execute=True, now=MARKET_TIME)
    assert res["plan"]["action"] == "none"
    assert fake.cancelled == [] and fake.placed == []
    assert [o["order_id"] for o in res["open_cycle_orders"]] == ["OPEN1"]


def test_dry_run_reports_would_cancel_without_cancelling(tmp_path, monkeypatch):
    fake, recorded = _setup(tmp_path, monkeypatch, _both_active_positions(), _buy_orders() + [dict(SL_M_OPEN)],
                            {CE_TOK: 300.0, PE_TOK: 280.0}, {CE_TOK: 300.0, PE_TOK: 280.0})
    res = m.run_profit_exit("thursday", 20.0, execute=False, now=MARKET_TIME)
    assert res["would_cancel"] == ["OPEN1"]
    assert fake.cancelled == [] and fake.placed == []


def test_market_holiday_flag_stops_before_any_broker_call(tmp_path, monkeypatch):
    _thursday_db(str(tmp_path))
    monkeypatch.setattr(m, "SQLITE_DIR", str(tmp_path))
    monkeypatch.setattr(m, "_market_holiday_flag", lambda: "yes")

    def boom(*a, **k):
        raise AssertionError("broker must not be contacted on a holiday")

    monkeypatch.setattr(m, "BrokerView", boom)
    res = m.run_profit_exit("thursday", 20.0, execute=True, now=MARKET_TIME)
    assert res["action"] == "skipped_market_holiday"


def test_unrelated_open_order_is_ignored(tmp_path, monkeypatch):
    orders = _buy_orders() + [{"order_id": "OTHER", "instrument_token": "NSE_FO|99999", "trading_symbol": "BANKNIFTY",
                               "transaction_type": "BUY", "status": "open", "filled_quantity": 0,
                               "average_price": 0.0, "tag": ""}]
    fake, recorded = _setup(tmp_path, monkeypatch, _both_active_positions(), orders,
                            {CE_TOK: 300.0, PE_TOK: 280.0}, {CE_TOK: 300.0, PE_TOK: 280.0})
    res = m.run_profit_exit("thursday", 20.0, execute=False, now=MARKET_TIME)
    assert res.get("action") != "skipped_open_orders"
    assert res["plan"]["action"] == "square_off"  # unrelated order did not block the plan


def test_execute_sells_higher_leg_first_then_records_both(tmp_path, monkeypatch):
    # CE leg_pl = (300-234.9)*65 = 4231.5 ; PE leg_pl = (280-239.5)*65 = 2632.5 -> CE first
    fake, recorded = _setup(tmp_path, monkeypatch, _both_active_positions(), _buy_orders(),
                            {CE_TOK: 300.0, PE_TOK: 280.0}, {CE_TOK: 300.0, PE_TOK: 280.0})
    res = m.run_profit_exit("thursday", 20.0, execute=True, now=MARKET_TIME)
    assert res["action"] == "square_off_executed"
    assert [p["tok"] for p in fake.placed] == [CE_TOK, PE_TOK]
    assert all(p["tag"] == m.PROFIT_EXIT_TAG for p in fake.placed)
    assert [r["strike"] for r in recorded] == [25800, 25900]
    assert all(r["status_override"] == "PROFIT_TARGET_CLOSED" and r["dry_run"] is False for r in recorded)
    assert all(r["exit_reason_override"] == m.PROFIT_EXIT_REASON for r in recorded)
    assert recorded[0]["expiry_iso"] == "2026-12-01"


def test_second_leg_not_sent_until_first_is_filled(tmp_path, monkeypatch):
    fake, recorded = _setup(tmp_path, monkeypatch, _both_active_positions(), _buy_orders(),
                            {CE_TOK: 300.0, PE_TOK: 280.0}, {CE_TOK: 300.0, PE_TOK: 280.0},
                            fill_behaviour={CE_TOK: "open"})
    res = m.run_profit_exit("thursday", 20.0, execute=True, now=MARKET_TIME)
    assert [p["tok"] for p in fake.placed] == [CE_TOK]  # PE never sent
    assert recorded == []
    assert res["fills"][-1]["state"] == "timeout"


def test_refuses_to_execute_outside_market_hours(tmp_path, monkeypatch):
    fake, recorded = _setup(tmp_path, monkeypatch, _both_active_positions(), _buy_orders(),
                            {CE_TOK: 300.0, PE_TOK: 280.0}, {CE_TOK: 300.0, PE_TOK: 280.0})
    after = _dt(2026, 11, 26, 16, 0, tzinfo=m.IST)
    res = m.run_profit_exit("thursday", 20.0, execute=True, now=after)
    assert res["action"] == "refused_market_closed"
    assert fake.placed == []


def test_order_placement_only_in_place_function():
    src = open(SCRIPT, encoding="utf-8").read()
    assert src.count("place_exit_order_market(") == 1
    start = src.index("def _place_market_sell")
    end = src.index("def _wait_for_fill")
    assert "place_exit_order_market(" in src[start:end]
    # the read-only path must never reference order placement
    read_only = src[:src.index("PROFIT_EXIT_TAG = ")]
    assert "place_exit_order" not in read_only


def test_cancel_only_inside_cancel_helper():
    src = open(SCRIPT, encoding="utf-8").read()
    assert src.count("cancel_order(") == 1
    start = src.index("def _cancel_and_confirm")
    end = src.index("def run_profit_exit")
    assert "cancel_order(" in src[start:end]
