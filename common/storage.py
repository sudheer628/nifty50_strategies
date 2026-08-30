"""
Storage layer for the NIFTY50 weekly option strategy.

Handles:
    - SQLite database creation (one file per weekly cycle).
    - Inserting hourly collection records.
    - Saving / loading the active week's buy-price JSON snapshot.
    - Archiving historical buy snapshots to a permanent SQLite table.
"""

import os
import sqlite3
import json
import uuid
from datetime import datetime, timezone

from config import (
    logger,
    SQLITE_DIR,
    SNAPSHOT_DIR,
    ACTIVE_SNAPSHOT_FILE,
    STRATEGY_NAME,
)


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def _ensure_dir(path: str) -> None:
    """Create a directory (and parents) if it does not exist."""
    os.makedirs(path, exist_ok=True)


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def build_db_path(start_date_str: str, expiry_date_str: str) -> str:
    """
    Construct the SQLite database file path for a weekly cycle.

    Args:
        start_date_str:   Week start date as ``"YYYYMMDD"``.
        expiry_date_str:  Expiry date as ``"YYYYMMDD"``.

    Returns:
        Full path e.g.
        ``/home/ubuntu/sqlite/strategies/nifty50_weekly_data_20260804_20260811.db``
    """
    _ensure_dir(SQLITE_DIR)
    return os.path.join(
        SQLITE_DIR,
        f"nifty50_weekly_data_{start_date_str}_{expiry_date_str}.db"
    )


def init_db(db_path: str) -> None:
    """
    Create the weekly data table and buy-snapshot table if they do not exist.

    ``strategy_hourly_data`` schema matches section 10 of the plan:

        - strategy_name
        - collection_timestamp
        - expiry_date
        - nifty_open
        - nifty_ltp
        - nifty_previous_close
        - put_strike
        - put_ltp
        - call_strike
        - call_ltp
        - call_buy_price
        - put_buy_price
        - source
        - cycle_id

    ``strategy_buy_snapshots`` schema matches section 8.2.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_hourly_data (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name   TEXT    NOT NULL,
            collection_timestamp INTEGER NOT NULL,
            expiry_date     TEXT    NOT NULL,
            nifty_open      REAL,
            nifty_ltp       REAL,
            nifty_previous_close REAL,
            put_strike      INTEGER,
            put_ltp         REAL,
            call_strike     INTEGER,
            call_ltp        REAL,
            call_buy_price  REAL,
            put_buy_price   REAL,
            gainloss        REAL,
            source          TEXT    DEFAULT 'angelone',
            cycle_id        TEXT    NOT NULL
        )
    """)

    # Migrate weekly databases created before gainloss was introduced.
    cur.execute("PRAGMA table_info(strategy_hourly_data)")
    hourly_columns = {row[1] for row in cur.fetchall()}
    if "gainloss" not in hourly_columns:
        cur.execute("ALTER TABLE strategy_hourly_data ADD COLUMN gainloss REAL")
        logger.info("Added gainloss column to existing database: %s", db_path)

    cur.execute("""
        UPDATE strategy_hourly_data
        SET gainloss = ROUND(
            (call_ltp - call_buy_price) + (put_ltp - put_buy_price),
            2
        )
        WHERE gainloss IS NULL
          AND call_ltp IS NOT NULL
          AND call_buy_price IS NOT NULL
          AND put_ltp IS NOT NULL
          AND put_buy_price IS NOT NULL
    """)
    if cur.rowcount:
        logger.info("Backfilled gainloss for %d existing rows", cur.rowcount)

    # Migrate collection_timestamp from TEXT (ISO string) to INTEGER (Unix epoch)
    # Uses Python-based conversion because SQLite's strftime('%s', ...) is
    # unavailable before v3.38.0 and can return NULL for some ISO formats,
    # which would violate the NOT NULL constraint on this column.
    text_rows = cur.execute(
        "SELECT id, collection_timestamp FROM strategy_hourly_data "
        "WHERE typeof(collection_timestamp) = 'text'"
    ).fetchall()

    if text_rows:
        migrated = 0
        for row_id, ts_text in text_rows:
            try:
                # Already a Unix epoch stored as text (e.g. '1787027402')
                if ts_text.isdigit():
                    epoch = int(ts_text)
                else:
                    # Handle both 'Z' suffix and explicit '+00:00' offset
                    ts_clean = ts_text.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(ts_clean)
                    epoch = int(dt.timestamp())
                cur.execute(
                    "UPDATE strategy_hourly_data SET collection_timestamp = ? WHERE id = ?",
                    (epoch, row_id),
                )
                migrated += 1
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "Could not convert timestamp '%s' (row %d): %s",
                    ts_text, row_id, exc,
                )
        if migrated:
            logger.info(
                "Migrated %d/%d collection_timestamp values to INTEGER in %s",
                migrated, len(text_rows), db_path,
            )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_buy_snapshots (
            strategy_name   TEXT    NOT NULL,
            cycle_id        TEXT    NOT NULL,
            week_start_date TEXT    NOT NULL,
            expiry_date     TEXT    NOT NULL,
            call_strike     INTEGER,
            put_strike      INTEGER,
            call_buy_price  REAL,
            put_buy_price   REAL,
            captured_at     INTEGER NOT NULL,
            static_call_strike INTEGER,
            static_put_strike  INTEGER,
            selection_mode  TEXT,
            selection_rationale TEXT,
            PRIMARY KEY (strategy_name, cycle_id)
        )
    """)

    # Migrate buy snapshot table if columns are missing
    cur.execute("PRAGMA table_info(strategy_buy_snapshots)")
    snapshot_cols = {row[1] for row in cur.fetchall()}
    for col_name, col_type in [
        ("static_call_strike", "INTEGER"),
        ("static_put_strike", "INTEGER"),
        ("selection_mode", "TEXT"),
        ("selection_rationale", "TEXT"),
    ]:
        if col_name not in snapshot_cols:
            cur.execute(f"ALTER TABLE strategy_buy_snapshots ADD COLUMN {col_name} {col_type}")

    conn.commit()
    conn.close()
    logger.info("Database initialised: %s", db_path)


def insert_record(db_path: str, record: dict) -> None:
    """
    Insert one hourly collection record into the weekly SQLite database.

    Args:
        db_path:  Path to the weekly ``.db`` file.
        record:   Dictionary with keys matching ``strategy_hourly_data`` columns.
                  Unavailable values should be ``None`` (stored as NULL).
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO strategy_hourly_data (
            strategy_name,
            collection_timestamp,
            expiry_date,
            nifty_open,
            nifty_ltp,
            nifty_previous_close,
            put_strike,
            put_ltp,
            call_strike,
            call_ltp,
            call_buy_price,
            put_buy_price,
            gainloss,
            source,
            cycle_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record.get("strategy_name", STRATEGY_NAME),
        int(record.get("collection_timestamp", 0)),
        record.get("expiry_date", ""),
        record.get("nifty_open"),
        record.get("nifty_ltp"),
        record.get("nifty_previous_close"),
        record.get("put_strike"),
        record.get("put_ltp"),
        record.get("call_strike"),
        record.get("call_ltp"),
        record.get("call_buy_price"),
        record.get("put_buy_price"),
        record.get("gainloss"),
        record.get("source", "angelone"),
        record.get("cycle_id", ""),
    ))
    conn.commit()
    conn.close()


