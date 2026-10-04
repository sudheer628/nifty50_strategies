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


if __name__ == "__main__":
    unittest.main()
