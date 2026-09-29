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
from typing import Optional

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


def build_thursday_db_path(start_date_str: str, expiry_date_str: str) -> str:
    """
    Construct the SQLite database file path for a 3-day Thursday strategy cycle.

    Args:
        start_date_str:   Cycle start date as "YYYYMMDD".
        expiry_date_str:  Expiry date as "YYYYMMDD".

    Returns:
        Full path e.g.
        /home/ubuntu/sqlite/strategies/nifty50_thursday_data_20261001_20261006.db
    """
    _ensure_dir(SQLITE_DIR)
    return os.path.join(
        SQLITE_DIR,
        f"nifty50_thursday_data_{start_date_str}_{expiry_date_str}.db"
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
            cycle_id        TEXT    NOT NULL,
            fsm_state       TEXT    DEFAULT 'DUAL_LONG',
            fsm_call_status TEXT    DEFAULT 'ACTIVE',
            fsm_put_status  TEXT    DEFAULT 'ACTIVE',
            fsm_call_exit_price REAL,
            fsm_put_exit_price  REAL,
            fsm_realized_pnl    REAL DEFAULT 0.0,
            fsm_unrealized_pnl  REAL DEFAULT 0.0,
            fsm_total_gainloss  REAL DEFAULT 0.0,
            fsm_roi_pct         REAL DEFAULT 0.0
        )
    """)

    # Migrate weekly databases created before gainloss was introduced.
    cur.execute("PRAGMA table_info(strategy_hourly_data)")
    hourly_columns = {row[1] for row in cur.fetchall()}
    if "gainloss" not in hourly_columns:
        cur.execute("ALTER TABLE strategy_hourly_data ADD COLUMN gainloss REAL")
        logger.info("Added gainloss column to existing database: %s", db_path)

    # Migrate weekly databases for Decoupled Alpha FSM strategy tracking
    fsm_columns = [
        ("fsm_state", "TEXT DEFAULT 'DUAL_LONG'"),
        ("fsm_call_status", "TEXT DEFAULT 'ACTIVE'"),
        ("fsm_put_status", "TEXT DEFAULT 'ACTIVE'"),
        ("fsm_call_exit_price", "REAL"),
        ("fsm_put_exit_price", "REAL"),
        ("fsm_realized_pnl", "REAL DEFAULT 0.0"),
        ("fsm_unrealized_pnl", "REAL DEFAULT 0.0"),
        ("fsm_total_gainloss", "REAL DEFAULT 0.0"),
        ("fsm_roi_pct", "REAL DEFAULT 0.0"),
    ]
    for col_name, col_def in fsm_columns:
        if col_name not in hourly_columns:
            cur.execute(f"ALTER TABLE strategy_hourly_data ADD COLUMN {col_name} {col_def}")
            logger.info("Added %s column to strategy_hourly_data in %s", col_name, db_path)

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gamma_sniper_trades (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_timestamp     INTEGER NOT NULL,
            expiry_date         TEXT    NOT NULL,
            nifty_spot          REAL    NOT NULL,
            option_type         TEXT    NOT NULL,
            strike              INTEGER NOT NULL,
            entry_price         REAL    NOT NULL,
            exit_price          REAL,
            exit_timestamp      INTEGER,
            pnl_points          REAL,
            pnl_pct             REAL,
            pnl_inr             REAL,
            exit_reason         TEXT,
            allocated_risk_inr  REAL,
            status              TEXT    NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gate_deferred_shadows (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp                   INTEGER NOT NULL,
            trade_date                  TEXT    NOT NULL,
            nifty_spot                  REAL    NOT NULL,
            gate_status                 TEXT    NOT NULL,
            defer_reason                TEXT,
            iv_slope                    REAL,
            vwap_distance               REAL,
            adx_14                      REAL,
            opening_range               REAL,
            hypothetical_call_strike    INTEGER NOT NULL,
            hypothetical_put_strike     INTEGER NOT NULL,
            hypothetical_call_ltp       REAL,
            hypothetical_put_ltp        REAL,
            selection_mode              TEXT,
            composite_direction_score   REAL,
            directional_bias            TEXT,
            target_call_delta           REAL,
            target_put_delta            REAL
        )
    """)

    # Migrate gate_deferred_shadows table if columns are missing
    cur.execute("PRAGMA table_info(gate_deferred_shadows)")
    shadow_cols = {row[1] for row in cur.fetchall()}
    for col_name, col_type in [
        ("composite_direction_score", "REAL"),
        ("directional_bias", "TEXT"),
        ("target_call_delta", "REAL"),
        ("target_put_delta", "REAL"),
    ]:
        if col_name not in shadow_cols:
            cur.execute(f"ALTER TABLE gate_deferred_shadows ADD COLUMN {col_name} {col_type}")

    # Migrate buy snapshot table if columns are missing
    cur.execute("PRAGMA table_info(strategy_buy_snapshots)")
    snapshot_cols = {row[1] for row in cur.fetchall()}
    for col_name, col_type in [
        ("static_call_strike", "INTEGER"),
        ("static_put_strike", "INTEGER"),
        ("selection_mode", "TEXT"),
        ("selection_rationale", "TEXT"),
        ("composite_direction_score", "REAL"),
        ("directional_bias", "TEXT"),
        ("target_call_delta", "REAL"),
        ("target_put_delta", "REAL"),
    ]:
        if col_name not in snapshot_cols:
            cur.execute(f"ALTER TABLE strategy_buy_snapshots ADD COLUMN {col_name} {col_type}")

    conn.commit()
    conn.close()
    logger.info("Database initialised: %s", db_path)


