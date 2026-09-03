"""Webhook handler: the deterministic, auditable core.

Three independent gates, in order:
  1. Signature verification  — HMAC-SHA256 over the RAW body bytes. Never
     re-serialize parsed JSON to hash, or whitespace/key-order differences
     silently break the signature.
  2. Idempotency            — `event_id` is a PRIMARY KEY, so a duplicate
     delivery is acknowledged and skipped, not reprocessed.
  3. State machine          — backward transitions (e.g. captured -> authorized)
     are dropped and logged; forward transitions are applied.

None of this touches the LLM. It is exact and replayable.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from fastapi import APIRouter, FastAPI, Request, HTTPException
from pydantic import BaseModel, field_validator

from app import db
from app.config import webhook_secret

router = APIRouter()
# Keep `app` for standalone `uvicorn app.webhook:app` and legacy imports; main app uses `router`
app = FastAPI(title="Ledger Sentinel Webhook")
app.include_router(router)

# Rank defines the only legal direction of travel for an order's state.
STATE_RANK = {
    "created": 0,
    "authorized": 1,
    "captured": 2,
    "refunded": 3,
    "failed": 3,
}

VALID_STATES = set(STATE_RANK.keys())


class WebhookPayload(BaseModel):
    event_id: str
    order_id: str
    event_type: str

    @field_validator("event_id", "order_id", "event_type")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be non-empty string")
        if len(v) > 200:
            raise ValueError("too long")
        return v.strip()

    @field_validator("event_type")
    @classmethod
    def valid_state(cls, v: str) -> str:
        if v not in VALID_STATES:
            raise ValueError(f"invalid event_type {v!r}, expected one of {sorted(VALID_STATES)}")
        return v


# --- gate 1: signature ------------------------------------------------------

def verify_signature(raw_body: bytes, signature: Optional[str], secret: str) -> bool:
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# --- gate 3: state machine --------------------------------------------------

def is_forward_transition(current_state: Optional[str], incoming_state: str) -> bool:
    if current_state is None:
        return True
    # Unknown states are defensive: treat as not forward so we don't corrupt ledger
    if incoming_state not in STATE_RANK or current_state not in STATE_RANK:
        return False
    incoming = STATE_RANK[incoming_state]
    current = STATE_RANK[current_state]
    return incoming > current


# --- gate 2 + 3 combined, for the offline pipeline --------------------------

def try_record_event(conn, event_id, order_id, event_type) -> str:
    """Apply one event. Returns a verdict string used by the audit log.

    Verdicts:
      duplicate_skipped   — event_id already seen
      out_of_order_dropped— backward transition, dropped, current state kept
      applied             — forward transition applied
      no_order            — order_id unknown (still recorded for the log)
    """
    if not event_id or not order_id or not event_type:
        return "out_of_order_dropped"
    if db.event_exists(conn, event_id):
        return "duplicate_skipped"

    current_state = db.get_order_status(conn, order_id)
    if not is_forward_transition(current_state, event_type):
        return "out_of_order_dropped"

    try:
        db.insert_event(conn, event_id, order_id, event_type)
    except Exception:
        # IntegrityError race — another thread inserted same event_id
        return "duplicate_skipped"
    if current_state is None:
        # Order may already exist (loaded from orders.csv with a real amount).
        # Only create a minimal stub if it is genuinely absent.
        if db.order_exists(conn, order_id):
            db.set_order_status(conn, order_id, event_type)
        else:
            db.upsert_order(conn, order_id, amount=0.0, status=event_type)
    else:
        db.set_order_status(conn, order_id, event_type)
    return "applied"


# --- HTTP endpoint (real webhook ingestion) ---------------------------------

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB — Razorpay payloads are small; block DoS


@router.post("/webhook")
async def razorpay_webhook(request: Request):
    raw_body = await request.body()  # MUST be raw bytes, parsed only after verify
    if len(raw_body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    if len(raw_body) == 0:
        raise HTTPException(status_code=400, detail="empty body")
    signature = request.headers.get("x-razorpay-signature")
    secret = webhook_secret()

    if not verify_signature(raw_body, signature, secret):
        raise HTTPException(status_code=400, detail="signature verification failed")

    try:
        payload_raw = await request.json()  # parse ONLY after verification
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    try:
        payload = WebhookPayload.model_validate(payload_raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid payload: {e}")

    # Handle case where app.state.db is not yet initialized (e.g., TestClient without lifespan)
    conn = getattr(request.app.state, "db", None)
    if conn is None:
        from app.config import db_path as _db_path
        conn = db.get_connection(_db_path())
        db.init_db(conn)
        request.app.state.db = conn

    verdict = try_record_event(conn, payload.event_id, payload.order_id, payload.event_type)
    return {"status": "ok", "verdict": verdict}
