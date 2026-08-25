"""Sanity checks for the deterministic webhook core.

Covers the three gates in app/webhook.py:
  1. HMAC-SHA256 signature verification over raw bytes
  2. Idempotency via event_id
  3. Forward-only state machine
"""
import hashlib
import hmac

import pytest

from app.webhook import is_forward_transition, try_record_event, verify_signature


def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


# --- gate 1: signature -------------------------------------------------------

SECRET = "test_secret"
BODY = '{"event_id":"evt_1"}'


def test_valid_signature_accepted():
    assert verify_signature(BODY.encode(), _sign(BODY, SECRET), SECRET)


def test_tampered_body_rejected():
    tampered = BODY.replace("evt_1", "evt_2")
    assert not verify_signature(tampered.encode(), _sign(BODY, SECRET), SECRET)


def test_wrong_secret_rejected():
    assert not verify_signature(BODY.encode(), _sign(BODY, "other"), SECRET)


def test_missing_signature_rejected():
    assert not verify_signature(BODY.encode(), None, SECRET)


# --- gate 3: state machine ---------------------------------------------------

def test_first_event_is_forward():
    assert is_forward_transition(None, "created")


def test_backward_transition_rejected():
    assert not is_forward_transition("captured", "authorized")
    assert not is_forward_transition("captured", "created")


def test_forward_transition_accepted():
    assert is_forward_transition("authorized", "captured")


def test_same_state_not_a_transition():
    assert not is_forward_transition("captured", "captured")


# --- gates 2 + 3 combined against a real connection --------------------------

@pytest.fixture()
def conn():
    from app import db
    c = db.get_connection(":memory:")
    db.init_db(c)
    yield c
    c.close()


def test_duplicate_delivery_skipped(conn):
    assert try_record_event(conn, "e1", "o1", "created") == "applied"
    assert try_record_event(conn, "e1", "o1", "created") == "duplicate_skipped"


def test_out_of_order_dropped_keeps_state(conn):
    assert try_record_event(conn, "e1", "o1", "authorized") == "applied"
    assert try_record_event(conn, "e2", "o1", "captured") == "applied"
    assert try_record_event(conn, "e3", "o1", "created") == "out_of_order_dropped"

    from app import db
    assert db.get_order_status(conn, "o1") == "captured"


def test_status_updates_on_existing_order(conn):
    from app import db
    db.upsert_order(conn, "o1", amount=100.0, status="created")
    assert try_record_event(conn, "e1", "o1", "authorized") == "applied"
    assert db.get_order_status(conn, "o1") == "authorized"


def test_unknown_order_gets_stub_row(conn):
    from app import db
    assert try_record_event(conn, "e1", "ghost", "captured") == "applied"
    assert db.order_exists(conn, "ghost")