def insert_gamma_sniper_trade(db_path: str, trade: dict) -> None:
    """Insert a Monday Gamma Sniper trade record into the database."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(gamma_sniper_trades)")
    cols = {row[1] for row in cur.fetchall()}
    ins = {k: v for k, v in trade.items() if k in cols}
    col_names = ", ".join(ins.keys())
    placeholders = ", ".join("?" for _ in ins)
    cur.execute(
        f"INSERT INTO gamma_sniper_trades ({col_names}) VALUES ({placeholders})",
        tuple(ins.values())
    )
    conn.commit()
    conn.close()


def update_gamma_sniper_trade(db_path: str, trade_id: int, update_dict: dict) -> None:
    """Update an existing gamma sniper trade in SQLite."""
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(gamma_sniper_trades)")
    cols = {row[1] for row in cur.fetchall()}
    valid_updates = {k: v for k, v in update_dict.items() if k in cols}
    if not valid_updates:
        conn.close()
        return
    set_clause = ", ".join(f"{k} = ?" for k in valid_updates)
    vals = list(valid_updates.values()) + [trade_id]
    cur.execute(f"UPDATE gamma_sniper_trades SET {set_clause} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def insert_gate_deferred_shadow(db_path: str, shadow_record: dict) -> None:
    """Insert a counterfactual shadow record when Smart Entry Gate defers execution."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(gate_deferred_shadows)")
    cols = {row[1] for row in cur.fetchall()}
    ins = {k: v for k, v in shadow_record.items() if k in cols}
    col_names = ", ".join(ins.keys())
    placeholders = ", ".join("?" for _ in ins)
    cur.execute(
        f"INSERT INTO gate_deferred_shadows ({col_names}) VALUES ({placeholders})",
        tuple(ins.values())
    )
    conn.commit()
    conn.close()
    logger.info("Inserted gate deferred shadow record into %s", db_path)


def get_gamma_sniper_trades(db_path: str) -> list:
    """Retrieve all gamma sniper trade records from a weekly database."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM gamma_sniper_trades ORDER BY trade_timestamp ASC")
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def insert_record(db_path: str, record: dict) -> None:
    """
    Insert one hourly collection record into the weekly SQLite database.

    Dynamically matches keys in ``record`` to available table columns,
    guaranteeing seamless compatibility for both base and FSM columns.

    Args:
        db_path:  Path to the weekly ``.db`` file.
        record:   Dictionary with keys matching ``strategy_hourly_data`` columns.
                  Unavailable values should be ``None`` (stored as NULL).
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(strategy_hourly_data)")
    table_cols = [row[1] for row in cur.fetchall()]

    insert_cols = [col for col in table_cols if col in record and col != "id"]
    placeholders = ["?"] * len(insert_cols)
    values = [record[col] for col in insert_cols]

    query = f"INSERT INTO strategy_hourly_data ({', '.join(insert_cols)}) VALUES ({', '.join(placeholders)})"
    cur.execute(query, values)
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

