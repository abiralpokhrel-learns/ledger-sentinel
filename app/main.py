"""FastAPI entrypoint + end-to-end pipeline runner.

`app.main:app` is the webhook server (mounts the routes from app.webhook).
Running this file directly also executes the full offline pipeline against the
synthetic batch:

    python -m app.main

which loads orders + webhook events + settlement, applies the deterministic
core (signature -> idempotency -> state machine -> reconciliation), sends only
the surviving exceptions to the AI classifier, and writes a complete audit_log.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pandas as pd
from fastapi import FastAPI

from app import db, reconcile, webhook
from app.classify import classify_exception
from app.config import db_path, webhook_secret
from app.webhook import try_record_event, verify_signature

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

app = FastAPI(title="Ledger Sentinel")
# Mount the webhook routes.
app.include_router(webhook.app.router)


@app.on_event("startup")
def _startup():
    # Lazily open the shared DB on startup, not at import time.
    # Import-time open locks the file on Windows and makes `rm db` fail.
    conn = db.get_connection(db_path())
    db.init_db(conn)
    app.state.db = conn


@app.on_event("shutdown")
def _shutdown():
    try:
        app.state.db.close()
    except Exception:
        pass


def _load_orders(conn, data_dir: Path):
    df = pd.read_csv(data_dir / "orders.csv")
    for _, r in df.iterrows():
        # Initialise with status=None; the webhook event stream sets the real
        # state, so the state machine is actually exercised (not pre-filled).
        db.upsert_order(
            conn, r["order_id"], float(r["amount"]),
            float(r["mdr"]), float(r["gst"]), r["category"], None,
        )


def _process_webhook_events(conn, data_dir: Path, secret: str):
    with open(data_dir / "webhook_events.jsonl") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            evt = json.loads(line)
            raw = evt["raw_body"].encode()
            sig = evt["signature"]
            if not verify_signature(raw, sig, secret):
                db.log_audit(
                    conn, evt["order_id"], "rejected",
                    "signature_rejected: HMAC mismatch on webhook delivery",
                )
                continue
            verdict = try_record_event(
                conn, evt["event_id"], evt["order_id"], evt["event_type"]
            )
            # Webhook-stage outcomes stay distinct from reconciliation outcomes
            # ("matched"/"exception") so the dashboard's match rate is computed
            # over reconciliation rows only.
            if verdict == "applied":
                db.log_audit(conn, evt["order_id"], "applied",
                             f"webhook event applied: {evt['event_type']}")
            elif verdict == "duplicate_skipped":
                db.log_audit(conn, evt["order_id"], "duplicate_skipped",
                             "duplicate event delivery skipped (idempotent)")
            elif verdict == "out_of_order_dropped":
                db.log_audit(
                    conn, evt["order_id"], "dropped",
                    f"out_of_order_dropped: '{evt['event_type']}' was a backward "
                    f"transition and was dropped",
                )


def _load_settlement(conn, data_dir: Path):
    df = pd.read_csv(data_dir / "settlement.csv")
    for _, r in df.iterrows():
        db.upsert_settlement(
            conn, r["order_id"], float(r["amount_settled"]),
            r["settlement_status"], r.get("utr", ""), r.get("settlement_date", ""),
        )


def run_pipeline(conn, data_dir: Path = DATA_DIR, fresh: bool = True) -> dict:
    if fresh:
        # Make re-runs idempotent without requiring `rm ledger_sentinel.db`.
        # On Windows that delete often fails with PermissionError because
        # uvicorn/streamlit holds a WAL handle.
        db.clear_all(conn)
    secret = webhook_secret()
    _load_orders(conn, data_dir)
    _process_webhook_events(conn, data_dir, secret)
    _load_settlement(conn, data_dir)

    orders_df = db.load_orders_df(conn)
    settlement_df = db.load_settlement_df(conn)
    matched, exceptions = reconcile.reconcile(orders_df, settlement_df)

    # Audit every matched row.
    for _, r in matched.iterrows():
        db.log_audit(conn, r["order_id"], "matched",
                     "reconciled within tolerance")

    # Audit every exception, routing to the AI classifier.
    for _, r in exceptions.iterrows():
        row = r.to_dict()
        result = classify_exception(row)
        db.log_audit(
            conn, r["order_id"], "exception",
            reason=row.get("reason", "exception"),
            classification=result["classification"],
            audit_note=result["audit_note"],
        )

    summary = reconcile.summarize(matched, exceptions)
    return summary


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run-pipeline")
def run_pipeline_endpoint(fresh: bool = True):
    conn = db.get_connection(db_path())
    db.init_db(conn)
    summary = run_pipeline(conn, fresh=fresh)
    conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run Ledger Sentinel pipeline")
    parser.add_argument(
        "--db", default=db_path(),
        help="SQLite database path (default: from LEDGER_DB_PATH env)",
    )
    parser.add_argument(
        "--no-fresh", action="store_true",
        help="Do NOT wipe DB before run (append mode, for debugging)",
    )
    parser.add_argument(
        "--clear-only", action="store_true",
        help="Only clear the DB and exit (alternative to rm on Windows)",
    )
    args = parser.parse_args()

    conn = db.get_connection(args.db)
    db.init_db(conn)
    if args.clear_only:
        db.clear_all(conn)
        conn.close()
        print(f"Cleared {args.db} (tables + audit_log). No file delete needed.")
        return
    print("Running Ledger Sentinel pipeline on synthetic batch...\n")
    summary = run_pipeline(conn, DATA_DIR, fresh=not args.no_fresh)
    conn.close()

    print("\n=== Reconciliation summary ===")
    print(f"  Total rows reconciled : {summary['total_rows']}")
    print(f"  Matched               : {summary['matched']}")
    print(f"  Exceptions            : {summary['exceptions']}")
    print(f"  Match rate            : {summary['match_rate_pct']}%")
    print(f"  Exceptions by reason  : {summary['by_reason']}")
    print("\nDone. Inspect the audit_log table for the full trail.")


if __name__ == "__main__":
    main()