def insert_buy_snapshot(db_path: str, snapshot: dict) -> None:
    """
    Persist a Tuesday buy-price snapshot to the historical SQLite table.

    Uses ``INSERT OR REPLACE`` so re-running on the same Tuesday is idempotent.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO strategy_buy_snapshots (
            strategy_name,
            cycle_id,
            week_start_date,
            expiry_date,
            call_strike,
            put_strike,
            call_buy_price,
            put_buy_price,
            captured_at,
            static_call_strike,
            static_put_strike,
            selection_mode,
            selection_rationale
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        snapshot.get("strategy_name", STRATEGY_NAME),
        snapshot.get("cycle_id", ""),
        snapshot.get("week_start_date", ""),
        snapshot.get("expiry_date", ""),
        snapshot.get("call_strike"),
        snapshot.get("put_strike"),
        snapshot.get("call_buy_price"),
        snapshot.get("put_buy_price"),
        snapshot.get("captured_at", int(datetime.now(timezone.utc).timestamp())),
        snapshot.get("static_call_strike"),
        snapshot.get("static_put_strike"),
        snapshot.get("selection_mode"),
        snapshot.get("selection_rationale"),
    ))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# JSON snapshot helpers (active week convenience file)
# ---------------------------------------------------------------------------

def save_active_snapshot(snapshot: dict) -> None:
    """
    Write the active week's buy prices to the JSON snapshot file.

    Also archives the previous snapshot if it exists, naming it
    after the **old** week's start date (so the archive name
    reflects the week it came from, not the new week).
    """
    _ensure_dir(SNAPSHOT_DIR)

    # Archive the existing snapshot before overwriting, using the
    # OLD snapshot's week_start_date so the name is meaningful.
    if os.path.exists(ACTIVE_SNAPSHOT_FILE):
        try:
            old_snapshot = load_active_snapshot()
            suffix = old_snapshot.get("week_start_date", "old")
        except (json.JSONDecodeError, OSError):
            suffix = "old"
        archive_name = ACTIVE_SNAPSHOT_FILE.replace(".json", f"_{suffix}.json")
        try:
            os.rename(ACTIVE_SNAPSHOT_FILE, archive_name)
            logger.info("Archived previous snapshot to %s", archive_name)
        except OSError as exc:
            logger.warning("Failed to archive snapshot: %s", exc)

    with open(ACTIVE_SNAPSHOT_FILE, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2)

    logger.info("Active buy snapshot saved: %s", ACTIVE_SNAPSHOT_FILE)


def load_active_snapshot() -> dict:
    """
    Load the currently active week's buy-price JSON snapshot.

    Returns:
        Snapshot dict or empty dict if the file does not exist.
    """
    if not os.path.exists(ACTIVE_SNAPSHOT_FILE):
        logger.info("No active snapshot found at %s", ACTIVE_SNAPSHOT_FILE)
        return {}
    with open(ACTIVE_SNAPSHOT_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Cycle ID generator
# ---------------------------------------------------------------------------

def generate_cycle_id(start_date_str: str) -> str:
    """
    Generate a unique cycle identifier.

    Combines a date prefix with a short UUID for uniqueness.

    Args:
        start_date_str:  Week start date as ``"YYYYMMDD"``.

    Returns:
        String like ``"20260804-a1b2c3d4"``.
    """
    short_uuid = uuid.uuid4().hex[:8]
    return f"{start_date_str}-{short_uuid}"
