#!/usr/bin/env python3
"""
Script to convert existing ISO string timestamps to Unix integer timestamps.

Usage:
    python scripts/convert_timestamps.py --db /home/ubuntu/sqlite/strategies/nifty50_weekly_data_20260818_20260825.db
"""

import argparse
import sqlite3
from datetime import datetime
import pytz

UTC = pytz.UTC


def convert_iso_to_unix(iso_string: str) -> int:
    """Convert ISO 8601 string to Unix timestamp (integer seconds)."""
    # Parse ISO format with timezone offset
    dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = UTC.localize(dt)
    return int(dt.timestamp())


def main():
    parser = argparse.ArgumentParser(description="Convert ISO timestamps to Unix integers")
    parser.add_argument("--db", required=True, help="Path to SQLite database file")
    args = parser.parse_args()

    db_path = args.db

    print(f"Converting timestamps in: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # First, check existing rows
    cursor.execute("SELECT COUNT(*) FROM strategy_hourly_data")
    total_rows = cursor.fetchone()[0]
    print(f"Total rows in strategy_hourly_data: {total_rows}")

    # Get sample of current timestamps
    cursor.execute("SELECT collection_timestamp FROM strategy_hourly_data LIMIT 3")
    sample = cursor.fetchall()
    print(f"Sample current timestamps: {[row[0] for row in sample]}")

    # Check if already converted (all integers)
    cursor.execute("SELECT typeof(collection_timestamp) FROM strategy_hourly_data LIMIT 1")
    current_type = cursor.fetchone()[0]
    print(f"Current column type: {current_type}")

    if current_type == "integer":
        print("Timestamps already in Unix integer format. No conversion needed.")
        conn.close()
        return

    # Convert timestamps
    cursor.execute("SELECT id, collection_timestamp FROM strategy_hourly_data")
    rows = cursor.fetchall()

    converted_count = 0
    for row_id, timestamp in rows:
        try:
            unix_ts = convert_iso_to_unix(timestamp)
            cursor.execute(
                "UPDATE strategy_hourly_data SET collection_timestamp = ? WHERE id = ?",
                (unix_ts, row_id)
            )
            converted_count += 1
        except Exception as e:
            print(f"Error converting row {row_id}: {e}")

    conn.commit()
    print(f"Successfully converted {converted_count} rows")

    # Verify conversion
    cursor.execute("SELECT typeof(collection_timestamp) FROM strategy_hourly_data LIMIT 1")
    new_type = cursor.fetchone()[0]
    print(f"New column type: {new_type}")

    # Show sample of new timestamps
    cursor.execute("SELECT collection_timestamp FROM strategy_hourly_data LIMIT 3")
    sample_new = cursor.fetchall()
    print(f"Sample new timestamps: {[row[0] for row in sample_new]}")

    conn.close()
    print("Conversion complete!")


if __name__ == "__main__":
    main()
