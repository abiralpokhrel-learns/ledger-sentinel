"""SQLite schema and helpers for Ledger Sentinel.

The database is the single source of truth that everything else writes into:
orders (state we believe is true), webhook_events (the immutable delivery log
used for idempotency), settlement (what Razorpay actually paid out), and
audit_log (the trail that makes the "audit" claim real).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT PRIMARY KEY,
    amount      REAL NOT NULL,
    mdr         REAL NOT NULL DEFAULT 0,
    gst         REAL NOT NULL DEFAULT 0,
    category    TEXT,
    status      TEXT
);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id    TEXT PRIMARY KEY,
    order_id    TEXT,
    event_type  TEXT,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settlement (
    order_id          TEXT PRIMARY KEY,
    amount_settled    REAL NOT NULL,
    settlement_status TEXT,
    utr               TEXT,
    settlement_date   TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT,
    outcome      TEXT,
    reason       TEXT,
    classification TEXT,
    audit_note   TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def clear_all(conn: sqlite3.Connection) -> None:
    """Wipe all pipeline tables so a re-run is idempotent without deleting the file.

    Useful on Windows where the DB file is often locked by a running uvicorn/
    streamlit process and `rm ledger_sentinel.db` fails with PermissionError.
    """
    conn.executescript(
        """
        DELETE FROM audit_log;
        DELETE FROM webhook_events;
        DELETE FROM settlement;
        DELETE FROM orders;
        DELETE FROM sqlite_sequence WHERE name='audit_log';
        """
    )
    conn.commit()
    try:
        conn.execute("VACUUM;")
    except Exception:
        pass


# --- orders -----------------------------------------------------------------

def upsert_order(conn, order_id, amount, mdr=0.0, gst=0.0, category=None, status=None):
    conn.execute(
        """
        INSERT INTO orders (order_id, amount, mdr, gst, category, status)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id) DO UPDATE SET
            amount=excluded.amount,
            mdr=excluded.mdr,
            gst=excluded.gst,
            category=excluded.category,
            status=excluded.status
        """,
        (order_id, amount, mdr, gst, category, status),
    )


def get_order_status(conn, order_id) -> Optional[str]:
    row = conn.execute(
        "SELECT status FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    return row["status"] if row else None


def set_order_status(conn, order_id, status):
    conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", (status, order_id))


def order_exists(conn, order_id) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        is not None
    )


# --- webhook events ---------------------------------------------------------

def event_exists(conn, event_id) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM webhook_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        is not None
    )


def insert_event(conn, event_id, order_id, event_type):
    conn.execute(
        "INSERT INTO webhook_events (event_id, order_id, event_type) VALUES (?, ?, ?)",
        (event_id, order_id, event_type),
    )


# --- settlement -------------------------------------------------------------

def upsert_settlement(conn, order_id, amount_settled, settlement_status, utr, settlement_date):
    conn.execute(
        """
        INSERT INTO settlement (order_id, amount_settled, settlement_status, utr, settlement_date)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(order_id) DO UPDATE SET
            amount_settled=excluded.amount_settled,
            settlement_status=excluded.settlement_status,
            utr=excluded.utr,
            settlement_date=excluded.settlement_date
        """,
        (order_id, amount_settled, settlement_status, utr, settlement_date),
    )


# --- audit log --------------------------------------------------------------

def log_audit(conn, order_id, outcome, reason, classification=None, audit_note=None):
    conn.execute(
        """INSERT INTO audit_log (order_id, outcome, reason, classification, audit_note)
           VALUES (?, ?, ?, ?, ?)""",
        (order_id, outcome, reason, classification, audit_note),
    )
    conn.commit()


# --- loaders for reconciliation / dashboard ---------------------------------

def load_orders_df(conn) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM orders", conn)


def load_settlement_df(conn) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM settlement", conn)


def load_audit_df(conn) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM audit_log ORDER BY id", conn)
