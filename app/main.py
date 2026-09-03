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
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd
from fastapi import FastAPI

from app import db, reconcile, webhook
from app.classify import classify_exception
from app.config import db_path, webhook_secret
from app.webhook import try_record_event, verify_signature

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ledger_sentinel")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

app = FastAPI(title="Ledger Sentinel")
# Mount the webhook routes.
app.include_router(webhook.app.router)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Use lifespan instead of deprecated on_event — and handle missing/crashing DB gracefully
    try:
        conn = db.get_connection(db_path())
        db.init_db(conn)
        app.state.db = conn
        log.info("DB initialized at %s", db_path())
    except Exception as e:
        log.warning("DB init failed at startup (%s) — will lazy-init on first request", e)
        app.state.db = None
    yield
    try:
        if getattr(app.state, "db", None) is not None:
            app.state.db.close()
    except Exception:
        pass


app.router.lifespan_context = lifespan

# Keep deprecated handlers for backwards compat (tests that use TestClient without lifespan)
@app.on_event("startup")
def _startup_compat():
    if getattr(app.state, "db", None) is None:
        try:
            conn = db.get_connection(db_path())
            db.init_db(conn)
            app.state.db = conn
        except Exception as e:
            log.warning("compat startup DB failed: %s", e)


@app.on_event("shutdown")
def _shutdown_compat():
    try:
        if getattr(app.state, "db", None) is not None:
            app.state.db.close()
    except Exception:
        pass


def _load_orders(conn, data_dir: Path):
    path = data_dir / "orders.csv"
    if not path.exists():
        log.error("Missing %s — run python data/generate_synthetic_data.py", path)
        raise FileNotFoundError(f"orders.csv not found at {path}. Run python data/generate_synthetic_data.py")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        log.error("Failed to read %s: %s", path, e)
        raise
    required = {"order_id", "amount"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"orders.csv missing columns {missing}, got {list(df.columns)}")
    for _, r in df.iterrows():
        try:
            # Initialise with status=None; the webhook event stream sets the real
            # state, so the state machine is actually exercised (not pre-filled).
            db.upsert_order(
                conn, str(r["order_id"]), float(r["amount"]),
                float(r["mdr"]) if pd.notna(r.get("mdr", 0)) else 0.0,
                float(r["gst"]) if pd.notna(r.get("gst", 0)) else 0.0,
                r.get("category"), None,
            )
        except Exception as e:
            log.warning("skipping bad order row %s: %s", r.get("order_id"), e)
            continue


def _process_webhook_events(conn, data_dir: Path, secret: str):
    path = data_dir / "webhook_events.jsonl"
    if not path.exists():
        log.error("Missing %s", path)
        raise FileNotFoundError(f"webhook_events.jsonl not found at {path}")
    loaded = 0
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("line %d bad JSON skipped: %s", lineno, e)
                continue
            raw = evt.get("raw_body", "")
            if isinstance(raw, str):
                raw = raw.encode()
            else:
                raw = str(raw).encode()
            sig = evt.get("signature", "")
            order_id = evt.get("order_id") or "unknown"
            if not verify_signature(raw, sig, secret):
                db.log_audit(
                    conn, order_id, "rejected",
                    "signature_rejected: HMAC mismatch on webhook delivery",
                )
                continue
            try:
                verdict = try_record_event(
                    conn, evt.get("event_id"), evt.get("order_id"), evt.get("event_type")
                )
            except Exception as e:
                log.warning("try_record_event failed line %d: %s", lineno, e)
                continue
            # Webhook-stage outcomes stay distinct from reconciliation outcomes
            # ("matched"/"exception") so the dashboard's match rate is computed
            # over reconciliation rows only.
            if verdict == "applied":
                db.log_audit(conn, evt.get("order_id"), "applied",
                             f"webhook event applied: {evt.get('event_type')}")
            elif verdict == "duplicate_skipped":
                db.log_audit(conn, evt.get("order_id"), "duplicate_skipped",
                             "duplicate event delivery skipped (idempotent)")
            elif verdict == "out_of_order_dropped":
                db.log_audit(
                    conn, evt.get("order_id"), "dropped",
                    f"out_of_order_dropped: '{evt.get('event_type')}' was a backward "
                    f"transition and was dropped",
                )
            loaded += 1
    log.info("webhook events processed: %d lines", loaded)


def _load_settlement(conn, data_dir: Path):
    path = data_dir / "settlement.csv"
    if not path.exists():
        log.error("Missing %s", path)
        raise FileNotFoundError(f"settlement.csv not found at {path}")
    try:
        df = pd.read_csv(path)
    except Exception as e:
        log.error("Failed to read %s: %s", path, e)
        raise
    if "order_id" not in df.columns or "amount_settled" not in df.columns:
        raise ValueError(f"settlement.csv missing required columns, got {list(df.columns)}")
    for _, r in df.iterrows():
        try:
            db.upsert_settlement(
                conn, str(r["order_id"]), float(r["amount_settled"]),
                str(r.get("settlement_status", "")), str(r.get("utr", "")), str(r.get("settlement_date", "")),
            )
        except Exception as e:
            log.warning("skipping bad settlement row %s: %s", r.get("order_id"), e)
            continue


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
    try:
        matched, exceptions = reconcile.reconcile(orders_df, settlement_df)
    except Exception as e:
        log.error("reconcile failed: %s", e)
        raise

    # Audit every matched row.
    for _, r in matched.iterrows():
        try:
            db.log_audit(conn, str(r["order_id"]), "matched",
                         "reconciled within tolerance")
        except Exception as e:
            log.warning("log matched %s failed: %s", r.get("order_id"), e)
    # Audit every exception, routing to the AI classifier.
    for _, r in exceptions.iterrows():
        row = {k: (v if not pd.isna(v) else None) for k, v in r.to_dict().items()}
        try:
            result = classify_exception(row)
        except Exception as e:
            log.warning("classify %s failed: %s", row.get("order_id"), e)
            result = {"classification": "unresolved", "audit_note": f"classify failed: {e}"}
        try:
            db.log_audit(
                conn, str(r["order_id"]), "exception",
                reason=str(row.get("reason", "exception")),
                classification=str(result.get("classification", "unresolved")),
                audit_note=str(result.get("audit_note", "")),
            )
        except Exception as e:
            log.warning("log exception %s failed: %s", row.get("order_id"), e)

    summary = reconcile.summarize(matched, exceptions)
    return summary


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run-pipeline")
def run_pipeline_endpoint(fresh: bool = True):
    try:
        conn = db.get_connection(db_path())
        db.init_db(conn)
        summary = run_pipeline(conn, fresh=fresh)
        conn.close()
        return summary
    except FileNotFoundError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("run-pipeline failed")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"pipeline failed: {e}")


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
