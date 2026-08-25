"""Sanity checks for the reconciliation engine.

Covers each edge case the build plan requires the system to isolate:
exact match, tolerance rounding, TDS-shaped gap, late-auth status mismatch,
missing order, and missing settlement.
"""
import pandas as pd
import pytest

from app.reconcile import reconcile, summarize, TOLERANCE


def _orders(rows):
    return pd.DataFrame(rows)


def _settlement(rows):
    return pd.DataFrame(rows)


def test_exact_match():
    o = _orders([{"order_id": "o1", "amount": 100, "mdr": 2, "gst": 0.36,
                  "category": "software", "status": "captured"}])
    s = _settlement([{"order_id": "o1", "amount_settled": 97.64,
                      "settlement_status": "captured"}])
    matched, exc = reconcile(o, s)
    assert len(matched) == 1
    assert len(exc) == 0


def test_rounding_within_tolerance():
    o = _orders([{"order_id": "o1", "amount": 100, "mdr": 2, "gst": 0.36,
                  "category": "software", "status": "captured"}])
    # off by half a paisa — within tolerance, must still match
    s = _settlement([{"order_id": "o1", "amount_settled": 97.645,
                      "settlement_status": "captured"}])
    matched, exc = reconcile(o, s)
    assert len(matched) == 1
    assert exc.empty


def test_tds_candidate_is_exception():
    o = _orders([{"order_id": "o1", "amount": 1000, "mdr": 20, "gst": 3.6,
                  "category": "professional_services", "status": "captured"}])
    # 2% of gross withheld -> ₹20 gap, outside tolerance
    s = _settlement([{"order_id": "o1", "amount_settled": 956.4,
                      "settlement_status": "captured"}])
    matched, exc = reconcile(o, s)
    assert len(exc) == 1
    assert exc.iloc[0]["reason"] == "exception_tds_candidate"


def test_late_authorization_flip():
    o = _orders([{"order_id": "o1", "amount": 100, "mdr": 2, "gst": 0.36,
                  "category": "software", "status": "failed"}])
    # amounts agree but ledger says failed while settlement says captured
    s = _settlement([{"order_id": "o1", "amount_settled": 97.64,
                      "settlement_status": "captured"}])
    matched, exc = reconcile(o, s)
    assert len(exc) == 1
    assert exc.iloc[0]["reason"] == "status_mismatch"


def test_missing_order():
    o = pd.DataFrame(columns=["order_id", "amount", "mdr", "gst", "category", "status"])
    s = _settlement([{"order_id": "ghost", "amount_settled": 50,
                      "settlement_status": "captured"}])
    matched, exc = reconcile(o, s)
    assert len(exc) == 1
    assert exc.iloc[0]["reason"] == "missing_order"


def test_missing_settlement():
    o = _orders([{"order_id": "o1", "amount": 100, "mdr": 2, "gst": 0.36,
                  "category": "software", "status": "captured"}])
    s = pd.DataFrame(columns=["order_id", "amount_settled", "settlement_status",
                              "utr", "settlement_date"])
    matched, exc = reconcile(o, s)
    assert len(exc) == 1
    assert exc.iloc[0]["reason"] == "missing_settlement"


def test_unexplained_gap():
    o = _orders([{"order_id": "o1", "amount": 100, "mdr": 2, "gst": 0.36,
                  "category": "software", "status": "captured"}])
    s = _settlement([{"order_id": "o1", "amount_settled": 97.64 + 137.5,
                      "settlement_status": "captured"}])
    matched, exc = reconcile(o, s)
    assert len(exc) == 1
    assert exc.iloc[0]["reason"] == "exception_unexplained"


def test_summarize_match_rate():
    o = _orders([
        {"order_id": "o1", "amount": 100, "mdr": 2, "gst": 0.36,
         "category": "software", "status": "captured"},
        {"order_id": "o2", "amount": 200, "mdr": 4, "gst": 0.72,
         "category": "software", "status": "captured"},
    ])
    s = _settlement([
        {"order_id": "o1", "amount_settled": 97.64, "settlement_status": "captured"},
        {"order_id": "o2", "amount_settled": 195.28, "settlement_status": "captured"},
    ])
    matched, exc = reconcile(o, s)
    summary = summarize(matched, exc)
    assert summary["match_rate_pct"] == 100.0
    assert summary["matched"] == 2
