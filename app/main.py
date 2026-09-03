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


app = FastAPI(title="Ledger Sentinel", lifespan=lifespan)
# Mount the webhook routes via router (not the full FastAPI app)
app.include_router(webhook.router)


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
    rows = []
    for _, r in df.iterrows():
        try:
            rows.append({
                "order_id": str(r["order_id"]),
                "amount": float(r["amount"]),
                "mdr": float(r["mdr"]) if pd.notna(r.get("mdr", 0)) else 0.0,
                "gst": float(r["gst"]) if pd.notna(r.get("gst", 0)) else 0.0,
                "category": r.get("category"),
                "status": None,  # webhook stream sets real state
            })
        except Exception as e:
            log.warning("skipping bad order row %s: %s", r.get("order_id"), e)
            continue
    # Batch insert in single transaction — 10x faster for 10k rows
    try:
        db.upsert_orders_batch(conn, rows)
    except Exception as e:
        log.warning("batch orders failed, falling back to row-by-row: %s", e)
        for r in rows:
            try:
                db.upsert_order(conn, r["order_id"], r["amount"], r["mdr"], r["gst"], r["category"], r["status"])
            except Exception as e2:
                log.warning("skipping row %s: %s", r["order_id"], e2)


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
    # Batch settlement inserts
    s_rows = []
    for _, r in df.iterrows():
        try:
            s_rows.append((str(r["order_id"]), float(r["amount_settled"]), str(r.get("settlement_status", "")), str(r.get("utr", "")), str(r.get("settlement_date", ""))))
        except Exception as e:
            log.warning("skipping bad settlement row %s: %s", r.get("order_id"), e)
            continue
    try:
        conn.executemany(
            """INSERT INTO settlement (order_id, amount_settled, settlement_status, utr, settlement_date)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET
                 amount_settled=excluded.amount_settled,
                 settlement_status=excluded.settlement_status,
                 utr=excluded.utr,
                 settlement_date=excluded.settlement_date""",
            s_rows,
        )
    except Exception as e:
        log.warning("batch settlement failed, falling back row-by-row: %s", e)
        for oid, amt, st, utr, dt in s_rows:
            try:
                db.upsert_settlement(conn, oid, amt, st, utr, dt)
            except Exception as e2:
                log.warning("skipping settlement %s: %s", oid, e2)


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


@app.get("/stats")
def stats():
    """Lightweight stats for dashboard / health checks."""
    try:
        conn = db.get_connection(db_path())
        db.init_db(conn)
        audit = db.load_audit_df(conn)
        conn.close()
        recon = audit[audit["outcome"].isin(["matched", "exception"])] if not audit.empty and "outcome" in audit.columns else audit
        matched = int((recon["outcome"] == "matched").sum()) if not recon.empty else 0
        exceptions = int((recon["outcome"] == "exception").sum()) if not recon.empty else 0
        total = matched + exceptions
        rate = round(matched / total * 100, 1) if total else 0.0
        return {"total_rows": total, "matched": matched, "exceptions": exceptions, "match_rate_pct": rate, "audit_rows": len(audit)}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/ask")
def ask_endpoint(payload: dict):
    """AI Finance Assistant — ask questions about the audit log."""
    q = (payload or {}).get("question", "") or (payload or {}).get("q", "")
    try:
        from app.assistant import ask as ledger_ask
        conn = db.get_connection(db_path())
        db.init_db(conn)
        audit = db.load_audit_df(conn)
        conn.close()
        result = ledger_ask(q, audit)
        return result
    except Exception as e:
        log.warning("ask failed: %s", e)
        return {"answer": f"Assistant unavailable: {e}", "source": "error"}


