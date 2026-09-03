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

-- Defense-only separation: machine vs human decisions never co-mingled.
-- machine_decisions: every automated outcome (policy engine, responder drafts).
-- human_resolutions: analyst overrides / approvals — authoritative if present.
CREATE TABLE IF NOT EXISTS machine_decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       TEXT,
    case_id        TEXT,
    decision       TEXT,  -- approve | step_up | review | block | draft
    reason         TEXT,
    policy_version TEXT,
    signals_json   TEXT,  -- snapshot of signals that drove the decision
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS human_resolutions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       TEXT,
    case_id        TEXT,
    resolution     TEXT,  -- approve | step_up | review | block | filed | dismissed
    analyst        TEXT,
    note           TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS detection_windows (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start     TEXT,
    window_end       TEXT,
    window_size      TEXT,
    count            INTEGER,
    fraud_count      INTEGER,
    fraud_rate       REAL,
    baseline_mean    REAL,
    baseline_std     REAL,
    threshold        REAL,
    is_spike         INTEGER,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    # isolation_level=None => autocommit; we manage transactions explicitly where needed
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    try:
        conn.executescript(SCHEMA)
        # Helpful index for dashboard queries: outcome filter is the hot path
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_outcome ON audit_log(outcome);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_order ON audit_log(order_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_order ON webhook_events(order_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_machine_order ON machine_decisions(order_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_human_order ON human_resolutions(order_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_detection_window ON detection_windows(window_start);")
        conn.commit()
    except sqlite3.OperationalError as e:
        # If DB is locked or corrupted, surface a clear error instead of silent fail
        raise RuntimeError(f"init_db failed: {e}") from e


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
        DELETE FROM machine_decisions;
        DELETE FROM human_resolutions;
        DELETE FROM detection_windows;
        DELETE FROM sqlite_sequence WHERE name='audit_log';
        DELETE FROM sqlite_sequence WHERE name='machine_decisions';
        DELETE FROM sqlite_sequence WHERE name='human_resolutions';
        """
    )
    conn.commit()
    try:
        conn.execute("VACUUM;")
    except Exception:
        pass


# --- orders -----------------------------------------------------------------

def upsert_order(conn, order_id, amount, mdr=0.0, gst=0.0, category=None, status=None):
    # Preserve existing status when caller passes None (used by pipeline's
    # _load_orders which wants to exercise the state machine, not pre-fill status).
    # This prevents a `fresh=False` re-run from wiping webhook-derived status.
    conn.execute(
        """
        INSERT INTO orders (order_id, amount, mdr, gst, category, status)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id) DO UPDATE SET
            amount=excluded.amount,
            mdr=excluded.mdr,
            gst=excluded.gst,
            category=excluded.category,
            status=COALESCE(excluded.status, orders.status)
        """,
        (order_id, amount, mdr, gst, category, status),
    )


def upsert_orders_batch(conn, rows: list[dict]) -> None:
    """Batch version for _load_orders — single transaction, far faster for 10k+ rows."""
    if not rows:
        return
    data = [(r["order_id"], r["amount"], r["mdr"], r["gst"], r["category"], r.get("status")) for r in rows]
    conn.executemany(
        """
        INSERT INTO orders (order_id, amount, mdr, gst, category, status)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(order_id) DO UPDATE SET
            amount=excluded.amount,
            mdr=excluded.mdr,
            gst=excluded.gst,
            category=excluded.category,
            status=COALESCE(excluded.status, orders.status)
        """,
        data,
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
    try:
        conn.execute(
            "INSERT INTO webhook_events (event_id, order_id, event_type) VALUES (?, ?, ?)",
            (event_id, order_id, event_type),
        )
    except sqlite3.IntegrityError:
        # Race: two deliveries with same event_id at same time
        # Let caller treat as duplicate
        raise


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
    # Truncate note to avoid blowing up the DB with a huge LLM output
    if audit_note and len(audit_note) > 2000:
        audit_note = audit_note[:1997] + "..."
    try:
        conn.execute(
            """INSERT INTO audit_log (order_id, outcome, reason, classification, audit_note)
               VALUES (?, ?, ?, ?, ?)""",
            (order_id, outcome, reason, classification, audit_note),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            # Retry once after a short wait — common on Windows with WAL
            import time
            time.sleep(0.2)
            conn.execute(
                """INSERT INTO audit_log (order_id, outcome, reason, classification, audit_note)
                   VALUES (?, ?, ?, ?, ?)""",
                (order_id, outcome, reason, classification, audit_note),
            )
            conn.commit()
        else:
            raise



# --- machine / human separation (defense-only, auditable) -------------------

def log_machine_decision(conn, order_id, decision, reason=None, policy_version=None, signals_json=None, case_id=None):
    """Automated decision — never overwrites human. Defense-only values only."""
    allowed = {"approve", "step_up", "review", "block", "draft", "flagged", "no_spike"}
    if decision not in allowed:
        raise ValueError(f"Decision {decision!r} not in defense-only allowlist {allowed}")
    conn.execute(
        """INSERT INTO machine_decisions (order_id, case_id, decision, reason, policy_version, signals_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (order_id, case_id, decision, reason[:2000] if reason else None, policy_version, signals_json[:8000] if signals_json else None),
    )
    conn.commit()

def log_human_resolution(conn, order_id, resolution, analyst=None, note=None, case_id=None):
    """Human analyst resolution — authoritative. Separate table by design."""
    allowed = {"approve", "step_up", "review", "block", "filed", "dismissed", "approved"}
    if resolution not in allowed:
        raise ValueError(f"Resolution {resolution!r} not in allowlist {allowed}")
    conn.execute(
        """INSERT INTO human_resolutions (order_id, case_id, resolution, analyst, note)
           VALUES (?, ?, ?, ?, ?)""",
        (order_id, case_id, resolution, analyst, note[:2000] if note else None),
    )
    conn.commit()

def load_machine_decisions_df(conn) -> "pd.DataFrame":
    try:
        return pd.read_sql("SELECT * FROM machine_decisions ORDER BY id DESC", conn)
    except Exception:
        return pd.DataFrame(columns=["id","order_id","case_id","decision","reason","policy_version","signals_json","created_at"])

def load_human_resolutions_df(conn) -> "pd.DataFrame":
    try:
        return pd.read_sql("SELECT * FROM human_resolutions ORDER BY id DESC", conn)
    except Exception:
        return pd.DataFrame(columns=["id","order_id","case_id","resolution","analyst","note","created_at"])

def get_final_outcome(conn, order_id: str) -> str | None:
    """Human wins if present, else machine."""
    try:
        r = conn.execute("SELECT resolution FROM human_resolutions WHERE order_id = ? ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
        if r and r[0]:
            return f"human:{r[0]}"
        r = conn.execute("SELECT decision FROM machine_decisions WHERE order_id = ? ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
        if r and r[0]:
            return f"machine:{r[0]}"
    except Exception:
        pass
    return None

# --- loaders for reconciliation / dashboard ---------------------------------

def load_orders_df(conn) -> pd.DataFrame:
    try:
        return pd.read_sql("SELECT * FROM orders", conn)
    except Exception:
        return pd.DataFrame(columns=["order_id", "amount", "mdr", "gst", "category", "status"])


def load_settlement_df(conn) -> pd.DataFrame:
    try:
        return pd.read_sql("SELECT * FROM settlement", conn)
    except Exception:
        return pd.DataFrame(columns=["order_id", "amount_settled", "settlement_status", "utr", "settlement_date"])


def load_audit_df(conn) -> pd.DataFrame:
    try:
        return pd.read_sql("SELECT * FROM audit_log ORDER BY id", conn)
    except Exception:
        return pd.DataFrame(columns=["id", "order_id", "outcome", "reason", "classification", "audit_note", "created_at"])
