"""Reconciliation engine — tolerance-based matching, clean exception isolation.

Deterministic. Compares the net amount we EXPECT to be paid out
(order amount minus MDR minus GST) against what settlement actually shows.
Money is never compared with `==`; a tolerance band absorbs rounding noise.
Anything that doesn't cleanly match — or whose settlement status disagrees
with our ledger — is isolated as an exception for the AI layer.
"""
from __future__ import annotations

import pandas as pd

from app.config import TOLERANCE, TDS_RATE, TDS_BAND

# settlement_status values that mean "money actually moved"
SETTLED_STATES = {"captured"}


def prepare_orders(orders_df: pd.DataFrame) -> pd.DataFrame:
    df = orders_df.copy()
    df["amount_calc"] = (df["amount"] - df["mdr"] - df["gst"]).round(2)
    return df


def reconcile(orders_df: pd.DataFrame, settlement_df: pd.DataFrame):
    """Return (matched, exceptions) DataFrames, both with a `reason` column."""
    orders = prepare_orders(orders_df)
    settled = settlement_df.copy()

    merged = orders.merge(
        settled, on="order_id", how="outer", suffixes=("_calc", "_settled")
    )

    # Amount difference where both sides exist; NaN where a side is missing.
    merged["diff"] = (
        merged["amount_calc"] - merged["amount_settled"]
    ).abs()

    # Status consistency: if both sides exist, do they agree on money moving?
    def status_consistent(row) -> bool:
        calc_status = row.get("status")
        sett_status = row.get("settlement_status")
        if pd.isna(calc_status) or pd.isna(sett_status):
            return True  # missing side handled as its own exception below
        calc_moved = calc_status in SETTLED_STATES
        sett_moved = sett_status in SETTLED_STATES
        return calc_moved == sett_moved

    merged["status_consistent"] = merged.apply(status_consistent, axis=1)

    # Classify each merged row.
    def classify(row) -> str:
        if pd.isna(row["amount_calc"]):
            return "missing_order"          # settlement exists, no order
        if pd.isna(row["amount_settled"]):
            return "missing_settlement"     # order exists, no settlement
        if not row["status_consistent"]:
            return "status_mismatch"        # late-auth flip etc.
        if row["diff"] <= TOLERANCE:
            return "matched"
        # Above tolerance: candidate for AI. Tag a hint for the classifier.
        gap_rate = (row["diff"] / row["amount_calc"]) if row["amount_calc"] else 0
        if TDS_RATE - TDS_BAND <= gap_rate <= TDS_RATE + TDS_BAND:
            return "exception_tds_candidate"
        return "exception_unexplained"

    merged["reason"] = merged.apply(classify, axis=1)

    matched = merged[merged["reason"] == "matched"].copy()
    exceptions = merged[merged["reason"] != "matched"].copy()
    return matched, exceptions


def summarize(matched: pd.DataFrame, exceptions: pd.DataFrame) -> dict:
    """Human-friendly match-rate summary for the dashboard / README."""
    total = len(matched) + len(exceptions)
    match_rate = (len(matched) / total * 100) if total else 0.0
    return {
        "total_rows": total,
        "matched": len(matched),
        "exceptions": len(exceptions),
        "match_rate_pct": round(match_rate, 1),
        "by_reason": exceptions["reason"].value_counts().to_dict(),
    }