@app.get("/export.csv")
def export_csv(outcome: str | None = None):
    """Download audit_log as CSV. ?outcome=exception for exceptions only."""
    from fastapi.responses import Response
    conn = db.get_connection(db_path())
    db.init_db(conn)
    audit = db.load_audit_df(conn)
    conn.close()
    if outcome and not audit.empty and "outcome" in audit.columns:
        audit = audit[audit["outcome"] == outcome]
    csv_bytes = audit.to_csv(index=False).encode("utf-8") if not audit.empty else b""
    return Response(content=csv_bytes, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=audit_{outcome or 'all'}.csv"})


@app.get("/report.pdf")
def report_pdf():
    """Download professional PDF audit report."""
    from fastapi.responses import Response
    try:
        from app.report import build_pdf
        conn = db.get_connection(db_path())
        db.init_db(conn)
        audit = db.load_audit_df(conn)
        conn.close()
        # Build summary
        recon = audit[audit["outcome"].isin(["matched", "exception"])] if not audit.empty and "outcome" in audit.columns else audit
        matched = int((recon["outcome"] == "matched").sum()) if not recon.empty else 0
        exceptions = int((recon["outcome"] == "exception").sum()) if not recon.empty else 0
        total = matched + exceptions
        rate = round(matched / total * 100, 1) if total else 0.0
        by_reason = recon["reason"].value_counts().to_dict() if not recon.empty and "reason" in recon.columns else {}
        summary = {"total_rows": total, "matched": matched, "exceptions": exceptions, "match_rate_pct": rate, "by_reason": by_reason}
        pdf_bytes = build_pdf(audit, summary)
        return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=Ledger_Sentinel_Audit_Report.pdf"})
    except Exception as e:
        log.exception("report.pdf failed")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reconcile-upload")
async def reconcile_upload(request: __import__("fastapi").Request):
    """Upload orders.csv + settlement.csv and reconcile live. Returns summary + exceptions."""
    import io
    import pandas as pd
    from fastapi import HTTPException
    try:
        form = await request.form()
        orders_file = form.get("orders")
        settlement_file = form.get("settlement")
        if not orders_file or not settlement_file:
            raise HTTPException(status_code=400, detail="Send multipart form with 'orders' and 'settlement' CSV files")
        # Read CSVs
        orders_bytes = await orders_file.read()  # type: ignore
        settlement_bytes = await settlement_file.read()  # type: ignore
        orders_df = pd.read_csv(io.BytesIO(orders_bytes))
        settlement_df = pd.read_csv(io.BytesIO(settlement_bytes))
        matched, exceptions = reconcile.reconcile(orders_df, settlement_df)
        summary = reconcile.summarize(matched, exceptions)
        # classify top exceptions for preview
        from app.classify import classify_exception
        preview = []
        for _, r in exceptions.head(10).iterrows():
            row = {k: (v if not pd.isna(v) else None) for k, v in r.to_dict().items()}
            try:
                c = classify_exception(row)
            except Exception:
                c = {"classification": "unresolved", "audit_note": ""}
            preview.append({"order_id": str(r.get("order_id")), "reason": str(r.get("reason")), **c, "diff": float(r.get("diff")) if pd.notna(r.get("diff")) else None})
        return {"summary": summary, "preview": preview, "matched_count": len(matched), "exceptions_count": len(exceptions)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"reconcile failed: {e}")


@app.post("/run-pipeline")
def run_pipeline_endpoint(fresh: bool = True):
    conn = db.get_connection(db_path())
    db.init_db(conn)
    try:
        summary = run_pipeline(conn, fresh=fresh)
        return summary
    except FileNotFoundError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("run-pipeline failed")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"pipeline failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Run Ledger Sentinel pipeline")
    parser.add_argument(
        "--db", default=None,
        help="SQLite database path (default: from LEDGER_DB_PATH env or ledger_sentinel.db)",
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
    # Resolve db path lazily so LEDGER_DB_PATH env is read at call time, not import time
    db_file = args.db or db_path()
    conn = db.get_connection(db_file)
    db.init_db(conn)
    if args.clear_only:
        db.clear_all(conn)
        conn.close()
        print(f"Cleared {db_file} (tables + audit_log). No file delete needed.")
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
