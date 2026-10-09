import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.storage import init_db, insert_record


class StorageMigrationTests(unittest.TestCase):
    def test_existing_hourly_table_gets_gainloss_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "legacy.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("""
                    CREATE TABLE strategy_hourly_data (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        strategy_name TEXT NOT NULL,
                        collection_timestamp INTEGER NOT NULL,
                        expiry_date TEXT NOT NULL,
                        nifty_open REAL,
                        nifty_ltp REAL,
                        nifty_previous_close REAL,
                        put_strike INTEGER,
                        put_ltp REAL,
                        call_strike INTEGER,
                        call_ltp REAL,
                        call_buy_price REAL,
                        put_buy_price REAL,
                        source TEXT DEFAULT 'angelone',
                        cycle_id TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    INSERT INTO strategy_hourly_data (
                        strategy_name, collection_timestamp, expiry_date,
                        put_ltp, call_ltp, call_buy_price, put_buy_price,
                        cycle_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    "test", 1786452120, "20260811",
                    33.7, 47.5, 92.0, 82.0, "legacy-cycle",
                ))
                conn.commit()
            finally:
                conn.close()

            init_db(db_path)

            conn = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(strategy_hourly_data)"
                    )
                }
                gainloss = conn.execute(
                    "SELECT gainloss FROM strategy_hourly_data"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertIn("gainloss", columns)
            self.assertEqual(gainloss, -92.8)

    def test_gainloss_is_inserted_with_hourly_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "new.db")
            init_db(db_path)
            insert_record(
                db_path,
                {
                    "strategy_name": "test",
                    "collection_timestamp": 1786452120,
                    "expiry_date": "20260811",
                    "gainloss": -92.8,
                    "cycle_id": "test-cycle",
                },
            )

            conn = sqlite3.connect(db_path)
            try:
                value = conn.execute(
                    "SELECT gainloss FROM strategy_hourly_data"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(value, -92.8)

    def test_legacy_snapshots_table_migrates_moneyness_and_backfills_by_geometry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "legacy_snapshots.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("""
                    CREATE TABLE strategy_buy_snapshots (
                        strategy_name   TEXT    NOT NULL,
                        cycle_id        TEXT    NOT NULL,
                        week_start_date TEXT    NOT NULL,
                        expiry_date     TEXT    NOT NULL,
                        call_strike     INTEGER,
                        put_strike      INTEGER,
                        call_buy_price  REAL,
                        put_buy_price   REAL,
                        captured_at     INTEGER NOT NULL,
                        PRIMARY KEY (strategy_name, cycle_id)
                    )
                """)
                # Row 1: Historical OTM Strangle (call > put)
                conn.execute("""
                    INSERT INTO strategy_buy_snapshots (
                        strategy_name, cycle_id, week_start_date, expiry_date,
                        call_strike, put_strike, call_buy_price, put_buy_price, captured_at
                    ) VALUES ('strat', 'cycle-otm', '20260901', '20260908', 24600, 24400, 100.0, 90.0, 1785000000)
                """)
                # Row 2: Historical ITM Long Gut (put > call)
                conn.execute("""
                    INSERT INTO strategy_buy_snapshots (
                        strategy_name, cycle_id, week_start_date, expiry_date,
                        call_strike, put_strike, call_buy_price, put_buy_price, captured_at
                    ) VALUES ('strat', 'cycle-itm', '20260908', '20260915', 24400, 24600, 160.0, 150.0, 1785600000)
                """)
                # Row 3: Historical ATM Straddle (put == call)
                conn.execute("""
                    INSERT INTO strategy_buy_snapshots (
                        strategy_name, cycle_id, week_start_date, expiry_date,
                        call_strike, put_strike, call_buy_price, put_buy_price, captured_at
                    ) VALUES ('strat', 'cycle-atm', '20260915', '20260922', 24500, 24500, 130.0, 130.0, 1786200000)
                """)
                conn.commit()
            finally:
                conn.close()

            # Run migration via init_db
            init_db(db_path)

            conn = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(strategy_buy_snapshots)")}
                self.assertIn("moneyness", columns)
                self.assertIn("entry_spot", columns)
                self.assertIn("spread_width", columns)

                otm_row = conn.execute("SELECT moneyness FROM strategy_buy_snapshots WHERE cycle_id = 'cycle-otm'").fetchone()
                itm_row = conn.execute("SELECT moneyness FROM strategy_buy_snapshots WHERE cycle_id = 'cycle-itm'").fetchone()
                atm_row = conn.execute("SELECT moneyness FROM strategy_buy_snapshots WHERE cycle_id = 'cycle-atm'").fetchone()
            finally:
                conn.close()

            self.assertEqual(otm_row[0], "OTM")
            self.assertEqual(itm_row[0], "ITM")
            self.assertEqual(atm_row[0], "ATM")

            # Re-running init_db on next tick must be strictly idempotent
            init_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                atm_recheck = conn.execute("SELECT moneyness FROM strategy_buy_snapshots WHERE cycle_id = 'cycle-atm'").fetchone()
            finally:
                conn.close()
            self.assertEqual(atm_recheck[0], "ATM")

    def test_timing_shadow_entries_storage_and_idempotency(self):
        from common.storage import insert_timing_shadow_entry, get_timing_shadow_entries

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "timing_test.db")
            init_db(db_path)

            record_v0 = {
                "cycle_id": "CYCLE-20261006",
                "trade_date": "2026-10-06",
                "variant": "V0_baseline_0931",
                "call_strike": 24900,
                "put_strike": 25100,
                "call_entry_time": "09:31:00",
                "call_entry_price": 280.0,
                "call_entry_ts": 1786452660,
                "put_entry_time": "09:31:00",
                "put_entry_price": 270.0,
                "put_entry_ts": 1786452660,
                "combined_entry_price": 550.0,
                "spot_at_call_entry": 25000.0,
                "spot_at_put_entry": 25000.0,
                "nifty_spot_entry": 25000.0,
                "moneyness": "ITM",
                "created_at": 1786452660,
            }
            insert_timing_shadow_entry(db_path, record_v0)

            # Insert second variant
            record_v1 = dict(record_v0)
            record_v1["variant"] = "V1_delayed_1000"
            record_v1["call_entry_price"] = 270.0
            record_v1["put_entry_price"] = 265.0
            record_v1["combined_entry_price"] = 535.0
            insert_timing_shadow_entry(db_path, record_v1)

            # Retrieve all
            entries = get_timing_shadow_entries(db_path)
            self.assertEqual(len(entries), 2)

            # Retrieve by cycle_id
            cycle_entries = get_timing_shadow_entries(db_path, cycle_id="CYCLE-20261006")
            self.assertEqual(len(cycle_entries), 2)
            self.assertEqual(cycle_entries[0]["variant"], "V0_baseline_0931")
            self.assertEqual(cycle_entries[1]["variant"], "V1_delayed_1000")

            # Update record_v1 (idempotent replace)
            record_v1["combined_entry_price"] = 530.0
            insert_timing_shadow_entry(db_path, record_v1)

            updated_entries = get_timing_shadow_entries(db_path, cycle_id="CYCLE-20261006")
            self.assertEqual(len(updated_entries), 2)
            v1_updated = [e for e in updated_entries if e["variant"] == "V1_delayed_1000"][0]
            self.assertEqual(v1_updated["combined_entry_price"], 530.0)


if __name__ == "__main__":
    unittest.main()
