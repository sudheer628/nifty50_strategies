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


if __name__ == "__main__":
    unittest.main()