def _atomic_write_json(file_path: str, data: dict) -> None:
    """
    Write JSON data to a unique temporary file and atomically replace the target file.

    Guarantees reader processes (collector, inference runner, peak monitor)
    never read half-written JSON files during concurrent updates, and each
    writer process writes to an isolated temp file to prevent collisions.
    """
    tmp_path = f"{file_path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def save_active_snapshot(snapshot: dict, filepath: Optional[str] = None) -> None:
    """
    Write the active week's buy prices to the JSON snapshot file.

    Also archives the previous snapshot if it exists, naming it
    after the **old** week's start date (so the archive name
    reflects the week it came from, not the new week).
    """
    target_file = filepath or ACTIVE_SNAPSHOT_FILE
    _ensure_dir(os.path.dirname(target_file))

    # Archive the existing snapshot before overwriting, using the
    # OLD snapshot's week_start_date so the name is meaningful.
    if os.path.exists(target_file):
        try:
            old_snapshot = load_active_snapshot(filepath=target_file)
            suffix = old_snapshot.get("week_start_date", "old")
        except (json.JSONDecodeError, OSError):
            suffix = "old"
        archive_name = target_file.replace(".json", f"_{suffix}.json")
        try:
            os.rename(target_file, archive_name)
            logger.info("Archived previous snapshot to %s", archive_name)
        except OSError as exc:
            logger.warning("Failed to archive snapshot: %s", exc)

    if "status" not in snapshot:
        snapshot["status"] = "ongoing"

    _atomic_write_json(target_file, snapshot)
    logger.info("Active buy snapshot saved: %s", target_file)


def load_active_snapshot(filepath: Optional[str] = None) -> dict:
    """
    Load the currently active week's buy-price JSON snapshot.

    Returns:
        Snapshot dict or empty dict if the file does not exist.
    """
    target_file = filepath or ACTIVE_SNAPSHOT_FILE
    if not os.path.exists(target_file):
        logger.info("No active snapshot found at %s", target_file)
        return {}
    with open(target_file, "r", encoding="utf-8") as fh:
        return json.load(fh)


def mark_active_cycle_closed(filepath: Optional[str] = None) -> bool:
    """
    Mark the active cycle in current_week_buy.json (or specified snapshot file) as 'closed'.
    
    Provides an explicit lifecycle flag so downstream inference runners
    and health validators know the weekly strategy has concluded.
    """
    target_file = filepath or ACTIVE_SNAPSHOT_FILE
    snapshot = load_active_snapshot(filepath=target_file)
    if not snapshot:
        return False
    snapshot["status"] = "closed"
    snapshot["closed_at"] = int(datetime.now(timezone.utc).timestamp())
    _atomic_write_json(target_file, snapshot)
    logger.info("Marked active strategy cycle %s as CLOSED in %s", snapshot.get("cycle_id"), target_file)
    return True


def update_active_fsm_state(fsm_dict: dict, filepath: Optional[str] = None) -> bool:
    """
    Update or initialize the alpha_fsm section of current_week_buy.json (or specified snapshot file) in-place.
    
    Guarantees that Decoupled Leg state, trailing stops, and realized gains
    persist across 30-minute cron executions without disturbing base snapshot fields.
    """
    target_file = filepath or ACTIVE_SNAPSHOT_FILE
    snapshot = load_active_snapshot(filepath=target_file)
    if not snapshot:
        return False
    snapshot["alpha_fsm"] = fsm_dict
    _atomic_write_json(target_file, snapshot)
    return True


def update_active_peak_state(peak_data: dict, filepath: Optional[str] = None) -> bool:
    """
    Update the peak_profit section of current_week_buy.json (or specified snapshot file) in-place atomically.

    Guarantees that high-water mark metrics, notification timestamps,
    and daily alert counters persist across 15-minute monitor executions
    without triggering file archive renaming or altering SQLite schema.
    """
    target_file = filepath or ACTIVE_SNAPSHOT_FILE
    snapshot = load_active_snapshot(filepath=target_file)
    if not snapshot:
        return False

    # Prevent stamping an old cycle's peak onto a new cycle if a rollover occurred
    expected_cycle = peak_data.get("cycle_id")
    if expected_cycle and snapshot.get("cycle_id") and snapshot.get("cycle_id") != expected_cycle:
        logger.warning(
            "Cycle ID mismatch in update_active_peak_state: snapshot has %s, peak_data has %s. Skipping update.",
            snapshot.get("cycle_id"), expected_cycle
        )
        return False

    snapshot["peak_profit"] = peak_data
    _atomic_write_json(target_file, snapshot)
    return True


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
